"""All-feature wiring, telemetry and explicit exploratory scaling; no inference."""

import hashlib
from types import SimpleNamespace

import pytest

from benchmark.ab_support import digest, read_json
from benchmark.agent_worker import NavigationTelemetry
from benchmark.run_baseline import atomic_json, result_record
from benchmark.run_combined_benchmark import ARM, FEATURES, SETTINGS, check_smoke


def test_navigation_observer_preserves_results_and_counts_attempts():
    bridge = object()

    class Navigator:
        def execute(self, actions, manual=False):
            return "unchanged result"

        def handle(self, content):
            if content == "ordinary":
                return None
            if content == "invalid":
                return "Navigation rejected: bad batch"
            return self.execute([("page_l1", ["a.py"])])

    nav = Navigator()
    nav.bridge = bridge
    coder = SimpleNamespace(
        agent_navigator=nav, repo_map=SimpleNamespace(semantic_graph=SimpleNamespace(bridge=bridge))
    )
    observer = NavigationTelemetry(coder, lambda: 3)
    assert nav.handle("normal") == "unchanged result"
    assert nav.handle("ordinary") is None
    assert nav.handle("invalid").startswith("Navigation rejected:")
    messages = [
        {
            "role": "system",
            "content": "You can navigate repository files before editing.\n```aider-nav\n...\n```",
        }
    ]
    assert observer.for_request(messages)["request_contains_instructions"]
    assert observer.snapshot()["model_requests_with_instructions"] == 0
    snapshot = observer.snapshot(messages, include_events=True)
    assert snapshot["action_attempts"] == {"page_l1": 1}
    assert snapshot["responses_with_navigation"] == 2
    assert snapshot["invalid_navigation_responses"] == 1
    assert snapshot["model_requests_with_instructions"] == 1
    assert snapshot["bridge_shared_with_ranking"]
    assert snapshot["events"] == [{"ordinal": 3, "actions": ["page_l1"], "manual": False}]
    assert NavigationTelemetry(SimpleNamespace(), lambda: 0).snapshot() is None
    summary = [
        {"role": "system", "content": "Summarize conversation"},
        {"role": "user", "content": messages[0]["content"]},
    ]
    assert not observer.for_request(summary)["request_contains_instructions"]
    assert observer.snapshot(summary)["model_requests_with_instructions"] == 1


def test_result_keeps_navigation_telemetry():
    navigation = {"enabled": True, "action_attempts": {"nav_def": 1}}
    row = result_record(
        {"experiment_id": "test", "arm": ARM, "features": FEATURES},
        {"instance_id": "task", "repo": "owner/repo"},
        {
            "attempt_turns": [],
            "failed_edit_count": 0,
            "context_at_turn_3": [],
            "peak_l1_tokens": 10,
            "peak_l2_tokens": 0,
            "navigation": navigation,
        },
        [],
        [],
        False,
        {},
    )
    assert row["navigation"] == navigation


