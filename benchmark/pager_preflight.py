"""Offline image preflight: whole-prompt checks and idle transitions, no inference."""

import contextlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aider.context_pager import ContextPager


def check_prompt(root):
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model

    source = root / "entry.py"
    dependency = root / "dependency.py"
    source.write_text(
        "from dependency import target\n\ndef entry():\n    payload = "
        + repr("implementation " * 4000)
        + "\n    return target(payload)\n",
        encoding="utf-8",
    )
    dependency.write_text(
        '"""' + "dependency contract " * 12000 + '"""\ndef target(value): return value\n',
        encoding="utf-8",
    )
    original = {path: path.read_bytes() for path in (source, dependency)}
    requests = []

    def forbidden(*args, **kwargs):
        requests.append(True)
        raise AssertionError("Offline prompt checks must never request inference")

    # Model supplies local tokenization/metadata; its completion method is disabled.
    with contextlib.redirect_stdout(io.StringIO()):
        model = Model("chatgpt/gpt-6.1-sol", weak_model=False)
        model.weak_model = model
        model.send_completion = forbidden
        coder = Coder.create(
            model,
            "diff",
            io=InputOutput(yes=True, pretty=False, chat_history_file=None),
            fnames=[str(source)],
            use_git=False,
            stream=False,
            map_tokens=0,
            auto_commits=False,
            auto_lint=False,
            suggest_shell_commands=False,
            enable_l2_paging=True,
        )
        coder.get_all_abs_files = lambda: [str(source), str(dependency)]
        control = coder.clone(enable_l2_paging=False)
        baseline_messages = control.format_messages().all_messages()
        active_messages = coder.format_messages().all_messages()
        assert active_messages == baseline_messages
        assert not coder.l2_interface_fnames
        for _ in range(3):
            coder.context_pager.prepare(coder)
            coder.context_pager.step(coder)
        paged_messages = coder.format_messages().all_messages()
        before = model.token_count(baseline_messages)
        after = model.token_count(paged_messages)
    assert coder.l2_interface_fnames == {str(source)}
    assert str(dependency) not in coder.l2_interface_fnames
    assert before > after > 0
    assert not requests
    assert original == {path: path.read_bytes() for path in original}
    return {
        "unchanged_active_prompt": True,
        "baseline_tokens": before,
        "paged_tokens": after,
        "saved_tokens": before - after,
        "dependency_preloads": coder.context_pager.stats.get("preloads", 0),
        "inference_requests": len(requests),
        "measurement": "Local full-message token estimate; not provider usage or task resolution",
    }


def run():
    with tempfile.TemporaryDirectory(prefix="pager-preflight-") as directory:
        root = Path(directory)
        files = []
        for index in range(6):
            path = root / f"file{index}.py"
            path.write_text(
                f'def work{index}():\n    return "' + "word " * 4000 + '"\n',
                encoding="utf-8",
            )
            files.append(str(path))
        coder = SimpleNamespace(
            abs_fnames=set(files),
            l2_interface_fnames=set(),
            abs_read_only_fnames=set(),
            partial_response_content="",
            reflected_message=None,
            test_outcome=None,
            get_all_abs_files=lambda: files,
            io=SimpleNamespace(
                read_text=lambda path: Path(path).read_text(encoding="utf-8"),
                tool_warning=lambda text: None,
            ),
        )
        original = {path: Path(path).read_bytes() for path in files}
        pager = ContextPager(root, lambda text: len(text.split()))
        assert (pager.max_l1_files, pager.max_l1_tokens, pager.max_l2_tokens) == (
            None,
            None,
            None,
        )
        for _ in range(2):
            pager.prepare(coder)
            pager.step(coder)
            assert coder.abs_fnames == set(files)
        assert pager.stats["l1_tokens"] > 16000
        pager.prepare(coder)
        pager.step(coder)
        assert coder.l2_interface_fnames == set(files) and not coder.abs_fnames
        assert pager.stats["demotions"] == 6
        assert original == {path: Path(path).read_bytes() for path in files}
        proof = {
            "status": "passed",
            "context_policy": "demand",
            "transition_counter_unit": "whitespace word-count units; not model tokens",
            "inference_requests": 0,
            "stats": dict(pager.stats),
            "completed_responses": pager.turn,
            "source_unchanged": True,
            "settings": {
                "max_l1_tokens": None,
                "max_l1_files": None,
                "max_l2_tokens": None,
                "l1_ttl_turns": 2,
            },
        }
        proof["prompt_check"] = check_prompt(root)
        return proof


if __name__ == "__main__":
    print(json.dumps(run()))
