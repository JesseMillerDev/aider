"""Small, dependency-free contracts shared by the paired benchmark tools."""

import hashlib
import json
import re
from pathlib import Path

SCHEMA_VERSION = 1
ARMS = ("control_stock_aider", "treatment_paged_lsp")
TASK_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")
METRICS = (
    "turns_to_first_edit",
    "tokens_to_first_edit",
    "total_turns",
    "total_input_tokens",
    "cached_input_tokens",
    "total_output_tokens",
    "total_cost_usd",
    "failed_edit_count",
    "localization_recall_at_turn_3",
    "peak_l1_tokens",
    "peak_l2_tokens",
)


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def public_task(row):
    """An allowlist, so future dataset columns cannot leak answers to the agent."""
    task = {field: row[field] for field in TASK_FIELDS}
    if any(not isinstance(value, str) or not value.strip() for value in task.values()):
        raise ValueError("Task fields must be nonempty strings")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", task["instance_id"]):
        raise ValueError("Unsafe instance_id")
    return task


def validate_subset(subset):
    if subset.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported subset schema")
    tasks = [public_task(row) for row in subset["tasks"]]
    if not tasks or len({task["instance_id"] for task in tasks}) != len(tasks):
        raise ValueError("Subset must contain unique tasks")
    if subset.get("tasks_sha256") != digest(tasks):
        raise ValueError("Subset task fingerprint does not match its contents")
    return tasks


def validate_manifest(manifest):
    tasks = validate_subset(manifest)
    identity = {key: value for key, value in manifest.items()
                if key not in ("experiment_id", "schedule")}
    if manifest.get("experiment_id") != digest(identity)[:20]:
        raise ValueError("Manifest configuration fingerprint does not match")
    expected = {(task["instance_id"], arm) for task in tasks for arm in ARMS}
    schedule = manifest.get("schedule", [])
    actual = [(run["instance_id"], run["arm"]) for run in schedule]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Manifest schedule must contain exactly one run per task and arm")
    return tasks
