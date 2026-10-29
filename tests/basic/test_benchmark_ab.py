"""Contract tests for reproducible, answer-free paired experiments."""

import copy
import json

import pytest

from benchmark.ab_support import ARMS, METRICS, digest, read_json, validate_manifest, write_json
from benchmark.analyze_results import load_pairs, mcnemar_p, report, wilcoxon_p
from benchmark.eval_harness_ab import build_plan, main as plan_main
from benchmark.grade_smoke import read_resolution, run_smoke
from benchmark.select_subset import build_subset, main as select_main, select_rows


def dataset():
    return [
        {
            "instance_id": f"{repo.replace('/', '__')}-{index}",
            "repo": repo,
            "base_commit": "a" * 40,
            "problem_statement": "The public issue text",
            "patch": f"+++ b/src/{index}.py\n+secret_gold_answer()",
            "test_patch": "secret_test_answer()",
            "FAIL_TO_PASS": "secret_test_names",
            "future_private_field": "secret_answer",
        }
        for repo in ("owner/one", "owner/two")
        for index in range(8)
    ]


@pytest.fixture
def manifest():
    subset, _ = build_subset(dataset(), 6, repos=("owner/one", "owner/two"))
    return build_plan(subset, "provider/exact-model-id", "a" * 40, "b" * 40)


@pytest.fixture
def results(tmp_path, manifest):
    for task in manifest["tasks"]:
        for arm in ARMS:
            row = {
                "schema_version": 1,
                "experiment_id": manifest["experiment_id"],
                "instance_id": task["instance_id"],
                "repo": task["repo"],
                "arm": arm,
                "status": "completed",
                "resolved": arm == ARMS[1],
                "grading": {"engine": "swebench", "run_id": f"{manifest['experiment_id']}-{arm}"},
                **{metric: 10 for metric in METRICS},
                "cached_input_tokens": 5,
                "localization_recall_at_turn_3": .4 if arm == ARMS[0] else .6,
                "tokens_to_first_edit": 100 if arm == ARMS[0] else 50,
                "failed_edit_count": 4 if arm == ARMS[0] else 2,
            }
            write_json(tmp_path / arm / f"{task['instance_id']}.json", row)
    return tmp_path


def mutate_first(results, mutate):
    path = next((results / ARMS[0]).glob("*.json"))
    row = read_json(path)
    mutate(row)
    write_json(path, row)


def test_selection_reproducible_balanced_and_order_independent():
    rows = dataset()
    repos = ("owner/one", "owner/two")
    selected = select_rows(rows, 7, 42, repos)
    assert selected == select_rows(list(reversed(rows)), 7, 42, tuple(reversed(repos)))
    assert selected != select_rows(rows, 7, 43, repos)
    counts = [sum(row["repo"] == repo for row in selected) for repo in repos]
    assert sorted(counts) == [3, 4]


def test_patch_and_test_metadata_never_reach_subset_or_plan(manifest):
    encoded = json.dumps(manifest)
    assert "secret" not in encoded
    assert "FAIL_TO_PASS" not in encoded
    rows = dataset()
    original = select_rows(rows, 6, repos=("owner/one", "owner/two"))
    changed = copy.deepcopy(rows)
    for row in changed:
        row["patch"] = "an entirely different patch"
    assert [row["instance_id"] for row in original] == [
        row["instance_id"] for row in select_rows(changed, 6, repos=("owner/one", "owner/two"))
    ]


@pytest.mark.parametrize("limit", [0, -1, 100])
def test_invalid_sample_size_rejected(limit):
    with pytest.raises(ValueError):
        select_rows(dataset(), limit, repos=("owner/one",))


def test_duplicates_and_absent_repos_rejected():
    rows = dataset()
    with pytest.raises(ValueError, match="Duplicate"):
        select_rows(rows + rows[:1], 2, repos=("owner/one",))
    with pytest.raises(ValueError, match="absent"):
        select_rows(rows, 2, repos=("unknown/repo",))


