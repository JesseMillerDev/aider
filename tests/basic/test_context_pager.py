"""Whole-file paging contracts; no provider inference calls."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aider.args import get_parser
from aider.coders import Coder
from aider.coders.chat_chunks import ChatChunks
from aider.context_pager import ContextPager
from aider.io import InputOutput
from aider.models import Model


class FilesIO:
    def __init__(self):
        self.warnings = []

    def read_text(self, path):
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def tool_warning(self, message):
        self.warnings.append(message)


def working_source(name="a"):
    return f"def {name}():\n    implementation = " + repr("word " * 200) + "\n    return 1\n"


def setup(tmp_path, files=None, active=None, **options):
    files = files or {"a.py": working_source()}
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    paths = {name: str((tmp_path / name).resolve()) for name in files}
    coder = SimpleNamespace(
        root=str(tmp_path),
        io=FilesIO(),
        abs_fnames={paths[name] for name in active or list(files)},
        abs_read_only_fnames=set(),
        l2_interface_fnames=set(),
        partial_response_content="",
        reflected_message=None,
        test_outcome=None,
        get_all_abs_files=lambda: list(paths.values()),
    )
    pager = ContextPager(tmp_path, lambda text: len(text.split()), **options)
    return pager, coder, paths


def turn(pager, coder, message="Continue"):
    pager.prepare(coder, message)
    pager.step(coder)


def test_python_interfaces_preserve_contracts_and_elide_implementations(tmp_path):
    source = '''"""Module contract."""
from typing import overload, TYPE_CHECKING
LIMIT = 42
if TYPE_CHECKING:
    from pkg import SomeType

class Derived(Base):
    """Class contract."""
    count: int = 0
    @classmethod
    async def work(cls, x: "SomeType", /, *, flag: bool = True) -> list[int]:
        """Method contract."""
        import os
        secret_implementation = x + 17
        return [secret_implementation]

def simple(a: int) -> int: return a * 1234
'''
    pager, _, paths = setup(tmp_path, {"a.py": source})
    stub = pager.render_l2_stub(paths["a.py"])
    tree = ast.parse(stub)
    assert ast.get_docstring(tree) == "Module contract."
    assert "LIMIT = 42" in stub and "from pkg import SomeType" in stub
    assert "Derived(Base)" in stub and "count: int" in stub
    assert "@classmethod" in stub and "async def work" in stub
    assert "flag: bool=True" in stub and "-> list[int]" in stub
    assert "Method contract." in stub and "import os" in stub
    assert "secret_implementation" not in stub and "1234" not in stub
    assert "⋮ implementation omitted" in stub


def test_idle_inspected_file_demotes_while_recently_edited_file_stays(tmp_path):
    pager, coder, paths = setup(
        tmp_path,
        {
            "a.py": working_source(),
            "b.py": "def b():\n    return 1\n",
        },
        active=["a.py"],
    )
    turn(pager, coder)
    coder.abs_fnames.add(paths["b.py"])
    for value in (2, 3):
        pager.prepare(coder, "b.py")
        Path(paths["b.py"]).write_text(f"def b():\n    return {value}\n", encoding="utf-8")
        pager.record_lint(coder, paths["b.py"], None)
        pager.step(coder)
    assert paths["a.py"] in coder.l2_interface_fnames
    assert paths["a.py"] not in coder.abs_fnames
    assert paths["b.py"] in coder.abs_fnames
    assert pager.states[paths["b.py"]].last_edited_turn == 3
    assert pager.turn == 3


@pytest.mark.parametrize("protection", ["lint", "unknown_edit", "test", "pending"])
def test_unresolved_or_unchecked_edits_are_never_automatically_evicted(tmp_path, protection):
    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    pager.prepare(coder)
    if protection == "lint":
        pager.record_lint(coder, paths["a.py"], "undefined name")
    elif protection == "test":
        pager.record_test(coder, False)
    elif protection == "pending":
        coder.reflected_message = "Regenerate the edit"
    else:
        Path(paths["a.py"]).write_text("def a():\n    return 2\n", encoding="utf-8")
    for _ in range(4):
        turn(pager, coder)
    assert paths["a.py"] in coder.abs_fnames
    assert not coder.l2_interface_fnames


def test_failed_test_survives_turn_reset_and_unknown_retest(tmp_path):
    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    turn(pager, coder)
    pager.record_test(coder, False)
    pager.record_test(coder, None)
    turn(pager, coder)
    assert paths["a.py"] in coder.abs_fnames
    pager.record_test(coder, True)
    turn(pager, coder)
    assert paths["a.py"] in coder.l2_interface_fnames


def test_current_file_references_reset_idle_clock(tmp_path):
    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    for _ in range(4):
        turn(pager, coder, "Inspect `a.py` again")
    assert paths["a.py"] in coder.abs_fnames
    assert pager.states[paths["a.py"]].last_referenced_turn == 4


def test_file_count_budget_demotes_only_whole_files(tmp_path):
    pager, coder, paths = setup(
        tmp_path,
        {f"{name}.py": working_source(name) for name in ("a", "b", "c")},
        max_l1_files=1,
    )
    before = {path: Path(path).read_bytes() for path in paths.values()}
    pager.prepare(coder, "Work on c.py")
    assert coder.abs_fnames == {paths["c.py"]}
    assert coder.l2_interface_fnames == {paths["a.py"], paths["b.py"]}
    assert before == {path: Path(path).read_bytes() for path in paths.values()}


def test_l2_budget_counts_exact_rendered_block_and_never_truncates_contract(tmp_path):
    files = {
        f"{name}.py": f"def {name}(x: int) -> int:\n    return x\n" for name in ("a", "b", "c")
    }
    pager, coder, _ = setup(tmp_path, files, max_l1_files=1, max_l2_tokens=60)
    pager.prepare(coder)
    content = pager.interface_content(coder)
    assert len(content.split()) <= 60
    assert len(coder.abs_fnames) >= 1
    for path in coder.l2_interface_fnames:
        assert pager.render_l2_stub(path).strip() in content


def test_protected_oversized_file_reports_overflow_instead_of_slicing(tmp_path):
    pager, coder, paths = setup(tmp_path, max_l1_tokens=2)
    pager.prepare(coder, "Edit a.py")
    assert coder.abs_fnames == {paths["a.py"]}
    assert pager.stats["budget_overflows"] == 1
    assert coder.io.warnings
    pager.prepare(coder)
    assert pager.stats["budget_overflows"] == 1


def test_imports_never_preload_or_scan_repository_dependencies(tmp_path):
    pager, coder, paths = setup(
        tmp_path,
        {
            "src/pkg/main.py": "from . import dep\nimport requests\ndef main(): return dep.run()\n",
            "src/pkg/dep.py": "from . import transitive\ndef run(): return transitive.run()\n",
            "src/pkg/transitive.py": "def run(): return 3\n",
        },
        active=["src/pkg/main.py"],
    )
    with patch.object(coder, "get_all_abs_files", side_effect=AssertionError("no bulk scan")):
        pager.prepare(coder)
    assert not coder.l2_interface_fnames
    assert coder.abs_fnames == {paths["src/pkg/main.py"]}


def test_source_hash_invalidates_stub_with_preserved_mtime(tmp_path):
    import os

    pager, _, paths = setup(tmp_path)
    path = Path(paths["a.py"])
    before = path.stat()
    assert "def a" in pager.render_l2_stub(path)
    path.write_text("def replacement(value: str) -> str:\n    return value\n", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert "def replacement" in pager.render_l2_stub(path)
    assert "def a(" not in pager.render_l2_stub(path)


def test_external_edit_to_demoted_file_promotes_complete_source(tmp_path):
    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    turn(pager, coder)
    turn(pager, coder)
    assert paths["a.py"] in coder.l2_interface_fnames
    Path(paths["a.py"]).write_text("def a():\n    return 'external'\n", encoding="utf-8")
    pager.prepare(coder)
    assert paths["a.py"] in coder.abs_fnames
    assert paths["a.py"] not in coder.l2_interface_fnames
    assert pager.states[paths["a.py"]].lint == "unknown"


def test_explicit_drop_is_remembered_until_an_explicit_load(tmp_path):
    pager, coder, paths = setup(
        tmp_path,
        {
            "a.py": "import b\ndef a(): return b.run()\n",
            "b.py": working_source("run"),
        },
        active=["a.py", "b.py"],
    )
    pager.prepare(coder)
    pager.demote_file(coder, paths["b.py"])
    assert paths["b.py"] in coder.l2_interface_fnames
    pager.forget(coder, paths["b.py"])
    pager.prepare(coder)
    assert not coder.l2_interface_fnames
    coder.abs_fnames.add(paths["b.py"])
    pager.prepare(coder)
    assert paths["b.py"] not in pager.forgotten


def test_malformed_and_unsupported_files_remain_full(tmp_path):
    pager, coder, paths = setup(
        tmp_path,
        {"a.py": "def broken(:\n", "notes.txt": "important text"},
        max_l1_files=1,
        l1_ttl_turns=1,
    )
    for _ in range(3):
        turn(pager, coder)
    assert coder.abs_fnames == set(paths.values())
    assert not coder.l2_interface_fnames


def test_interface_order_and_cache_boundary_preserve_static_prefix():
    chunks = ChatChunks(
        system=[{"role": "system", "content": "system"}],
        repo=[{"role": "user", "content": "map"}],
        interfaces=[{"role": "user", "content": "interfaces"}],
        done=[{"role": "user", "content": "old conversation"}],
        chat_files=[{"role": "user", "content": "full source"}],
        cur=[{"role": "user", "content": "current request"}],
    )
    assert [row["content"] for row in chunks.all_messages()] == [
        "system",
        "map",
        "interfaces",
        "old conversation",
        "full source",
        "current request",
    ]
    chunks.add_cache_control_headers()
    assert chunks.interfaces[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cli_paging_defaults_on_and_limits_are_positive(tmp_path):
    parser = get_parser([], str(tmp_path))
    args = parser.parse_args([])
    assert args.enable_l2_paging is True
    assert (
        args.max_l1_tokens,
        args.max_l1_files,
        args.max_l2_tokens,
        args.l1_ttl_turns,
    ) == (None, None, None, 2)
    assert (
        parser.parse_args(["--enable-l2-paging", "--no-enable-l2-paging"]).enable_l2_paging is False
    )
    for flag in (
        "--max-l1-tokens",
        "--max-l1-files",
        "--max-l2-tokens",
        "--l1-ttl-turns",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args([flag, "0"])


def real_coder(tmp_path, source_text=None, **kwargs):
    source = tmp_path / "a.py"
    source.write_text(source_text or working_source(), encoding="utf-8")
    model = Model("gpt-3.5-turbo")
    return Coder.create(
        model,
        "diff",
        io=InputOutput(yes=True, pretty=False),
        fnames=[str(source)],
        use_git=False,
        stream=False,
        auto_commits=False,
        auto_lint=False,
        map_tokens=0,
        **kwargs,
    )


def test_real_coder_promotes_l2_and_rejects_the_entire_stale_edit_batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True, l1_ttl_turns=1)
    pager = coder.context_pager
    turn(pager, coder)
    turn(pager, coder)
    source = tmp_path / "a.py"
    assert str(source) in coder.l2_interface_fnames
    assert coder.prepare_to_edit([("a.py", "return 1", "return 2")]) == []
    assert str(source) in coder.abs_fnames
    assert str(source) not in coder.l2_interface_fnames
    assert coder.reflected_message
    assert source.read_text(encoding="utf-8") == working_source()


def test_real_coder_clone_preserves_independent_pager_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True, l1_ttl_turns=1)
    turn(coder.context_pager, coder)
    turn(coder.context_pager, coder)
    clone = coder.clone()
    assert clone.l2_interface_fnames == coder.l2_interface_fnames
    assert clone.context_pager is not coder.context_pager
    assert clone.context_pager.states is not coder.context_pager.states
    clone.drop_rel_fname("a.py")
    assert coder.l2_interface_fnames
    assert not clone.l2_interface_fnames


def test_disabled_pager_leaves_prompt_messages_identical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path)
    before = coder.format_chat_chunks().all_messages()
    assert coder.context_pager is None
    clone = coder.clone(enable_l2_paging=False, max_l1_tokens=1, max_l1_files=1)
    assert clone.format_chat_chunks().all_messages() == before
    assert clone.abs_fnames == coder.abs_fnames


def test_real_completed_responses_drive_ttl_without_model_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True, l1_ttl_turns=2)

    def send(*args, **kwargs):
        coder.partial_response_content = "Reviewed."
        coder.partial_response_function_call = {}
        yield from ()

    with patch.object(coder, "send", side_effect=send):
        coder.run(with_message="Inspect a.py", preproc=False)
        coder.run(with_message="Continue", preproc=False)
        coder.run(with_message="Continue", preproc=False)
    assert coder.context_pager.turn == 3
    assert str(tmp_path / "a.py") in coder.l2_interface_fnames
    assert not coder.abs_fnames


def test_failed_provider_response_does_not_advance_pager_clock(tmp_path, monkeypatch):
    from aider.chatgpt_errors import ChatGPTError

    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True)
    with patch.object(coder, "send", side_effect=ChatGPTError("Disconnected")):
        coder.run(with_message="Inspect a.py", preproc=False)
    assert coder.context_pager.turn == 0
    assert str(tmp_path / "a.py") in coder.abs_fnames


def test_map_fallbacks_exclude_full_files_and_l2_interfaces(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True)
    for name in ("b.py", "cold.py"):
        (tmp_path / name).write_text("def run(): return 1\n", encoding="utf-8")
    a, b, cold = (str(tmp_path / name) for name in ("a.py", "b.py", "cold.py"))
    coder.l2_interface_fnames = {b}
    coder.repo_map = MagicMock()
    coder.repo_map.get_repo_map.return_value = None
    with patch.object(coder, "get_all_abs_files", return_value=[a, b, cold]):
        coder.get_repo_map()
    assert len(coder.repo_map.get_repo_map.call_args_list) == 3
    for call in coder.repo_map.get_repo_map.call_args_list:
        assert call.args[1] == {cold}


def test_l2_prompts_are_read_only_and_precede_history_and_working_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True)
    dependency = tmp_path / "b.py"
    dependency.write_text("def b():\n    return 'hidden implementation'\n", encoding="utf-8")
    coder.l2_interface_fnames = {str(dependency)}
    coder.done_messages = [{"role": "user", "content": "Earlier conversation"}]
    chunks = coder.format_chat_chunks()
    messages = chunks.all_messages()
    assert chunks.interfaces
    assert "hidden implementation" not in str(chunks.interfaces)
    assert messages.index(chunks.interfaces[0]) < messages.index(chunks.done[0])
    assert messages.index(chunks.interfaces[0]) < messages.index(chunks.chat_files[0])


def test_unknown_lint_recheck_does_not_clear_an_existing_failure(tmp_path):
    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    pager.prepare(coder)
    pager.record_lint(coder, paths["a.py"], "error", "failed")
    pager.record_lint(coder, paths["a.py"], None, "unknown")
    for _ in range(3):
        turn(pager, coder)
    assert paths["a.py"] in coder.abs_fnames
    pager.record_lint(coder, paths["a.py"], None, "clean")
    turn(pager, coder)
    assert paths["a.py"] in coder.l2_interface_fnames


def test_unavailable_lint_command_is_unknown_and_keeps_edited_source_full(tmp_path):
    from aider.linter import Linter

    pager, coder, paths = setup(tmp_path, l1_ttl_turns=1)
    pager.prepare(coder)
    Path(paths["a.py"]).write_text("def a(): return 3\n", encoding="utf-8")
    linter = Linter(root=str(tmp_path))
    with patch("aider.linter.run_cmd_subprocess", side_effect=OSError("unavailable")):
        errors = linter.lint(paths["a.py"], cmd="missing-checker")
    assert errors is None and linter.last_status == "unknown"
    pager.record_lint(coder, paths["a.py"], errors, linter.last_status)
    for _ in range(3):
        turn(pager, coder)
    assert paths["a.py"] in coder.abs_fnames


def test_fences_in_docstrings_cannot_terminate_interface_blocks(tmp_path):
    pager, coder, _ = setup(
        tmp_path,
        {"a.py": '"""Example:\n```python\nvalue = 1\n```\n"""\n' + working_source()},
        l1_ttl_turns=1,
    )
    turn(pager, coder)
    turn(pager, coder)
    content = pager.interface_content(coder)
    assert "a.py\n````\n" in content
    assert content.endswith("````\n")


def test_typescript_summaries_keep_declarations_and_strip_method_bodies(tmp_path):
    pager, _, paths = setup(
        tmp_path,
        {
            "api.ts": (
                'import { Request } from "./types";\n'
                "/** API contract */\nexport class Api extends Base {\n"
                "  count: number = 0;\n"
                "  run(request: Request): number { const secret = 12345; return secret; }\n}\n"
            )
        },
    )
    stub = pager.render_l2_stub(paths["api.ts"])
    assert "import { Request }" in stub and "API contract" in stub
    assert "extends Base" in stub and "count: number" in stub
    assert "run(request: Request): number" in stub
    assert "12345" not in stub and "omitted" in stub


def test_file_mentions_reload_l2_without_git_tracking(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True, l1_ttl_turns=1)
    turn(coder.context_pager, coder)
    turn(coder.context_pager, coder)
    assert "a.py" in coder.get_addable_relative_files()
    assert coder.check_for_file_mentions("Please load a.py")
    assert str(tmp_path / "a.py") in coder.abs_fnames
    assert not coder.l2_interface_fnames


def test_explicit_drop_also_removes_protected_outside_root_files(tmp_path):
    pager, coder, _ = setup(tmp_path / "repo")
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    coder.abs_fnames.add(str(outside))
    pager.forget(coder, outside)
    assert str(outside) not in coder.abs_fnames


def test_default_policy_keeps_large_active_context_until_it_is_idle(tmp_path):
    files = {
        f"file{index}.py": f'def work{index}():\n    return "' + "word " * 4000 + '"\n'
        for index in range(6)
    }
    pager, coder, paths = setup(tmp_path, files)
    assert (pager.max_l1_tokens, pager.max_l1_files, pager.max_l2_tokens) == (
        None,
        None,
        None,
    )
    turn(pager, coder)
    turn(pager, coder)
    assert coder.abs_fnames == set(paths.values())
    assert not coder.l2_interface_fnames
    assert pager.stats["l1_tokens"] > 16000
    assert not coder.io.warnings
    turn(pager, coder)
    assert not coder.abs_fnames
    assert coder.l2_interface_fnames == set(paths.values())


def test_default_l2_has_no_implicit_summary_token_cap(tmp_path):
    source = '"""' + "contract " * 6500 + '"""\n' + working_source("work")
    pager, coder, paths = setup(tmp_path, {"a.py": source}, l1_ttl_turns=1)
    turn(pager, coder)
    turn(pager, coder)
    assert coder.l2_interface_fnames == {paths["a.py"]}
    assert pager.stats["l2_tokens"] > 6000
    assert not coder.io.warnings


def test_explicit_cap_does_not_enable_the_other_caps(tmp_path):
    parser = get_parser([], str(tmp_path))
    args = parser.parse_args(["--enable-l2-paging", "--max-l1-files", "8"])
    assert args.max_l1_files == 8
    assert args.max_l1_tokens is None and args.max_l2_tokens is None
    pager, coder, paths = setup(
        tmp_path,
        {f"{name}.py": working_source(name) for name in ("a", "b", "c")},
        max_l1_files=1,
    )
    pager.prepare(coder)
    assert len(coder.abs_fnames) == 1
    assert len(coder.l2_interface_fnames) == 2


@pytest.mark.parametrize("bad", [0, -1, True, "100"])
def test_programmatic_optional_caps_reject_invalid_explicit_values(tmp_path, bad):
    for name in ("max_l1_tokens", "max_l1_files", "max_l2_tokens"):
        with pytest.raises(ValueError, match="caps"):
            ContextPager(tmp_path, len, **{name: bad})
    with pytest.raises(ValueError, match="TTL"):
        ContextPager(tmp_path, len, l1_ttl_turns=bad)


def test_imported_interfaces_do_not_inflate_the_complete_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True)
    source = tmp_path / "a.py"
    source.write_text("from dependency import target\ndef a(): return target()\n", encoding="utf-8")
    dependency = tmp_path / "dependency.py"
    dependency.write_text(
        '"""' + "dependency contract " * 12000 + '"""\ndef target(): return 1\n',
        encoding="utf-8",
    )
    control = coder.clone(enable_l2_paging=False)
    files = [str(source), str(dependency)]
    with (
        patch.object(coder, "get_all_abs_files", return_value=files),
        patch.object(control, "get_all_abs_files", return_value=files),
        patch.object(coder.io, "read_text", wraps=coder.io.read_text) as reads,
    ):
        treatment_messages = coder.format_messages().all_messages()
        control_messages = control.format_messages().all_messages()
    assert not any(str(call.args[0]) == str(dependency) for call in reads.call_args_list)
    assert treatment_messages == control_messages
    assert not coder.l2_interface_fnames


def test_idle_demotion_keeps_tiny_source_when_interface_increases_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, source_text="def a():\n    return 1\n", enable_l2_paging=True)
    before = coder.format_messages().all_messages()
    for _ in range(3):
        turn(coder.context_pager, coder)
    after = coder.format_messages().all_messages()
    assert str(tmp_path / "a.py") in coder.abs_fnames
    assert not coder.l2_interface_fnames
    assert after == before
    assert coder.context_pager.stats["unhelpful_demotions"] > 0


def test_idle_replacement_reduces_the_complete_prompt_without_mutating_source(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    coder = real_coder(tmp_path, enable_l2_paging=True)
    path = tmp_path / "a.py"
    original = path.read_bytes()
    before = coder.main_model.token_count(coder.format_messages().all_messages())
    for _ in range(3):
        turn(coder.context_pager, coder)
    after = coder.main_model.token_count(coder.format_messages().all_messages())
    assert before > after > 0
    assert str(path) in coder.l2_interface_fnames
    assert path.read_bytes() == original


@pytest.mark.parametrize("unknown", [None, 0])
def test_unknown_demotion_cost_keeps_source_and_original_context(tmp_path, unknown):
    pager, coder, paths = setup(tmp_path)
    pager.prepare(coder)
    pager.token_count = lambda text: (
        unknown if "Read-only interface summaries" in text else len(text.split())
    )
    with pytest.raises(ValueError):
        pager.demote_file(coder, paths["a.py"])
    assert coder.abs_fnames == {paths["a.py"]}
    assert not coder.l2_interface_fnames and not pager.l2_paths
    assert not pager.stats["demotions"]
