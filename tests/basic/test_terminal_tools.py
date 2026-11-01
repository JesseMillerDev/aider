"""Actual local shell results, literal command parsing and explicit YOLO behavior."""

import os
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import psutil

from aider.agent_nav import parse_actions
from aider.io import InputOutput
from aider.terminal_tools import run_terminal


def python_command(script):
    if os.name == "nt":
        executable = sys.executable.replace("'", "''")
        # Avoid legacy Windows PowerShell's native-argument quote removal.
        program = "exec(bytes(" + repr(list(script.encode())) + "))"
        return "& '" + executable + "' -c '" + program + "'"
    return shlex.quote(sys.executable) + " -c " + shlex.quote(script)


@pytest.mark.parametrize("command", [
    (r"Start-Process -FilePath 'C:\Games\Godot\Godot.exe' "
     r"-ArgumentList '--editor','--path','C:\Game' "),
    r'''python -c "print('a\\nb')"''',
])
def test_terminal_action_preserves_quotes_backslashes_and_arguments(command):
    parsed = parse_actions("```aider-nav\n/run_command " + command + "\n```")
    assert parsed == [("run_command", [command.strip()])]


def test_terminal_returns_real_exit_output_and_workspace_without_reading_stdin(tmp_path):
    io = InputOutput(pretty=False, fancy_input=False, yolo=True)
    coder = SimpleNamespace(io=io, root=str(tmp_path))
    command = python_command(
        'import os,sys; print(os.getcwd()); print("hello"); '
        'print("error detail",file=sys.stderr); '
        'print("eof="+str(sys.stdin.read()=="")); sys.exit(7)'
    )
    with patch("builtins.input", side_effect=AssertionError("confirmation")):
        result = run_terminal(coder, command)
    assert "Exit code: 7" in result and "hello" in result and "error detail" in result
    assert "eof=True" in result and str(tmp_path) in result


@pytest.mark.skipif(os.name != "nt", reason="PowerShell native argument regression")
def test_powershell_terminal_preserves_spaced_native_export_preset(tmp_path):
    import json

    (tmp_path / "probe.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path('args.json').write_text(json.dumps(sys.argv[1:]))\n", encoding="utf-8",
    )
    coder = SimpleNamespace(
        io=InputOutput(pretty=False, fancy_input=False, yolo=True), root=str(tmp_path)
    )
    command = ("& '" + sys.executable.replace("'", "''")
               + "' probe.py --export-release \"Windows Desktop\" builds/game.exe")
    result = run_terminal(coder, command)
    assert "Exit code: 0" in result
    assert json.loads((tmp_path / "args.json").read_text()) == [
        "--export-release", "Windows Desktop", "builds/game.exe"
    ]


def test_terminal_requires_explicit_confirmation_without_yolo_and_does_not_execute(tmp_path):
    io = InputOutput(pretty=False, fancy_input=False, yes=True)
    coder = SimpleNamespace(io=io, root=str(tmp_path))
    with patch("aider.terminal_tools.subprocess.Popen") as process:
        result = run_terminal(coder, "echo should-not-run")
    process.assert_not_called()
    assert "declined" in result


def test_terminal_timeout_and_output_shortening_are_reported(tmp_path):
    coder = SimpleNamespace(
        io=InputOutput(pretty=False, fancy_input=False, yolo=True), root=str(tmp_path)
    )
    marker = tmp_path / "child.pid"
    script = ("import os,time; open(" + repr(str(marker))
              + ",'w').write(str(os.getpid())); time.sleep(30)")
    result = run_terminal(coder, "--timeout=3 " + python_command(script))
    assert "Timed out after 3s" in result
    assert marker.exists() and not psutil.pid_exists(int(marker.read_text()))
    result = run_terminal(coder, python_command('print("a"*30000+"END")'))
    assert "Terminal output shortened" in result and result.endswith("END")


@pytest.mark.parametrize("yolo,yes", [(True, None), (True, False), (False, True)])
def test_yolo_approves_explicit_shell_confirmation_while_yes_always_stays_restricted(yolo, yes):
    io = InputOutput(pretty=False, fancy_input=False, yolo=yolo, yes=yes)
    with patch("builtins.input", side_effect=AssertionError("confirmation")):
        assert io.confirm_ask("Run command?", explicit_yes_required=True) is yolo
        assert io.confirm_ask("Add file?") is True


def test_yolo_uses_question_defaults_without_reading_input():
    io = InputOutput(pretty=False, fancy_input=False, yolo=True)
    with patch("builtins.input", side_effect=AssertionError("question")):
        assert io.prompt_ask("Select a branch", default="main") == "main"


def test_yolo_cli_flag_reaches_io_and_can_be_disabled(tmp_path, monkeypatch):
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    from aider.main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIDER_CHECK_UPDATE", "false")
    monkeypatch.setenv("AIDER_ANALYTICS", "false")
    for flag, expected in [("--yolo", True), ("--no-yolo", False)]:
        coder = main(
            [flag, "--no-git", "--no-check-update", "--no-analytics", "--no-show-model-warnings",
             "--model", "gpt-3.5-turbo"],
            input=DummyInput(), output=DummyOutput(), return_coder=True,
        )
        assert coder.io.yolo is expected


def test_legacy_command_runner_does_not_take_terminal_input_during_agent_work():
    from aider.run_cmd import run_cmd

    with (patch("aider.run_cmd.sys.stdin.isatty", return_value=True),
          patch("aider.run_cmd.platform.system", return_value="Linux"),
          patch("aider.run_cmd.run_cmd_pexpect") as foreground,
          patch("aider.run_cmd.run_cmd_subprocess", return_value=(0, "ok")) as captured):
        assert run_cmd("echo ok", interactive=False) == (0, "ok")
    foreground.assert_not_called()
    captured.assert_called_once_with("echo ok", False, None, interactive=False)


def test_terminal_action_returns_its_output_through_the_agent_protocol(tmp_path, monkeypatch):
    from aider.coders import Coder
    from aider.models import Model

    monkeypatch.chdir(tmp_path)
    io = InputOutput(pretty=False, fancy_input=False, yolo=True)
    coder = Coder.create(Model("gpt-3.5-turbo"), "diff", io=io, use_git=False,
                         map_tokens=0, auto_commits=False, enable_agent_nav=True)
    command = python_command('print("terminal-action-ok")')
    messages = coder.format_messages().all_messages()
    instructions = "\n".join(message["content"] for message in messages
                             if isinstance(message.get("content"), str))
    assert "/run_command" in instructions and "YOLO mode is active" in instructions
    result = coder.agent_navigator.handle("```aider-nav\n/run_command " + command + "\n```")
    assert "terminal-action-ok" in result and "Exit code: 0" in result
    assert coder.agent_navigator.stats["succeeded"] == 1
