"""Run one evaluator-only gold patch through the Linux SWE-bench grader."""

import argparse
import hashlib
import subprocess
import uuid
from pathlib import Path

from benchmark.ab_support import read_json, write_json
from benchmark.select_subset import DATASET_REVISION, load_rows

IMAGE = "better-aider-swebench-grader:4.1.0"


def read_resolution(report_path, instance_id):
    report = read_json(report_path)
    resolved = report[instance_id]["resolved"]
    if type(resolved) is not bool:
        raise ValueError("Official report must contain a boolean resolved value")
    return resolved


def run_smoke(dataset, instance_id, output_dir, image=IMAGE, build=True):
    root = Path(__file__).resolve().parents[1]
    rows = [row for row in load_rows(dataset) if row["instance_id"] == instance_id]
    if len(rows) != 1:
        raise ValueError("The dataset must contain exactly one matching instance")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError("Use a fresh output directory to avoid cached grading reports")
    output_dir.mkdir(parents=True)
    run_id = f"gold-smoke-{uuid.uuid4().hex}"
    summary = {
        "kind": "gold_infrastructure_smoke",
        "instance_id": instance_id,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "grading_run_id": run_id,
        "image": image,
        "status": "starting",
        "resolved": None,
    }
    # This evaluator mount contains the oracle patch. It must never be used by an agent.
    write_json(output_dir / "dataset.json", rows)
    write_json(output_dir / "summary.json", summary)

    def command(args, log_name):
        with (output_dir / log_name).open("w", encoding="utf-8") as log:
            subprocess.run(args, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)

    try:
        command(["docker", "info"], "docker-info.log")
        if build:
            command(["docker", "build", "--tag", image, str(root / "benchmark/grader")], "build.log")
        summary["image_id"] = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
        ).strip()
        command([
            "docker", "run", "--rm",
            "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount", f"type=bind,source={output_dir},target=/grading",
            image,
            "--dataset_name", "/grading/dataset.json",
            "--predictions_path", "gold",
            "--instance_ids", instance_id,
            "--max_workers", "1",
            "--run_id", run_id,
            "--timeout", "300",
            "--report_dir", "/grading/reports",
        ], "grading.log")
        report_path = (output_dir / "logs/run_evaluation" / run_id / "gold"
                       / instance_id / "report.json")
        summary["resolved"] = read_resolution(report_path, instance_id)
        summary["status"] = "completed"
        summary["report"] = report_path.relative_to(output_dir).as_posix()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        summary["status"] = "infrastructure_error"
        summary["error"] = str(exc)
    write_json(output_dir / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(
        f"benchmark/.cache/swe-bench-lite-test-{DATASET_REVISION}.parquet"))
    parser.add_argument("--instance-id", default="pytest-dev__pytest-7220")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args(argv)
    output = args.output_dir or Path("benchmark/oracle") / f"gold-smoke-{uuid.uuid4().hex[:12]}"
    try:
        summary = run_smoke(args.dataset, args.instance_id, output, args.image, not args.skip_build)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(f"Gold smoke: {summary['status']}, resolved={summary['resolved']}; logs: {output}")
    return 0 if summary["status"] == "completed" and summary["resolved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
