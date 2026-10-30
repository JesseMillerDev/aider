"""Paging experiment setup, gates and telemetry without external inference."""

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
    options = coder_feature_options(
        {"enable_l2_paging": True, "enable_lsp_graph": False}
    )
    assert options == {"enable_l2_paging": True, **SETTINGS}
    assert "enable_lsp_graph" not in options
    assert coder_feature_options({"enable_lsp_graph": True, "lsp_timeout": 10}) == {
        "enable_lsp_graph": True,
        "lsp_timeout": 10,
    }


def test_offline_fixture_demotes_after_idle_without_mutating_source():
    proof = preflight()
    assert proof["status"] == "passed"
    assert proof["inference_requests"] == 0 and proof["source_unchanged"]
    assert proof["stats"]["demotions"] == 6
    assert proof["stats"]["peak_l1_tokens"] > 16000
    assert proof["settings"] == SETTINGS


def smoke_rows(tmp_path):
    manifest = {
        "experiment_id": "test",
        "smoke_instance_ids": [f"task{i}" for i in range(5)],
    }
    for key in manifest["smoke_instance_ids"]:
        request = {"l1_files": ["a.py"], "l2_files": ["b.py"]}
        pager = {
            "enabled": True,
            "settings": SETTINGS,
            "completed_responses": 2,
            "stats": {"preloads": 1, "demotions": 0},
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
                "grading": {"engine": "swebench", "status": "empty_prediction"},
            },
        )
        atomic_json(
            tmp_path / "agents" / key / "telemetry.json", {"pager": pager, "lsp": None}
        )
    return manifest


def test_smoke_accepts_l2_exposure_without_selecting_for_score_or_demotions(tmp_path):
    manifest = smoke_rows(tmp_path)
    gate = check_smoke(manifest, tmp_path)
    assert gate["requests_with_l2"] == 10 and gate["totals"]["demotions"] == 0
    assert not any(task["resolved"] for task in gate["tasks"])


@pytest.mark.parametrize(
    "problem",
    ["cap", "lsp", "missing_requests", "missing_steps", "overlap", "no_l2", "usage"],
)
def test_smoke_stops_on_wrong_settings_or_incomplete_telemetry(tmp_path, problem):
    manifest = smoke_rows(tmp_path)
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
        elif problem == "no_l2":
            for request in data["pager"]["request_contexts"]:
                request["l2_files"] = []
        else:
            row_path = tmp_path / ARM / (key + ".json")
            row = read_json(row_path)
            row["usage_complete"] = False
            atomic_json(row_path, row)
        atomic_json(path, data)
    with pytest.raises(ValueError):
        check_smoke(manifest, tmp_path)
    assert not (tmp_path / "smoke_gate.json").exists()


def test_prepare_rejects_a_different_subset_before_docker_or_inference(
    tmp_path, monkeypatch
):
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
