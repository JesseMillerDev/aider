# Milestone 2: whole-file L1/L2 context paging

The pager is opt-in and independent of LSP ranking. It retains complete editable source
in L1, maintains read-only interfaces in L2, and excludes those files from the
cold repository map. It does not introduce source windows, autonomous navigation actions,
trace weighting or LSP diagnostic reflection.

```powershell
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol --enable-l2-paging
```

| Setting | Default | Meaning |
|---|---:|---|
| `--enable-l2-paging` | false | Enable automatic working-context management |
| `--max-l1-tokens` | no cap | Optional cap for source-file blocks in working context |
| `--max-l1-files` | no cap | Optional cap for whole working files |
| `--max-l2-tokens` | no cap | Optional cap for the complete interface block, including instructions and filenames |
| `--l1-ttl-turns` | 2 | Completed idle responses before automatic demotion |

The default policy is idle-file paging: file count and token size do not trigger demotion
unless you explicitly configure a cap. TTL and configured caps must be positive integers;
omitted caps are `None` in the Python interface. `--no-enable-l2-paging` uses the stock prompt path.
`--enable-lsp-graph` remains a separate option, with its current ten-second default.
The completed LSP-only benchmark and its explicit two-second setting are preserved.

For example, adding only `--max-l1-files 8` enables a file-count cap without introducing
either token cap. The earlier 4-file / 16,000-token L1 and 6,000-token L2 defaults were
replaced by this idle-only policy; recorded benchmark results have not been changed.

## File lifecycle and budgets

A completed response advances the pager's turn counter, including responses that ask
to load a file or reflect on an edit/lint failure. Failed provider requests, interrupted
responses and formatting/token-preview operations do not advance it. File references
and source-content changes update per-file activity; preserved mtimes do not hide changes.

An idle file may move from L1 into a complete L2 summary after the TTL. An explicitly
configured file-count or token-budget overflow may demote an older eligible file sooner. Recently edited files,
current file references, pending reflections, known lint failures and unresolved test
failures are protected. Edited files without an observed clean lint result are protected
too. An unknown validation result never clears an existing failure. Test status remains
unknown when tests have not run; the pager does not claim they passed.

When configured, L1 caps are targets when protected files cannot fit. The pager retains full source
and reports the overflow rather than slicing it, silently omitting it, or claiming the
budget was met. It also retains files whose complete summaries cannot fit L2. Token
counts cover file blocks; chat history, system instructions and the repository map have
their existing separate budgets. Binary/image token accounting is not part of these
source-text budgets.

An explicitly configured L2 token cap is a hard limit on the assembled text block. Whole summaries are
selected deterministically; signatures and docstrings are never truncated to fill the
last available tokens. Older L2 files may return to the cold repository map when space
is needed under a configured cap. Without a cap, complete summaries are retained without
size-based eviction. The model's overall context-window checks remain unchanged.
Manual `/drop` also removes summaries and prevents immediate dependency
preloading from adding those files back. `/add` restores full source through the usual
file-addition flow. An external edit to a summarized file promotes it back to L1.

## Interfaces and preloading

Python summaries use the standard AST parser and preserve module/class/function
docstrings, decorators, inheritance, full function and async-function arguments and
annotations, imports, module exports/constants and attribute annotations. Function
implementations become marked ellipses. Conditional imports/declarations retain their
conditions. Original source files are never rewritten by rendering or paging.

For other languages, Tree-sitter can preserve declaration text and class members while
eliding recognized braced function/method bodies. TypeScript class methods are covered
by a focused fixture. Unsupported declarations, unavailable parsers and malformed source
fall back to full L1 files; this is not a claim of complete interface extraction for every
language. Unsupported forms or executable module-level code can reduce compression.

Direct dependency preloading currently resolves Python imports against local repository
files, including relative imports and `src/` layouts. It executes no project code, imports
no project modules and makes no network requests. External modules are omitted. It loads
only one import hop from L1; it does not recursively expand dependencies of L2 files.

Summary caches use source content hashes. Summaries are ordered by filename and placed
after the repository map and before conversation history and full working files. Cache
markers include the static interface prefix when prompt caching is enabled. Actual
provider cache hits remain a measurement, not a guarantee.

## Edit integrity

The model is told that L2 summaries are read-only and must request full source before
editing them. The existing file-mention flow loads full files for a subsequent response.
The edit adapter also checks edit targets: when a response targets an L2 file, it loads
the requested full source with the normal confirmation mechanism, applies none of that
response's edit batch, and asks for regenerated edits against full contents. This also
protects whole-file replacement formats from overwriting implementations with summaries.

Lint checks expose unknown/clean/failed status to the pager without changing their
existing return values. `/test`, automatic test runs and callable test commands preserve
failures across Coder message resets. Passing a known retest clears that protection.
Arbitrary `/run` output and pasted tracebacks do not yet have structured failure tracking;
that belongs to the navigation/trace milestone.

## Verification and measurements

```powershell
.venv/Scripts/python.exe -m pytest tests/basic/test_context_pager.py tests/basic/test_linter.py -q
```

The focused pager/linter group passed 45 tests. It covers TTL/activity, source hashes,
protected/unchecked files, exact L2 budgeting, explicit overflows, local dependency
resolution, manual drops, external edits, cloning, static prompt ordering/cache markers,
read-only edit recovery, actual completed-response integration, provider failures,
default-off prompt compatibility, Unicode omission markers and TypeScript headers.
The existing command/subscription/Coder group passed 165 tests and 29 subtests.
The full basic suite reported 640 passed, 4 failed, 5 skipped and 67 subtests passed;
all four failures match the previously recorded Windows editor/color/shell/symlink cases.
The final focused run also checks L2 reloading without Git tracking and manual removal
of protected files outside the repository root. CLI startup with paging enabled, Flake8
and Git whitespace checks passed.
The optional-cap policy checks also cover more than four full files and 16,000 working
tokens staying in L1 until idle, summaries exceeding 6,000 tokens without an explicit
cap, independently configured caps, and rejection of invalid explicit values.
The Coder/command/message regression group passed 124 tests and 29 subtests after the
default-policy change. CLI and direct-constructor checks confirm all three caps are
unset while the idle TTL remains two responses.

Local GPT-6.1 Sol token counting on `aider/repomap.py` measured **6,539 source tokens versus
873 interface tokens (86.65% reduction)**, excluding file wrappers and instructions.
This measures source compression only. It does not establish whole-conversation token
savings or improved resolution. Preview measurements are under the ignored
`benchmark/.cache/context-pager-compression.json`.

Implementation verification made no inference requests. The separate
[fifty-task paging run](PAGER_RUN.md) now isolates idle-only paging with LSP disabled
and no size caps. The historical LSP treatment and its explicit timeout are preserved.
