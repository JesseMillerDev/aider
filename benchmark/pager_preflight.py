"""Offline image preflight: exercise the frozen pager without any model client."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aider.context_pager import ContextPager


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
        return {
            "status": "passed",
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


if __name__ == "__main__":
    print(json.dumps(run()))
