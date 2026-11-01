# Local file discovery and DOCX reference reading

The assistant previously had only the Git-backed code inventory and a text/image
reader. An untracked Word document was absent from filename detection, and decoding
the ZIP archive as source text could not provide its contents. Naming a local DOCX
therefore left the model asking for an upload even though the file was present.

With agent navigation enabled (the CLI default), the assistant now has:

| Action | Behavior |
| --- | --- |
| `/list_files "*.docx"` | List matching workspace filenames, including non-ignored untracked documents |
| `/read_file "docs/Game Design.docx"` | Load reference text read-only; DOCX body paragraphs and tables are extracted locally |

Actions use the same separate `aider-nav` fence and three-action response limit.
The terminal accepts `/list-files` and `/read-file`, with underscore aliases.
Listing returns metadata rather than loading file contents, reports incomplete
results, and limits displayed entries to 80 so the assistant can refine the glob.
Inventory traversal is bounded by the navigation time budget and 20,000 candidates.

When the **user** explicitly names a unique local DOCX, it is found and loaded
read-only before the next model request, without another paste/upload or confirmation
round trip. Both relative paths and unique basenames work, including names with
spaces. Ambiguous basenames require a relative path. Model responses do not activate
this automatic user-filename behavior.
Restrictive wording such as “do not read” suppresses automatic loading so the
assistant can interpret the user's constraints first.

For example, from the game repository:

```text
Read @Bannerhold_Dynasty_Game_Design.docx and help me plan the game.
```

`@file_name` is the preferred explicit reference syntax. It also works for source
and plain text files, including untracked files. A unique basename or a relative
path resolves within the workspace. Use `@"docs/Game Design.docx"` for spaces;
both Windows and forward-slash paths work. Multiple references can appear in one
message. Type `@` and use tab completion to choose a workspace file.

References load read-only without an extra confirmation prompt. Files already in
editable context stay editable. Use `/add path` to make source files editable;
DOCX references always remain read-only. Ambiguous or missing references report
the problem instead of guessing. Email addresses are not references. Explicit
user references allow loading untracked source files; the model's autonomous
navigation still requires an explicit user load for those files.

Bare DOCX filenames continue to work for compatibility.

Or explicitly:

```text
/read-file Bannerhold_Dynasty_Game_Design.docx
```

`/read-only path.docx`, `/add path.docx`, and positional DOCX filenames also load
read-only. Unsupported or corrupt documents produce an extraction error rather
than binary text or an encoding recommendation. The original Word archive is
never rewritten, and both the edit permission check and text writer reject DOCX
writes. Plans and implementation files can be created separately.
The input status line shows `RO` when reference files are loaded, and `/status`
estimates the extracted text rather than attempting to decode the archive as source.

## Scope and fidelity

Autonomous navigation stays within the workspace and preserves ignored-path,
declined-load, repository-metadata and symlink-escape protections. Untracked DOCX
references can be read without granting edit permission. Untracked source code
continues to require an explicit user load. Existing explicit user `/read-only`
commands retain their own path behavior.

Extraction uses Python's standard library: no Word, Pandoc, installation download,
macro execution, network resource loading or archive extraction is involved. It
preserves body text order, headings, paragraph breaks, table-cell text and hyperlink
labels. It omits deleted revision text. Embedded images, layout, headers, footnotes
and comments are not included; the extracted reference states those limitations.
It does not claim full Word layout or list-numbering fidelity.

Malformed XML, document type declarations/entities, encrypted/invalid archives and
empty text are rejected. The main XML part has a 32 MiB parsing limit; oversized
documents fail explicitly instead of being truncated. This parsing bound does not
introduce a context token/file cap. Normal model context-window checks still apply.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/basic/test_documents.py tests/basic/test_agent_nav.py -q
```

Tests cover DOCX text/tables, malformed and oversized archives, entity rejection,
untracked discovery, explicit filename loading, ambiguous basenames, scoped reads,
read-only command/constructor behavior and unchanged source bytes after blocked
writes. No provider inference calls are needed.

The `@` reference regression run covering documents, navigation, coder, commands,
input and status passed 210 tests and 29 subtests, with two skips and the known
headless color-initialization test deselected. An earlier main/benchmark integration
selection passed 134 tests. Targeted lint and Git whitespace checks also passed.

A read-only smoke check of the actual Bannerhold document loaded roughly 36,000
characters into reference context with zero provider requests. Its SHA-256 and Git
status remained unchanged. This validates discovery and extraction; it is not a
game-design assessment or a live model-quality result.
