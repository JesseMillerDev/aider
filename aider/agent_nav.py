"""Explicit, bounded file navigation for assistant responses and user commands."""

import ast
import hashlib
import fnmatch
import os
import subprocess
import re
import shlex
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from aider.lsp_bridge import LSPBridge, locations as normalize_locations
from aider.documents import is_docx

ACTIONS = {"nav_def", "nav_refs", "page_l1", "demote_l2", "list_files", "read_file", "run_command"}
FILE_MENTION = re.compile(
    r'''(?<![\w@/\\])@(?:"([^"\r\n]+)"|'([^'\r\n]+)'|([^\s"'`<>(),;!?]+))'''
)
RESTRICTIVE_REFERENCE = re.compile(r"\b(don['’]t|do\s+not|never|avoid|exclude|skip)\b", re.I)


def file_mentions(content):
    """Return explicit user references and their spans, excluding email addresses."""
    return [
        (next(value for value in match.groups() if value is not None).rstrip(".:"),
         match.start(), match.end())
        for match in FILE_MENTION.finditer(content)
    ]


INSTRUCTIONS = """
You can navigate repository files before editing. Send a separate response containing
one fenced `aider-nav` block with up to three actions, one per line:
```aider-nav
/nav_def symbol_or_file:line[:column]
/nav_refs symbol_or_file:line[:column]
/page_l1 "path/to/file" [start_line] [end_line]
/demote_l2 "path/to/file"
/list_files "*.docx"
/read_file "path/to/design.docx"
/run_command git status --short
```
Use only the actions you need. Edit directly when current files are sufficient.
When asked about a local document, discover it with /list_files and load it with
/read_file before asking the user to paste or upload it. Listing includes local
untracked files. /read_file loads reference text read-only, including DOCX text
and tables; embedded images and layout are not included. It does not grant edit
permission. Untracked DOCX references can be read directly; untracked source
files still require an explicit user load. Ignored, declined and outside-workspace
paths remain inaccessible.
You have terminal access via /run_command. Use it to inspect installed tools, run
tests, and perform requested terminal work instead of claiming you cannot do so.
The rest of that action's line is a literal shell command, including quotes and
backslashes. Commands run in the workspace root and return output and exit status.
Windows uses PowerShell; other platforms use their configured shell. To open a
requested Windows project/application, use Start-Process and verify the result.
Discover executable locations with the terminal before asking the user for paths.
Background helpers use -WindowStyle Hidden unless the user needs a visible app.
Commands require confirmation unless YOLO is enabled. The default timeout is 120s;
prefix the command with --timeout=SECONDS to allow up to 3600s. Terminal output
may be shortened with an explicit notice. These are real local commands; installed
programs on PATH are available, but tools/MCP servers from other agents are not
automatically connected. Send this action separately from any proposed edits.
Definition lookup loads the complete target file.
Page line numbers are location hints; whole files are loaded, without truncation.
Reference results say whether they are semantic or syntactic. Ambiguous symbols
require a file and position. A navigation response cannot also edit files: wait
for navigation results, then generate edits against the complete file contents.
Navigation uses the existing response/reflection budget.
Demotion is refused when its complete interface does not reduce context tokens;
keep short files full rather than repeatedly trying to demote them.
"""


class NavigationError(ValueError):
    pass


