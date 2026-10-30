"""Execute the frozen stock-behavior baseline with subscription inference and official grading."""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from filelock import FileLock

from benchmark.ab_support import ARMS, digest, read_json, validate_subset, write_json
from benchmark.grade_smoke import read_resolution
from benchmark.select_subset import DATASET_REVISION, load_rows

ROOT = Path(__file__).resolve().parents[1]
CONTROL = "8400741ce7a9d26aecdbe97da423de5f3e80def1"
MODEL = "chatgpt/gpt-6.1-sol"
AGENT_IMAGE = "better-aider-baseline:8400741"
GRADER_IMAGE = "better-aider-swebench-grader:4.1.0"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def command(args, log_path=None, **kwargs):
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_path).open("w", encoding="utf-8") as log:
            return subprocess.run(
                args,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                **kwargs,
            )
    return subprocess.check_output(args, cwd=ROOT, text=True, **kwargs).strip()


def image_id(name):
    return command(["docker", "image", "inspect", name, "--format", "{{.Id}}"])


def prepare_repo(task, log_dir):
    repo, commit = task["repo"], task["base_commit"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(
        r"[a-f0-9]{40}", commit
    ):
        raise ValueError("Unsafe repository or commit in frozen task")
    path = ROOT / "benchmark/.cache/repos" / (repo.replace("/", "__") + ".git")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=600):
        if not path.exists():
            command(["git", "init", "--bare", str(path)], log_dir / "git-init.log")
            command(
                [
                    "git",
                    "-C",
                    str(path),
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/{repo}.git",
                ]
            )
        exists = (
            subprocess.run(
                ["git", "-C", str(path), "cat-file", "-e", commit + "^{commit}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not exists:
            command(
                [
                    "git",
                    "-C",
                    str(path),
                    "fetch",
                    "--depth=1",
                    "origin",
                    f"{commit}:refs/heads/baseline_{commit}",
                ],
                log_dir / "git-fetch.log",
                timeout=600,
            )
    return path


def successful_calls(transcript_dir):
    return [
        read_json(path) for path in sorted(transcript_dir.glob("[0-9][0-9][0-9].json"))
    ]


def request_hash(event):
    return digest(
        {
            key: event.get(key)
            for key in ("model", "messages", "functions", "extra_params")
        }
    )


def service_request(event, transcript_dir, transport, max_turns):
    ordinal = event["ordinal"]
    if type(ordinal) is not int or not 1 <= ordinal <= max_turns:
        return {
            "type": "model_response",
            "error": {"code": "turn_limit", "message": "Model-call limit reached"},
        }
    if event["model"] != MODEL:
        raise ValueError("Worker requested a different model")
    path = transcript_dir / f"{ordinal:03d}.json"
    fingerprint = request_hash(event)
    if path.exists():
        cached = read_json(path)
        if cached["request_sha256"] != fingerprint:
            raise ValueError(
                "Resume request differs from the saved transcript; refusing to replay a different prompt"
            )
        return cached["reply"]
    from aider.chatgpt_errors import ChatGPTError

    start = time.time()
    try:
        _, response = transport.send_completion(
            "gpt-6.1-sol",
            event["messages"],
            event.get("functions"),
            False,
            event.get("extra_params"),
        )
        reply = {
            "type": "model_response",
            "text": response.choices[0].message.content,
            "usage": vars(response.usage),
            "response_id": response.response_id,
        }
    except ChatGPTError as exc:
        reply = {
            "type": "model_response",
            "error": {"code": exc.code or "subscription_error", "message": str(exc)},
        }
        atomic_json(
            transcript_dir / f"{ordinal:03d}.error.json",
            {
                "request_sha256": fingerprint,
                "reply": reply,
                "time": time.time(),
                "status": exc.status,
                "request_id": exc.request_id,
            },
        )
        return reply
    atomic_json(
        path,
        {
            "request_sha256": fingerprint,
            "request": event,
            "reply": reply,
            "elapsed_seconds": time.time() - start,
        },
    )
    return reply


def run_agent(manifest, task, output, transport):
    artifacts = output / "agents" / task["instance_id"]
    artifacts.mkdir(parents=True, exist_ok=True)
    with FileLock(str(artifacts / "agent.lock"), timeout=1):
        telemetry = artifacts / "telemetry.json"
        if telemetry.exists():
            return read_json(telemetry)
        return _run_agent(manifest, task, output, transport)


def _run_agent(manifest, task, output, transport):
    instance_id = task["instance_id"]
    artifact_dir = output / "agents" / instance_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir = output / "transcripts" / instance_id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    repo = prepare_repo(task, artifact_dir)
    job = {
        "task": task,
        "model": MODEL,
        "reasoning_effort": "medium",
        "max_turns": manifest["max_turns"],
    }
    if "features" in manifest:
        job["features"] = manifest["features"]
    if manifest.get("preflight"):
        job["preflight"] = True
    atomic_json(artifact_dir / "job.json", job)
    container = (
        "better-aider-agent-" + digest([manifest["experiment_id"], instance_id])[:16]
    )
    args = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--name",
        container,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "2",
        "--memory",
        "6g",
        "--pids-limit",
        "512",
        "--tmpfs",
        "/workspace:rw,exec,uid=65532,gid=65532,mode=0700",
        "--tmpfs",
        "/tmp:rw,exec,mode=1777",
        "--mount",
        f"type=bind,source={repo},target=/source,readonly",
        "--mount",
        f"type=bind,source={artifact_dir.resolve()},target=/artifacts",
        manifest.get("agent_image_id", manifest["agent_image"]),
    ]
    done = None
    with (artifact_dir / "container.log").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        try:
            for line in process.stdout:
                event = json.loads(line)
                if event["type"] == "model_request":
                    print(f"  {instance_id}: model call {event['ordinal']}", flush=True)
                    reply = service_request(
                        event, transcript_dir, transport, manifest["max_turns"]
                    )
                    process.stdin.write(json.dumps(reply) + "\n")
                    process.stdin.flush()
                elif event["type"] == "done":
                    done = event
                elif event["type"] == "worker_error":
                    raise RuntimeError(event["message"])
                else:
                    raise ValueError("Unexpected container protocol message")
            if process.wait(timeout=60) != 0 or not done:
                raise RuntimeError("Agent container did not finish; inspect its logs")
        finally:
            if process.poll() is None:
                subprocess.run(
                    ["docker", "rm", "--force", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                process.wait(timeout=30)
    if done.get("stop_reason") and done["stop_reason"].get("code") != "turn_limit":
        atomic_json(artifact_dir / "paused.json", done)
        raise RuntimeError("SUBSCRIPTION_PAUSED: " + done["stop_reason"]["message"])
    atomic_json(artifact_dir / "telemetry.json", done)
    return done


def grade(manifest, task, oracle_row, patch, output):
    arm = manifest.get("arm", ARMS[0])
    instance_id = task["instance_id"]
    patch_hash = hashlib.sha256(patch).hexdigest()
    run_id = (
        "baseline-" + digest([manifest["experiment_id"], instance_id, patch_hash])[:24]
    )
    provenance = {"engine": "swebench", "run_id": run_id, "patch_sha256": patch_hash}
    if not patch.strip():
        # Empty predictions receive no successful fix; do not pretend tests ran.
        return False, {**provenance, "status": "empty_prediction", "report": None}
    directory = output / "grading" / instance_id
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "dataset.json", [oracle_row])
    prediction = {
        "instance_id": instance_id,
        "model_name_or_path": arm,
        "model_patch": patch.decode("utf-8"),
    }
    (directory / "prediction.jsonl").write_text(
        json.dumps(prediction) + "\n", encoding="utf-8"
    )
    report = (
        directory / "logs/run_evaluation" / run_id / arm / instance_id / "report.json"
    )
    if not report.exists():
        args = [
            "docker",
            "run",
            "--rm",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            f"type=bind,source={directory.resolve()},target=/grading",
            manifest.get("grader_image_id", manifest["grader_image"]),
            "--dataset_name",
            "/grading/dataset.json",
            "--predictions_path",
            "/grading/prediction.jsonl",
            "--instance_ids",
            instance_id,
            "--max_workers",
            "1",
            "--run_id",
            run_id,
            "--timeout",
            "900",
            "--report_dir",
            "/grading/reports",
        ]
        command(args, directory / "grading.log", timeout=2400)
    resolved = read_resolution(report, instance_id)
    return resolved, {
        **provenance,
        "status": "graded",
        "report": str(report.relative_to(output)),
    }


def result_record(
    manifest,
    task,
    telemetry,
    calls,
    oracle_files,
    resolved,
    grading,
    usage_complete=True,
):
    first = min(telemetry["attempt_turns"], default=None)
    inputs = sum(call["reply"]["usage"]["prompt_tokens"] for call in calls)
    outputs = sum(call["reply"]["usage"]["completion_tokens"] for call in calls)
    cached = sum(call["reply"]["usage"]["prompt_cache_hit_tokens"] for call in calls)
    first_tokens = (
        sum(call["reply"]["usage"]["total_tokens"] for call in calls[:first])
        if first
        else None
    )
    recall = (
        len(set(telemetry["context_at_turn_3"]) & set(oracle_files)) / len(oracle_files)
        if oracle_files
        else None
    )
    return {
        "schema_version": 1,
        "kind": "treatment_result" if manifest.get("features") else "baseline_result",
        "experiment_id": manifest["experiment_id"],
        "arm": manifest.get("arm", ARMS[0]),
        "instance_id": task["instance_id"],
        "repo": task["repo"],
        "status": "completed",
        "resolved": resolved,
        "grading": grading,
        "billing_mode": "chatgpt_subscription",
        "turns_to_first_edit": first,
        "tokens_to_first_edit": first_tokens if usage_complete else None,
        "total_turns": len(calls),
        "total_input_tokens": inputs if usage_complete else None,
        "cached_input_tokens": cached if usage_complete else None,
        "total_output_tokens": outputs if usage_complete else None,
        "total_cost_usd": None,
        "usage_complete": usage_complete,
        "cache_hit_ratio": cached / inputs if inputs and usage_complete else None,
        "failed_edit_count": telemetry["failed_edit_count"],
        "localization_recall_at_turn_3": recall,
        "peak_l1_tokens": telemetry["peak_l1_tokens"],
        "peak_l2_tokens": telemetry["peak_l2_tokens"],
        **({"lsp": telemetry["lsp"]} if telemetry.get("lsp") is not None else {}),
        **({"pager": telemetry["pager"]} if telemetry.get("pager") is not None else {}),
        **(
            {"elapsed_seconds": telemetry["elapsed_seconds"]}
            if "elapsed_seconds" in telemetry
            else {}
        ),
    }


def progress_report(manifest, output):
    rows = [
        read_json(path)
        for path in (output / manifest.get("arm", ARMS[0])).glob("*.json")
    ]
    complete = [row for row in rows if row["status"] == "completed"]
    resolved = sum(row["resolved"] for row in complete)
    failures = [row for row in rows if row["status"] != "completed"]
    summary = {
        "experiment_id": manifest["experiment_id"],
        "planned": len(manifest["tasks"]),
        "completed": len(complete),
        "resolved": resolved,
        "infrastructure_errors": len(failures),
        "pass_at_1": (
            resolved / len(complete)
            if len(complete) == len(manifest["tasks"])
            else None
        ),
    }
    atomic_json(output / "progress.json", summary)
    treatment = bool(manifest.get("features"))
    treatment_label = manifest.get("treatment_label", "LSP-only treatment")
    (
        output / ("treatment_report.md" if treatment else "baseline_report.md")
    ).write_text(
        f"# GPT-6.1 Sol {treatment_label if treatment else 'baseline'}\n\nExperiment `{manifest['experiment_id']}`.\n\n"
        f"Completed: {len(complete)}/{len(manifest['tasks'])}; resolved: {resolved}; infrastructure errors: {len(failures)}.\n\n"
        + (
            f"Pass@1: {100 * summary['pass_at_1']:.1f}%.\n"
            if summary["pass_at_1"] is not None
            else "Pass@1 is withheld until all planned instances have a result.\n"
        )
        + "\nSubscription usage; API dollar cost is unavailable.\n"
        + (
            "Historical control comparison is pending complete coverage.\n"
            if treatment
            else "No treatment comparison has run.\n"
        ),
        encoding="utf-8",
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instances", type=Path, default=Path("benchmark/subset_50.json")
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(
            f"benchmark/.cache/swe-bench-lite-test-{DATASET_REVISION}.parquet"
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/results/baseline-50")
    )
    parser.add_argument(
        "--limit", type=int, help="Maximum tasks this invocation, for smoke checks"
    )
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--agent-image", default=AGENT_IMAGE)
    parser.add_argument("--grader-image", default=GRADER_IMAGE)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (
        FileLock(str(output / "run.lock"), timeout=1),
        FileLock(str(output / "generate.lock"), timeout=1),
        FileLock(str(output / "grade.lock"), timeout=1),
    ):
        tasks = validate_subset(read_json(args.instances))
        rows = {row["instance_id"]: row for row in load_rows(args.dataset)}
        for task in tasks:
            row = rows[task["instance_id"]]
            if any(task[key] != row[key] for key in task):
                raise ValueError("Frozen agent task differs from the evaluator dataset")
        manifest = {
            "schema_version": 1,
            "kind": "baseline",
            "model": MODEL,
            "control_revision": CONTROL,
            "tasks": tasks,
            "tasks_sha256": digest(tasks),
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "reasoning_effort": "medium",
            "max_turns": args.max_turns,
            "temperature": None,
            "agent_image": args.agent_image,
            "agent_image_id": image_id(args.agent_image),
            "grader_image": args.grader_image,
            "grader_image_id": image_id(args.grader_image),
            "worker_sha256": hashlib.sha256(
                (ROOT / "benchmark/agent_worker.py").read_bytes()
            ).hexdigest(),
            "settings": {
                "map_tokens": 1024,
                "auto_lint": True,
                "auto_test": False,
                "auto_commits": False,
                "suggest_shell_commands": False,
            },
        }
        manifest["experiment_id"] = digest(manifest)[:20]
        path = output / "baseline_manifest.json"
        if path.exists() and read_json(path) != manifest:
            raise ValueError(
                "Baseline configuration changed; use a new output directory"
            )
        atomic_json(path, manifest)
        from aider.chatgpt_transport import ChatGPTTransport

        transport = ChatGPTTransport()
        completed_this_run = 0
        for task in tasks:
            result_path = output / ARMS[0] / (task["instance_id"] + ".json")
            if result_path.exists() and read_json(result_path)["status"] == "completed":
                continue
            if args.limit is not None and completed_this_run >= args.limit:
                break
            print(f"Starting {task['instance_id']}", flush=True)
            try:
                artifact_dir = output / "agents" / task["instance_id"]
                telemetry_path = artifact_dir / "telemetry.json"
                telemetry = (
                    read_json(telemetry_path)
                    if telemetry_path.exists()
                    else run_agent(manifest, task, output, transport)
                )
                patch = (artifact_dir / "prediction.patch").read_bytes()
                if hashlib.sha256(patch).hexdigest() != telemetry["patch_sha256"]:
                    raise ValueError("Prediction patch fingerprint mismatch")
                print(
                    f"Grading {task['instance_id']} ({len(patch)} patch bytes)",
                    flush=True,
                )
                resolved, grading = grade(
                    manifest, task, rows[task["instance_id"]], patch, output
                )
                transcript_dir = output / "transcripts" / task["instance_id"]
                oracle_files = re.findall(
                    r"^\+\+\+ b/(.+)$", rows[task["instance_id"]]["patch"], re.M
                )
                result = result_record(
                    manifest,
                    task,
                    telemetry,
                    successful_calls(transcript_dir),
                    oracle_files,
                    resolved,
                    grading,
                    not any(transcript_dir.glob("*.error.json")),
                )
            except Exception as exc:
                result = {
                    "schema_version": 1,
                    "instance_id": task["instance_id"],
                    "repo": task["repo"],
                    "experiment_id": manifest["experiment_id"],
                    "status": "infrastructure_error",
                    "resolved": None,
                    "error": str(exc),
                }
                atomic_json(result_path, result)
                progress_report(manifest, output)
                print(f"STOPPED: {exc}", flush=True)
                return 1  # Stop new subscription calls on any unresolved infrastructure/plan failure.
            atomic_json(result_path, result)
            completed_this_run += 1
            progress = progress_report(manifest, output)
            print(
                f"Completed {progress['completed']}/{progress['planned']}: {task['instance_id']}, resolved={result['resolved']}",
                flush=True,
            )
        progress_report(manifest, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
