"""Gate the LSP-only fifty-task treatment on real container and five-task smoke checks."""

import argparse
import hashlib
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from benchmark.ab_support import digest, read_json, validate_subset
from benchmark.build_lsp_agent import BUILD_RECORD
from benchmark.run_baseline import ROOT, atomic_json, image_id, run_agent
from benchmark.select_subset import DATASET_REVISION, load_rows

BASELINE = ROOT / "benchmark/baselines/gpt-6.1-sol-subscription-50.json"
ARM = "treatment_lsp_graph"


def prepare(output, instances, dataset):
    baseline = read_json(BASELINE)
    build = read_json(BUILD_RECORD)
    tasks = validate_subset(read_json(instances))
    config = baseline["configuration"]
    if digest(tasks) != config["tasks_sha256"]:
        raise ValueError("Treatment must use the frozen baseline task selection")
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != config["dataset_sha256"]:
        raise ValueError("Evaluator dataset differs from the frozen baseline")
    rows = {row["instance_id"]: row for row in load_rows(dataset)}
    for task in tasks:
        if any(value != rows[task["instance_id"]][key] for key, value in task.items()):
            raise ValueError("Public task differs from the evaluator dataset")
    if image_id(build["agent_image"]) != build["agent_image_id"]:
        raise ValueError("Treatment image changed after the build")
    if image_id(config["grader_image"]) != config["grader_image_id"]:
        raise ValueError("Grader image differs from the frozen baseline")
    worker_hash = hashlib.sha256(
        (ROOT / "benchmark/agent_worker.py").read_bytes()
    ).hexdigest()
    if worker_hash != build["worker_sha256"]:
        raise ValueError("Worker changed after building the image")
    manifest = {
        "schema_version": 1,
        "kind": "lsp_only_treatment",
        "arm": ARM,
        "model": baseline["model"],
        "control_experiment_id": baseline["experiment_id"],
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "control_revision": config["control_revision"],
        "treatment_revision": build["source_revision"],
        "source_archive_sha256": build["source_archive_sha256"],
        "dockerfile_sha256": build["dockerfile_sha256"],
        "base_image_id": build["base_image_id"],
        "tasks": tasks,
        "tasks_sha256": digest(tasks),
        "dataset_sha256": config["dataset_sha256"],
        "reasoning_effort": config["reasoning_effort"],
        "max_turns": config["max_turns"],
        "temperature": None,
        "agent_image": build["agent_image"],
        "agent_image_id": build["agent_image_id"],
        "grader_image": config["grader_image"],
        "grader_image_id": config["grader_image_id"],
        "worker_sha256": worker_hash,
        "host_files_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "benchmark/run_baseline.py",
                "benchmark/run_pipeline.py",
                "benchmark/run_lsp_benchmark.py",
                "benchmark/compare_lsp.py",
                "aider/chatgpt_transport.py",
            )
        },
        "settings": config["settings"],
        "features": {"enable_lsp_graph": True, "lsp_timeout": 2.0},
        "language_servers": {
            "pyright": build["pyright_version"],
            "node": build["node_version"],
        },
        "smoke_instance_ids": [task["instance_id"] for task in tasks[:5]],
    }
    manifest["experiment_id"] = digest(manifest)[:20]
    path = output / "run_manifest.json"
    if path.exists() and read_json(path) != manifest:
        raise ValueError("Treatment configuration changed; use a new output directory")
    atomic_json(path, manifest)
    return manifest


def check_smoke(manifest, output):
    totals, bridge = Counter(), Counter()
    details = []
    for instance_id in manifest["smoke_instance_ids"]:
        row = read_json(output / ARM / (instance_id + ".json"))
        telemetry = read_json(output / "agents" / instance_id / "telemetry.json")
        if row.get("status") != "completed" or not row.get("usage_complete"):
            raise ValueError("Smoke task has incomplete grading or usage")
        lsp = telemetry.get("lsp")
        if not lsp or not lsp["maps"]:
            raise ValueError("LSP instrumentation was not active in the container")
        if "python" in lsp.get("unavailable_languages", []) or "python" in lsp.get(
            "errors", {}
        ):
            raise ValueError(
                "Container Python language server failed; inspect smoke telemetry"
            )
        totals.update(lsp["totals"])
        bridge.update(lsp["bridge"])
        details.append({"instance_id": instance_id, "lsp": lsp})
    if bridge["crashed_servers"]:
        raise ValueError("Container language server failed; inspect smoke telemetry")
    if not (
        bridge["started_servers"]
        and bridge["queries"]
        and totals["resolved_sites"]
        and totals["boosted_edges"] + totals["dampened_edges"]
    ):
        raise ValueError("Smoke made no verified semantic graph changes")
    gate = {
        "status": "passed",
        "experiment_id": manifest["experiment_id"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "totals": dict(totals),
        "bridge": dict(bridge),
        "tasks": details,
        "criterion": "Five complete graded tasks; usable servers; actual resolved sites and changed edges. Score is not a gate.",
    }
    atomic_json(output / "smoke_gate.json", gate)
    return gate


def phase(output, dataset, name, *options):
    return [
        sys.executable,
        "-u",
        "-m",
        "benchmark.run_pipeline",
        "--phase",
        name,
        "--output",
        str(output),
        "--dataset",
        str(dataset),
        *options,
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/results/lsp-graph-50-v2")
    )
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
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Build a real task map without model inference",
    )
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output / "orchestrator.lock"), timeout=1):
        manifest = prepare(output, args.instances, args.dataset)
        if args.prepare_only:
            print(
                f"Prepared {manifest['experiment_id']}: {len(manifest['tasks'])} tasks",
                flush=True,
            )
            return 0
        if args.preflight:

            class NoInference:
                def send_completion(self, *args, **kwargs):
                    raise RuntimeError("Preflight must never request model inference")

            task = next(
                task for task in manifest["tasks"] if task["repo"] == "psf/requests"
            )
            telemetry = run_agent(
                {**manifest, "preflight": True},
                task,
                output / "preflight",
                NoInference(),
            )
            atomic_json(output / "preflight.json", telemetry)
            print(f"No-inference container preflight: {telemetry['lsp']}", flush=True)
            return 0

        def status(state, **extra):
            atomic_json(
                output / "run_status.json",
                {
                    "status": state,
                    "experiment_id": manifest["experiment_id"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    **extra,
                },
            )

        try:
            if not (output / "smoke_gate.json").exists():
                status("smoke_generation")
                subprocess.run(
                    phase(output, args.dataset, "generate", "--limit", "5"), check=True
                )
                status("smoke_grading")
                subprocess.run(phase(output, args.dataset, "grade"), check=True)
                gate = check_smoke(manifest, output)
                print(f"Smoke passed: {gate['totals']}; {gate['bridge']}", flush=True)
            else:
                check_smoke(manifest, output)
            status("full_run")
            with (output / "grade.log").open("a", encoding="utf-8") as log:
                grader = subprocess.Popen(
                    phase(output, args.dataset, "grade", "--watch"),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                generated = subprocess.run(phase(output, args.dataset, "generate"))
                graded = grader.wait()
                if generated.returncode or graded:
                    raise RuntimeError(
                        f"Pipeline stopped: generate={generated.returncode}, grade={graded}"
                    )
            from benchmark.compare_lsp import compare

            compare(output)
            status("complete")
            print(
                "Full treatment and historical-control comparison complete", flush=True
            )
            return 0
        except Exception as exc:
            status("blocked", error=str(exc))
            print(f"STOPPED: {exc}", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
