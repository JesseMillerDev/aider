"""Context inspection must not change the agent, call a model, or query LSP."""

import copy
import io
import json
from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from rich.cells import cell_len
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from aider.args import get_parser
from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.terminal_ui import context_status, session_status, show_header, toolbar


@pytest.fixture
def coder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "sample.py"
    source.write_text("def sample(value):\n" + "    value += 1\n" * 100 + "    return value\n")
    instance = Coder.create(
        Model("gpt-3.5-turbo"), "diff", io=InputOutput(pretty=False),
        fnames=[str(source)], use_git=False, map_tokens=0, stream=False,
        auto_commits=False, enable_agent_nav=True, enable_l2_paging=True,
    )
    return instance


def test_status_inspects_l2_without_changing_context_or_making_requests(coder):
    pager = coder.context_pager
    pager.prepare(coder)
    path = next(iter(coder.abs_fnames))
    source = coder.io.read_text(path)
    pager.demote_file(coder, path)
    before = copy.deepcopy((pager.turn, pager.stats, pager.states, coder.abs_fnames,
                            coder.l2_interface_fnames, coder.cur_messages, coder.done_messages))
    with patch.object(coder.main_model, "send_completion", side_effect=AssertionError("inference")):
        status = context_status(coder)
    assert status["l1_files"] == 0 and status["l2_files"] == 1
    assert status["source_tokens"]["L2"] > 0
    assert status["files"][0]["state"] == "interface; implementation omitted"
    assert (pager.turn, pager.stats, pager.states, coder.abs_fnames,
            coder.l2_interface_fnames, coder.cur_messages, coder.done_messages) == before
    assert coder.io.read_text(path) == source
    assert "not provider usage or measured savings" in status["estimate_note"]
    coder.add_rel_fname("sample.py")
    assert pager.stats["full_file_reloads"] == 1


def test_status_command_exports_auditable_json_without_switching_mode(coder):
    with patch.object(coder.io, "tool_output") as output:
        assert coder.commands.cmd_status("json") is None
    status = json.loads(output.call_args.args[0])
    assert status["l1_files"] == 1
    assert status["lsp"]["maps"] == 0
    assert status["lsp"]["last_map_seconds"] is None
    assert coder.edit_format == "diff"
    assert coder.commands.completions_status() == ["json"]
    with patch.object(coder.io, "tool_error") as error:
        coder.commands.cmd_status("bad argument")
        error.assert_called_once_with("Usage: /status [json]")
    capture = io.StringIO()
    coder.io.console = Console(file=capture, width=30)
    coder.commands.cmd_status("json")
    assert json.loads(capture.getvalue())["l1_files"] == 1


def test_status_explains_protection_and_failed_cost_check(coder):
    pager = coder.context_pager
    pager.prepare(coder)
    path = next(iter(coder.abs_fnames))
    state = pager.states[path]
    state.pending = True
    assert "pending" in pager.file_status(path)
    state.pending = False
    state.edited = True
    assert "lint not confirmed clean" in pager.file_status(path)
    state.lint = "clean"
    pager.demotion_notes[path] = "replacement did not lower estimated message cost"
    assert "did not lower" in pager.file_status(path)
    pager.test_status = "failed"
    assert "test failure" in pager.file_status(path)


def test_l2_exposure_counts_completed_requests_not_failed_attempts(coder):
    pager = coder.context_pager
    pager.prepare(coder)
    pager.demote_file(coder, next(iter(coder.abs_fnames)))
    messages = coder.get_l2_interface_messages()
    coder.stream = False
    with (patch.object(coder, "show_send_output"),
          patch.object(coder, "calculate_and_show_tokens_and_cost"),
          patch.object(coder.main_model, "send_completion", return_value=(Mock(), Mock()))):
        list(coder.send(messages))
    assert pager.stats["completed_model_requests"] == 1
    assert pager.stats["completed_requests_with_l2"] == 1
    with patch.object(coder.main_model, "send_completion", side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError):
            list(coder.send(messages))
    assert pager.stats["completed_model_requests"] == 1
    assert pager.stats["completed_requests_with_l2"] == 1


@pytest.mark.parametrize("width", [0, 1, 20, 40, 65, 100, 160])
def test_toolbar_fits_terminal_and_keeps_untrusted_text_literal(coder, width):
    status = session_status(coder)
    status["branch"] = "feature/[red]literal[/red]"
    rendered = "".join(text for style, text in toolbar(status, width))
    assert cell_len(rendered) <= width
    assert "\x1b" not in rendered


def test_header_uses_bounded_layout_and_plain_mode_has_no_toolbar(coder):
    capture = io.StringIO()
    coder.io.console = Console(file=capture, width=65, force_terminal=True)
    show_header(coder.io, coder, coder.get_announcements())
    assert "Better Aider" in capture.getvalue() and "/status" in capture.getvalue()
    assert not coder.io.compact_ui
    coder.io.pretty = True
    coder.io.prompt_session = Mock()
    assert coder.io.compact_ui
    coder.io.terminal_ui = "classic"
    assert not coder.io.compact_ui
    parser = get_parser([], None)
    assert parser.parse_args([]).terminal_ui == "compact"
    assert parser.parse_args(["--terminal-ui", "classic"]).terminal_ui == "classic"


def test_compact_input_uses_real_prompt_session_and_toolbar(coder):
    with (create_pipe_input() as keyboard,
          patch("aider.io.is_dumb_terminal", return_value=False)):
        terminal = InputOutput(input=keyboard, output=DummyOutput(), fancy_input=True)
        terminal.pretty = True
        terminal.console = Console(file=io.StringIO(), width=80, force_terminal=True)
        coder.io = terminal
        coder.commands.io = terminal
        keyboard.send_text("/status\n")
        assert coder.get_input() == "/status"
        assert terminal.session_status["l1_files"] == 1
        assert terminal.get_bottom_toolbar()
        assert terminal.prompt_prefix.endswith("› ")
