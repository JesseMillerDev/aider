"""Freeze a repository-balanced SWE-bench Lite subset without patch-based selection."""

import argparse
import hashlib
import json
import random
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from benchmark.ab_support import SCHEMA_VERSION, digest, public_task, write_json

DATASET = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
DEFAULT_REPOS = (
    "django/django",
    "sympy/sympy",
    "scikit-learn/scikit-learn",
    "pytest-dev/pytest",
    "matplotlib/matplotlib",
    "astropy/astropy",
    "sphinx-doc/sphinx",
    "psf/requests",
)


def select_rows(rows, limit=50, seed=42, repos=DEFAULT_REPOS):
    if limit <= 0:
        raise ValueError("limit must be positive")
    repos = sorted(set(repos))
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        task = public_task(row)
        if task["instance_id"] in seen:
            raise ValueError(f"Duplicate task: {task['instance_id']}")
        seen.add(task["instance_id"])
        if task["repo"] in repos:
            groups[task["repo"]].append(row)
    missing = set(repos) - set(groups)
    if missing:
        raise ValueError(f"Requested repositories absent from dataset: {sorted(missing)}")
    if limit > sum(map(len, groups.values())):
        raise ValueError("limit exceeds available tasks")
    rng = random.Random(seed)
    for repo in repos:
        groups[repo].sort(key=lambda row: row["instance_id"])
        rng.shuffle(groups[repo])
    # Rotate the remainder across repositories, including small smoke samples.
    rng.shuffle(repos)
    selected = []
    while len(selected) < limit:
        for repo in repos:
            if groups[repo] and len(selected) < limit:
                selected.append(groups[repo].pop())
    return selected


def build_subset(rows, limit=50, seed=42, repos=DEFAULT_REPOS, source=None):
    selected = select_rows(rows, limit, seed, repos)
    tasks = [public_task(row) for row in selected]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "selection": {"seed": seed, "limit": limit, "policy": "balanced_round_robin"},
        "repo_counts": dict(sorted(Counter(task["repo"] for task in tasks).items())),
        "tasks_sha256": digest(tasks),
        "tasks": tasks,
    }, selected


def load_rows(path):
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ValueError("Parquet input requires pyarrow (see requirements-benchmark.in)") from exc
        return pq.read_table(path).to_pylist()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("Input JSON must be an array of dataset rows")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Local dataset JSON, JSONL or Parquet")
    parser.add_argument("--revision", default=DATASET_REVISION)
    parser.add_argument("--cache-dir", type=Path, default=Path("benchmark/.cache"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repos", nargs="+", default=DEFAULT_REPOS)
    parser.add_argument("--output", type=Path, default=Path("benchmark/subset.json"))
    parser.add_argument("--labels-output", type=Path, help="Evaluator-only oracle file labels")
    args = parser.parse_args(argv)
    try:
        path = args.input
        source = {"kind": "local"}
        if path is None:
            if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
                raise ValueError("Use an immutable 40-character dataset revision")
            url = (
                f"https://huggingface.co/datasets/{DATASET}/resolve/{args.revision}"
                "/data/test-00000-of-00001.parquet"
            )
            path = args.cache_dir / f"swe-bench-lite-test-{args.revision}.parquet"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # A failed download must never leave a seemingly valid cached dataset.
                partial = path.with_suffix(".download")
                try:
                    with urllib.request.urlopen(url, timeout=60) as response:
                        partial.write_bytes(response.read())
                    partial.replace(path)
                finally:
                    partial.unlink(missing_ok=True)
            source = {"dataset": DATASET, "revision": args.revision, "split": "test", "url": url}
        source["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        subset, selected = build_subset(load_rows(path), args.limit, args.seed, args.repos, source)
        if args.labels_output and args.labels_output.resolve() == args.output.resolve():
            raise ValueError("Labels and agent subset must use different paths")
        write_json(args.output, subset)
        if args.labels_output:
            labels = {
                row["instance_id"]: sorted(set(re.findall(r"^\+\+\+ b/(.+)$", row["patch"], re.M)))
                for row in selected
            }
            write_json(args.labels_output, {"tasks_sha256": subset["tasks_sha256"], "files": labels})
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(f"Selected {len(subset['tasks'])} tasks: {args.output}")


if __name__ == "__main__":
    main()
