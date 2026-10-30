"""The optional semantic graph must be bounded and must preserve unknown edges."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import pytest

from aider.args import get_parser, positive_seconds
from aider.io import InputOutput
from aider.lsp_bridge import LSPBridge, Location, incremental_change, locations
from aider.lsp_graph import SemanticGraph
from aider.repomap import RepoMap

SERVER = Path(__file__).resolve().parents[1] / "fixtures/fake_lsp_server.py"


def bridge(tmp_path, mode="normal", timeout=1.5):
    return LSPBridge(
        tmp_path,
        timeout=timeout,
        commands={
            "python": (
                sys.executable,
                "-u",
                str(SERVER),
                "--mode",
                mode,
                "--log",
                str(tmp_path / "rpc.jsonl"),
            )
        },
    )


def messages(tmp_path):
    return [
        json.loads(line)
        for line in (tmp_path / "rpc.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_stdio_lifecycle_fragmentation_queries_notifications_and_cleanup(tmp_path):
    source = tmp_path / "caller.py"
    source.write_text("target()\n", encoding="utf-8")
    with bridge(tmp_path) as client:
        expected = [Location((tmp_path / "target.py").as_uri(), 1, 2)]
        assert client.resolve_definition("caller.py", 0, 0) == expected
        assert client.find_references("caller.py", 0, 0) == expected
        diagnostics = client.get_diagnostics("caller.py")
        assert diagnostics[0].severity == 1
        assert "😀" in diagnostics[0].message
        process = client.servers["python"].process
        assert client.resolve_definition("caller.py", 0, 0) == expected
    assert process.poll() is not None
    rows = messages(tmp_path)
    assert sum(row.get("method") == "initialize" for row in rows) == 1
    assert sum(row.get("method") == "textDocument/didOpen" for row in rows) == 1
    assert next(row for row in rows if row.get("id") == "config" and "result" in row)[
        "result"
    ] == [{}]
    assert (
        next(row for row in rows if row.get("id") == "edit" and "result" in row)[
            "result"
        ]["applied"]
        is False
    )


@pytest.mark.parametrize("mode", ["normal", "full_sync"])
def test_versioned_document_sync_uses_server_capability(tmp_path, mode):
    source = tmp_path / "caller.py"
    source.write_text("target('😀')\n", encoding="utf-8")
    with bridge(tmp_path, mode) as client:
        client.resolve_definition("caller.py", 0, 0)
        source.write_text("target('new')\n", encoding="utf-8")
        client.resolve_definition("caller.py", 0, 0)
        assert "version 2" in client.get_diagnostics("caller.py")[0].message
    changed = next(
        row
        for row in messages(tmp_path)
        if row.get("method") == "textDocument/didChange"
    )
    assert changed["params"]["textDocument"]["version"] == 2
    change = changed["params"]["contentChanges"][0]
    assert ("range" in change) == (mode == "normal")
    assert change["text"] == source.read_bytes().decode("utf-8")


def test_incremental_ranges_preserve_unchanged_lines_and_utf16_eof():
    change = incremental_change("same\n😀old", "same\n😀new")
    assert change == {
        "range": {
            "start": {"line": 1, "character": 0},
            "end": {"line": 1, "character": 5},
        },
        "text": "😀new",
    }
    assert incremental_change("a\nb\nc\n", "a\nB\nc\n")["range"] == {
        "start": {"line": 1, "character": 0},
        "end": {"line": 2, "character": 0},
    }


@pytest.mark.parametrize(
    "mode", ["slow_init", "slow_definition", "malformed", "die", "utf8_only"]
)
def test_unavailable_or_broken_server_falls_back_within_deadline(tmp_path, mode):
    (tmp_path / "caller.py").write_text("target()\n")
    started = time.monotonic()
    with bridge(tmp_path, mode, timeout=0.3) as client:
        assert client.resolve_definition("caller.py", 0, 0) == []
    assert time.monotonic() - started < 2


def test_backpressured_stdin_cannot_bypass_query_timeout(tmp_path):
    (tmp_path / "caller.py").write_text("value = '" + "x" * 2_000_000 + "'\n")
    started = time.monotonic()
    with bridge(tmp_path, "stop_reading", timeout=0.4) as client:
        assert client.resolve_definition("caller.py", 0, 0) == []
    assert time.monotonic() - started < 2


def test_query_deadline_also_bounds_waiting_for_another_caller(tmp_path):
    import threading

    locked, release = threading.Event(), threading.Event()
    with bridge(tmp_path, timeout=0.1) as client:

        def hold_lock():
            with client.lock:
                locked.set()
                release.wait(timeout=2)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        assert locked.wait(timeout=1)
        try:
            started = time.monotonic()
            assert client.resolve_definition("caller.py", 0, 0) == []
            assert time.monotonic() - started < 0.4
            assert client.stats["lock_timeouts"] == 1
        finally:
            release.set()
            worker.join(timeout=1)


def test_missing_server_and_outside_root_are_safe(tmp_path):
    (tmp_path / "caller.py").write_text("target()\n")
    with LSPBridge(
        tmp_path, commands={"python": ("aider-no-such-language-server",)}
    ) as client:
        assert client.resolve_definition("caller.py", 0, 0) == []
        assert client.resolve_definition("../outside.py", 0, 0) == []
        assert client.stats["missing_servers"] == 1
        assert not client.servers


def test_stale_diagnostics_and_location_links(tmp_path):
    (tmp_path / "caller.py").write_text("target()\n")
    with bridge(tmp_path, "stale_diagnostics") as client:
        client.resolve_definition("caller.py", 0, 0)
        assert client.get_diagnostics("caller.py") == []
    with bridge(tmp_path, "links") as client:
        assert client.resolve_definition("caller.py", 0, 0)[0].uri.endswith(
            "/target.py"
        )


def test_malformed_locations_are_unknown():
    assert locations(None) == []
    assert (
        locations(
            [{"uri": "file:///x", "range": {"start": {"line": -1, "character": 0}}}]
        )
        == []
    )


def test_uri_normalization_preserves_literal_percent_and_encoded_windows_drive(
    tmp_path,
):
    from aider.lsp_graph import uri_key

    path = tmp_path / "a%20b.py"
    assert uri_key(path.as_uri()) == os.path.normcase(str(path))
    if os.name == "nt":
        assert uri_key("file:///c%3A/Users/a.py") == uri_key("file:///C:/Users/a.py")


class FakeBridge:
    session_id = "test-session"
    deadline = None

    def __init__(self, target=None):
        self.session_id = str(id(self))
        self.target = target
        self.calls = []
        self.changed = []

    def resolve_definition(self, fname, line, col):
        self.calls.append((fname, line, col))
        return [Location(self.target, 0, 0)] if self.target else []

    def workspace_changed(self, files):
        self.changed.append(files)


class Tokens:
    def token_count(self, text):
        return len(text) // 4


def repo(tmp_path, enabled=False, client=None):
    return RepoMap(
        root=str(tmp_path),
        main_model=Tokens(),
        io=InputOutput(),
        enable_lsp_graph=enabled,
        lsp_bridge=client,
    )


def fixture_files(tmp_path):
    contents = {
        "a.py": "def run():\n    return 1\n",
        "b.py": "def run():\n    return 2\n",
        "caller.py": "from a import run\nrun()\n",
    }
    for name, content in contents.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return [str(tmp_path / name) for name in contents]


def weights(mapping, files):
    seen = {}
    original = nx.pagerank

    def capture(graph, *args, **kwargs):
        for source, dest, data in graph.edges(data=True):
            if data.get("ident") == "run":
                seen[(source, dest)] = data["weight"]
        return original(graph, *args, **kwargs)

    with patch("networkx.pagerank", side_effect=capture):
        mapping.get_ranked_tags([], files, set(), set())
    return seen


def test_semantic_edge_boost_penalty_unknown_and_disabled_compatibility(tmp_path):
    files = fixture_files(tmp_path)
    stock, disabled = repo(tmp_path), repo(tmp_path)
    known = FakeBridge((tmp_path / "a.py").as_uri())
    enabled, unknown = repo(tmp_path, True, known), repo(tmp_path, True, FakeBridge())
    try:
        baseline = weights(stock, files)
        semantic = weights(enabled, files)
        assert semantic[("caller.py", "a.py")] == pytest.approx(
            baseline[("caller.py", "a.py")] * 2.5
        )
        assert semantic[("caller.py", "b.py")] == pytest.approx(
            baseline[("caller.py", "b.py")] * 0.05
        )
        assert weights(unknown, files) == baseline
        with patch.object(
            disabled,
            "get_reference_positions",
            side_effect=AssertionError("LSP disabled"),
        ):
            assert weights(disabled, files) == baseline
        assert stock.get_repo_map([], files) == unknown.get_repo_map([], files)
    finally:
        for mapping in (stock, disabled, enabled, unknown):
            mapping.close()


def test_reference_columns_use_utf16_and_distinguish_same_line_calls(tmp_path):
    path = tmp_path / "caller.py"
    code = 'label = "😀"; run(); run()\n'
    path.write_text(code, encoding="utf-8")
    mapping = repo(tmp_path)
    try:
        sites = mapping.get_reference_positions(str(path), "caller.py")["run"]
        first = len(code[: code.index("run")].encode("utf-16-le")) // 2
        assert sites == [(0, first), (0, first + 7)]
    finally:
        mapping.close()


def test_dependency_content_changes_invalidate_cached_resolution_even_with_same_mtime(
    tmp_path,
):
    files = fixture_files(tmp_path)
    client = FakeBridge((tmp_path / "a.py").as_uri())
    mapping = repo(tmp_path, True, client)
    try:
        weights(mapping, files)
        first_calls = len(client.calls)
        weights(mapping, files)
        assert len(client.calls) == first_calls
        path = tmp_path / "a.py"
        before = path.stat()
        path.write_text(path.read_text().replace("1", "3"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        client.target = (tmp_path / "b.py").as_uri()
        changed = weights(mapping, files)
        assert len(client.calls) > first_calls
        assert client.changed and "a.py" in client.changed[-1]
        assert changed[("caller.py", "b.py")] > changed[("caller.py", "a.py")]
    finally:
        mapping.close()


def test_unknown_responses_are_retried_after_indexing(tmp_path):
    files = fixture_files(tmp_path)
    client = FakeBridge()
    mapping = repo(tmp_path, True, client)
    try:
        weights(mapping, files)
        previous = len(client.calls)
        client.target = (tmp_path / "a.py").as_uri()
        weights(mapping, files)
        assert len(client.calls) > previous
    finally:
        mapping.close()


def test_partial_evidence_keeps_unknown_weight():
    graph = SemanticGraph.__new__(SemanticGraph)
    graph.root = Path.cwd()
    target = (graph.root / "target.py").as_uri()
    from aider.lsp_graph import uri_key

    graph.resolutions = {("caller.py", "run"): [(uri_key(target),), None]}
    assert graph.multiplier("caller.py", "target.py", "run") == 1.75
    assert graph.multiplier("caller.py", "other.py", "run") == 0.525


def test_cli_defaults_explicit_disable_and_timeout_validation(tmp_path):
    parser = get_parser([], str(tmp_path))
    assert parser.parse_args([]).enable_lsp_graph is False
    assert parser.parse_args([]).lsp_timeout == 10.0
    assert (
        parser.parse_args(
            ["--enable-lsp-graph", "--no-enable-lsp-graph"]
        ).enable_lsp_graph
        is False
    )
    assert (
        parser.parse_args(["--enable-lsp-graph", "--lsp-timeout", "3.5"]).lsp_timeout
        == 3.5
    )
    for value in ("0", "-1", "nan", "inf", "words"):
        with pytest.raises(Exception):
            positive_seconds(value)


def test_per_map_budget_includes_all_sites_and_leaves_unqueried_sites_unknown(tmp_path):
    files = fixture_files(tmp_path)
    client = FakeBridge((tmp_path / "a.py").as_uri())
    original = client.resolve_definition

    def slow(fname, line, col):
        time.sleep(0.03)
        return original(fname, line, col)

    client.resolve_definition = slow
    graph = SemanticGraph(tmp_path, timeout=0.15, bridge=client)
    started = time.monotonic()
    try:
        graph.prepare(
            files,
            {"run": {"a.py", "b.py"}},
            {"run": ["caller.py"] * 100},
            [],
            set(),
            lambda *_: {"run": [(line, 0) for line in range(100)]},
        )
        assert time.monotonic() - started < 0.7
        assert len(client.calls) <= 5
        assert graph.stats["budget_exhausted"]
        assert 1 < graph.multiplier("caller.py", "a.py", "run") < 2.5
        assert client.deadline is None
    finally:
        graph.close()


def test_cache_failure_preserves_semantic_evidence_and_uses_memory(tmp_path):
    files = fixture_files(tmp_path)
    client = FakeBridge((tmp_path / "a.py").as_uri())
    mapping = repo(tmp_path, True, client)
    try:
        with patch("aider.lsp_graph.Cache", side_effect=OSError("cache unavailable")):
            first = weights(mapping, files)
            calls = len(client.calls)
            assert mapping.semantic_graph.stats["cache_errors"]
            assert weights(mapping, files) == first
            assert len(client.calls) == calls
    finally:
        mapping.close()


def test_enabled_tag_cache_detects_content_changes_with_preserved_mtime(tmp_path):
    source = tmp_path / "caller.py"
    source.write_text("run()\n", encoding="utf-8")
    mapping = repo(tmp_path, True, FakeBridge())
    try:
        assert "run" in {tag.name for tag in mapping.get_tags(str(source), "caller.py")}
        before = source.stat()
        source.write_text("other()\n", encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert "other" in {
            tag.name for tag in mapping.get_tags(str(source), "caller.py")
        }
    finally:
        mapping.close()


def test_external_builtin_definition_dampens_unrelated_internal_candidates(tmp_path):
    files = fixture_files(tmp_path)
    client = FakeBridge((tmp_path.parent / "builtins.pyi").as_uri())
    stock, semantic = repo(tmp_path), repo(tmp_path, True, client)
    try:
        base, enriched = weights(stock, files), weights(semantic, files)
        for name in ("a.py", "b.py"):
            assert enriched[("caller.py", name)] == pytest.approx(
                base[("caller.py", name)] * 0.05
            )
    finally:
        stock.close()
        semantic.close()


def test_typescript_uses_semantic_server_and_disables_type_downloads(tmp_path):
    (tmp_path / "caller.ts").write_text("target();\n")
    command = (sys.executable, "-u", str(SERVER), "--log", str(tmp_path / "rpc.jsonl"))
    with LSPBridge(tmp_path, commands={"typescript": command}) as client:
        assert client.resolve_definition("caller.ts", 0, 0)
    initialize = next(
        row for row in messages(tmp_path) if row.get("method") == "initialize"
    )
    options = initialize["params"]["initializationOptions"]
    assert options["tsserver"]["useSyntaxServer"] == "never"
    assert options["disableAutomaticTypingAcquisition"] is True


@pytest.mark.skipif(
    os.name != "nt", reason="Windows npm launcher quoting and process trees"
)
def test_windows_batch_launcher_with_spaces_and_child_cleanup(tmp_path):
    import psutil

    directory = tmp_path / "server with spaces"
    directory.mkdir()
    wrapper = directory / "server.cmd"
    wrapper.write_text(
        f'@echo off\n"{sys.executable}" -u "{SERVER}" --log "{tmp_path / "rpc.jsonl"}"\n'
    )
    (tmp_path / "caller.py").write_text("target()\n")
    with LSPBridge(tmp_path, timeout=2, commands={"python": (str(wrapper),)}) as client:
        assert client.resolve_definition("caller.py", 0, 0)
        process = client.servers["python"].process
        children = psutil.Process(process.pid).children(recursive=True)
    assert process.poll() is not None
    assert all(not psutil.pid_exists(child.pid) for child in children)


def test_coder_clone_retains_feature_settings_and_owner_closes_server(
    tmp_path, monkeypatch
):
    import subprocess
    from aider.coders import Coder
    from aider.models import Model

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "caller.py"
    source.write_text("target()\n")
    model = Model("gpt-3.5-turbo")
    coder = Coder.create(
        model,
        "diff",
        io=InputOutput(yes=True),
        fnames=[str(source)],
        enable_lsp_graph=True,
        lsp_timeout=3.5,
    )
    clone = coder.clone()
    try:
        assert coder.repo_map.semantic_graph.timeout == 3.5
        assert clone.repo_map.semantic_graph.timeout == 3.5
        owned = coder.repo_map.semantic_graph.bridge
        coder.__del__()
        assert owned.closed
    finally:
        clone.__del__()
