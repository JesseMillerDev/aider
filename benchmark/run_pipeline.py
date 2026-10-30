"""Resume generation and grading as independent, credential-separated baseline stages."""

import argparse
import hashlib
import re
import time
from pathlib import Path

from filelock import FileLock

from benchmark.ab_support import ARMS, read_json
from benchmark.run_baseline import (
    ROOT,
    atomic_json,
    grade,
    image_id,
    progress_report,
    result_record,
    run_agent,
    successful_calls,
)
from benchmark.select_subset import DATASET_REVISION, load_rows


def generated_count(manifest, output):
    return sum(
        (output / "agents" / task["instance_id"] / "telemetry.json").exists()
        for task in manifest["tasks"]
    )


def stage_status(output, phase, status, manifest, **extra):
    atomic_json(
        output / (phase + "_status.json"),
        {
            "phase": phase,
            "status": status,
            "experiment_id": manifest["experiment_id"],
            "generated": generated_count(manifest, output),
            "planned": len(manifest["tasks"]),
            **extra,
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("generate", "grade"), required=True)
    parser.add_argument(
        "--limit", type=int, help="Maximum newly generated tasks for a smoke stage"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/results/baseline-50")
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(
            f"benchmark/.cache/swe-bench-lite-test-{DATASET_REVISION}.parquet"
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Wait for newly generated patches when grading",
    )
    args = parser.parse_args(argv)
    output = args.output.resolve()
    manifest_path = output / "run_manifest.json"
    manifest = read_json(
        manifest_path if manifest_path.exists() else output / "baseline_manifest.json"
    )
    arm = manifest.get("arm", ARMS[0])
    if image_id(manifest["agent_image"]) != manifest["agent_image_id"]:
        raise ValueError("The pinned agent image changed")
    if image_id(manifest["grader_image"]) != manifest["grader_image_id"]:
        raise ValueError("The pinned grader image changed")
    if (
        hashlib.sha256((ROOT / "benchmark/agent_worker.py").read_bytes()).hexdigest()
        != manifest["worker_sha256"]
    ):
        raise ValueError("The pinned worker source changed")
    if (
        hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        != manifest["dataset_sha256"]
    ):
        raise ValueError("The evaluator dataset changed")
    for name, fingerprint in manifest.get("host_files_sha256", {}).items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != fingerprint:
            raise ValueError(f"The pinned runner source changed: {name}")
    with FileLock(str(output / (args.phase + ".lock")), timeout=1):
        stage_status(output, args.phase, "running", manifest)
        if args.phase == "generate":
            from aider.chatgpt_transport import ChatGPTTransport

            transport = ChatGPTTransport()
            newly_generated = 0
            for task in manifest["tasks"]:
                telemetry = output / "agents" / task["instance_id"] / "telemetry.json"
                if telemetry.exists():
                    continue
                if args.limit is not None and newly_generated >= args.limit:
                    stage_status(output, args.phase, "partial", manifest)
                    return 0
                print(f"Generating {task['instance_id']}", flush=True)
                try:
                    run_agent(manifest, task, output, transport)
                except Exception as exc:
                    stage_status(
                        output,
                        args.phase,
                        "blocked",
                        manifest,
                        instance_id=task["instance_id"],
                        error=str(exc),
                    )
                    print(f"Generation stopped: {exc}", flush=True)
                    return 1
                newly_generated += 1
                stage_status(output, args.phase, "running", manifest)
                print(
                    f"Generated {generated_count(manifest, output)}/{len(manifest['tasks'])}",
                    flush=True,
                )
            stage_status(output, args.phase, "complete", manifest)
            return 0

        rows = {row["instance_id"]: row for row in load_rows(args.dataset)}
        while True:
            processed = False
            for task in manifest["tasks"]:
                instance_id = task["instance_id"]
                result_path = output / arm / (instance_id + ".json")
                if (
                    result_path.exists()
                    and read_json(result_path)["status"] == "completed"
                ):
                    continue
                artifacts = output / "agents" / instance_id
                telemetry_path = artifacts / "telemetry.json"
                if not telemetry_path.exists():
                    continue
                telemetry = read_json(telemetry_path)
                patch = (artifacts / "prediction.patch").read_bytes()
                if hashlib.sha256(patch).hexdigest() != telemetry["patch_sha256"]:
                    raise ValueError("Prediction changed after generation")
                print(f"Grading {instance_id}", flush=True)
                try:
                    resolved, grading = grade(
                        manifest, task, rows[instance_id], patch, output
                    )
                except Exception as exc:
                    atomic_json(
                        result_path,
                        {
                            "schema_version": 1,
                            "instance_id": instance_id,
                            "experiment_id": manifest["experiment_id"],
                            "repo": task["repo"],
                            "status": "infrastructure_error",
                            "resolved": None,
                            "error": str(exc),
                        },
                    )
                    stage_status(
                        output,
                        args.phase,
                        "blocked",
                        manifest,
                        instance_id=instance_id,
                        error=str(exc),
                    )
                    progress_report(manifest, output)
                    print(f"Grading stopped: {exc}", flush=True)
                    return 1
                transcripts = output / "transcripts" / instance_id
                oracle = re.findall(
                    r"^\+\+\+ b/(.+)$", rows[instance_id]["patch"], re.M
                )
                result = result_record(
                    manifest,
                    task,
                    telemetry,
                    successful_calls(transcripts),
                    oracle,
                    resolved,
                    grading,
                    not any(transcripts.glob("*.error.json")),
                )
                atomic_json(result_path, result)
                summary = progress_report(manifest, output)
                print(
                    f"Graded {summary['completed']}/{summary['planned']}: resolved={resolved}",
                    flush=True,
                )
                processed = True
            summary = progress_report(manifest, output)
            if summary["completed"] == summary["planned"]:
                stage_status(output, args.phase, "complete", manifest)
                return 0
            generation_path = output / "generate_status.json"
            generation = read_json(generation_path) if generation_path.exists() else {}
            if generation.get("status") == "blocked" and not processed:
                stage_status(output, args.phase, "waiting_for_generation", manifest)
                return 1
            if not args.watch:
                stage_status(output, args.phase, "partial", manifest)
                return 0
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
