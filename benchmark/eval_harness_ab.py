"""Prepare an immutable A/B plan. Execution adapters are a separate milestone."""

import argparse
import hashlib
import importlib.metadata
import platform
import random
import subprocess
from pathlib import Path

from benchmark.ab_support import ARMS, SCHEMA_VERSION, digest, read_json, validate_subset, write_json

UPSTREAM_REVISION = "5dc9490bb35f9729ef2c95d00a19ccd30c26339c"
FEATURES = ["lsp_graph", "l2_paging", "agent_nav"]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=Path(__file__).resolve().parents[1]).strip()


def build_plan(subset, model, control_revision, treatment_revision, seed=42,
               max_turns=25, max_cost_usd=3.0, source_fingerprint=None, environment=None):
    tasks = validate_subset(subset)
    if not model.strip() or max_turns <= 0 or not 0 < max_cost_usd < float("inf"):
        raise ValueError("Model and positive finite budgets are required")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "status": "planned_only",
        "model": model,
        "temperature": 0,
        "seed": seed,
        "dataset_source": subset.get("source"),
        "tasks_sha256": subset["tasks_sha256"],
        "source_fingerprint": source_fingerprint,
        "environment": environment,
        "budget_per_task_per_arm": {"max_turns": max_turns, "max_cost_usd": max_cost_usd},
        "arms": {
            ARMS[0]: {"revision": control_revision, "map_tokens": 1024, "features": []},
            ARMS[1]: {
                "revision": treatment_revision,
                "map_tokens": 1024,
                "features": FEATURES,
                "features_implemented": False,
                "max_l1_tokens": 16000,
                "max_l2_tokens": 6000,
                "max_l1_files": 4,
                "l1_ttl_turns": 2,
                "lsp_timeout_seconds": 2.0,
            },
        },
        "tasks": tasks,
    }
    plan["experiment_id"] = digest(plan)[:20]
    rng = random.Random(seed)
    schedule = []
    for task in tasks:
        arms = list(ARMS)
        rng.shuffle(arms)
        for arm in arms:
            schedule.append({
                "instance_id": task["instance_id"],
                "arm": arm,
                "worktree": f"worktrees/{arm}/{task['instance_id']}",
                "result": f"{arm}/{task['instance_id']}.json",
                "grading_run_id": f"{plan['experiment_id']}-{arm}",
            })
    plan["schedule"] = schedule
    return plan


def source_fingerprint():
    """Capture dirty/untracked source as well as HEAD; generated outputs are excluded."""
    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                "aider", "benchmark", "requirements", "requirements.txt", "pyproject.toml").split(b"\0")
    root = Path(__file__).resolve().parents[1]
    entries = {}
    for name in sorted(set(files) - {b""}):
        path = root / name.decode("utf-8")
        entries[name.decode("utf-8")] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
        )
    return digest(entries)


def environment_snapshot():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": sorted(
            [distribution.metadata["Name"], distribution.version]
            for distribution in importlib.metadata.distributions()
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Exact provider model ID, no implicit default")
    parser.add_argument("--control-revision", default=UPSTREAM_REVISION)
    parser.add_argument("--treatment-revision", default="HEAD")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--max-cost-usd", type=float, default=3.0)
    parser.add_argument("--output", type=Path, default=Path("benchmark/results/manifest.json"))
    args = parser.parse_args(argv)
    try:
        control = git("rev-parse", "--verify", f"{args.control_revision}^{{commit}}").decode()
        treatment = git("rev-parse", "--verify", f"{args.treatment_revision}^{{commit}}").decode()
        plan = build_plan(read_json(args.instances), args.model, control, treatment, args.seed,
                          args.max_turns, args.max_cost_usd, source_fingerprint(), environment_snapshot())
        if args.output.exists() and read_json(args.output) != plan:
            raise ValueError("Refusing to overwrite a different experiment; use a new output path")
        write_json(args.output, plan)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f"Planned {len(plan['schedule'])} runs: {args.output}")
    print("Planning only. No agents or grading containers were executed.")


if __name__ == "__main__":
    main()
