"""Treatment provenance and smoke gates, without model calls or Docker."""

from collections import Counter
from unittest.mock import Mock

import pytest

from benchmark.ab_support import digest
from benchmark.run_baseline import atomic_json, grade, result_record
from benchmark.run_lsp_benchmark import ARM, check_smoke, prepare


def test_treatment_grading_uses_distinct_arm_and_patch_bound_report(
    tmp_path, monkeypatch
):
    from benchmark import run_baseline

    commands = []
    monkeypatch.setattr(
        run_baseline, "command", lambda args, *a, **kw: commands.append(args)
    )
    monkeypatch.setattr(
        run_baseline, "read_resolution", lambda path, key: ARM in path.parts
    )
    resolved, provenance = grade(
        {"experiment_id": "treatment", "arm": ARM, "grader_image": "grader"},
        {"instance_id": "task"},
        {},
        b"patch",
        tmp_path,
    )
    assert resolved
    assert ARM in provenance["report"]
    prediction = (tmp_path / "grading/task/prediction.jsonl").read_text()
    assert ARM in prediction
    assert provenance["patch_sha256"]
    assert commands


def smoke_rows(tmp_path, *, queries=12, resolved=4, missing=0, complete=True):
    manifest = {
        "experiment_id": "test",
        "smoke_instance_ids": [f"task{i}" for i in range(5)],
    }
    for key in manifest["smoke_instance_ids"]:
        atomic_json(
            tmp_path / ARM / (key + ".json"),
            {
                "status": "completed",
                "usage_complete": complete,
            },
        )
        atomic_json(
            tmp_path / "agents" / key / "telemetry.json",
            {
                "lsp": {
                    "maps": [{"stats": {"queries": queries}}],
                    "totals": {"resolved_sites": resolved, "boosted_edges": resolved},
                    "bridge": {
                        "queries": queries,
                        "started_servers": 1,
                        "missing_servers": missing,
                    },
                    "unavailable_languages": ["python"] if missing else [],
                }
            },
        )
    return manifest


def test_smoke_requires_actual_semantic_evidence_not_only_an_enabled_flag(tmp_path):
    manifest = smoke_rows(tmp_path, resolved=0)
    with pytest.raises(ValueError, match="no verified"):
        check_smoke(manifest, tmp_path)
    assert not (tmp_path / "smoke_gate.json").exists()


@pytest.mark.parametrize(
    "options, message",
    [
        ({"missing": 1}, "language server failed"),
        ({"complete": False}, "incomplete grading or usage"),
    ],
)
def test_smoke_stops_on_unusable_servers_or_incomplete_usage(
    tmp_path, options, message
):
    manifest = smoke_rows(tmp_path, **options)
    with pytest.raises(ValueError, match=message):
        check_smoke(manifest, tmp_path)


def test_smoke_aggregates_all_five_tasks_and_preserves_gate(tmp_path):
    manifest = smoke_rows(tmp_path)
    gate = check_smoke(manifest, tmp_path)
    assert gate["totals"]["resolved_sites"] == 20
    assert gate["bridge"]["queries"] == 60
    assert len(gate["tasks"]) == 5
    assert (tmp_path / "smoke_gate.json").exists()


def test_optional_language_fallback_does_not_hide_a_working_python_server(tmp_path):
    from benchmark.ab_support import read_json

    manifest = smoke_rows(tmp_path)
    for key in manifest["smoke_instance_ids"]:
        path = tmp_path / "agents" / key / "telemetry.json"
        telemetry = read_json(path)
        telemetry["lsp"]["bridge"]["missing_servers"] = 1
        telemetry["lsp"]["started_languages"] = ["python"]
        telemetry["lsp"]["unavailable_languages"] = ["typescript"]
        atomic_json(path, telemetry)
    assert check_smoke(manifest, tmp_path)["status"] == "passed"


def test_prepare_rejects_different_subset_before_build_or_model_calls(
    tmp_path, monkeypatch
):
    from benchmark import run_lsp_benchmark as runner

    task = {
        "instance_id": "task",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "fix",
    }
    subset = {"schema_version": 1, "tasks": [task], "tasks_sha256": digest([task])}
    baseline = {"configuration": {"tasks_sha256": "different"}}
    inputs = {
        runner.BASELINE: baseline,
        runner.BUILD_RECORD: {},
        tmp_path / "subset.json": subset,
    }
    monkeypatch.setattr(runner, "read_json", lambda path: inputs[path])
    image_lookup = Mock()
    monkeypatch.setattr(runner, "image_id", image_lookup)
    with pytest.raises(ValueError, match="frozen baseline task"):
        prepare(tmp_path, tmp_path / "subset.json", tmp_path / "dataset")
    image_lookup.assert_not_called()


def test_treatment_record_retains_lsp_and_runtime_without_changing_control_kind():
    telemetry = {
        "attempt_turns": [],
        "context_at_turn_3": [],
        "failed_edit_count": 0,
        "peak_l1_tokens": 10,
        "peak_l2_tokens": 0,
        "lsp": {"totals": dict(Counter(resolved_sites=2))},
        "elapsed_seconds": 3.5,
    }
    row = result_record(
        {"experiment_id": "test", "arm": ARM, "features": {"enable_lsp_graph": True}},
        {"instance_id": "task", "repo": "owner/repo"},
        telemetry,
        [],
        [],
        False,
        {},
    )
    assert row["kind"] == "treatment_result"
    assert row["arm"] == ARM
    assert row["lsp"]["totals"]["resolved_sites"] == 2
    assert row["elapsed_seconds"] == 3.5