def smoke_rows(tmp_path, monkeypatch):
    from benchmark import run_combined_benchmark as runner

    path = tmp_path / "baseline.json"
    monkeypatch.setattr(runner, "BASELINE", path)
    tasks = [{"instance_id": f"task{i}"} for i in range(5)]
    atomic_json(
        path,
        {
            "configuration": {"tasks_sha256": digest(tasks)},
            "instances": [
                {
                    "instance_id": task["instance_id"],
                    "resolved": False,
                    "total_input_tokens": 100,
                    "total_output_tokens": 10,
                }
                for task in reversed(tasks)
            ],
        },
    )
    manifest = {
        "experiment_id": "test",
        "features": FEATURES,
        "tasks": tasks,
        "smoke_instance_ids": [task["instance_id"] for task in tasks],
        "baseline_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "scaling_policy": "user_requested_full_50_exploratory",
    }
    for task in tasks:
        key = task["instance_id"]
        atomic_json(
            tmp_path / ARM / (key + ".json"),
            {
                "status": "completed",
                "resolved": False,
                "usage_complete": True,
                "total_input_tokens": 200,
                "total_output_tokens": 10,
                "total_turns": 2,
                "grading": {"engine": "swebench", "status": "empty_prediction"},
            },
        )
        atomic_json(
            tmp_path / "agents" / key / "telemetry.json",
            {
                "lsp": {
                    "maps": [{}],
                    "totals": {"resolved_sites": 1, "boosted_edges": 1},
                    "bridge": {"started_servers": 1, "queries": 2},
                    "errors": {},
                    "unavailable_languages": [],
                },
                "pager": {
                    "enabled": True,
                    "settings": SETTINGS,
                    "completed_responses": 2,
                    "stats": {"preloads": 0},
                    "events": [{}, {}],
                    "request_contexts": [{"l1_files": ["a.py"], "l2_files": []}] * 2,
                },
                "navigation": {
                    "enabled": True,
                    "model_requests_with_instructions": 2,
                    "bridge_shared_with_ranking": True,
                    "action_attempts": {},
                },
            },
        )
    return manifest


def test_explicit_combined_run_can_explore_higher_cost_without_changing_prior_gate(
    tmp_path, monkeypatch
):
    manifest = smoke_rows(tmp_path, monkeypatch)
    prior = tmp_path / "prior-demand-smoke" / "smoke_gate.json"
    atomic_json(prior, {"status": "blocked", "saved_tokens": -296})
    before = prior.read_bytes()
    gate = check_smoke(manifest, tmp_path)
    assert gate["status"] == "passed"
    assert gate["measurement"]["treatment_tokens"] > gate["measurement"]["control_tokens"]
    assert prior.read_bytes() == before


def test_combined_gate_counts_auxiliary_requests_without_claiming_context_exposure(
    tmp_path, monkeypatch
):
    manifest = smoke_rows(tmp_path, monkeypatch)
    key = manifest["smoke_instance_ids"][0]
    path = tmp_path / ARM / (key + ".json")
    row = read_json(path)
    row["total_turns"] = 3
    atomic_json(path, row)
    telemetry_path = tmp_path / "agents" / key / "telemetry.json"
    data = read_json(telemetry_path)
    data["pager"]["request_contexts"].append(
        {"l1_files": [], "l2_files": [], "auxiliary_request": True}
    )
    atomic_json(telemetry_path, data)
    assert check_smoke(manifest, tmp_path)["status"] == "passed"


@pytest.mark.parametrize(
    "problem",
    ["authorization", "flag", "nav", "protocol", "shared", "caps", "preload", "lsp", "steps"],
)
def test_combined_gate_rejects_missing_features_or_infrastructure(tmp_path, monkeypatch, problem):
    manifest = smoke_rows(tmp_path, monkeypatch)
    if problem == "authorization":
        manifest["scaling_policy"] = "ordinary"
    elif problem == "flag":
        manifest["features"] = {**FEATURES, "enable_agent_nav": False}
    else:
        key = manifest["smoke_instance_ids"][0]
        path = tmp_path / "agents" / key / "telemetry.json"
        data = read_json(path)
        if problem == "nav":
            data["navigation"] = None
        elif problem == "protocol":
            data["navigation"]["model_requests_with_instructions"] = 0
        elif problem == "shared":
            data["navigation"]["bridge_shared_with_ranking"] = False
        elif problem == "caps":
            data["pager"]["settings"]["max_l1_tokens"] = 16000
        elif problem == "preload":
            data["pager"]["stats"]["preloads"] = 1
        elif problem == "lsp":
            data["lsp"] = None
        else:
            data["pager"]["events"].pop()
        atomic_json(path, data)
    with pytest.raises(ValueError):
        check_smoke(manifest, tmp_path)
    assert not (tmp_path / "smoke_gate.json").exists()
