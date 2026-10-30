"""Paging experiment setup, gates and telemetry without external inference."""

import hashlib
from unittest.mock import Mock

import pytest

from benchmark.ab_support import digest, read_json
from benchmark.agent_worker import coder_feature_options
from benchmark.pager_preflight import run as preflight
from benchmark.run_baseline import atomic_json, result_record
from benchmark.run_pager_benchmark import ARM, SETTINGS, check_smoke, prepare


def test_worker_passes_idle_only_defaults_and_preserves_stock_call_shape():
    assert coder_feature_options({}) == {}
    assert coder_feature_options({"enable_lsp_graph": False}) == {}
    options = coder_feature_options({"enable_l2_paging": True, "enable_lsp_graph": False})
    assert options == {"enable_l2_paging": True, **SETTINGS}
    assert "enable_lsp_graph" not in options
    assert coder_feature_options({"enable_lsp_graph": True, "lsp_timeout": 10}) == {
        "enable_lsp_graph": True,
        "lsp_timeout": 10,
    }


def test_worker_enables_navigation_with_paging_and_semantic_ranking():
    options = coder_feature_options(
        {
            "enable_agent_nav": True,
            "enable_l2_paging": True,
            "enable_lsp_graph": True,
            "lsp_timeout": 10,
        }
    )
    assert options == {
        "enable_agent_nav": True,
        "enable_l2_paging": True,
        "enable_lsp_graph": True,
        "lsp_timeout": 10,
        **SETTINGS,
    }


def test_offline_fixture_demotes_after_idle_without_mutating_source():
    proof = preflight()
    assert proof["status"] == "passed"
    assert proof["inference_requests"] == 0 and proof["source_unchanged"]
    assert proof["stats"]["demotions"] == 6
    assert proof["stats"]["peak_l1_tokens"] > 16000
    assert proof["settings"] == SETTINGS
    assert proof["context_policy"] == "demand"
    assert proof["prompt_check"]["unchanged_active_prompt"]
    assert proof["prompt_check"]["saved_tokens"] > 0
    assert proof["prompt_check"]["dependency_preloads"] == 0