def parse_actions(content):
    """Return None when absent; reject the entire batch before running any action."""
    fence = None
    nav = False
    found = False
    actions = []
    for line in content.splitlines():
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^`~]*)", line)
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = None
                nav = False
            elif nav:
                if not line.strip():
                    continue
                try:
                    terminal = re.fullmatch(r"/run[-_]command\s+(.+)", line.strip())
                    parts = (["/run_command", terminal[1]] if terminal
                             else shlex.split(line.strip().replace("\\", "/")))
                except ValueError as err:
                    raise NavigationError(str(err)) from err
                name = parts[0].lstrip("/").replace("-", "_") if parts else ""
                if not parts or not parts[0].startswith("/") or name not in ACTIONS:
                    raise NavigationError("Only supported navigation actions are allowed.")
                args = parts[1:]
                if (name in {"nav_def", "nav_refs", "demote_l2", "read_file", "run_command"}
                    and len(args) != 1) or (name == "list_files" and len(args) > 1) or (
                    name == "page_l1" and not 1 <= len(args) <= 3
                ):
                    raise NavigationError(f"Invalid arguments for /{name}.")
                if name == "page_l1":
                    try:
                        numbers = [int(value) for value in args[1:]]
                    except ValueError as err:
                        raise NavigationError("Page line hints must be positive integers.") from err
                    if any(value <= 0 for value in numbers) or (
                        len(numbers) == 2 and numbers[1] < numbers[0]
                    ):
                        raise NavigationError("Invalid page line hints.")
                actions.append((name, args))
                if len(actions) > 3:
                    raise NavigationError(
                        "At most three navigation actions are allowed per response."
                    )
        elif opening:
            fence = opening[1]
            nav = opening[2].strip() == "aider-nav"
            if opening[2].strip().split()[:1] == ["aider-nav"] and not nav:
                raise NavigationError("Use an exact aider-nav fence without extra options.")
            found |= nav
    if nav:
        raise NavigationError("The aider-nav fence must be closed.")
    if found and not actions:
        raise NavigationError("The aider-nav block is empty.")
    return actions if found else None


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    column: int
    name: str
    qualified: str = ""
    kind: str = "ref"


class AgentNavigator:
    """Own the navigation protocol, repository scope, lookup, and file transitions."""

    def __init__(self, coder, bridge=None, timeout=10.0):
        self.coder = coder
        self.root = Path(coder.root).resolve()
        self.timeout = timeout
        semantic = getattr(getattr(coder, "repo_map", None), "semantic_graph", None)
        self.bridge = bridge or (semantic.bridge if semantic else None)
        self.owns_bridge = False
        self.index = {}
        self.owned_map = None
        self.allowed = None
        self.synced = {}
        self.stats = Counter()
        self.events = deque(maxlen=8)

    def _allowed(self):
        if self.allowed is None:
            known = set(self.coder.get_all_abs_files())
            known |= self.coder.abs_fnames | self.coder.l2_interface_fnames
            known |= self.coder.abs_read_only_fnames
            self.allowed = {Path(p).resolve(): str(Path(p).resolve()) for p in known}
        return self.allowed

    def close(self):
        if self.owns_bridge and self.bridge is not None:
            self.bridge.close()
        if self.owned_map is not None:
            self.owned_map.close()

    def _path(self, name, manual=False, read_only=False):
        path = (self.root / name).resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError as err:
            raise NavigationError("Navigation is limited to this repository.") from err
        if not relative.parts or any(part.lower() == ".git" for part in relative.parts):
            raise NavigationError("Repository metadata is not a navigation target.")
        repo = self.coder.repo
        if repo and (repo.ignored_file(path) or repo.git_ignored_file(path)):
            raise NavigationError("The requested path is ignored.")
        allowed = self._allowed()
        if not manual:
            if path not in allowed and not (read_only and is_docx(path)):
                raise NavigationError(
                    "The path is not a tracked or already loaded repository file."
                )
            declined = self.coder.ignore_mentions
            if str(path) in declined or relative.as_posix() in declined or path.name in declined:
                raise NavigationError("Loading this file was previously declined.")
        if not path.is_file():
            raise NavigationError("The requested file does not exist.")
        return allowed.get(path, str(path))

    def _name(self, path):
        return Path(path).relative_to(self.root).as_posix()

    def _page(self, path, hints=()):
        if is_docx(path):
            return self._read_reference(path)
        if path in self.coder.abs_read_only_fnames:
            raise NavigationError("This file was explicitly added as read-only.")
        self.coder.context_pager.promote_file(self.coder, path)
        self._allowed()[Path(path)] = path
        suffix = f" Line hints: {'-'.join(hints)}." if hints else ""
        return f'Loaded complete file "{self._name(path)}" into L1.{suffix}'

    def _read_reference(self, path):
        content = self.coder.io.read_text(path)
        if content is None:
            raise NavigationError("The file has no readable text; nothing was added.")
        if path in self.coder.abs_fnames and not is_docx(path):
            return f'"{self._name(path)}" is already loaded in L1.'
        self.coder.abs_fnames.discard(path)
        self.coder.l2_interface_fnames.discard(path)
        self.coder.abs_read_only_fnames.add(path)
        self._allowed()[Path(path)] = path
        return f'Loaded "{self._name(path)}" as read-only reference text.'

    def workspace_files(self, pattern="*", deadline=None):
        """List metadata within the workspace without adding source to context."""
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts or pattern.startswith(":"):
            raise NavigationError("Use a workspace-relative glob.")
        deadline = deadline or time.monotonic() + self.timeout
        if self.coder.repo:
            try:
                git_pattern = f":(icase){pattern}" if os.name == "nt" else pattern
                result = subprocess.run(
                    ["git", "ls-files", "--cached", "--others", "--exclude-standard",
                     "-z", "--", git_pattern], cwd=self.root, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=max(.1, deadline - time.monotonic()),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                names = result.stdout.decode("utf-8", "surrogateescape").split("\0")
            except (OSError, subprocess.SubprocessError) as err:
                raise NavigationError("Unable to list workspace files with Git.") from err
        else:
            def walk():
                for folder, dirs, files in os.walk(self.root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in
                                     {".git", ".venv", "node_modules", "__pycache__"})
                    for name in sorted(files):
                        yield str((Path(folder) / name).relative_to(self.root))
            names = walk()
        found = set()
        for index, name in enumerate(names):
            if time.monotonic() >= deadline or index >= 20000:
                return sorted(found), False
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            if ".git" in Path(relative).parts:
                continue
            if self.coder.repo and self.coder.repo.ignored_file(path):
                continue
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
                found.add(relative)
        return sorted(found), True

    def load_mentioned_documents(self, content):
        """An explicit user filename authorizes reading a unique local DOCX reference."""
        if ".docx" not in content.lower():
            return None
        if RESTRICTIVE_REFERENCE.search(content):
            # Let the assistant interpret restrictive instructions before any automatic read.
            return None
        try:
            files, complete = self.workspace_files("*.[dD][oO][cC][xX]")
        except NavigationError as err:
            return str(err)
        if not complete:
            return "Local document search incomplete; use /read-file with its relative path."
        aliases = {}
        for name in files:
            for alias in {name, Path(name).name}:
                aliases.setdefault(alias.casefold(), set()).add(name)
        messages = []
        normalized = content.replace("\\", "/")
        selected = set()
        for alias, matches in aliases.items():
            if not re.search(r"(?<![\w@/.-])" + re.escape(alias) + r"(?![\w/-])",
                             normalized, re.I):
                continue
            if len(matches) != 1:
                messages.append("Ambiguous local document name; use a relative path: "
                                + ", ".join(sorted(matches)))
            else:
                selected.update(matches)
        self.allowed = None
        for name in sorted(selected):
            try:
                path = self._path(name, read_only=True)
                if path in self.coder.abs_read_only_fnames:
                    continue
                message = self._read_reference(path)
                messages.append(message)
                self.coder.io.tool_output(message)
            except (NavigationError, OSError, ValueError) as err:
                messages.append(f"Local document not loaded: {err}")
        return "\n".join(messages) or None

    def load_file_mentions(self, content):
        """A user @reference explicitly loads a local file as read-only context."""
        mentions = file_mentions(content)
        if not mentions or RESTRICTIVE_REFERENCE.search(content):
            return None
        try:
            files, complete = self.workspace_files()
        except NavigationError as err:
            return str(err)
        aliases = {}
        for name in files:
            for alias in {name, Path(name).name}:
                aliases.setdefault(alias.casefold(), set()).add(name)
        messages = []
        self.allowed = None
        for name in dict.fromkeys(name for name, _, _ in mentions):
            matches = aliases.get(name.replace("\\", "/").casefold(), set())
            if not complete:
                messages.append(f"Search incomplete for @{name}; use /read-file with its path.")
            elif not matches:
                messages.append(f"No matching workspace file for @{name}; nothing was loaded.")
            elif len(matches) > 1:
                messages.append(f"Ambiguous @{name}; use a relative path: "
                                + ", ".join(sorted(matches)))
            else:
                try:
                    path = self._path(next(iter(matches)), manual=True, read_only=True)
                    if path in self.coder.abs_read_only_fnames:
                        continue
                    message = self._read_reference(path)
                    messages.append(message)
                    self.coder.io.tool_output(message)
                except (NavigationError, OSError, ValueError) as err:
                    messages.append(f"Reference @{name} not loaded: {err}")
        return "\n".join(messages) or None

    def _inventory(self):
        from grep_ast import filename_to_lang

        from aider.repomap import get_scm_fname

        names = self._allowed().values()
        result = []
        for name in sorted(names):
            language = filename_to_lang(name)
            if not language or (language != "python" and not get_scm_fname(language).exists()):
                continue
            try:
                result.append(self._path(name))
            except (NavigationError, OSError, ValueError):
                continue
        return result

    def _sites(self, path):
        source = self.coder.io.read_text(path)
        if source is None:
            return [], "unreadable"
        digest = hashlib.sha256(source.encode()).hexdigest()
        cached = self.index.get(path)
        if cached and cached[0] == digest:
            return cached[1], cached[2]
        lines = source.splitlines()
        sites = []
        status = "complete"
        if Path(path).suffix in {".py", ".pyi"}:
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                tree = None
                status = "invalid Python"

            def column(row, byte):
                prefix = lines[row].encode("utf-8")[:byte].decode("utf-8")
                return len(prefix.encode("utf-16-le")) // 2

            def visit(node, scope=()):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    row = node.lineno - 1
                    match = re.search(r"\b" + re.escape(node.name) + r"\b", lines[row])
                    col = len(lines[row][: match.start()].encode("utf-16-le")) // 2
                    sites.append(
                        Site(path, row, col, node.name, ".".join((*scope, node.name)), "def")
                    )
                    scope = (*scope, node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    sites.append(
                        Site(
                            path, node.lineno - 1, column(node.lineno - 1, node.col_offset), node.id
                        )
                    )
                elif isinstance(node, ast.Attribute):
                    row = node.end_lineno - 1
                    byte = node.end_col_offset - len(node.attr.encode("utf-8"))
                    sites.append(Site(path, row, column(row, byte), node.attr))
                for child in ast.iter_child_nodes(node):
                    visit(child, scope)

            if tree is not None:
                visit(tree)
        else:
            from grep_ast import filename_to_lang
            from grep_ast.tsl import get_parser

            try:
                if get_parser(filename_to_lang(path)).parse(source.encode()).root_node.has_error:
                    status = "invalid syntax"
            except (ImportError, LookupError, ValueError, RuntimeError, TypeError):
                status = "syntax parser unavailable"
            repo_map = self.coder.repo_map
            if repo_map is None:
                from aider.repomap import RepoMap

                if self.owned_map is None:
                    self.owned_map = RepoMap(0, self.root, self.coder.main_model, self.coder.io)
                repo_map = self.owned_map
            tags = list(repo_map.get_tags_raw(path, self._name(path)) or [])
            positions = repo_map.get_reference_positions(path, self._name(path))
            for tag in tags:
                if tag.kind != "def" or not 0 <= tag.line < len(lines):
                    continue
                col = lines[tag.line].find(tag.name)
                if col >= 0:
                    sites.append(
                        Site(
                            path,
                            tag.line,
                            len(lines[tag.line][:col].encode("utf-16-le")) // 2,
                            tag.name,
                            tag.name,
                            "def",
                        )
                    )
            for name, locations in positions.items():
                sites.extend(Site(path, line, col, name) for line, col in locations)
        self.index[path] = digest, sites, status
        return sites, status

    def _index(self, deadline):
        sites = []
        incomplete = []
        for path in self._inventory():
            if time.monotonic() >= deadline:
                incomplete.append("lookup time budget reached")
                break
            try:
                found, status = self._sites(path)
            except (OSError, UnicodeError, ValueError) as err:
                incomplete.append(f"{self._name(path)}: {type(err).__name__}")
                continue
            sites.extend(found)
            if status != "complete":
                incomplete.append(f"{self._name(path)}: {status}")
        return sites, incomplete

    def _target(self, value, deadline, manual):
        location = re.fullmatch(r"(.+):(\d+)(?::(\d+))?", value)
        if location:
            # The greedy path group must not swallow the line when a column is supplied.
            parts = value.rsplit(":", 2)
            if len(parts) == 3 and parts[-2].isdigit():
                name, line, col = parts
            else:
                name, line = value.rsplit(":", 1)
                col = None
            path = self._path(name, manual)
            lines = (self.coder.io.read_text(path) or "").splitlines()
            row = int(line) - 1
            if not 0 <= row < len(lines):
                raise NavigationError("The line is outside the file.")
            sites, _ = self._sites(path)
            on_line = [site for site in sites if site.line == row]
            if col is not None:
                column = int(col) - 1
                if not 0 <= column < len(lines[row].encode("utf-16-le")) // 2:
                    raise NavigationError("The column is outside the line (columns use UTF-16).")
                matches = [
                    site
                    for site in on_line
                    if site.column <= column < site.column + len(site.name.encode("utf-16-le")) // 2
                ]
                if len(matches) == 1:
                    return matches[0], None
                return Site(path, row, column, ""), None
            declarations = [site for site in on_line if site.kind == "def"]
            if len(declarations) == 1:
                return declarations[0], None
            if len(on_line) != 1:
                raise NavigationError(
                    "The line has multiple or no indexed symbols; include a 1-based UTF-16 column."
                )
            return on_line[0], None
        if not re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", value):
            raise NavigationError("Use a symbol or file:line[:column].")
        sites, incomplete = self._index(deadline)
        matches = [
            site for site in sites if site.kind == "def" and value in (site.name, site.qualified)
        ]
        files = {site.path for site in matches}
        if incomplete:
            raise NavigationError(
                "The syntax index is incomplete; use file:line[:column]. "
                + "; ".join(incomplete[:3])
            )
        if len(files) != 1:
            details = "\n".join(self._describe(site) for site in matches[:20])
            raise NavigationError(
                ("Ambiguous symbol" if files else "Symbol not found")
                + "; use file:line[:column].\n"
                + details
            )
        return matches[0], matches

    def _describe(self, site):
        lines = (self.coder.io.read_text(site.path) or "").splitlines()
        header = lines[site.line].strip()[:180] if 0 <= site.line < len(lines) else ""
        return f"{self._name(site.path)}:{site.line + 1}:{site.column + 1}: {header}"

    def _query(self, method, site, deadline):
        if time.monotonic() >= deadline:
            return None
        if self.bridge is None:
            self.bridge = LSPBridge(self.root, timeout=self.timeout)
            self.owns_bridge = True
        previous = self.bridge.deadline
        self.bridge.deadline = min(previous, deadline) if previous is not None else deadline
        try:
            changed = []
            loaded = (
                self.coder.abs_fnames
                | self.coder.l2_interface_fnames
                | self.coder.abs_read_only_fnames
            )
            for path in sorted(loaded):
                try:
                    path = self._path(path)
                    source = self.coder.io.read_text(path)
                except (OSError, ValueError):
                    continue
                if source is None:
                    continue
                digest = hashlib.sha256(source.encode()).hexdigest()
                if self.synced.get(path) != digest:
                    changed.append(self._name(path))
                    self.synced[path] = digest
            if changed:
                self.bridge.workspace_changed(changed)
            return getattr(self.bridge, method)(self._name(site.path), site.line, site.column)
        finally:
            self.bridge.deadline = previous

    def _locations(self, result, manual):
        if isinstance(result, dict) or (
            isinstance(result, list) and result and isinstance(result[0], dict)
        ):
            result = normalize_locations(result)
        sites, excluded = [], 0
        for location in result or []:
            uri = location.uri
            parsed = urlparse(uri)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                excluded += 1
                continue
            name = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", name):
                name = name[1:]
            try:
                path = self._path(name, manual)
                lines = (self.coder.io.read_text(path) or "").splitlines()
                if not 0 <= location.line < len(lines) or location.character < 0:
                    raise NavigationError("Invalid definition location.")
                if location.character > len(lines[location.line].encode("utf-16-le")) // 2:
                    raise NavigationError("Invalid definition column.")
                sites.append(Site(path, location.line, location.character, ""))
            except (NavigationError, OSError, ValueError, KeyError, TypeError):
                excluded += 1
        return sorted(set(sites), key=lambda site: (site.path, site.line, site.column)), excluded

    def _lookup(self, action, value, deadline, manual):
        target, definitions = self._target(value, deadline, manual)
        method = "resolve_definition" if action == "nav_def" else "find_references"
        result = self._query(method, target, deadline)
        if result:
            sites, excluded = self._locations(result, manual)
            label = "LSP"
        else:
            if action == "nav_def" and (definitions or target.kind == "def"):
                sites, incomplete = definitions or [target], []
            else:
                sites, incomplete = self._index(deadline)
            name = target.name
            if not name:
                raise NavigationError("LSP unavailable and no syntax symbol at this position.")
            sites = [
                site
                for site in sites
                if (site.kind == "def") == (action == "nav_def") and site.name == name
            ]
            if action == "nav_def":
                sites = definitions or sites
                if incomplete or len({site.path for site in sites}) != 1:
                    raise NavigationError(
                        "Definition fallback is ambiguous or incomplete; "
                        "specify a definition file and line."
                    )
            label = "Syntactic fallback (name matches; not proof of semantic identity)"
            if incomplete:
                label += "; partial index: " + "; ".join(incomplete[:3])
            excluded = 0
        messages = [label + f": {len(sites)} result(s)."]
        messages.extend(self._describe(site) for site in sites[:40])
        if len(sites) > 40:
            messages.append(f"Showing 40 of {len(sites)} results.")
        if excluded:
            messages.append(f"{excluded} external, ignored, or invalid locations excluded.")
        if action == "nav_def":
            paths = sorted({site.path for site in sites})
            if len(paths) == 1:
                messages.append(self._page(paths[0]))
            elif len(paths) > 1:
                messages.append("Multiple target files; use /page_l1 to select one.")
        return "\n".join(messages), len(sites)

    def execute(self, actions, manual=False):
        if not 1 <= len(actions) <= 3:
            return "Navigation rejected: use one to three actions per response."
        self.allowed = None
        deadline = time.monotonic() + self.timeout
        messages = []
        for action, args in actions:
            status = "succeeded"
            try:
                if action not in ACTIONS:
                    raise NavigationError("Unknown navigation action.")
                if action == "run_command":
                    from aider.terminal_tools import run_terminal

                    started = time.monotonic()
                    message = run_terminal(self.coder, args[0])
                    deadline += time.monotonic() - started
                elif action == "list_files":
                    files, complete = self.workspace_files(args[0] if args else "*", deadline)
                    message = "\n".join(files[:80]) or "No matching workspace files."
                    if not complete or len(files) > 80:
                        message += "\nListing limited; refine the glob to see other files."
                    if not files:
                        status = "empty"
                elif action == "read_file":
                    message = self._read_reference(self._path(args[0], manual, read_only=True))
                elif action == "page_l1":
                    message = self._page(self._path(args[0], manual), args[1:])
                elif action == "demote_l2":
                    path = self._path(args[0], manual)
                    self.coder.context_pager.demote_file(self.coder, path)
                    message = f'Compressed "{self._name(path)}" into a read-only L2 interface.'
                else:
                    message, matched = self._lookup(action, args[0], deadline, manual)
                    if not matched:
                        status = "empty"
            except (NavigationError, OSError, UnicodeError, ValueError) as err:
                message = str(err)
                status = "failed"
            self.stats["attempted"] += 1
            self.stats[status] += 1
            self.events.append({"action": action, "status": status,
                                "detail": message.splitlines()[0][:180] if message else ""})
            messages.append(f"/{action}: {message}")
        return "\n\n".join(messages)

    def handle(self, content):
        """None means ordinary edit response; any string means navigation-only feedback."""
        try:
            actions = parse_actions(content)
        except NavigationError as err:
            self.stats["invalid_responses"] += 1
            return f"Navigation rejected: {err} No edits from this response were applied."
        if actions is None:
            return None
        return (
            self.execute(actions) + "\n\nContinue using the current complete files. "
            "No proposed text edit blocks were applied. Terminal commands executed only "
            "as reported above."
        )
