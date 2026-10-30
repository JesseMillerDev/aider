# Explicit file navigation

Enable navigation with `--enable-agent-nav`. It is off by default and does not
require `--enable-l2-paging` or `--enable-lsp-graph`.

Navigation lets the assistant find a definition, inspect references, load source,
or replace an eligible source file with its interface. It uses the existing model
response/reflection limits. One response can contain at most three actions.

````text
```aider-nav
/nav_def src/service.py:42:9
/nav_refs Service.handle
/page_l1 "src/other service.py" 80 140
```
````

Each action occupies one line. A navigation response can contain prose,
but its edits are discarded. After receiving navigation results, the assistant
must generate a separate edit response against the newly loaded source.

| Action | Behavior |
| --- | --- |
| `/nav_def symbol_or_file:line[:column]` | Find a definition and load its complete file when there is one target file. Multiple target files are listed for selection. |
| `/nav_refs symbol_or_file:line[:column]` | Return a compact list of repository references, without loading every referenced file. |
| `/page_l1 filepath [start_line] [end_line]` | Load a complete editable file. Optional lines are location hints. |
| `/demote_l2 filepath` | Replace an eligible L1 file with its complete read-only interface summary. |

The terminal also accepts `/nav-def`, `/nav-refs`, `/page-l1`, and `/demote-l2`;
the underscore spellings work as aliases. User commands print their results
without making a model request.

## Lookup and scope

File lines and columns are 1-based. Columns use UTF-16, matching LSP. A definition
on the requested line takes precedence over its annotations. Other lines with
multiple indexed symbols require a column.

Definition/reference lookup uses the existing ranking bridge when available,
otherwise starts its own language server lazily. The existing 10-second default
lookup budget is shared across the action batch. Paging actions do not start a
language server. An empty or unavailable LSP lookup permits a content-hashed
syntax fallback: Python AST, or RepoMap's Tree-sitter queries for other supported
languages. Fallback results are explicitly labeled as name matches, rather than
proof of semantic identity. A partial index cannot establish a unique definition.

Autonomous reads are limited to tracked or already loaded repository files.
Ignored files, repository metadata, paths outside the root, symlink escapes, and
previously declined file loads are rejected. An explicit user command can load
an existing untracked file inside the root. Explicitly read-only files remain
read-only. No action dispatches shell commands.

## Interaction with paging

Navigation never preloads imports. With navigation alone, file transitions are
explicit actions and there is no TTL demotion. Enabling `--enable-l2-paging`
also enables idle-file demotion, while dependency loading stays explicit.
Optional size caps remain opt-in in either mode.

Manual demotion bypasses idle age, but preserves files with pending operations,
failed tests/lint, or unchecked edits. A file stays in L1 if a complete summary
cannot be produced, fit a configured L2 cap, or reduce source/interface message
cost. No arbitrary default size limits are introduced. Navigation feedback does
not mark newly inspected files as pending edits or clear real pending failures.

Source windows are deferred: all loads contain complete files, including when
line hints are supplied. This preserves the existing whole-file edit contract.
Trace-weighted ranking and LSP diagnostic reflection are separate future changes.

## Verification

`tests/basic/test_agent_nav.py` covers protocol validation, action limits, scoped
reads, fallback ambiguity/freshness, UTF-16 positions, file transitions, pending
state, command aliases, clone isolation, bridge ownership, and a mocked Coder
navigation-then-edit exchange. It makes no provider inference requests.

An optional real Pyright definition/reference smoke test runs when
`pyright-langserver` is on `PATH`; otherwise it skips. Run:

```powershell
.venv/Scripts/python.exe -m pytest tests/basic/test_agent_nav.py tests/basic/test_context_pager.py -q
```

Local Windows verification: the focused navigation/paging suite passed 80 tests
with one symlink-privilege skip, including the real Pyright smoke. The full basic
regression run passed 698 tests and 67 subtests, with six skips and the four
previously recorded Windows editor/color/shell/symlink failures. Subsequent
focused checks cover the completed-response guard, per-batch inventory cache,
read-only scope, and TypeScript fallback alongside non-code and empty files.

A new fifty-task navigation experiment has not been run. The current paging
experiment's frozen source and Docker image are unaffected by this work.