def smoke_rows(tmp_path, monkeypatch):
    from benchmark import run_pager_benchmark as runner

    baseline_path = tmp_path / "baseline.json"
    monkeypatch.setattr(runner, "BASELINE", baseline_path)
    manifest = {
        "experiment_id": "test",
        "smoke_instance_ids": [f"task{i}" for i in range(5)],
        "tasks": [{"instance_id": f"task{i}"} for i in range(5)],
    }
    atomic_json(
        baseline_path,
        {
            "configuration": {"tasks_sha256": digest(manifest["tasks"])},
            "instances": [
                {
                    "instance_id": key,
                    "resolved": False,
                    "usage_complete": True,
                    "total_input_tokens": 1200,
                    "total_output_tokens": 100,
                }
                for key in manifest["smoke_instance_ids"]
            ],
        },
    )
    manifest["baseline_sha256"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    for key in manifest["smoke_instance_ids"]:
        request = {"l1_files": ["a.py"], "l2_files": ["b.py"]}
        pager = {
            "enabled": True,
            "settings": SETTINGS,
            "completed_responses": 2,
            "stats": {"preloads": 0, "demotions": 0},
            "request_contexts": [request, request],
            "events": [{}, {}],
        }
        atomic_json(
            tmp_path / ARM / (key + ".json"),
            {
                "status": "completed",
                "resolved": False,
                "usage_complete": True,
                "total_turns": 2,
                "total_input_tokens": 1000,
                "total_output_tokens": 50,
                "grading": {"engine": "swebench", "status": "empty_prediction"},
            },
        )
        atomic_json(tmp_path / "agents" / key / "telemetry.json", {"pager": pager, "lsp": None})
    return manifest


def test_smoke_accepts_measured_savings_without_requiring_demotion(tmp_path, monkeypatch):
    manifest = smoke_rows(tmp_path, monkeypatch)
    gate = check_smoke(manifest, tmp_path)
    assert gate["requests_with_l2"] == 10 and gate["totals"]["demotions"] == 0
    assert not any(task["resolved"] for task in gate["tasks"])


def test_smoke_matches_baseline_by_id_independently_of_record_order(tmp_path, monkeypatch):
    manifest = smoke_rows(tmp_path, monkeypatch)
    path = tmp_path / "baseline.json"
    baseline = read_json(path)
    baseline["instances"].reverse()
    atomic_json(path, baseline)
    manifest["baseline_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    gate = check_smoke(manifest, tmp_path)
    assert gate["status"] == "passed"
    assert gate["measurement"]["control_tokens"] == 6500


@pytest.mark.parametrize("change", ["selected_ids", "frozen_order"])
def test_smoke_still_rejects_a_changed_task_selection(tmp_path, monkeypatch, change):
    manifest = smoke_rows(tmp_path, monkeypatch)
    manifest["smoke_instance_ids"].reverse()
    if change == "frozen_order":
        manifest["tasks"].reverse()
    with pytest.raises(ValueError, match="frozen"):
        check_smoke(manifest, tmp_path)


def test_smoke_allows_zero_l2_exposure_when_total_usage_is_lower(tmp_path, monkeypatch):
    manifest = smoke_rows(tmp_path, monkeypatch)
    for key in manifest["smoke_instance_ids"]:
        path = tmp_path / "agents" / key / "telemetry.json"
        data = read_json(path)
        for request in data["pager"]["request_contexts"]:
            request["l2_files"] = []
        atomic_json(path, data)
    assert check_smoke(manifest, tmp_path)["requests_with_l2"] == 0


@pytest.mark.parametrize("inputs", [1250, 1400])
def test_smoke_blocks_equal_or_higher_total_tokens(tmp_path, monkeypatch, inputs):
    manifest = smoke_rows(tmp_path, monkeypatch)
    for key in manifest["smoke_instance_ids"]:
        path = tmp_path / ARM / (key + ".json")
        row = read_json(path)
        row["total_input_tokens"] = inputs
        atomic_json(path, row)
    with pytest.raises(ValueError, match="token"):
        check_smoke(manifest, tmp_path)
    gate = read_json(tmp_path / "smoke_gate.json")
    assert gate["status"] == "blocked"


def test_smoke_blocks_resolution_loss_despite_token_savings(tmp_path, monkeypatch):
    manifest = smoke_rows(tmp_path, monkeypatch)
    path = tmp_path / "baseline.json"
    baseline = read_json(path)
    baseline["instances"][0]["resolved"] = True
    atomic_json(path, baseline)
    manifest["baseline_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="resolution"):
        check_smoke(manifest, tmp_path)
    assert read_json(tmp_path / "smoke_gate.json")["status"] == "blocked"


@pytest.mark.parametrize("smoke_only", [False, True])
def test_failed_efficiency_gate_never_launches_remaining_tasks(tmp_path, monkeypatch, smoke_only):
    from benchmark import run_pager_benchmark as runner

    manifest = smoke_rows(tmp_path, monkeypatch)
    for key in manifest["smoke_instance_ids"]:
        path = tmp_path / ARM / (key + ".json")
        row = read_json(path)
        row["total_input_tokens"] = 2000
        atomic_json(path, row)
    monkeypatch.setattr(runner, "prepare", lambda *args: manifest)
    run = Mock(return_value=Mock(returncode=0))
    launch = Mock()
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    args = ["--output", str(tmp_path)] + (["--smoke-only"] if smoke_only else [])
    assert runner.main(args) == 1
    launch.assert_not_called()
    assert run.call_count == 1 and "grade" in run.call_args.args[0]
    assert read_json(tmp_path / "run_status.json")["status"] == "blocked"


def test_smoke_only_stops_after_passing_efficiency_gate(tmp_path, monkeypatch):
    from benchmark import run_pager_benchmark as runner

    manifest = smoke_rows(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "prepare", lambda *args: manifest)
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=Mock(returncode=0)))
    launch = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    assert runner.main(["--output", str(tmp_path), "--smoke-only"]) == 0
    launch.assert_not_called()
    status = read_json(tmp_path / "run_status.json")
    assert status["status"] == "smoke_passed"
    assert status["measurement"]["saved_tokens"] > 0


@pytest.mark.parametrize(
    "problem",
    ["cap", "lsp", "missing_requests", "missing_steps", "overlap", "preloads", "usage", "tokens"],
)
def test_smoke_stops_on_wrong_settings_or_incomplete_telemetry(tmp_path, monkeypatch, problem):
    manifest = smoke_rows(tmp_path, monkeypatch)
    for key in manifest["smoke_instance_ids"]:
        path = tmp_path / "agents" / key / "telemetry.json"
        data = read_json(path)
        if problem == "cap":
            data["pager"]["settings"]["max_l1_tokens"] = 16000
        elif problem == "lsp":
            data["lsp"] = {"bridge": {}}
        elif problem == "missing_requests":
            data["pager"]["request_contexts"].pop()
        elif problem == "missing_steps":
            data["pager"]["events"].pop()
        elif problem == "overlap":
            data["pager"]["request_contexts"][0]["l2_files"] = ["a.py"]
        elif problem == "preloads":
            data["pager"]["stats"]["preloads"] = 1
        else:
            row_path = tmp_path / ARM / (key + ".json")
            row = read_json(row_path)
            if problem == "tokens":
                row["total_input_tokens"] = None
            else:
                row["usage_complete"] = False
            atomic_json(row_path, row)
        atomic_json(path, data)
    with pytest.raises(ValueError):
        check_smoke(manifest, tmp_path)
    assert not (tmp_path / "smoke_gate.json").exists()


def test_prepare_rejects_a_different_subset_before_docker_or_inference(tmp_path, monkeypatch):
    from benchmark import run_pager_benchmark as runner

    task = {
        "instance_id": "task",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "fix",
    }
    subset = {"schema_version": 1, "tasks": [task], "tasks_sha256": digest([task])}
    inputs = {
        runner.BASELINE: {"configuration": {"tasks_sha256": "different"}},
        runner.BUILD_RECORD: {},
        tmp_path / "subset.json": subset,
    }
    monkeypatch.setattr(runner, "read_json", lambda path: inputs[path])
    lookup = Mock()
    monkeypatch.setattr(runner, "image_id", lookup)
    with pytest.raises(ValueError, match="frozen baseline task"):
        prepare(tmp_path, tmp_path / "subset.json", tmp_path / "dataset")
    lookup.assert_not_called()


def test_result_keeps_measured_l2_peak_and_paging_counters():
    pager = {"enabled": True, "stats": {"demotions": 3}}
    row = result_record(
        {"experiment_id": "test", "arm": ARM, "features": {"enable_l2_paging": True}},
        {"instance_id": "task", "repo": "owner/repo"},
        {
            "attempt_turns": [],
            "failed_edit_count": 0,
            "context_at_turn_3": [],
            "peak_l1_tokens": 20000,
            "peak_l2_tokens": 3000,
            "pager": pager,
        },
        [],
        [],
        False,
        {},
    )
    assert row["pager"] == pager and row["peak_l2_tokens"] == 3000
    assert row["arm"] == ARM
