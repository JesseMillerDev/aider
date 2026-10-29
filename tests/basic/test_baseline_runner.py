"""No model calls: test replay, limits, metrics and honest baseline reporting."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aider.chatgpt_errors import ChatGPTError
from benchmark.ab_support import ARMS, read_json
from benchmark.run_baseline import (
    MODEL,
    atomic_json,
    grade,
    prepare_repo,
    progress_report,
    request_hash,
    result_record,
    service_request,
    successful_calls,
)


def request(ordinal=1):
    return {
        "ordinal": ordinal,
        "model": MODEL,
        "messages": [{"role": "user", "content": "Fix the issue"}],
        "functions": None,
        "extra_params": {"extra_body": {"reasoning_effort": "medium"}},
    }


def transport():
    fake = MagicMock()
    fake.send_completion.return_value = (
        None,
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="patch"))],
            response_id="resp_mock",
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                prompt_cache_hit_tokens=60,
                reasoning_tokens=5,
            ),
        ),
    )
    return fake


def test_completed_inference_is_replayed_without_consuming_more_usage(tmp_path):
    fake = transport()
    first = service_request(request(), tmp_path, fake, 25)
    assert service_request(request(), tmp_path, fake, 25) == first
    fake.send_completion.assert_called_once()
    assert len(successful_calls(tmp_path)) == 1


def test_replay_refuses_changed_prompt(tmp_path):
    fake = transport()
    service_request(request(), tmp_path, fake, 25)
    changed = request()
    changed["messages"][0]["content"] = "different issue"
    with pytest.raises(ValueError, match="differs"):
        service_request(changed, tmp_path, fake, 25)
    assert fake.send_completion.call_count == 1


@pytest.mark.parametrize("ordinal", [0, 26, True])
def test_call_limit_checked_before_inference(tmp_path, ordinal):
    fake = transport()
    reply = service_request(request(ordinal), tmp_path, fake, 25)
    assert reply["error"]["code"] == "turn_limit"
    fake.send_completion.assert_not_called()


def test_different_model_is_never_substituted(tmp_path):
    fake = transport()
    event = request()
    event["model"] = "another-model"
    with pytest.raises(ValueError, match="different model"):
        service_request(event, tmp_path, fake, 25)
    fake.send_completion.assert_not_called()


def test_plan_error_is_not_a_successful_or_zero_usage_model_completion(tmp_path):
    fake = transport()
    fake.send_completion.side_effect = ChatGPTError(
        "Plan usage disabled", code="not_eligible", status=403
    )
    reply = service_request(request(), tmp_path, fake, 25)
    assert reply["error"]["code"] == "not_eligible"
    assert successful_calls(tmp_path) == []
    assert read_json(tmp_path / "001.error.json")["status"] == 403
    # An error is not replayed as a completed response when the user resumes later.
    fake.send_completion.side_effect = None
    assert "error" not in service_request(request(), tmp_path, fake, 25)
    assert fake.send_completion.call_count == 2


def test_context_metadata_does_not_change_the_actual_model_request_identity():
    event = request()
    original = request_hash(event)
    event["context"] = ["file.py"]
    assert request_hash(event) == original


def test_unsafe_repository_and_commit_are_rejected_before_git(tmp_path):
    with pytest.raises(ValueError, match="Unsafe"):
        prepare_repo(
            {"repo": "owner/repo", "base_commit": "--upload-pack=evil"}, tmp_path
        )


def test_metrics_count_first_attempt_inclusively_and_do_not_double_count_cache(
    tmp_path,
):
    fake = transport()
    service_request(request(1), tmp_path, fake, 25)
    service_request(request(2), tmp_path, fake, 25)
    record = result_record(
        {"experiment_id": "test"},
        {"instance_id": "task", "repo": "owner/repo"},
        {
            "attempt_turns": [2],
            "failed_edit_count": 1,
            "context_at_turn_3": ["a.py"],
            "peak_l1_tokens": 1000,
            "peak_l2_tokens": 0,
        },
        successful_calls(tmp_path),
        ["a.py", "b.py"],
        True,
        {"engine": "swebench"},
    )
    assert record["total_turns"] == 2
    assert record["turns_to_first_edit"] == 2
    assert record["tokens_to_first_edit"] == 240
    assert record["total_input_tokens"] == 200
    assert record["cached_input_tokens"] == 120
    assert record["cache_hit_ratio"] == 0.6
    assert record["total_cost_usd"] is None
    assert record["localization_recall_at_turn_3"] == 0.5


def test_partial_usage_is_unavailable_not_zero(tmp_path):
    record = result_record(
        {"experiment_id": "test"},
        {"instance_id": "task", "repo": "owner/repo"},
        {
            "attempt_turns": [],
            "failed_edit_count": 0,
            "context_at_turn_3": [],
            "peak_l1_tokens": 0,
            "peak_l2_tokens": 0,
        },
        [],
        [],
        False,
        {},
        usage_complete=False,
    )
    assert record["total_input_tokens"] is None
    assert record["total_output_tokens"] is None
    assert record["turns_to_first_edit"] is None
    assert record["tokens_to_first_edit"] is None
    assert record["localization_recall_at_turn_3"] is None


def test_empty_patch_is_explicitly_ungraded_and_never_a_success(tmp_path):
    resolved, grading = grade(
        {"experiment_id": "test"}, {"instance_id": "task"}, {}, b"", tmp_path
    )
    assert resolved is False
    assert grading["status"] == "empty_prediction"
    assert grading["report"] is None


def test_pass_at_1_withheld_for_partial_or_infrastructure_failed_run(tmp_path):
    manifest = {
        "experiment_id": "test",
        "tasks": [{"instance_id": "a"}, {"instance_id": "b"}],
    }
    atomic_json(
        tmp_path / ARMS[0] / "a.json", {"status": "completed", "resolved": True}
    )
    summary = progress_report(manifest, tmp_path)
    assert summary["completed"] == 1 and summary["pass_at_1"] is None
    atomic_json(
        tmp_path / ARMS[0] / "b.json",
        {"status": "infrastructure_error", "resolved": None},
    )
    assert progress_report(manifest, tmp_path)["pass_at_1"] is None
    atomic_json(
        tmp_path / ARMS[0] / "b.json", {"status": "completed", "resolved": False}
    )
    assert progress_report(manifest, tmp_path)["pass_at_1"] == 0.5
