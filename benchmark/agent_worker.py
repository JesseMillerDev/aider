"""Offline container worker. All model inference is delegated over JSON-line stdio."""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


def coder_feature_options(features):
    options = {}
    if features.get("enable_agent_nav"):
        options["enable_agent_nav"] = True
    if features.get("enable_lsp_graph"):
        options.update(enable_lsp_graph=True, lsp_timeout=features["lsp_timeout"])
    if features.get("enable_l2_paging"):
        options.update(
            enable_l2_paging=True,
            max_l1_tokens=features.get("max_l1_tokens"),
            max_l1_files=features.get("max_l1_files"),
            max_l2_tokens=features.get("max_l2_tokens"),
            l1_ttl_turns=features.get("l1_ttl_turns", 2),
        )
    return options


class NavigationTelemetry:
    """Observe real navigation attempts without changing their results or prompts."""

    def __init__(self, coder, current_turn):
        self.navigator = getattr(coder, "agent_navigator", None)
        self.graph = getattr(getattr(coder, "repo_map", None), "semantic_graph", None)
        self.events = []
        self.actions = Counter()
        self.responses = 0
        self.invalid_responses = 0
        self.protocol_requests = 0
        if self.navigator is None:
            return
        execute = self.navigator.execute
        handle = self.navigator.handle

        def tracked_execute(actions, manual=False):
            names = [name for name, args in actions]
            self.actions.update(names)
            self.events.append({"ordinal": current_turn(), "actions": names, "manual": manual})
            return execute(actions, manual=manual)

        def tracked_handle(content):
            result = handle(content)
            if result is not None:
                self.responses += 1
                self.invalid_responses += result.startswith("Navigation rejected:")
            return result

        self.navigator.execute = tracked_execute
        self.navigator.handle = tracked_handle

    @staticmethod
    def contains_instructions(messages):
        for message in messages:
            if message.get("role") not in ("system", "developer"):
                continue
            content = message.get("content")
            texts = (
                [content]
                if isinstance(content, str)
                else [block.get("text", "") for block in content or [] if isinstance(block, dict)]
            )
            if any(
                "You can navigate repository files before editing." in text
                and "```aider-nav" in text
                for text in texts
            ):
                return True
        return False

    def for_request(self, messages):
        snapshot = self.snapshot()
        if snapshot is not None:
            snapshot["request_contains_instructions"] = self.contains_instructions(messages)
        return snapshot

    def snapshot(self, messages=None, include_events=False):
        """Passing messages counts an accepted request, never a rejected budget attempt."""
        if self.navigator is None:
            return None
        if messages is not None:
            self.protocol_requests += self.contains_instructions(messages)
        return {
            "enabled": True,
            "model_requests_with_instructions": self.protocol_requests,
            "responses_with_navigation": self.responses,
            "invalid_navigation_responses": self.invalid_responses,
            "action_attempts": dict(self.actions),
            "bridge_shared_with_ranking": (
                self.graph is not None and self.navigator.bridge is self.graph.bridge
            ),
            **({"events": list(self.events)} if include_events else {}),
        }


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
    started = time.monotonic()
    semantic_totals = Counter()
    semantic_maps = []
    paging_events = []
    paging_requests = []
    paging_reloads = 0

    def pager_telemetry(include_events=False):
        pager = getattr(coder, "context_pager", None)
        if pager is None:
            return None
        return {
            "enabled": True,
            "settings": {
                key: getattr(pager, key)
                for key in (
                    "max_l1_tokens",
                    "max_l1_files",
                    "max_l2_tokens",
                    "l1_ttl_turns",
                )
            },
            "completed_responses": pager.turn,
            "stats": {
                **dict(pager.stats),
                "full_file_reloads": paging_reloads + pager.stats.get("full_file_reloads", 0),
                "file_mention_reloads": paging_reloads,
                "navigation_file_loads": pager.stats.get("full_file_reloads", 0),
            },
            "l1_files": sorted(coder.get_rel_fname(path) for path in coder.abs_fnames),
            "l2_files": sorted(coder.get_rel_fname(path) for path in coder.l2_interface_fnames),
            **(
                {"events": paging_events, "request_contexts": paging_requests}
                if include_events
                else {}
            ),
        }

    def lsp_telemetry():
        graph = getattr(coder.repo_map, "semantic_graph", None)
        if graph is None:
            return None
        return {
            "maps": semantic_maps,
            "totals": dict(semantic_totals),
            "bridge": dict(graph.bridge.stats),
            "errors": dict(graph.bridge.errors),
            "started_languages": sorted(graph.bridge.servers),
            "unavailable_languages": sorted(graph.bridge.unavailable),
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
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", "/source"],
            check=True,
        )
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
                coder.get_rel_fname(path) for path in getattr(coder, "l2_interface_fnames", [])
            )
            tokens = sum(
                model.token_count(Path(path).read_text(errors="replace"))
                for path in coder.abs_fnames
                if Path(path).is_file()
            )
            state["peak_l1_tokens"] = max(state["peak_l1_tokens"], tokens)
            if getattr(coder, "context_pager", None) is not None:
                content = coder.context_pager.interface_content(coder)
                l2_tokens = model.token_count(content) if content else 0
                state["peak_l2_tokens"] = max(state["peak_l2_tokens"], l2_tokens)
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
                paging_snapshot = pager_telemetry()
                if (
                    paging_snapshot is not None
                    and navigation.navigator is not None
                    and not navigation.contains_instructions(messages)
                ):
                    paging_snapshot = {
                        **paging_snapshot,
                        "l1_files": [],
                        "l2_files": [],
                        "auxiliary_request": True,
                    }
                emit(
                    "model_request",
                    ordinal=state["turn"],
                    model=self.name,
                    messages=messages,
                    functions=functions,
                    extra_params=self.extra_params,
                    context=state["contexts"][state["turn"]],
                    lsp=lsp_telemetry(),
                    pager=paging_snapshot,
                    navigation=navigation.for_request(messages),
                    elapsed_seconds=time.monotonic() - started,
                )
                line = protocol_in.readline()
                if not line:
                    raise ChatGPTError("Benchmark host disconnected")
                reply = json.loads(line)
                if reply.get("type") != "model_response":
                    raise ChatGPTError("Invalid benchmark host response")
                if reply.get("error"):
                    state["stop_reason"] = reply["error"]
                    raise ChatGPTError(reply["error"]["message"], code=reply["error"].get("code"))
                navigation.snapshot(messages)
                usage = SimpleNamespace(**reply["usage"])
                if paging_snapshot is not None:
                    paging_requests.append({"ordinal": state["turn"], **paging_snapshot})
                result = SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=reply["text"], tool_calls=None),
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
        features = job.get("features", {})
        feature_options = coder_feature_options(features)
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
            **feature_options,
        )
        navigation = NavigationTelemetry(coder, lambda: state["turn"])
        if getattr(coder, "context_pager", None) is not None:
            original_step = coder.context_pager.step
            original_add_rel_fname = coder.add_rel_fname

            def add_rel_fname(path):
                nonlocal paging_reloads
                full = coder.abs_root_path(path)
                was_l2 = full in coder.l2_interface_fnames
                result = original_add_rel_fname(path)
                if was_l2 and full in coder.abs_fnames:
                    paging_reloads += 1
                return result

            coder.add_rel_fname = add_rel_fname

            def paging_step(owner):
                result = original_step(owner)
                context()
                paging_events.append({"ordinal": state["turn"], **pager_telemetry()})
                return result

            coder.context_pager.step = paging_step
        graph = getattr(coder.repo_map, "semantic_graph", None)
        if graph is not None:
            original_prepare = graph.prepare
            original_multiplier = graph.multiplier

            def prepare(*args, **kwargs):
                begin = time.monotonic()
                try:
                    return original_prepare(*args, **kwargs)
                finally:
                    stats = dict(graph.stats)
                    semantic_totals.update(stats)
                    semantic_maps.append(
                        {
                            "turn": state["turn"],
                            "elapsed_seconds": time.monotonic() - begin,
                            "stats": stats,
                        }
                    )

            def multiplier(*args, **kwargs):
                factor = original_multiplier(*args, **kwargs)
                semantic_totals[
                    (
                        "boosted_edges"
                        if factor > 1
                        else "dampened_edges" if factor < 1 else "unchanged_edges"
                    )
                ] += 1
                return factor

            graph.prepare, graph.multiplier = prepare, multiplier
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
        if job.get("preflight"):
            coder.cur_messages = [{"role": "user", "content": job["task"]["problem_statement"]}]
            coder.get_repo_map(force_refresh=True)
        else:
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
        patch = subprocess.check_output(["git", "diff", "--no-ext-diff", "--binary", "HEAD"])
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
            lsp=lsp_telemetry(),
            pager=pager_telemetry(include_events=True),
            navigation=navigation.snapshot(include_events=True),
            elapsed_seconds=time.monotonic() - started,
        )
        if coder.repo_map:
            coder.repo_map.close()


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except Exception as exc:
        sys.__stdout__.write(json.dumps({"type": "worker_error", "message": str(exc)}) + "\n")
        sys.__stdout__.flush()
        raise
