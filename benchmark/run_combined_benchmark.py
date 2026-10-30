"""Run the explicitly requested full fifty-task all-feature exploratory treatment."""

import argparse
import hashlib
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from benchmark.ab_support import digest, read_json, validate_subset
from benchmark.build_combined_agent import BUILD_RECORD
from benchmark.grade_smoke import read_resolution
from benchmark.run_baseline import ROOT, atomic_json, image_id
from benchmark.run_lsp_benchmark import phase
from benchmark.select_subset import DATASET_REVISION, load_rows

BASELINE = ROOT / "benchmark/baselines/gpt-6.1-sol-subscription-50.json"
ARM = "treatment_combined_all"
FEATURES = {
    "enable_lsp_graph": True,
    "lsp_timeout": 10.0,
    "enable_agent_nav": True,
    "enable_l2_paging": True,
    "max_l1_tokens": None,
    "max_l1_files": None,
    "max_l2_tokens": None,
    "l1_ttl_turns": 2,
}
SETTINGS = {
    key: value for key, value in FEATURES.items() if key.startswith("max_") or key == "l1_ttl_turns"
}


def prepare(output, instances, dataset):
    baseline, build = read_json(BASELINE), read_json(BUILD_RECORD)
    config = baseline["configuration"]
    tasks = validate_subset(read_json(instances))
    if digest(tasks) != config["tasks_sha256"]:
        raise ValueError("Combined run must use the frozen task IDs and order")
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != config["dataset_sha256"]:
        raise ValueError("Frozen dataset changed")
    rows = {row["instance_id"]: row for row in load_rows(dataset)}
    for task in tasks:
        if any(value != rows[task["instance_id"]][key] for key, value in task.items()):
            raise ValueError("Public task differs from evaluator data")
    if build["base_image_id"] != config["agent_image_id"]:
        raise ValueError("Control Python runtime differs")
    if image_id(build["agent_image"]) != build["agent_image_id"]:
        raise ValueError("Pinned combined image changed")
    if image_id(config["grader_image"]) != config["grader_image_id"]:
        raise ValueError("Pinned grader changed")
    for name, field in (
        ("benchmark/agent_worker.py", "worker_sha256"),
        ("benchmark/combined_preflight.py", "preflight_sha256"),
        ("benchmark/pager_preflight.py", "pager_preflight_sha256"),
    ):
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != build[field]:
            raise ValueError("Built worker/fixture source changed: " + name)
    proof = build["preflight"]
    combined = proof.get("combined", {})
    if proof.get("status") != "passed" or proof.get("inference_requests") != 0:
        raise ValueError("Combined offline preflight failed")
    if combined.get("enabled") != {"lsp_ranking": True, "navigation": True, "idle_paging": True}:
        raise ValueError("Not all implemented features were enabled")
    if not all(
        combined.get(key)
        for key in (
            "shared_bridge",
            "native_definition_lookup",
            "native_reference_lookup",
            "source_unchanged",
        )
    ):
        raise ValueError("Combined navigation/LSP/page lifecycle was not verified")
    host_files = [
        "benchmark/run_baseline.py",
        "benchmark/run_pipeline.py",
        "benchmark/run_lsp_benchmark.py",
        "benchmark/run_combined_benchmark.py",
        "benchmark/build_combined_agent.py",
        "benchmark/compare_lsp.py",
        "benchmark/combined_preflight.py",
        "benchmark/pager_preflight.py",
        "aider/chatgpt_transport.py",
    ]
    manifest = {
        "schema_version": 1,
        "kind": "combined_context_treatment",
        "arm": ARM,
        "treatment_label": "all implemented context features",
        "paging_policy": "demand",
        "model": baseline["model"],
        "control_experiment_id": baseline["experiment_id"],
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "control_revision": config["control_revision"],
        "treatment_revision": build["source_revision"],
        "source_archive_sha256": build["source_archive_sha256"],
        "worker_sha256": build["worker_sha256"],
        "preflight_sha256": build["preflight_sha256"],
        "dockerfile_sha256": build["dockerfile_sha256"],
        "base_image_id": build["base_image_id"],
        "language_server_base_image_id": build["language_server_base_image_id"],
        "node_version": build["node_version"],
        "pyright_version": build["pyright_version"],
        "agent_image": build["agent_image"],
        "agent_image_id": build["agent_image_id"],
        "grader_image": config["grader_image"],
        "grader_image_id": config["grader_image_id"],
        "reasoning_effort": config["reasoning_effort"],
        "max_turns": config["max_turns"],
        "temperature": None,
        "settings": config["settings"],
        "features": FEATURES,
        "tasks": tasks,
        "tasks_sha256": digest(tasks),
        "dataset_sha256": config["dataset_sha256"],
        "smoke_instance_ids": [task["instance_id"] for task in tasks[:5]],
        "scaling_policy": "user_requested_full_50_exploratory",
        "authorization": (
            "User requested all implemented features on the full fifty despite the prior demand-paging efficiency gate; preserve that negative result separately."
        ),
        "prior_smoke_experiment": "400d3df9df553936552a",
        "host_files_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in host_files
        },
    }
    manifest["experiment_id"] = digest(manifest)[:20]
    path = output / "run_manifest.json"
    if path.exists() and read_json(path) != manifest:
        raise ValueError("Combined run configuration changed; use a new output directory")
    atomic_json(path, manifest)
    atomic_json(output / "preflight.json", proof)
    return manifest


