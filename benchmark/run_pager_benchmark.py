"""Run five gated smoke tasks, then the frozen fifty-task idle-only paging treatment."""

import argparse
import hashlib
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from benchmark.ab_support import digest, read_json, validate_subset
from benchmark.build_pager_agent import BUILD_RECORD, preflight
from benchmark.grade_smoke import read_resolution
from benchmark.run_baseline import ROOT, atomic_json, image_id
from benchmark.run_lsp_benchmark import phase
from benchmark.select_subset import DATASET_REVISION, load_rows

BASELINE = ROOT / "benchmark/baselines/gpt-6.1-sol-subscription-50.json"
ARM = "treatment_pager_idle"
FEATURES = {
    "enable_l2_paging": True,
    "enable_lsp_graph": False,
    "max_l1_tokens": None,
    "max_l1_files": None,
    "max_l2_tokens": None,
    "l1_ttl_turns": 2,
}
SETTINGS = {
    key: value
    for key, value in FEATURES.items()
    if key.startswith("max_") or key == "l1_ttl_turns"
}


def prepare(output, instances, dataset):
    baseline, build = read_json(BASELINE), read_json(BUILD_RECORD)
    config = baseline["configuration"]
    tasks = validate_subset(read_json(instances))
    if digest(tasks) != config["tasks_sha256"]:
        raise ValueError("Treatment must use the frozen baseline task selection")
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != config["dataset_sha256"]:
        raise ValueError("Evaluator dataset differs from the frozen baseline")
    rows = {row["instance_id"]: row for row in load_rows(dataset)}
    for task in tasks:
        if any(value != rows[task["instance_id"]][key] for key, value in task.items()):
            raise ValueError("Public task differs from evaluator data")
    if image_id(build["agent_image"]) != build["agent_image_id"]:
        raise ValueError("Treatment image changed after building")
    if build["base_image_id"] != config["agent_image_id"]:
        raise ValueError("Treatment uses a different control runtime")
    if image_id(config["grader_image"]) != config["grader_image_id"]:
        raise ValueError("Grader image differs from the frozen baseline")
    worker_hash = hashlib.sha256(
        (ROOT / "benchmark/agent_worker.py").read_bytes()
    ).hexdigest()
    if worker_hash != build["worker_sha256"]:
        raise ValueError("Worker changed after building the image")
    if build["preflight"]["status"] != "passed":
        raise ValueError("Offline paging preflight has not passed")
    manifest = {
        "schema_version": 1,
        "kind": "paging_only_treatment",
        "arm": ARM,
        "treatment_label": "idle-only paging treatment",
        "model": baseline["model"],
        "control_experiment_id": baseline["experiment_id"],
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "control_revision": config["control_revision"],
        "treatment_revision": build["source_revision"],
        "source_archive_sha256": build["source_archive_sha256"],
        "dockerfile_sha256": build["dockerfile_sha256"],
        "preflight_sha256": build["preflight_sha256"],
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
        "settings": config["settings"],
        "features": FEATURES,
        "smoke_instance_ids": [task["instance_id"] for task in tasks[:5]],
        "host_files_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "benchmark/run_baseline.py",
                "benchmark/run_pipeline.py",
                "benchmark/run_lsp_benchmark.py",
                "benchmark/run_pager_benchmark.py",
                "benchmark/compare_lsp.py",
                "aider/chatgpt_transport.py",
            )
        },
    }
    manifest["experiment_id"] = digest(manifest)[:20]
    path = output / "run_manifest.json"
    if path.exists() and read_json(path) != manifest:
        raise ValueError("Treatment configuration changed; use a new output directory")
    atomic_json(path, manifest)
    atomic_json(output / "preflight.json", build["preflight"])
    return manifest


