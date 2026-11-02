"""Provider-free reproductions of repository discovery and multi-step agent work."""

from unittest.mock import patch
from pathlib import Path

import git
import pytest

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = git.Repo.init(tmp_path)
    files = []
    for name in ["Battlefield.cs", "Battalion.cs"]:
        path = tmp_path / "Scripts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("public class " + path.stem + " {}\n", encoding="utf-8")
        files.append(path)
    repo.index.add([str(path) for path in files])
    repo.close()
    coder = Coder.create(
        Model("gpt-3.5-turbo"), "diff", io=InputOutput(pretty=False, fancy_input=False),
        map_tokens=0, auto_commits=False, auto_lint=False, stream=False,
        enable_agent_nav=True,
    )
    yield coder, files
    coder.agent_navigator.close()
    coder.repo.repo.close()


def answer_sequence(coder, replies):
    replies = iter(replies)

    def respond(*args, **kwargs):
        coder.partial_response_content = next(replies)
        coder.partial_response_function_call = {}
        return iter(())

    return respond


def test_model_request_to_attach_known_absolute_paths_loads_files_and_continues(workspace):
    coder, files = workspace
    request = "Please add these files to the chat so I can make the first changes:\n"
    request += "\n".join(" * " + str(path) for path in files)
    with (patch.object(coder, "send", side_effect=answer_sequence(coder, [request, "Inspected."]))
          as sent,
          patch.object(coder.io, "confirm_ask", side_effect=AssertionError("manual file load"))):
        coder.run_one("Build this game", preproc=False)
    assert coder.abs_fnames == {str(path) for path in files}
    assert sent.call_count == 2, "Agent stopped to ask the user instead of loading repository files"
    messages = str(sent.call_args.args[0])
    assert "public class Battlefield" in messages and "public class Battalion" in messages


def test_normal_tool_steps_can_continue_beyond_three_reflections(workspace):
    coder, _ = workspace
    steps = ['```aider-nav\n/list_files "Scripts/*.cs"\n```'] * 7 + ["Inspection complete."]
    with patch.object(coder, "send", side_effect=answer_sequence(coder, steps)) as sent:
        coder.run_one("Inspect the repository and continue until done", preproc=False)
    assert sent.call_count == len(steps), "Normal agent steps used the three-repair allowance"
    coder.run_one("/status", preproc=True)
    from aider.terminal_ui import session_status

    assert session_status(coder)["agent_steps"] == len(steps)


def test_navigation_loads_untracked_workspace_source_without_user_attachment(workspace):
    coder, _ = workspace
    path = Path(coder.root) / "Scripts" / "NewFeature.cs"
    path.write_text("public class NewFeature {}\n", encoding="utf-8")
    result = coder.agent_navigator.handle('```aider-nav\n/page_l1 Scripts/NewFeature.cs\n```')
    assert str(path) in coder.abs_fnames, result


def test_fallback_handles_spaced_paths_and_sentence_punctuation(workspace):
    coder, _ = workspace
    path = Path(coder.root) / "Game Scripts" / "Combat System.cs"
    path.parent.mkdir()
    path.write_text("public class CombatSystem {}\n", encoding="utf-8")
    with patch.object(coder.io, "confirm_ask", side_effect=AssertionError("manual attachment")):
        coder.preproc_user_input(f'Please inspect "{path}".')
    assert str(path) in coder.abs_fnames


def test_fallback_reports_ambiguity_instead_of_guessing_or_asking_to_attach(workspace):
    coder, files = workspace
    other = Path(coder.root) / "Other" / files[1].name
    other.parent.mkdir()
    other.write_text("public class OtherBattalion {}\n", encoding="utf-8")
    result = coder.check_for_file_mentions("Please add Battalion.cs.")
    assert "Ambiguous" in result and not coder.abs_fnames
    coder.check_for_file_mentions("Please add Scripts/Battalion.cs.")
    assert coder.abs_fnames == {str(files[1])}


def test_automatic_loading_preserves_readonly_declined_ignored_and_outside_rules(workspace):
    coder, files = workspace
    coder.abs_read_only_fnames.add(str(files[0]))
    coder.ignore_mentions.add("Scripts\\Battalion.cs")
    root = Path(coder.root)
    (root / ".gitignore").write_text("Scripts/Private.cs\n", encoding="utf-8")
    private = root / "Scripts" / "Private.cs"
    private.write_text("must not load", encoding="utf-8")
    outside = root.parent / "Outside.cs"
    outside.write_text("must not load", encoding="utf-8")
    with patch.object(coder.io, "confirm_ask", side_effect=AssertionError("attachment")):
        coder.check_for_file_mentions(
            "Please add these files:\n" + "\n".join(map(str, [*files, private, outside]))
        )
    assert not coder.abs_fnames and coder.abs_read_only_fnames == {str(files[0])}


def test_summary_mentions_do_not_reload_l2_but_a_request_for_contents_does(workspace):
    coder, files = workspace
    assert coder.check_for_file_mentions("Updated Scripts/Battlefield.cs.") is None
    assert not coder.abs_fnames
    coder.l2_interface_fnames.add(str(files[1]))
    assert coder.check_for_file_mentions("Updated Scripts/Battalion.cs.") is None
    assert str(files[1]) in coder.l2_interface_fnames and not coder.abs_fnames
    coder.check_for_file_mentions("Please add Scripts/Battalion.cs.")
    assert str(files[1]) in coder.abs_fnames and not coder.l2_interface_fnames


def test_explicit_agent_limit_stops_at_configured_steps_and_clones_keep_it(workspace):
    coder, files = workspace
    coder.max_agent_steps = 2
    steps = ['```aider-nav\n/list_files "Scripts/*.cs"\n```'] * 5
    with patch.object(coder, "send", side_effect=answer_sequence(coder, steps)) as sent:
        coder.run_one("Inspect", preproc=False)
    assert sent.call_count == 2 and coder.num_agent_steps == 2
    clone = coder.clone()
    try:
        assert clone.max_agent_steps == 2
    finally:
        clone.agent_navigator.close()
        clone.repo.repo.close()


def test_classic_mode_keeps_the_existing_repair_limit(workspace):
    coder, _ = workspace

    def retry(message):
        coder.reflected_message = "Repair still failed"
        return iter(())

    with (patch.object(coder, "agent_navigator", None),
          patch.object(coder, "send_message", side_effect=retry) as sent):
        coder.run_one("Repair", preproc=False)
    assert sent.call_count == 4 and coder.num_reflections == 3


def test_agent_limit_cli_defaults_uncapped_and_validates_positive_values():
    from aider.args import get_parser

    parser = get_parser([], None)
    assert parser.parse_args([]).max_agent_steps is None
    assert parser.parse_args(["--max-agent-steps", "8"]).max_agent_steps == 8
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-agent-steps", "0"])


def test_agent_limit_cli_reaches_the_coder(tmp_path, monkeypatch):
    from aider.main import main
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    monkeypatch.chdir(tmp_path)
    coder = main(
        ["--max-agent-steps", "5", "--model", "gpt-3.5-turbo", "--no-git",
         "--no-check-update", "--no-analytics", "--no-show-model-warnings"],
        input=DummyInput(), output=DummyOutput(), return_coder=True,
    )
    try:
        assert coder.agent_navigator is not None and coder.max_agent_steps == 5
    finally:
        coder.agent_navigator.close()
