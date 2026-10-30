# Milestone 1: optional semantic repo-map edges

The LSP graph is implemented behind `--enable-lsp-graph` and remains off by default.
The frozen [50-task baseline](baselines/README.md) is unchanged. The LSP-only treatment
completed with 32/50 resolved (64%), matching control, and 2.32% more tokens; see
[LSP_RUN.md](LSP_RUN.md#completed-result). No token-saving advantage was demonstrated.

## Run it

With a suitable language server on PATH:

```powershell
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol --enable-lsp-graph
```

`--lsp-timeout 2.0` is the default. It bounds startup/query waits and supplies one shared
semantic-enrichment budget per map, not a new two-second allowance for every symbol.
There is also a 64-definition-query cap per map. OS process cleanup may add bounded overhead.
`--no-enable-lsp-graph` explicitly restores the stock graph path. With repo maps disabled
(`--map-tokens 0`), no LSP bridge is created.

| Language | Server command | Extensions |
|---|---|---|
| Python | `pyright-langserver --stdio` | `.py`, `.pyi` |
| TypeScript/JavaScript | `typescript-language-server --stdio` | `.ts`, `.tsx`, `.js`, `.jsx` |
| Rust | `rust-analyzer` | `.rs` |
| Go | `gopls` | `.go` |

Servers start lazily when a supported call site needs resolution. The client does not install
or download servers automatically. Missing executables, unsupported files, startup failures,
unresolved symbols, indexing delays and timed-out queries retain syntax-based weights.

For the Python benchmark repositories, this local Windows installation was tested with
Pyright 1.1.414 and Node 22.20.0. A project-local install avoids changing the global npm setup:

```powershell
npm.cmd install --prefix benchmark/.cache/lsp pyright@1.1.414 --no-audit --no-fund
$taskLspBin = (Resolve-Path benchmark/.cache/lsp/node_modules/.bin).Path
$env:PATH = "$taskLspBin;$env:PATH"
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol --enable-lsp-graph
```

The client also checks the Python interpreter's executable directory for installed server
wrappers. Windows npm `.cmd` launchers, paths with spaces, and their child-process cleanup
are covered by tests.

Real TypeScript checks used typescript-language-server 5.3.0 with TypeScript 5.9.3 installed
in the fixture workspace. That server needs the classic `tsserver` API. The tested TypeScript
7.0.2 package did not contain it and produced a clean initialization fallback; the client does
not downgrade a user's project. For semantic graph queries the TypeScript client disables
the syntax-only server and automatic type-package downloads. This prevents an early syntax-only
import-alias result from being treated as a resolved module dependency.

Rust and Go have stdio command mappings and share the protocol implementation, but no real
Rust/Go project smoke test was run on this machine. Their missing/crashed-server behavior is
covered by the same bounded fallback tests.

## What changed

- `aider/lsp_bridge.py` provides lazy language-server lifecycles, asynchronous framed stdio
  reader/writer threads, correlated requests, server-request handling, deadlines, bounded
  backpressure, document synchronization and explicit shutdown. UTF-16 positions are negotiated;
  unsupported position encodings fall back rather than silently using wrong columns.
- `resolve_definition()` and `find_references()` return sorted `Location` records.
  `get_diagnostics()` returns current-version diagnostic records when available. Diagnostics
  are exposed for later work; edits are not yet rejected or reflected using them.
- `aider/lsp_graph.py` collects semantic evidence in deterministic order, prioritizing chat files
  and ambiguous names. It maintains a workspace-content fingerprint and a separate
  `.aider.lsp.cache.v1` cache. Positive definitions are scoped by workspace, server session,
  source content, symbol, line and column. Unknown responses are retried on later maps.
- `aider/repomap.py` retains the existing `Tag` tuple and obtains call-site positions through a
  sidecar Tree-sitter query. It converts UTF-8 byte columns to UTF-16 rather than searching for
  the first text occurrence of a name. The enabled tag cache also checks content hashes so
  preserved mtimes cannot retain stale source positions/tags.
- `aider/args.py`, `aider/main.py` and `Coder` carry the default-off switch and positive timeout
  through construction and coder cloning. The owning coder closes its server resources.

For each syntactic referencer/definer/name edge, a resolved target in the definer receives
**2.5x** weight. A positively resolved target in another file or builtin receives **0.05x**.
Unknown evidence receives **1x**. Multiple sites use the mean factor; unqueried sites remain
unknown, so hitting a deadline cannot turn one verified site into evidence for every occurrence.
These remain file-level graph edges, not a complete method-level call graph.

When enabled, `--map-refresh auto` recomputes the map so a cached syntax fallback does not
permanently hide a server finishing indexing or a dependency edit. Explicit `files`, `manual`,
and `always` refresh choices remain intact. With the feature disabled, parsing, graph weights,
tag tuple layout and rendered-map behavior follow the original path.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/basic/test_lsp_graph.py -q
```

The focused suite covers fragmented JSON-RPC framing and UTF-8 payloads, server requests,
definition links, versioned full/incremental document changes, UTF-16 columns, stale diagnostics,
crashes, malformed frames, startup/query deadlines, blocked stdin, lock contention, missing
binaries, exact semantic edge factors, unknown fallback compatibility, per-map query budgets,
cache failure, dependency/source changes with preserved mtimes, CLI defaults, coder cloning,
resource cleanup and Windows batch launchers.

Live local checks verified Pyright definitions/references, semantic weighting, import-target
changes, TypeScript cross-file definitions, and the actual Windows npm Pyright wrapper.
They consumed no LLM tokens. The existing RepoMap/Coder/CLI regression group passed 164 tests
and 29 subtests. Full-suite results retain the four pre-existing Windows cases documented in
[the baseline plan](CONTEXT_PAGING_PLAN.md#initial-local-verification).

Recorded validation: **28 focused LSP tests passed**. The full basic suite reported **590 passed,
4 failed, 5 skipped, and 67 subtests passed**; all four failures match the recorded Windows
baseline. CLI startup, Flake8 and Git whitespace checks passed. No paid/subscription model calls
or treatment SWE-bench runs were made during this milestone.

Protocol and server references:

- [LSP specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)
- [TypeScript server configuration](https://github.com/typescript-language-server/typescript-language-server/blob/master/docs/configuration.md)

## Next milestone

Implement whole-file L1/L2 paging before introducing source windows, preserving unresolved-error
state and editable-file semantics. Then add navigation, trace personalization and diagnostic
reflection. The treatment runner must explicitly build the treatment revision with server binaries
and pass feature flags; the existing baseline builder intentionally still archives control revision
`8400741ce7a9d26aecdbe97da423de5f3e80def1`.
