"""Exercise all implemented context features together without model inference."""

import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

from benchmark.agent_worker import NavigationTelemetry
from benchmark.pager_preflight import run as pager_preflight


def run():
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model

    with tempfile.TemporaryDirectory(prefix="combined-preflight-") as directory:
        root = Path(directory)
        caller = root / "caller.py"
        api = root / "api.py"
        other = root / "other.py"
        caller.write_text(
            "from api import double\n\ndef run():\n    return double(3)\n", encoding="utf-8"
        )
        api.write_text(
            "def double(value: int) -> int:\n    payload = "
            + repr("implementation " * 500)
            + "\n    return value * 2\n",
            encoding="utf-8",
        )
        other.write_text("def double(value):\n    return value + 2\n", encoding="utf-8")
        original = {path: path.read_bytes() for path in (caller, api, other)}
        for args in (
            ["init", "-q"],
            ["config", "user.name", "Offline fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "."],
            ["-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"],
        ):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        requests = []

        def forbidden(*args, **kwargs):
            requests.append(True)
            raise AssertionError("Combined preflight must not request inference")

        coder = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                model = Model("chatgpt/gpt-6.1-sol", weak_model=False)
                model.weak_model = model
                model.send_completion = forbidden
                coder = Coder.create(
                    model,
                    "diff",
                    io=InputOutput(yes=True, pretty=False, chat_history_file=None),
                    fnames=[str(caller)],
                    use_git=True,
                    stream=False,
                    map_tokens=1024,
                    auto_commits=False,
                    auto_lint=False,
                    suggest_shell_commands=False,
                    enable_lsp_graph=True,
                    lsp_timeout=10.0,
                    enable_l2_paging=True,
                    enable_agent_nav=True,
                )
                graph = coder.repo_map.semantic_graph
                assert graph is not None and coder.agent_navigator is not None
                assert coder.context_pager.automatic
                assert coder.agent_navigator.bridge is graph.bridge
                assert graph.bridge.timeout == 10.0
                assert all(
                    getattr(coder.context_pager, key) is None
                    for key in ("max_l1_tokens", "max_l1_files", "max_l2_tokens")
                )
                tracker = NavigationTelemetry(coder, lambda: 0)
                messages = coder.format_messages().all_messages()
                assert tracker.snapshot(messages)["model_requests_with_instructions"] == 1
                assert not coder.l2_interface_fnames
                definition = coder.agent_navigator.handle("```aider-nav\n/nav_def caller.py:4\n```")
                assert "LSP:" in definition and "Loaded complete" in definition, definition
                assert str(api) in coder.abs_fnames
                demotion = coder.agent_navigator.handle("```aider-nav\n/demote_l2 api.py\n```")
                assert "Compressed" in demotion, demotion
                assert coder.l2_interface_fnames == {str(api)}
                assert coder.format_chat_chunks().interfaces
                references = coder.agent_navigator.handle("```aider-nav\n/nav_refs api.py:1\n```")
                assert "LSP:" in references and "caller.py:4:" in references, references
                loaded = coder.agent_navigator.handle("```aider-nav\n/page_l1 api.py\n```")
                assert "Loaded complete" in loaded and not coder.l2_interface_fnames
                assert graph.bridge.stats["started_servers"] > 0
                assert graph.bridge.stats["queries"] > 0
                assert original == {path: path.read_bytes() for path in original}
                assert not requests
                combined = {
                    "enabled": {"lsp_ranking": True, "navigation": True, "idle_paging": True},
                    "lsp_timeout": 10.0,
                    "shared_bridge": True,
                    "native_definition_lookup": True,
                    "native_reference_lookup": True,
                    "source_unchanged": True,
                    "inference_requests": len(requests),
                    "navigation": tracker.snapshot(include_events=True),
                    "lsp_bridge": dict(graph.bridge.stats),
                    "graph": dict(graph.stats),
                }
        finally:
            if coder is not None and coder.repo_map is not None:
                coder.repo_map.close()
    return {
        "status": "passed",
        "inference_requests": 0,
        "combined": combined,
        "paging": pager_preflight(),
    }


if __name__ == "__main__":
    print(json.dumps(run()))
