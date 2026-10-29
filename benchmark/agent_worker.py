"""Offline container worker. All model inference is delegated over JSON-line stdio."""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def run(job_path):
    job = json.loads(Path(job_path).read_text())
    protocol_in, protocol_out = sys.stdin, sys.stdout
    artifacts = Path("/artifacts")
    state = {
        "turn": 0,
        "attempt_turns": [],
        "failed_turns": set(),
        "contexts": {},
        "peak_l1_tokens": 0,
        "peak_l2_tokens": 0,
        "stop_reason": None,
    }

    def emit(kind, **data):
        protocol_out.write(json.dumps({"type": kind, **data}) + "\n")
        protocol_out.flush()

    with (
        (artifacts / "agent.log").open("w") as log,
        contextlib.redirect_stdout(log),
        contextlib.redirect_stderr(log),
    ):
        os.environ["AIDER_ANALYTICS"] = "false"
        Path.home().mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", "/source"], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=/source",
                "clone",
                "--no-hardlinks",
                "--no-local",
                "/source",
                "/workspace",
            ],
            check=True,
            stdout=log,
            stderr=log,
        )
        os.chdir("/workspace")
        subprocess.run(
            ["git", "checkout", "--detach", job["task"]["base_commit"]],
            check=True,
            stdout=log,
            stderr=log,
        )
        subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], check=True)
        with Path(".git/info/exclude").open("a") as exclude:
            exclude.write("\n.aider*\n")

        from aider.chatgpt_errors import ChatGPTError
        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model

        def context():
            l1 = sorted(coder.get_rel_fname(path) for path in coder.abs_fnames)
            l2 = sorted(
                coder.get_rel_fname(path)
                for path in getattr(coder, "l2_interface_fnames", [])
            )
            tokens = sum(
                model.token_count(Path(path).read_text(errors="replace"))
                for path in coder.abs_fnames
                if Path(path).is_file()
            )
            state["peak_l1_tokens"] = max(state["peak_l1_tokens"], tokens)
            return sorted(set(l1 + l2))

        class RemoteModel(Model):
            def validate_environment(self):
                return {"keys_in_environment": True, "missing_keys": []}

            def send_completion(self, messages, functions, stream, temperature=None):
                if stream:
                    raise ValueError(
                        "Benchmark worker uses non-streaming UI with streamed host inference"
                    )
                state["turn"] += 1
                state["contexts"][state["turn"]] = context()
                emit(
                    "model_request",
                    ordinal=state["turn"],
                    model=self.name,
                    messages=messages,
                    functions=functions,
                    extra_params=self.extra_params,
                    context=state["contexts"][state["turn"]],
                )
                line = protocol_in.readline()
                if not line:
                    raise ChatGPTError("Benchmark host disconnected")
                reply = json.loads(line)
                if reply.get("type") != "model_response":
                    raise ChatGPTError("Invalid benchmark host response")
                if reply.get("error"):
                    state["stop_reason"] = reply["error"]
                    raise ChatGPTError(
                        reply["error"]["message"], code=reply["error"].get("code")
                    )
                usage = SimpleNamespace(**reply["usage"])
                result = SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=reply["text"], tool_calls=None
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=usage,
                    total_cost_usd=None,
                    billing_mode="chatgpt_subscription",
                )
                return (
                    hashlib.sha1(json.dumps(messages, sort_keys=True).encode()),
                    result,
                )

        model = RemoteModel(job["model"])
        model.set_reasoning_effort(job["reasoning_effort"])
        io = InputOutput(
            yes=True,
            pretty=False,
            input_history_file=str(artifacts / "input.history"),
            chat_history_file=str(artifacts / "chat.md"),
        )
        coder = Coder.create(
            model,
            "diff",
            io=io,
            fnames=[],
            stream=False,
            map_tokens=1024,
            auto_commits=False,
            dirty_commits=False,
            auto_lint=True,
            auto_test=False,
            suggest_shell_commands=False,
            cache_prompts=False,
            num_cache_warming_pings=0,
        )
        coder.max_reflections = job["max_turns"]
        original_get_edits = coder.get_edits
        original_lint = coder.lint_edited
        original_apply_updates = coder.apply_updates

        def attempt():
            if state["turn"] not in state["attempt_turns"]:
                state["attempt_turns"].append(state["turn"])

        def get_edits():
            try:
                edits = original_get_edits()
            except ValueError:
                if "<<<<<<< SEARCH" in coder.partial_response_content:
                    attempt()
                    state["failed_turns"].add(state["turn"])
                raise
            if edits:
                attempt()
            return edits

        def lint(fnames):
            errors = original_lint(fnames)
            if errors:
                state["failed_turns"].add(state["turn"])
            return errors

        def apply_updates():
            malformed = coder.num_malformed_responses
            edited = original_apply_updates()
            if coder.num_malformed_responses > malformed:
                state["failed_turns"].add(state["turn"])
            return edited

        coder.get_edits, coder.lint_edited = get_edits, lint
        coder.apply_updates = apply_updates
        coder.run(
            with_message="Please fix the following issue in this repository:\n\n"
            + job["task"]["problem_statement"]
        )
        final_context = context()
        subprocess.run(
            ["git", "add", "--intent-to-add", "--", "."],
            check=True,
            stdout=log,
            stderr=log,
        )
        patch = subprocess.check_output(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"]
        )
        (artifacts / "prediction.patch").write_bytes(patch)
        emit(
            "done",
            attempt_turns=state["attempt_turns"],
            failed_edit_count=len(state["failed_turns"]),
            context_at_turn_3=state["contexts"].get(4, final_context),
            peak_l1_tokens=state["peak_l1_tokens"],
            peak_l2_tokens=state["peak_l2_tokens"],
            stop_reason=state["stop_reason"],
            patch_sha256=hashlib.sha256(patch).hexdigest(),
        )


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except Exception as exc:
        sys.__stdout__.write(
            json.dumps({"type": "worker_error", "message": str(exc)}) + "\n"
        )
        sys.__stdout__.flush()
        raise