def comparison_fixture(tmp_path, monkeypatch):
    import hashlib

    from benchmark import compare_lsp as comparison

    tasks = [
        {"instance_id": f"task{i}", "repo": "owner/repo", "base_commit": "a" * 40}
        for i in range(50)
    ]
    baseline_path = tmp_path / "baseline.json"
    baseline = {
        "experiment_id": "control",
        "configuration": {"tasks_sha256": digest(tasks)},
        "summary": {"resolved": 0, "combined_tokens": 10000},
        "instances": [{**task, "resolved": False} for task in tasks],
    }
    atomic_json(baseline_path, baseline)
    output = tmp_path / "run"
    atomic_json(
        output / "run_manifest.json",
        {
            "experiment_id": "test",
            "model": "model",
            "tasks": tasks,
            "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        },
    )
    for task in tasks:
        key = task["instance_id"]
        patch = output / "agents" / key / "prediction.patch"
        patch.parent.mkdir(parents=True)
        patch.write_bytes(b"")
        atomic_json(
            output / ARM / (key + ".json"),
            {
                "instance_id": key,
                "repo": task["repo"],
                "experiment_id": "test",
                "status": "completed",
                "arm": ARM,
                "resolved": False,
                "usage_complete": True,
                "total_input_tokens": 10,
                "total_output_tokens": 1,
                "cached_input_tokens": 0,
                "total_turns": 1,
                "lsp": {"totals": {}, "bridge": {}},
                "grading": {
                    "status": "empty_prediction",
                    "report": None,
                    "patch_sha256": hashlib.sha256(b"").hexdigest(),
                },
            },
        )
    monkeypatch.setattr(comparison, "BASELINE", baseline_path)
    monkeypatch.setattr(comparison, "ROOT", tmp_path)
    return comparison, output


def test_comparison_records_complete_pairs_and_preserves_unknown_usage(
    tmp_path, monkeypatch
):
    from benchmark.ab_support import read_json

    comparison, output = comparison_fixture(tmp_path, monkeypatch)
    path = output / ARM / "task0.json"
    row = read_json(path)
    row["usage_complete"] = False
    row["total_input_tokens"] = None
    row["total_output_tokens"] = None
    atomic_json(path, row)
    atomic_json(
        output / "setup_usage.json",
        {"diagnostic_combined_tokens": 10340, "abandoned_request_tokens": None},
    )
    summary = comparison.compare(output)
    assert summary["usage_complete_tasks"] == 49
    assert summary["combined_tokens"] is None
    assert summary["empty_predictions"] == 50
    snapshot = read_json(tmp_path / "benchmark/experiments/lsp-graph-50.json")
    assert len(snapshot["instances"]) == 50
    assert "tasks" not in snapshot["configuration"]
    assert snapshot["excluded_setup_usage"]["diagnostic_combined_tokens"] == 10340
    assert "historical" in (output / "comparison_report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["missing", "prediction", "provenance"])
def test_comparison_refuses_incomplete_or_changed_predictions(
    tmp_path, monkeypatch, change
):
    from benchmark.ab_support import read_json

    comparison, output = comparison_fixture(tmp_path, monkeypatch)
    path = output / ARM / "task0.json"
    if change == "missing":
        path.unlink()
    elif change == "prediction":
        (output / "agents/task0/prediction.patch").write_bytes(b"different")
    else:
        row = read_json(path)
        row["experiment_id"] = "other"
        atomic_json(path, row)
    with pytest.raises((ValueError, FileNotFoundError)):
        comparison.compare(output)
    assert not (output / "comparison_report.md").exists()


def test_paging_comparison_records_a_separate_treatment_without_overwriting_lsp(
    tmp_path, monkeypatch
):
    from benchmark.ab_support import read_json
    from benchmark.run_pager_benchmark import ARM as pager_arm

    comparison, output = comparison_fixture(tmp_path, monkeypatch)
    manifest = read_json(output / "run_manifest.json")
    manifest.update(kind="paging_only_treatment", arm=pager_arm)
    atomic_json(output / "run_manifest.json", manifest)
    prior = tmp_path / "benchmark/experiments/lsp-graph-50.json"
    atomic_json(prior, {"experiment_id": "previous-lsp", "preserve": True})
    before = prior.read_bytes()
    for task in manifest["tasks"]:
        path = output / ARM / (task["instance_id"] + ".json")
        row = read_json(path)
        row["arm"] = pager_arm
        row.pop("lsp")
        row["pager"] = {
            "enabled": True,
            "stats": {"demotions": 1, "preloads": 2},
            "request_contexts": [{"l2_files": ["b.py"]}],
        }
        atomic_json(output / pager_arm / path.name, row)
        path.unlink()
    summary = comparison.compare(output)
    assert summary["pager_totals"]["demotions"] == 50
    assert summary["tasks_sent_l2_context"] == 50
    assert prior.read_bytes() == before
    assert (tmp_path / "benchmark/experiments/pager-idle-50.json").exists()
    report = (output / "comparison_report.md").read_text(encoding="utf-8")
    assert "Idle-only paging" in report and "LSP was disabled" in report