def check_smoke(manifest, output):
    totals, details = Counter(), []
    actual_l2_requests = 0
    for key in manifest["smoke_instance_ids"]:
        row = read_json(output / ARM / (key + ".json"))
        data = read_json(output / "agents" / key / "telemetry.json")
        if row.get("status") != "completed" or not row.get("usage_complete"):
            raise ValueError("Smoke task has incomplete grading or usage")
        grading = row.get("grading", {})
        if grading.get("engine") != "swebench" or grading.get("status") not in (
            "graded",
            "empty_prediction",
        ):
            raise ValueError("Smoke task has no official grading provenance")
        if grading["status"] == "graded":
            if read_resolution(output / grading["report"], key) != row["resolved"]:
                raise ValueError("Smoke resolution differs from official report")
        elif row["resolved"]:
            raise ValueError("Empty prediction cannot be resolved")
        pager = data.get("pager")
        if not pager or not pager.get("enabled") or pager.get("settings") != SETTINGS:
            raise ValueError("Paging-only idle defaults were not active")
        if data.get("lsp") is not None or row.get("lsp") is not None:
            raise ValueError("LSP was enabled in the paging-only treatment")
        requests = pager.get("request_contexts", [])
        if not requests or len(requests) != row["total_turns"]:
            raise ValueError("Paging model-request telemetry is incomplete")
        if (
            pager["completed_responses"] != row["total_turns"]
            or len(pager["events"]) != row["total_turns"]
        ):
            raise ValueError("Paging completed-response telemetry is inconsistent")
        for request in requests:
            if set(request["l1_files"]) & set(request["l2_files"]):
                raise ValueError("A file was sent in both context levels")
            actual_l2_requests += bool(request["l2_files"])
        totals.update(
            {
                name: pager["stats"].get(name, 0)
                for name in (
                    "demotions",
                    "preloads",
                    "promotions",
                    "full_file_reloads",
                    "budget_overflows",
                )
            }
        )
        details.append(
            {
                "instance_id": key,
                "resolved": row["resolved"],
                "pager_stats": pager["stats"],
                "model_calls": row["total_turns"],
            }
        )
    if not actual_l2_requests:
        raise ValueError("No L2 interface context was actually sent during smoke")
    gate = {
        "status": "passed",
        "experiment_id": manifest["experiment_id"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "totals": dict(totals),
        "requests_with_l2": actual_l2_requests,
        "tasks": details,
        "criterion": "Five graded tasks with complete usage; pager active with no size caps; LSP disabled; actual L2 model exposure. Score/demotions are not gates.",
    }
    atomic_json(output / "smoke_gate.json", gate)
    return gate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/results/pager-idle-50-v1")
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
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output / "orchestrator.lock"), timeout=1):
        manifest = prepare(output, args.instances, args.dataset)
        if args.preflight:
            proof = preflight(manifest["agent_image_id"])
            atomic_json(output / "preflight.json", proof)
            print(f"No-inference pager preflight passed: {proof}", flush=True)
            return 0
        if args.prepare_only:
            print(
                f"Prepared {manifest['experiment_id']}: 50 tasks, paging only, no size caps",
                flush=True,
            )
            return 0

        def status(value, **extra):
            atomic_json(
                output / "run_status.json",
                {
                    "status": value,
                    "experiment_id": manifest["experiment_id"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    **extra,
                },
            )

        try:
            if not (output / "smoke_gate.json").exists():
                remaining = sum(
                    not (output / "agents" / key / "telemetry.json").exists()
                    for key in manifest["smoke_instance_ids"]
                )
                status("smoke_generation")
                if remaining:
                    subprocess.run(
                        phase(
                            output, args.dataset, "generate", "--limit", str(remaining)
                        ),
                        check=True,
                    )
                status("smoke_grading")
                subprocess.run(phase(output, args.dataset, "grade"), check=True)
            gate = check_smoke(manifest, output)
            print(
                f"Smoke passed: {gate['totals']}; {gate['requests_with_l2']} requests with L2",
                flush=True,
            )
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
                "Full paging treatment and historical-control comparison complete",
                flush=True,
            )
            return 0
        except Exception as exc:
            status("blocked", error=str(exc))
            print(f"STOPPED: {exc}", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
