import pytest  # noqa: F401
import json
import os
import sys
from unittest.mock import patch

from aider.run_cmd import run_cmd


@pytest.mark.skipif(os.name != "nt", reason="Windows shell regression")
def test_windows_native_command_keeps_quoted_preset_as_one_argument(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    (tmp_path / "probe.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path('args.json').write_text(json.dumps(sys.argv[1:]))\n", encoding="utf-8",
    )
    with patch("aider.run_cmd.get_windows_parent_process_name", return_value="powershell.exe"):
        status, output = run_cmd(
            'python.exe probe.py --export-release "Windows Desktop" builds/game.exe',
            cwd=tmp_path, interactive=False,
        )
    assert status == 0, output
    assert json.loads((tmp_path / "args.json").read_text()) == [
        "--export-release", "Windows Desktop", "builds/game.exe"
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI executable regression")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_windows_export_waits_for_gui_executable_before_success_chain(tmp_path, exit_code):
    from pathlib import Path

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("pythonw.exe not installed")
    (tmp_path / "probe.py").write_text(
        "import time,sys\nfrom pathlib import Path\ntime.sleep(.3)\n"
        "Path('export-finished.txt').write_text('done')\n"
        f"sys.exit({exit_code})\n", encoding="utf-8",
    )
    command = (f'"{pythonw}" probe.py && '
               'if exist export-finished.txt (echo ready>launch-state.txt) '
               'else (echo too-early>launch-state.txt)')
    status, output = run_cmd(command, cwd=tmp_path, interactive=False)
    assert status == exit_code, output
    if exit_code == 0:
        assert (tmp_path / "launch-state.txt").read_text().strip() == "ready"
    else:
        assert not (tmp_path / "launch-state.txt").exists()


@pytest.fixture
def windows_export(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows shell regression")
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model

    monkeypatch.chdir(tmp_path)
    probe = tmp_path / "fake export.py"
    probe.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path('export-args.json').write_text(json.dumps(sys.argv[1:]))\n"
        "if sys.argv[sys.argv.index('--export-release')+1] != 'Windows Desktop':\n"
        "    sys.exit(41)\n"
        "Path(sys.argv[-1]).write_text('fake exported game')\n", encoding="utf-8",
    )
    executable = tmp_path / "fake godot.cmd"
    executable.write_text(f'@"{sys.executable}" "{probe}" %*\n', encoding="utf-8")
    io = InputOutput(pretty=False, fancy_input=False, yolo=True)
    coder = Coder.create(Model("gpt-3.5-turbo"), "diff", io=io, use_git=False,
                         map_tokens=0, auto_commits=False)
    return coder, tmp_path, executable


def test_windows_shell_fence_creates_directory_preserves_preset_and_launches_after_export(
    windows_export,
):
    coder, root, executable = windows_export
    commands = (
        'if not exist builds\\windows mkdir builds\\windows\n'
        f'call "{executable}" --headless --path . --export-release "Windows Desktop" '
        'builds/windows/Bannerhold.exe && echo launched>launched.txt'
    )
    with (patch("aider.run_cmd.get_windows_parent_process_name", return_value="powershell.exe"),
          patch("builtins.input", side_effect=AssertionError("YOLO confirmation"))):
        coder.handle_shell_commands(commands, None)
    assert (root / "builds/windows/Bannerhold.exe").is_file()
    args = json.loads((root / "export-args.json").read_text())
    assert args[args.index("--export-release") + 1] == "Windows Desktop"
    assert (root / "launched.txt").read_text().strip() == "launched"


def test_windows_shell_fence_stops_after_failed_setup(windows_export):
    coder, root, executable = windows_export
    failure = root / "failed setup.cmd"
    failure.write_text("@exit /b 7\n", encoding="utf-8")
    commands = (f'call "{failure}"\n'
                f'call "{executable}" --export-release "Windows Desktop" builds/game.exe')
    with patch("aider.run_cmd.get_windows_parent_process_name", return_value="cmd.exe"):
        coder.handle_shell_commands(commands, None)
    assert not (root / "export-args.json").exists(), "Export ran despite failed setup"


def test_windows_shell_failure_stops_later_blocks_and_returns_to_the_agent(windows_export):
    coder, root, executable = windows_export
    coder.init_before_message()
    failure = root / "failed setup.cmd"
    failure.write_text("@exit /b 7\n", encoding="utf-8")

    def respond(*args, **kwargs):
        coder.partial_response_content = "I will build and launch the game."
        coder.partial_response_function_call = {}
        coder.shell_commands = [
            f'call "{failure}"',
            f'call "{executable}" --export-release "Windows Desktop" builds/game.exe',
        ]
        if False:
            yield

    with (patch("aider.run_cmd.get_windows_parent_process_name", return_value="powershell.exe"),
          patch.object(coder, "send", side_effect=respond),
          patch.object(coder, "apply_updates", return_value=set())):
        list(coder.send_message("Build and launch the game"))
    assert not (root / "export-args.json").exists()
    assert "correct the failure" in coder.reflected_message
    assert "exit code 7" in str(coder.cur_messages)


def test_run_cmd_echo():
    command = "echo Hello, World!"
    exit_code, output = run_cmd(command)

    assert exit_code == 0
    assert output.strip() == "Hello, World!"