def check_smoke(manifest, output):
    if (
        manifest.get("features") != FEATURES
        or manifest.get("scaling_policy") != "user_requested_full_50_exploratory"
    ):
        raise ValueError("Combined features or explicit full-run authorization missing")
    if hashlib.sha256(BASELINE.read_bytes()).hexdigest() != manifest["baseline_sha256"]:
        raise ValueError("Frozen baseline changed")
    baseline = read_json(BASELINE)
    if digest(manifest["tasks"]) != baseline["configuration"]["tasks_sha256"]:
        raise ValueError("Frozen task order changed")
    keys = manifest["smoke_instance_ids"]
    if len(keys) != 5 or keys != [task["instance_id"] for task in manifest["tasks"][:5]]:
        raise ValueError("Combined smoke must use the original first five tasks")
    control = {row["instance_id"]: row for row in baseline["instances"]}
    semantic, bridge, actions = Counter(), Counter(), Counter()
    details = []
    token_total = baseline_total = 0
    for key in keys:
        row = read_json(output / ARM / (key + ".json"))
        telemetry = read_json(output / "agents" / key / "telemetry.json")
        if row.get("status") != "completed" or not row.get("usage_complete"):
            raise ValueError("Incomplete combined smoke grading or usage")
        grading = row.get("grading", {})
        if grading.get("engine") != "swebench" or grading.get("status") not in (
            "graded",
            "empty_prediction",
        ):
            raise ValueError("Official smoke grading missing")
        if grading["status"] == "graded":
            if read_resolution(output / grading["report"], key) != row["resolved"]:
                raise ValueError("Smoke resolution differs from official report")
        elif row["resolved"]:
            raise ValueError("Empty prediction cannot be resolved")
        lsp, pager, nav = telemetry.get("lsp"), telemetry.get("pager"), telemetry.get("navigation")
        if not lsp or not lsp.get("maps"):
            raise ValueError("LSP ranking telemetry missing")
        if not pager or not pager.get("enabled") or pager.get("settings") != SETTINGS:
            raise ValueError("Idle paging with unset caps was not enabled")
        if pager["stats"].get("preloads", 0):
            raise ValueError("Unexpected bulk dependency preloading")
        if (
            not nav
            or not nav.get("enabled")
            or nav.get("model_requests_with_instructions") != pager["completed_responses"]
        ):
            raise ValueError("Navigation flag or protocol instructions were not active")
        if not nav.get("bridge_shared_with_ranking"):
            raise ValueError("Navigation did not share the ranking bridge")
        if (
            not 0 < pager["completed_responses"] <= row["total_turns"]
            or len(pager["request_contexts"]) != row["total_turns"]
            or len(pager["events"]) != pager["completed_responses"]
        ):
            raise ValueError("Combined context/request telemetry incomplete")
        for request in pager["request_contexts"]:
            if set(request["l1_files"]) & set(request["l2_files"]):
                raise ValueError("A file was sent in both context levels")
        if "python" in lsp.get("errors", {}) or "python" in lsp.get("unavailable_languages", []):
            raise ValueError("Python language server failed during smoke")
        semantic.update(lsp["totals"])
        bridge.update(lsp["bridge"])
        actions.update(nav["action_attempts"])
        values = [row.get("total_input_tokens"), row.get("total_output_tokens")]
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Combined smoke usage invalid")
        token_total += sum(values)
        baseline_total += control[key]["total_input_tokens"] + control[key]["total_output_tokens"]
        details.append(
            {
                "instance_id": key,
                "resolved": row["resolved"],
                "control_resolved": control[key]["resolved"],
                "model_calls": row["total_turns"],
            }
        )
    if bridge["crashed_servers"] or not (
        bridge["started_servers"]
        and bridge["queries"]
        and semantic["resolved_sites"]
        and semantic["boosted_edges"] + semantic["dampened_edges"]
    ):
        raise ValueError("No functioning semantic ranking during smoke")
    gate = {
        "status": "passed",
        "experiment_id": manifest["experiment_id"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "criterion": (
            "All three feature flags/protocols verified; complete official grading/usage; usable semantic ranking. Full fifty explicitly requested; score/tokens do not gate this exploratory arm."
        ),
        "measurement": {"treatment_tokens": token_total, "control_tokens": baseline_total},
        "semantic": dict(semantic),
        "bridge": dict(bridge),
        "navigation_attempts": dict(actions),
        "tasks": details,
    }
    atomic_json(output / "smoke_gate.json", gate)
    return gate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmark/results/combined-all-50-v1"))
    parser.add_argument("--instances", type=Path, default=Path("benchmark/subset_50.json"))
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(f"benchmark/.cache/swe-bench-lite-test-{DATASET_REVISION}.parquet"),
    )
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output / "orchestrator.lock"), timeout=1):
        manifest = prepare(output, args.instances, args.dataset)
        if args.prepare_only:
            print(
                f"Prepared {manifest['experiment_id']}: all implemented features; full 50 explicitly requested",
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
                        phase(output, args.dataset, "generate", "--limit", str(remaining)),
                        check=True,
                    )
                status("smoke_grading")
                subprocess.run(phase(output, args.dataset, "grade"), check=True)
            gate = check_smoke(manifest, output)
            print(
                f"All-feature infrastructure gate passed: {gate['measurement']}; scaling under explicit full-50 request",
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
            print("Full combined fifty-task result saved", flush=True)
            return 0
        except Exception as exc:
            status("blocked", error=str(exc))
            print(f"STOPPED: {exc}", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
