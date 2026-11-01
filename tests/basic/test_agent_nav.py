"""Navigation protocol, scoped lookup, and complete-file editing; no provider calls."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aider.agent_nav import AgentNavigator, parse_actions
from aider.args import get_parser
from aider.coders import Coder
from aider.context_pager import ContextPager
from aider.io import InputOutput
from aider.lsp_bridge import LSPBridge, Location
from aider.models import Model


class FilesIO:
    def read_text(self, path):
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def tool_warning(self, text):
        pass


class FakeBridge:
    deadline = None

    def __init__(self, definitions=(), references=()):
        self.definitions = definitions
        self.references = references
        self.calls = []
        self.closed = False
        self.changed = []

    def workspace_changed(self, files):
        self.changed.extend(files)

    def resolve_definition(self, *args):
        self.calls.append(("definition", args, self.deadline))
        return self.definitions

    def find_references(self, *args):
        self.calls.append(("references", args, self.deadline))
        return self.references

    def close(self):
        self.closed = True


def setup(tmp_path, files=None, active=("a.py",), **pager_options):
    files = files or {
        "a.py": (
            "from b import target\n\ndef caller(): # "
            + "implementation " * 200
            + "\n    return target(1)\n"
        ),
        "b.py": (
            "def target(value: int) -> int: # "
            + "implementation " * 200
            + "\n    return value + 100\n"
        ),
    }
    paths = {}
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        paths[name] = str(path.resolve())
    coder = SimpleNamespace(
        root=str(tmp_path),
        io=FilesIO(),
        repo=None,
        repo_map=None,
        abs_fnames={paths[name] for name in active},
        abs_read_only_fnames=set(),
        l2_interface_fnames=set(),
        ignore_mentions=set(),
        partial_response_content="",
        reflected_message=None,
        test_outcome=None,
        get_all_abs_files=lambda: list(paths.values()),
    )
    coder.context_pager = ContextPager(
        tmp_path, lambda s: len(s.split()), automatic=False, **pager_options
    )
    navigator = AgentNavigator(coder, bridge=FakeBridge())
    return coder, navigator, paths


def block(*actions):
    return "```aider-nav\n" + "\n".join(actions) + "\n```"


def test_navigation_audit_distinguishes_success_failure_and_empty_results(tmp_path):
    coder, nav, paths = setup(tmp_path)
    nav.handle(block("/page_l1 b.py", "/page_l1 missing.py", "/nav_refs caller"))
    assert nav.stats == {"attempted": 3, "succeeded": 1, "failed": 1, "empty": 1}
    assert [event["status"] for event in nav.events] == ["succeeded", "failed", "empty"]
    assert paths["b.py"] in coder.abs_fnames
    assert "not a tracked" in nav.events[1]["detail"]
    for _ in range(10):
        nav.handle(block("/page_l1 b.py"))
    assert len(nav.events) == 8
    nav.handle(block("/nav_def"))
    assert nav.stats["invalid_responses"] == 1


def test_protocol_only_exact_explicit_blocks():
    assert parse_actions("Please /page_l1 a.py") is None
    assert parse_actions('```python\nx = "```aider-nav"\n/page_l1 a.py\n```') is None
    assert parse_actions("````markdown\n" + block("/page_l1 a.py") + "\n````") is None
    assert parse_actions("> ```aider-nav\n> /page_l1 a.py\n> ```") is None
    assert parse_actions("    ```aider-nav\n    /page_l1 a.py\n    ```") is None
    assert parse_actions(block('/page_l1 "path with spaces.py" 1 20')) == [
        ("page_l1", ["path with spaces.py", "1", "20"])
    ]
    assert len(parse_actions(block("/nav_def target", "/nav_refs target", "/demote_l2 a.py"))) == 3


@pytest.mark.parametrize(
    "text",
    [
        block("/run echo bad"),
        block("/page_l1 a.py 0"),
        block("/page_l1 a.py 20 1"),
        block("/nav_def"),
        block('/page_l1 "unclosed'),
        block(),
        block(*(["/page_l1 a.py"] * 4)),
        "```aider-nav\n/page_l1 a.py",
        "```aider-nav extra\n/page_l1 a.py\n```",
    ],
)
def test_bad_batch_has_no_side_effects(tmp_path, text):
    coder, nav, paths = setup(tmp_path)
    before = set(coder.abs_fnames)
    assert "Navigation rejected" in nav.handle(text)
    assert coder.abs_fnames == before
    assert not nav.bridge.calls


def test_page_hints_keep_complete_source_and_manual_only_paging(tmp_path):
    coder, nav, paths = setup(tmp_path)
    before = Path(paths["b.py"]).read_bytes()
    result = nav.handle(block("/page_l1 b.py 1 1"))
    assert "complete file" in result and "Line hints: 1-1" in result
    assert paths["b.py"] in coder.abs_fnames
    assert Path(paths["b.py"]).read_bytes() == before
    for _ in range(5):
        coder.context_pager.prepare(coder, "continue")
        coder.context_pager.step(coder)
    assert coder.abs_fnames == set(paths.values())
    assert not coder.l2_interface_fnames
    assert not nav.bridge.calls


def test_nav_only_does_not_preload_dependencies(tmp_path):
    coder, nav, paths = setup(tmp_path)
    coder.context_pager.prepare(coder)
    assert coder.abs_fnames == {paths["a.py"]}
    assert not coder.l2_interface_fnames


def test_manual_demote_and_reload_preserve_source(tmp_path):
    coder, nav, paths = setup(tmp_path)
    original = Path(paths["a.py"]).read_bytes()
    assert "Compressed" in nav.handle(block("/demote_l2 a.py"))
    assert paths["a.py"] in coder.l2_interface_fnames
    assert "return target" not in coder.context_pager.interface_content(coder)
    assert "complete file" in nav.handle(block("/page_l1 a.py"))
    assert paths["a.py"] in coder.abs_fnames and not coder.l2_interface_fnames
    assert Path(paths["a.py"]).read_bytes() == original


@pytest.mark.parametrize("protection", ["pending", "lint", "test", "edited"])
def test_manual_demote_retains_pending_and_unchecked_files(tmp_path, protection):
    coder, nav, paths = setup(tmp_path)
    pager = coder.context_pager
    pager.prepare(coder)
    state = pager.states[paths["a.py"]]
    setattr(state, protection, True if protection in {"pending", "edited"} else "failed")
    assert "must stay in L1" in nav.handle(block("/demote_l2 a.py"))
    assert paths["a.py"] in coder.abs_fnames


def test_navigation_feedback_preserves_true_pending_without_pinning_new_files(tmp_path):
    coder, nav, paths = setup(tmp_path)
    pager = coder.context_pager
    pager.prepare(coder)
    pager.states[paths["a.py"]].pending = True
    coder.reflected_message = nav.handle(block("/page_l1 b.py"))
    coder._navigation_reflection = True
    pager.step(coder)
    assert pager.states[paths["a.py"]].pending
    assert not pager.states[paths["b.py"]].pending
    assert "Compressed" in nav.handle(block("/demote_l2 b.py"))


def test_explicit_caps_still_apply_and_never_slice(tmp_path):
    coder, nav, paths = setup(tmp_path, max_l2_tokens=1)
    assert "exceeds" in nav.handle(block("/demote_l2 a.py"))
    assert paths["a.py"] in coder.abs_fnames


def test_ast_definition_and_refs_are_labeled_and_hash_fresh(tmp_path):
    coder, nav, paths = setup(tmp_path)
    result = nav.handle(block("/nav_def target", "/nav_refs target"))
    assert "Syntactic fallback" in result
    assert "b.py:1:" in result and "a.py:4:" in result
    assert paths["b.py"] in coder.abs_fnames
    stamp = Path(paths["b.py"]).stat().st_mtime_ns
    Path(paths["b.py"]).write_text("def renamed(value):\n    return value\n", encoding="utf-8")
    os.utime(paths["b.py"], ns=(stamp, stamp))
    assert "Symbol not found" in nav.handle(block("/nav_def target"))


def test_ambiguous_definition_does_not_guess(tmp_path):
    coder, nav, paths = setup(
        tmp_path,
        files={
            "a.py": "def target():\n    return 1\n",
            "b.py": "def target():\n    return 2\n",
        },
    )
    result = nav.handle(block("/nav_def target"))
    assert "Ambiguous" in result and "b.py:1:" in result
    assert paths["b.py"] not in coder.abs_fnames


def test_lsp_uses_utf16_positions_and_loads_definition(tmp_path):
    coder, nav, paths = setup(
        tmp_path,
        files={
            "a.py": 'label = "😀"; target(1)\n',
            "b.py": "def target(value):\n    return value\n",
        },
    )
    nav.bridge = FakeBridge(definitions=[Location(Path(paths["b.py"]).as_uri(), 0, 4)])
    assert "Loaded complete" in nav.handle(block("/nav_def a.py:1"))
    call = nav.bridge.calls[0]
    assert call[1] == ("a.py", 0, 14)
    assert call[2] is not None and nav.bridge.deadline is None


def test_lsp_external_location_does_not_fall_back_to_unrelated_name(tmp_path):
    coder, nav, paths = setup(tmp_path)
    nav.bridge = FakeBridge(
        definitions=[Location((tmp_path.parent / "external.py").as_uri(), 0, 0)]
    )
    result = nav.handle(block("/nav_def target"))
    assert "1 external" in result and "Syntactic fallback" not in result
    assert paths["b.py"] not in coder.abs_fnames


def test_ambiguous_line_requires_column(tmp_path):
    coder, nav, _ = setup(tmp_path, files={"a.py": "def a():\n    return outer(inner())\n"})
    assert "include a 1-based UTF-16 column" in nav.handle(block("/nav_refs a.py:2"))
    nav.handle(block("/nav_refs a.py:2:12"))
    assert nav.bridge.calls[-1][1] == ("a.py", 1, 11)


def test_scope_declines_and_readonly_are_respected(tmp_path):
    coder, nav, paths = setup(tmp_path)
    assert "limited to this repository" in nav.handle(block("/page_l1 ../secret.py"))
    (tmp_path / "unknown.py").write_text("secret = 1\n")
    assert "not a tracked" in nav.handle(block("/page_l1 unknown.py"))
    assert "complete file" in nav.execute([("page_l1", ["unknown.py"])], manual=True)
    coder.ignore_mentions.add("b.py")
    assert "previously declined" in nav.handle(block("/page_l1 b.py"))
    coder.ignore_mentions.clear()
    coder.abs_read_only_fnames.add(paths["b.py"])
    assert "read-only" in nav.handle(block("/page_l1 b.py"))
    assert "metadata" in nav.handle(block("/page_l1 .git/config"))


def real_coder(path, **options):
    return Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="diff",
        fnames=[str(path)],
        io=InputOutput(yes=True, pretty=False),
        use_git=False,
        auto_commits=False,
        stream=False,
        map_tokens=0,
        suggest_shell_commands=False,
        **options,
    )


def test_cli_defaults_on_and_programmatic_navigation_remains_opt_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("def a(): # " + "implementation " * 200 + "\n    return 1\n")
    coder = real_coder(path)
    assert coder.agent_navigator is None and coder.context_pager is None
    assert "/nav-def" not in coder.commands.get_commands()
    assert "aider-nav" not in str(coder.format_messages().all_messages())
    enabled = real_coder(path, enable_agent_nav=True)
    assert not enabled.context_pager.automatic
    assert "/nav-def" in enabled.commands.get_commands()
    assert "aider-nav" in str(enabled.format_messages().all_messages())
    assert enabled.commands.run("/demote_l2 a.py") is None
    assert str(path) in enabled.l2_interface_fnames
    assert enabled.commands.run("/page-l1 a.py") is None
    assert str(path) in enabled.abs_fnames
    parser = get_parser([], None)
    assert parser.parse_args([]).enable_agent_nav
    assert parser.parse_args(["--enable-agent-nav"]).enable_agent_nav
    assert not parser.parse_args(["--no-enable-agent-nav"]).enable_agent_nav


def test_navigation_response_cannot_apply_mixed_edits_and_next_reply_can(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("def a():\n    return 1\n")
    coder = real_coder(path, enable_agent_nav=True, auto_lint=False)
    coder.aider_edited_files = set()
    edit = (
        "a.py\n```python\n<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE\n```"
    )

    def respond(content):
        def send(*args, **kwargs):
            coder.partial_response_content = content
            coder.partial_response_function_call = {}
            return iter(())

        return send

    with patch.object(coder, "send", side_effect=respond(block("/page_l1 a.py") + "\n" + edit)):
        list(coder.send_message("inspect then fix"))
    assert path.read_text().endswith("return 1\n")
    assert coder._navigation_reflection
    assert not coder.context_pager.states[str(path)].pending
    coder.reflected_message = None
    with patch.object(coder, "send", side_effect=respond(edit)):
        list(coder.send_message("apply the change"))
    assert path.read_text().endswith("return 2\n")
    assert not coder._navigation_reflection


def test_clone_owns_independent_paging_and_navigation_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("def a(): # " + "implementation " * 200 + "\n    return 1\n")
    coder = real_coder(path, enable_agent_nav=True)
    coder.agent_navigator.handle(block("/demote_l2 a.py"))
    clone = coder.clone()
    assert clone.agent_navigator is not coder.agent_navigator
    assert clone.l2_interface_fnames == coder.l2_interface_fnames
    clone.agent_navigator.handle(block("/page_l1 a.py"))
    assert str(path) in coder.l2_interface_fnames
    assert str(path) not in clone.l2_interface_fnames


def test_borrowed_bridge_is_not_closed(tmp_path):
    _, nav, _ = setup(tmp_path)
    nav.close()
    assert not nav.bridge.closed


def test_owned_bridge_is_lazy_and_closed(tmp_path):
    coder, nav, _ = setup(tmp_path)
    nav.bridge = None
    bridge = FakeBridge()
    with patch("aider.agent_nav.LSPBridge", return_value=bridge) as factory:
        nav.handle(block("/page_l1 b.py"))
        factory.assert_not_called()
        nav.handle(block("/nav_def target"))
        factory.assert_called_once()
    nav.close()
    assert bridge.closed


def test_existing_ranking_bridge_is_borrowed(tmp_path):
    coder, _, _ = setup(tmp_path)
    bridge = FakeBridge()
    coder.repo_map = SimpleNamespace(semantic_graph=SimpleNamespace(bridge=bridge))
    nav = AgentNavigator(coder)
    nav.handle(block("/nav_def target"))
    assert bridge.calls
    nav.close()
    assert not bridge.closed


def test_lsp_references_report_positions_without_loading_every_file(tmp_path):
    coder, nav, paths = setup(tmp_path)
    nav.bridge = FakeBridge(references=[Location(Path(paths["a.py"]).as_uri(), 3, 11)])
    result = nav.handle(block("/nav_refs target"))
    assert "LSP: 1 result" in result and "a.py:4:12:" in result
    assert paths["b.py"] not in coder.abs_fnames


def test_unavailable_lsp_and_partial_index_are_honest(tmp_path):
    coder, nav, _ = setup(tmp_path)
    with patch.object(nav, "_index", return_value=([], ["lookup time budget reached"])):
        result = nav.handle(block("/nav_def target"))
    assert "incomplete" in result and "Loaded" not in result


def test_symlink_escape_is_rejected_before_read(tmp_path):
    coder, nav, _ = setup(tmp_path)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def secret():\n    pass\n")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this Windows host")
    with patch.object(coder.io, "read_text", side_effect=AssertionError("must not read")):
        result = nav.handle(block("/page_l1 linked.py"))
    assert "limited to this repository" in result


def test_ignored_file_is_rejected_before_read(tmp_path):
    coder, nav, paths = setup(tmp_path)
    coder.repo = SimpleNamespace(
        ignored_file=lambda p: str(p) == paths["b.py"], git_ignored_file=lambda p: False
    )
    with patch.object(coder.io, "read_text", side_effect=AssertionError("must not read")):
        assert "ignored" in nav.handle(block("/page_l1 b.py"))


def test_manual_demotion_bypasses_age_but_keeps_failed_tests(tmp_path):
    coder, nav, paths = setup(tmp_path)
    coder.context_pager.record_test(coder, False)
    assert "must stay in L1" in nav.handle(block("/demote_l2 a.py"))
    coder.context_pager.record_test(coder, True)
    assert "Compressed" in nav.handle(block("/demote_l2 a.py"))
    assert paths["a.py"] in coder.l2_interface_fnames


def test_real_pyright_navigation_when_available(tmp_path):
    command = shutil.which("pyright-langserver")
    if not command:
        pytest.skip("Optional real Pyright smoke test needs pyright-langserver on PATH")
    coder, nav, paths = setup(tmp_path)
    bridge = LSPBridge(tmp_path, timeout=10, commands={"python": (command, "--stdio")})
    nav.bridge = bridge
    try:
        result = nav.handle(block("/nav_def a.py:4"))
        assert "LSP: 1 result" in result, result
        assert paths["b.py"] in coder.abs_fnames
        result = nav.handle(block("/nav_refs b.py:1"))
        assert "LSP:" in result and "a.py:4:" in result, result
    finally:
        bridge.close()


def test_open_dependency_edits_are_notified_before_next_lsp_query(tmp_path):
    coder, nav, paths = setup(tmp_path)
    nav.bridge = FakeBridge(definitions=[Location(Path(paths["b.py"]).as_uri(), 0, 4)])
    nav.handle(block("/nav_def a.py:4"))
    Path(paths["b.py"]).write_text("def target(value):\n    return str(value)\n")
    nav.handle(block("/nav_def a.py:4"))
    assert "b.py" in nav.bridge.changed


def test_failed_provider_reply_cannot_execute_partial_navigation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("def a(): # " + "implementation " * 200 + "\n    return 1\n")
    coder = real_coder(path, enable_agent_nav=True)

    class ProviderFailure(Exception):
        pass

    def fail(*args, **kwargs):
        coder.partial_response_content = block("/demote_l2 a.py")
        coder.partial_response_function_call = {}
        raise ProviderFailure("connection failed")

    errors = SimpleNamespace(
        exceptions_tuple=lambda: (ProviderFailure,),
        get_ex_info=lambda err: SimpleNamespace(name="APIError", retry=False, description=None),
    )
    with (
        patch("aider.coders.base_coder.LiteLLMExceptions", return_value=errors),
        patch.object(coder, "send", side_effect=fail),
        patch.object(coder, "check_and_open_urls"),
    ):
        list(coder.send_message("inspect"))
    assert str(path) in coder.abs_fnames
    assert not coder.l2_interface_fnames
    assert coder.context_pager.turn == 0


def test_inventory_is_built_once_per_batch(tmp_path):
    coder, nav, paths = setup(tmp_path)
    with patch.object(coder, "get_all_abs_files", return_value=list(paths.values())) as inventory:
        nav.handle(block("/nav_def target", "/nav_refs target", "/page_l1 b.py"))
    assert inventory.call_count == 1


def test_explicit_readonly_context_can_be_queried_but_not_promoted(tmp_path):
    coder, nav, paths = setup(tmp_path)
    coder.get_all_abs_files = lambda: [paths["a.py"]]
    coder.abs_read_only_fnames.add(paths["b.py"])
    result = nav.handle(block("/nav_refs b.py:1"))
    assert "Syntactic fallback" in result and "a.py:4:" in result
    assert "read-only" in nav.handle(block("/page_l1 b.py"))
    assert paths["b.py"] not in coder.abs_fnames


def test_noncode_files_do_not_make_python_index_incomplete(tmp_path):
    coder, nav, paths = setup(
        tmp_path,
        files={
            "a.py": "def caller():\n    return 1\n",
            "b.py": "def target():\n    return 2\n",
            "README.md": "# Project\n",
            "config.json": '{"name": "project"}\n',
        },
    )
    assert "Loaded complete" in nav.handle(block("/nav_def target"))
    assert paths["b.py"] in coder.abs_fnames


def test_tree_sitter_fallback_handles_valid_empty_files(tmp_path):
    coder, nav, paths = setup(
        tmp_path,
        files={
            "a.py": "def caller():\n    return 1\n",
            "b.ts": "export function target(x: number) { return x + 1; }\n",
            "empty.ts": "",
            "README.md": "# Project\n",
        },
    )
    coder.main_model = SimpleNamespace(token_count=lambda text: len(text.split()))
    try:
        result = nav.handle(block("/nav_def target"))
        assert "Syntactic fallback" in result and "Loaded complete" in result, result
        assert paths["b.ts"] in coder.abs_fnames
    finally:
        nav.close()