def test_local_cli_separates_oracle_labels(tmp_path):
    source, output, labels = [tmp_path / name for name in ("source.json", "subset.json", "labels.json")]
    write_json(source, dataset())
    select_main(["--input", str(source), "--output", str(output), "--labels-output", str(labels),
                 "--limit", "4", "--repos", "owner/one", "owner/two"])
    subset = read_json(output)
    oracle = read_json(labels)
    assert oracle["tasks_sha256"] == subset["tasks_sha256"]
    assert len(oracle["files"]) == 4
    assert "secret" not in output.read_text()
    assert all(files[0].startswith("src/") for files in oracle["files"].values())


def test_plan_pairs_every_task_and_keeps_control_stock(manifest):
    assert len(manifest["schedule"]) == 2 * len(manifest["tasks"])
    for task in manifest["tasks"]:
        runs = [run for run in manifest["schedule"] if run["instance_id"] == task["instance_id"]]
        assert {run["arm"] for run in runs} == set(ARMS)
        assert len({run["worktree"] for run in runs}) == 2
        assert len({run["grading_run_id"] for run in runs}) == 2
    assert manifest["arms"][ARMS[0]]["features"] == []
    assert manifest["arms"][ARMS[1]]["features_implemented"] is False
    assert manifest["status"] == "planned_only"


def test_manifest_identity_changes_with_model_budget_or_source(manifest):
    subset = {"schema_version": 1, "tasks": manifest["tasks"],
              "tasks_sha256": manifest["tasks_sha256"]}
    args = (subset, "model", "a", "b")
    original = build_plan(*args)
    assert original == build_plan(*args)
    assert original["experiment_id"] != build_plan(*args, max_turns=26)["experiment_id"]
    assert original["experiment_id"] != build_plan(*args, source_fingerprint="dirty")["experiment_id"]
    changed = copy.deepcopy(subset)
    changed["tasks"][0]["problem_statement"] = "tampered"
    with pytest.raises(ValueError, match="fingerprint"):
        build_plan(changed, "model", "a", "b")


def test_planner_cli_is_idempotent_and_protects_different_experiments(tmp_path, monkeypatch):
    subset, _ = build_subset(dataset(), 2, repos=("owner/one",))
    source, output = tmp_path / "subset.json", tmp_path / "manifest.json"
    write_json(source, subset)
    monkeypatch.setattr("benchmark.eval_harness_ab.git", lambda *a: b"a" * 40)
    monkeypatch.setattr("benchmark.eval_harness_ab.source_fingerprint", lambda: "source")
    args = ["--instances", str(source), "--model", "provider/model", "--output", str(output)]
    plan_main(args)
    original = output.read_bytes()
    plan_main(args)
    assert output.read_bytes() == original
    with pytest.raises(SystemExit) as error:
        plan_main([*args, "--model", "provider/changed-model"])
    assert error.value.code == 2
    assert output.read_bytes() == original


def test_unsafe_id_and_nonfinite_budget_rejected():
    rows = dataset()
    rows[0]["instance_id"] = "../escape"
    with pytest.raises(ValueError, match="Unsafe"):
        build_subset(rows, 1, repos=("owner/one",))
    subset, _ = build_subset(dataset(), 1, repos=("owner/one",))
    with pytest.raises(ValueError, match="budgets"):
        build_plan(subset, "model", "a", "b", max_cost_usd=float("nan"))


def test_analysis_and_engineering_thresholds(manifest, results):
    pairs = load_pairs(manifest, results)
    assert all(a["cache_hit_ratio"] == .5 for a, _ in pairs)
    text = report(manifest, pairs)
    assert text.count("**PASS**") == 3
    assert "+20.00 percentage points" in text
    assert mcnemar_p(pairs) == (.03125, 0, 6)
    assert wilcoxon_p([1, 1], [1, 1]) == "1"


def test_missing_result_is_not_scored_as_failure(manifest, results):
    next((results / ARMS[0]).glob("*.json")).unlink()
    with pytest.raises(ValueError, match="Missing 1"):
        load_pairs(manifest, results)


def test_unknown_manifest_schema_rejected(manifest, results):
    manifest["schema_version"] = 2
    with pytest.raises(ValueError, match="Unsupported"):
        load_pairs(manifest, results)


