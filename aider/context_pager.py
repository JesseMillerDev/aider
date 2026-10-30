"""Idle-file context paging with optional size caps and read-only interfaces."""

import ast
import copy
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

INTERFACE_PREFIX = (
    "Read-only interface summaries (L2). Implementation bodies are omitted. "
    "These are not complete file contents. Request a full file before editing it; "
    "never use an interface summary as replacement file contents.\n\n"
)


def _doc(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _python_interface(source):
    tree = ast.parse(source, type_comments=True)

    def body(nodes, class_scope=False):
        result = []
        for index, original in enumerate(nodes):
            node = copy.deepcopy(original)
            if index == 0 and _doc(node):
                result.append(node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                result.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kept = [node.body[0]] if node.body and _doc(node.body[0]) else []
                kept += [
                    copy.deepcopy(item)
                    for item in ast.walk(node)
                    if isinstance(item, (ast.Import, ast.ImportFrom))
                ]
                # Attribute annotations declared in __init__ are useful contracts too.
                for item in ast.walk(node):
                    if (
                        isinstance(item, ast.AnnAssign)
                        and isinstance(item.target, ast.Attribute)
                        and isinstance(item.target.value, ast.Name)
                        and item.target.value.id in ("self", "cls")
                    ):
                        annotation = copy.deepcopy(item)
                        annotation.value = None
                        kept.append(annotation)
                node.body = kept + [ast.Expr(value=ast.Constant(value=Ellipsis))]
                result.append(node)
            elif isinstance(node, ast.ClassDef):
                node.body = body(node.body, True) or [ast.Pass()]
                result.append(node)
            elif isinstance(node, ast.AnnAssign):
                node.value = None
                result.append(node)
            elif isinstance(node, ast.Assign):
                names = [
                    target.id for target in node.targets if isinstance(target, ast.Name)
                ]
                if class_scope or any(
                    not name.startswith("_") or name == "__all__" or name.isupper()
                    for name in names
                ):
                    if not isinstance(
                        node.value,
                        (
                            ast.Constant,
                            ast.List,
                            ast.Tuple,
                            ast.Set,
                            ast.Dict,
                            ast.Name,
                            ast.Attribute,
                            ast.Subscript,
                            ast.BinOp,
                        ),
                    ):
                        node.value = ast.Constant(value=Ellipsis)
                    result.append(node)
            elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                result.append(node)
            elif isinstance(node, ast.If):
                node.body, node.orelse = body(node.body, class_scope), body(
                    node.orelse, class_scope
                )
                if node.body or node.orelse:
                    node.body = node.body or [ast.Pass()]
                    result.append(node)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                node.body = body(node.body, class_scope)
                node.orelse = body(node.orelse, class_scope)
                node.finalbody = body(node.finalbody, class_scope)
                for handler in node.handlers:
                    handler.body = body(handler.body, class_scope) or [ast.Pass()]
                if node.body:
                    result.append(node)
        return result

    tree.body = body(tree.body)
    ast.fix_missing_locations(tree)
    text = ast.unparse(tree)
    # Keep the stub parseable while making the omitted bodies explicit.
    return (
        re.sub(r"(?m)^(\s*)\.\.\.$", r"\1# ⋮ implementation omitted\n\1...", text)
        + "\n"
    )


def _other_interface(path, source):
    """Keep Tree-sitter declaration headers and class members, eliding function bodies."""
    from grep_ast import filename_to_lang
    from grep_ast.tsl import get_parser

    lang = filename_to_lang(str(path))
    if not lang:
        return None
    data = source.encode("utf-8")
    tree = get_parser(lang).parse(data)
    if tree.root_node.has_error:
        return None
    edits = []

    def visit(node):
        function = any(
            word in node.type for word in ("function", "method", "constructor")
        )
        block = node.child_by_field_name("body") if function else None
        if block is not None and block.type in (
            "statement_block",
            "block",
            "compound_statement",
        ):
            original = data[block.start_byte : block.end_byte]
            if original.startswith(b"{") and original.endswith(b"}"):
                edits.append(
                    (
                        block.start_byte + 1,
                        block.end_byte - 1,
                        b" /* ... omitted ... */ ",
                    )
                )
                return
        for child in node.named_children:
            visit(child)

    visit(tree.root_node)
    if not edits:
        return None
    for start, end, replacement in sorted(edits, reverse=True):
        data = data[:start] + replacement + data[end:]
    return data.decode("utf-8")


@dataclass
class FileState:
    digest: str = ""
    last_edited_turn: int = -1
    last_referenced_turn: int = 0
    edited: bool = False
    lint: str = "unknown"
    lint_digest: str = ""
    test: str = "unknown"
    pending: bool = False


class ContextPager:
    def __init__(
        self,
        root,
        token_count,
        max_l1_tokens=None,
        max_l1_files=None,
        max_l2_tokens=None,
        l1_ttl_turns=2,
    ):
        for value in (max_l1_tokens, max_l1_files, max_l2_tokens):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(
                    "Configured context paging caps must be positive integers"
                )
        if type(l1_ttl_turns) is not int or l1_ttl_turns <= 0:
            raise ValueError("Context paging TTL must be a positive integer")
        self.root = Path(root).resolve()
        self.token_count = token_count
        self.max_l1_tokens, self.max_l1_files = max_l1_tokens, max_l1_files
        self.max_l2_tokens, self.l1_ttl_turns = max_l2_tokens, l1_ttl_turns
        self.turn = 0
        self.states, self.stubs, self.sources = {}, {}, {}
        self.forgotten, self.current_refs = set(), set()
        self.l2_paths = set()
        self.test_status = "unknown"
        self.stats = Counter()
        self._last_overflow = None

    def _path(self, path):
        try:
            resolved = (self.root / path).resolve()
            resolved.relative_to(self.root)
            return str(resolved)
        except (OSError, ValueError):
            return None

    def _read(self, coder, path):
        try:
            return coder.io.read_text(path)
        except (OSError, UnicodeError):
            return None

    @staticmethod
    def _digest(source):
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _observe(self, coder, turn):
        changed = set()
        self.sources = {}
        for name in sorted(set(coder.abs_fnames) | set(coder.l2_interface_fnames)):
            path = self._path(name)
            if not path:
                continue
            source = self._read(coder, path)
            if source is None:
                coder.l2_interface_fnames.discard(path)
                continue
            digest = self._digest(source)
            state = self.states.setdefault(path, FileState(last_referenced_turn=turn))
            if state.digest and state.digest != digest:
                state.edited, state.last_edited_turn = True, turn
                if state.lint_digest != digest:
                    state.lint = "unknown" if state.lint != "failed" else "failed"
                if state.test == "clean":
                    state.test = "unknown"
                changed.add(path)
                if path in coder.l2_interface_fnames:
                    coder.abs_fnames.add(path)
                    self.stats["promotions"] += 1
            state.digest = digest
            self.sources[path] = source
        coder.l2_interface_fnames.difference_update(coder.abs_fnames)
        return changed

    def _references(self, text):
        words = {
            word.strip("`'\"*(),.:;![]") for word in text.replace("\\", "/").split()
        }
        basenames = Counter(Path(path).name for path in self.states)
        return {
            path
            for path in self.states
            if Path(path).relative_to(self.root).as_posix() in words
            or (basenames[Path(path).name] == 1 and Path(path).name in words)
        }

    def _count(self, text):
        count = self.token_count(text)
        if type(count) is not int or count < 0:
            raise ValueError("Context pager needs a valid token counter")
        return count

    def render_l2_stub(self, path, source=None):
        path = self._path(path)
        if not path:
            return None
        if source is None:
            try:
                source = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return None
        digest = self._digest(source)
        if path in self.stubs and self.stubs[path][0] == digest:
            return self.stubs[path][1]
        try:
            stub = (
                _python_interface(source)
                if Path(path).suffix in (".py", ".pyi")
                else _other_interface(path, source)
            )
        except (
            SyntaxError,
            ValueError,
            TypeError,
            LookupError,
            RecursionError,
            OSError,
            RuntimeError,
        ):
            stub = None
        self.stubs[path] = digest, stub
        return stub

    def _block(self, path, source):
        try:
            name = Path(path).relative_to(self.root).as_posix()
        except ValueError:
            name = str(path)
        longest = max(
            (len(match.group()) for match in re.finditer(r"`+", source)), default=0
        )
        fence = "`" * max(3, longest + 1)
        return f"{name}\n{fence}\n{source}\n{fence}\n"

    def _l1_tokens(self, coder):
        total = 0
        for path in coder.abs_fnames:
            source = self.sources.get(path)
            if source is None:
                source = self._read(coder, path)
            if source is not None:
                total += self._count(self._block(path, source))
        return total

    def _safe(self, path):
        state = self.states[path]
        return (
            self.test_status != "failed"
            and path not in self.current_refs
            and not state.pending
            and state.lint != "failed"
            and state.test != "failed"
            and (not state.edited or state.lint == "clean")
            and (
                not state.edited
                or self.turn - state.last_edited_turn >= self.l1_ttl_turns
            )
        )

    def _select_l2(self, coder, candidates):
        selected = set()
        blocks = {}
        for path in candidates:
            if path in coder.abs_fnames or path in self.forgotten or path in selected:
                continue
            source = self.sources.get(path)
            if source is None:
                source = self._read(coder, path)
            if source is None:
                continue
            stub = self.render_l2_stub(path, source)
            if stub is None:
                continue
            block = self._block(path, stub)
            # Count the complete assembled block, not an additive estimate per symbol.
            trial = {**blocks, path: block}
            if self.max_l2_tokens is None or (
                self._count(
                    INTERFACE_PREFIX + "\n".join(trial[key] for key in sorted(trial))
                )
                <= self.max_l2_tokens
            ):
                blocks = trial
                selected.add(path)
        return selected

    def _over_l1_budget(self, coder, tokens):
        return (
            self.max_l1_files is not None and len(coder.abs_fnames) > self.max_l1_files
        ) or (self.max_l1_tokens is not None and tokens > self.max_l1_tokens)

    def _demote(self, coder, path):
        previous = set(coder.l2_interface_fnames)
        coder.abs_fnames.remove(path)
        candidates = [path] + sorted(
            previous,
            key=lambda p: (-self.states.get(p, FileState()).last_referenced_turn, p),
        )
        selected = self._select_l2(coder, candidates)
        if path not in selected:
            coder.abs_fnames.add(path)
            return False
        coder.l2_interface_fnames = selected
        self.l2_paths = set(selected)
        self.stats["demotions"] += 1
        return True

    def _dependencies(self, coder):
        modules = {}
        for name in sorted(set(coder.get_all_abs_files()) | set(coder.abs_fnames)):
            path = self._path(name)
            if not path or Path(path).suffix not in (".py", ".pyi"):
                continue
            parts = list(Path(path).relative_to(self.root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            variants = [parts, parts[1:]] if parts and parts[0] == "src" else [parts]
            for variant in variants:
                modules.setdefault(".".join(variant), path)
        deps = set()
        for path in sorted(coder.abs_fnames):
            source = self.sources.get(path)
            if source is None or Path(path).suffix not in (".py", ".pyi"):
                continue
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue
            package = list(Path(path).relative_to(self.root).parent.parts)
            if package and package[0] == "src":
                package = package[1:]
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    anchor = (
                        package[: len(package) - node.level + 1] if node.level else []
                    )
                    if node.level > len(package):
                        continue
                    module = ".".join(anchor + ([node.module] if node.module else []))
                    names = [module] + [
                        f"{module}.{alias.name}".lstrip(".") for alias in node.names
                    ]
                for name in names:
                    if name in modules:
                        deps.add(modules[name])
        return deps - set(coder.abs_fnames) - set(coder.abs_read_only_fnames)

    def _refresh(self, coder, expire=False):
        candidates = sorted(
            (
                path
                for path in coder.abs_fnames
                if path in self.sources and self._safe(path)
            ),
            key=lambda path: (
                max(
                    self.states[path].last_referenced_turn,
                    self.states[path].last_edited_turn,
                ),
                path,
            ),
        )
        l1_tokens = self._l1_tokens(coder)
        for path in candidates:
            state = self.states[path]
            idle = self.turn - max(state.last_referenced_turn, state.last_edited_turn)
            over = self._over_l1_budget(coder, l1_tokens)
            if over or (expire and idle >= self.l1_ttl_turns):
                if self._demote(coder, path):
                    l1_tokens -= self._count(self._block(path, self.sources[path]))
        history = sorted(
            coder.l2_interface_fnames,
            key=lambda p: (-self.states.get(p, FileState()).last_referenced_turn, p),
        )
        selected = self._select_l2(coder, history + sorted(self._dependencies(coder)))
        self.stats["preloads"] += len(selected - coder.l2_interface_fnames)
        coder.l2_interface_fnames = selected
        self.l2_paths = set(selected)
        l1 = self._l1_tokens(coder)
        l2 = self._count(self.interface_content(coder)) if selected else 0
        self.stats["l1_tokens"], self.stats["l2_tokens"] = l1, l2
        self.stats["peak_l1_tokens"] = max(self.stats["peak_l1_tokens"], l1)
        self.stats["peak_l2_tokens"] = max(self.stats["peak_l2_tokens"], l2)
        overflow = self._over_l1_budget(coder, l1)
        marker = tuple(sorted(coder.abs_fnames)) if overflow else None
        if overflow and marker != self._last_overflow:
            self.stats["budget_overflows"] += 1
            coder.io.tool_warning(
                "Context paging: L1 exceeds its budget; active, unchecked, "
                "or unsummarizable files remain in full context."
            )
        self._last_overflow = marker

    def prepare(self, coder, message=None):
        self._observe(coder, self.turn + 1)
        if message is not None:
            self.current_refs = self._references(message)
            for path in self.current_refs:
                self.states[path].last_referenced_turn = self.turn + 1
        self.forgotten.difference_update(coder.abs_fnames)
        self._refresh(coder)

    def step(self, coder):
        """Advance one completed response, including an edit/lint reflection response."""
        self.turn += 1
        self._observe(coder, self.turn)
        response_refs = self._references(coder.partial_response_content or "")
        self.current_refs |= response_refs
        for path in response_refs:
            self.states[path].last_referenced_turn = self.turn
        for path in coder.abs_fnames:
            if path not in self.states:
                continue
            state = self.states[path]
            state.pending = bool(getattr(coder, "reflected_message", None))
            if coder.test_outcome is not None:
                state.test = "clean" if coder.test_outcome else "failed"
        if coder.test_outcome is not None:
            self.record_test(coder, coder.test_outcome)
        self._refresh(coder, expire=True)
        self.current_refs = set()

    def record_lint(self, coder, path, errors, status=None):
        path = self._path(path)
        if not path:
            return
        state = self.states.setdefault(
            path, FileState(last_referenced_turn=self.turn + 1)
        )
        source = self._read(coder, path)
        outcome = status or (
            "failed" if errors else "clean" if source is not None else "unknown"
        )
        if outcome != "unknown" or state.lint != "failed":
            state.lint = outcome
        state.lint_digest = self._digest(source) if source is not None else ""

    def record_test(self, coder, passed):
        if passed is not None or self.test_status != "failed":
            self.test_status = (
                "unknown" if passed is None else "clean" if passed else "failed"
            )
        for path in coder.abs_fnames:
            path = self._path(path)
            if path:
                self.states.setdefault(path, FileState()).test = self.test_status

    def forget(self, coder, path=None):
        paths = (
            set(coder.abs_fnames) | set(coder.l2_interface_fnames)
            if path is None
            else {self._path(path) or str(Path(path).resolve())}
        )
        for name in paths:
            if name:
                coder.abs_fnames.discard(name)
                coder.l2_interface_fnames.discard(name)
                self.forgotten.add(name)
                self.states.pop(name, None)
                self.stubs.pop(name, None)
        self.l2_paths = set(coder.l2_interface_fnames)

    def inherit_state(self, other, coder):
        if self.root != other.root:
            return
        self.turn, self.states = other.turn, copy.deepcopy(other.states)
        self.forgotten = set(other.forgotten)
        self.test_status = other.test_status
        coder.l2_interface_fnames = set(other.l2_paths)
        self.l2_paths = set(other.l2_paths)

    def interface_content(self, coder):
        self.l2_paths = set(coder.l2_interface_fnames)
        blocks = []
        for path in sorted(coder.l2_interface_fnames):
            stub = self.render_l2_stub(path, self.sources.get(path))
            if stub is not None:
                blocks.append(self._block(path, stub))
        return INTERFACE_PREFIX + "\n".join(blocks) if blocks else ""
