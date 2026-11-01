"""Run an assistant terminal action and return its actual result to the next request."""

import os
import re
import shutil
import subprocess
import tempfile
import time

import psutil


def _stop_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        psutil.wait_procs(children, timeout=3)
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=5)


def run_terminal(coder, command):
    timeout = 120.0
    option = re.match(r"--timeout=(\d+(?:\.\d+)?)\s+(.+)", command, re.S)
    if option:
        timeout, command = float(option[1]), option[2]
    if not command.strip() or not 0 < timeout <= 3600:
        raise ValueError("Use a command and a timeout between 0 and 3600 seconds.")
    if not coder.io.confirm_ask(
        "Run terminal command?", subject=command, explicit_yes_required=True, allow_never=True
    ):
        return "Terminal command declined; nothing was executed."
    coder.io.tool_output(f"Running in {coder.root}: {command}")
    started = time.monotonic()
    options = {}
    if os.name == "nt":
        script = ("$ErrorActionPreference='Stop'; "
                  "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                  "$global:LASTEXITCODE=$null; & { " + command + "\n}; "
                  "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")
        argv = [shutil.which("pwsh.exe") or "powershell.exe", "-NoProfile", "-NonInteractive",
                "-Command", script]
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        argv = command
        options.update(shell=True, executable=os.environ.get("SHELL", "/bin/sh"))
    timed_out = False
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            argv, cwd=coder.root, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, **options,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_tree(process)
        except BaseException:
            _stop_tree(process)
            raise
        size = output.tell()
        output.seek(0)
        if size > 24000:
            before = output.read(12000)
            output.seek(-12000, os.SEEK_END)
            data = before + b"\n[Terminal output shortened; showing first/last 12k bytes.]\n"
            data += output.read()
        else:
            data = output.read()
    text = data.decode("utf-8-sig", errors="replace").rstrip()
    elapsed = time.monotonic() - started
    status = f"Timed out after {timeout:g}s" if timed_out else f"Exit code: {process.returncode}"
    return f"Command: {command}\nWorking directory: {coder.root}\n{status} ({elapsed:.2f}s)\n{text}"