def test_tampered_configuration_rejected(manifest):
    manifest["model"] = "a-different-model"
    with pytest.raises(ValueError, match="configuration fingerprint"):
        validate_manifest(manifest)


def test_duplicate_scheduled_run_rejected(manifest):
    manifest["schedule"][0] = manifest["schedule"][1]
    with pytest.raises(ValueError, match="exactly one"):
        validate_manifest(manifest)


@pytest.mark.parametrize("update", [
    {"status": "infrastructure_error", "resolved": False},
    {"resolved": None},
    {"resolved": "false"},
    {"experiment_id": "another-experiment"},
    {"grading": {"engine": "fake"}},
    {"total_cost_usd": -1},
    {"total_turns": float("nan")},
    {"total_turns": 1.5},
    {"tokens_to_first_edit": True},
    {"localization_recall_at_turn_3": 1.1},
    {"cached_input_tokens": 11},
])
def test_invalid_results_rejected(manifest, results, update):
    mutate_first(results, lambda row: row.update(update))
    with pytest.raises(ValueError):
        load_pairs(manifest, results)


def test_unknown_instance_rejected(manifest, results):
    mutate_first(results, lambda row: row.update(instance_id="unknown"))
    with pytest.raises(ValueError, match="Unknown"):
        load_pairs(manifest, results)


def test_missing_metric_does_not_pass_hypothesis(manifest, results):
    mutate_first(results, lambda row: row.update(tokens_to_first_edit=None))
    text = report(manifest, load_pairs(manifest, results))
    assert "**NOT EVALUABLE**: Median tokens" in text


def test_zero_baseline_relative_reduction_is_undefined(manifest, results):
    for arm in ARMS:
        for path in (results / arm).glob("*.json"):
            row = read_json(path)
            row["failed_edit_count"] = 0
            write_json(path, row)
    text = report(manifest, load_pairs(manifest, results))
    assert "**NOT EVALUABLE**: Mean failed edits" in text


def test_manifest_tasks_are_fingerprinted(manifest):
    assert manifest["tasks_sha256"] == digest(manifest["tasks"])


def test_gold_runner_exports_only_one_evaluator_task_and_imports_real_report(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    output = tmp_path / "grading"
    write_json(source, dataset())
    instance_id = dataset()[0]["instance_id"]
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        if args[:2] == ["docker", "run"]:
            run_id = args[args.index("--run_id") + 1]
            path = output / "logs/run_evaluation" / run_id / "gold" / instance_id / "report.json"
            write_json(path, {instance_id: {"resolved": True}})

    monkeypatch.setattr("benchmark.grade_smoke.subprocess.run", fake_run)
    monkeypatch.setattr("benchmark.grade_smoke.subprocess.check_output", lambda *a, **k: "sha256:image")
    summary = run_smoke(source, instance_id, output)
    assert summary["status"] == "completed"
    assert summary["resolved"] is True
    assert summary["kind"] == "gold_infrastructure_smoke"
    exported = read_json(output / "dataset.json")
    assert len(exported) == 1
    assert exported[0]["patch"] == dataset()[0]["patch"]
    assert commands[-1][commands[-1].index("--predictions_path") + 1] == "gold"


def test_grader_exit_zero_without_report_is_infrastructure_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    write_json(source, dataset())
    monkeypatch.setattr("benchmark.grade_smoke.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("benchmark.grade_smoke.subprocess.check_output", lambda *a, **k: "image")
    summary = run_smoke(source, dataset()[0]["instance_id"], tmp_path / "grading", build=False)
    assert summary["status"] == "infrastructure_error"
    assert summary["resolved"] is None


def test_gold_runner_refuses_to_reuse_output_directory(tmp_path):
    source = tmp_path / "source.json"
    write_json(source, dataset())
    with pytest.raises(ValueError, match="fresh output"):
        run_smoke(source, dataset()[0]["instance_id"], tmp_path)


def test_grader_report_requires_boolean_resolution(tmp_path):
    path = tmp_path / "report.json"
    write_json(path, {"instance": {"resolved": "false"}})
    with pytest.raises(ValueError, match="boolean"):
        read_resolution(path, "instance")
