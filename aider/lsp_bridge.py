"""Bounded, lazy LSP queries backed by an asynchronous stdio reader.

Empty results mean unknown, not proof that a syntactic graph edge is wrong.
"""

import atexit
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SERVERS = {
    "python": ("pyright-langserver", "--stdio"),
    "typescript": ("typescript-language-server", "--stdio"),
    "rust": ("rust-analyzer",),
    "go": ("gopls",),
}
LANGUAGES = {
    ".py": ("python", "python"),
    ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"),
    ".js": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"),
    ".rs": ("rust", "rust"),
    ".go": ("go", "go"),
}


@dataclass(frozen=True, order=True)
class Location:
    uri: str
    line: int
    character: int


@dataclass(frozen=True)
class Diagnostic:
    line: int
    character: int
    severity: int
    message: str
    code: object = None


def position(text):
    """LSP UTF-16 position at EOF; a trailing newline starts an empty line."""
    return {
        "line": text.count("\n"),
        "character": len(text.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2,
    }


def incremental_change(old, new):
    """Replace just the differing whole lines, with an exact UTF-16 EOF bound."""
    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(before), len(after)) - prefix
        and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]
    ):
        suffix += 1
    old_end = len(before) - suffix
    end = position(old) if old_end == len(before) else {"line": old_end, "character": 0}
    return {
        "range": {"start": {"line": prefix, "character": 0}, "end": end},
        "text": "".join(after[prefix : len(after) - suffix if suffix else len(after)]),
    }


def locations(result):
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    found = set()
    for item in result:
        try:
            uri = item.get("targetUri", item.get("uri"))
            start = item.get("targetSelectionRange", item.get("range"))["start"]
            line, col = start["line"], start["character"]
            if (
                isinstance(uri, str)
                and ":" in uri
                and type(line) is int
                and type(col) is int
                and line >= 0
                and col >= 0
            ):
                found.add(Location(uri, line, col))
        except (AttributeError, KeyError, TypeError):
            continue
    return sorted(found)


class _Server:
    def __init__(self, command, root, stats):
        self.root = root
        self.stats = stats
        self.pending = {}
        self.lock = threading.RLock()
        self.outgoing = queue.Queue(maxsize=8)
        self.failed = False
        self.next_id = 0
        self.documents = {}
        self.diagnostics = {}
        self.diagnostic_ready = threading.Event()
        self.closed = False
        self.capabilities = {}
        self.last_error = None
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            **options,
        )
        self.writer = threading.Thread(
            target=self._write_loop, daemon=True, name="aider-lsp-writer"
        )
        self.writer.start()
        self.reader = threading.Thread(
            target=self._read, daemon=True, name="aider-lsp-reader"
        )
        self.reader.start()

    def _write(self, message):
        data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode(
            "utf-8"
        )
        if self.closed or self.failed:
            raise OSError("Language server closed")
        try:
            self.outgoing.put_nowait(
                f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
            )
        except queue.Full:
            raise OSError("Language server write queue is full") from None

    def _write_loop(self):
        try:
            while not self.closed:
                data = self.outgoing.get()
                if data is None:
                    return
                data = memoryview(data)
                while data:
                    count = self.process.stdin.write(data)
                    if not count:
                        raise OSError("Language server stdin disconnected")
                    data = data[count:]
                self.process.stdin.flush()
        except (OSError, ValueError):
            self.failed = True
            self._fail_pending()

    def _fail_pending(self):
        with self.lock:
            for waiting in self.pending.values():
                try:
                    waiting.put_nowait(None)
                except queue.Full:
                    pass

    def notify(self, method, params=None):
        self._write({"method": method, "params": params or {}})

    def request(self, method, params, deadline):
        if time.monotonic() >= deadline or self.closed:
            return None
        response = queue.Queue(maxsize=1)
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            self.pending[request_id] = response
        try:
            self._write({"id": request_id, "method": method, "params": params})
            message = response.get(timeout=max(0.001, deadline - time.monotonic()))
            if not message or "error" in message:
                self.last_error = message.get("error") if message else None
                self.stats["rpc_errors"] += 1
                return None
            return message.get("result")
        except (OSError, ValueError, queue.Empty):
            self.stats["timeouts_or_disconnects"] += 1
            return None
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def _read(self):
        try:
            while not self.closed:
                headers = {}
                while True:
                    line = self.process.stdout.readline(8193)
                    if not line or len(line) > 8192:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    name, value = line.decode("ascii").split(":", 1)
                    headers[name.lower()] = value.strip()
                length = int(headers["content-length"])
                if not 0 <= length <= 16 * 1024 * 1024:
                    return
                data = bytearray()
                while len(data) < length:
                    part = self.process.stdout.read(length - len(data))
                    if not part:
                        return
                    data.extend(part)
                message = json.loads(data)
                if not isinstance(message, dict):
                    return
                if "method" in message:
                    self._server_message(message)
                else:
                    with self.lock:
                        waiting = self.pending.get(message.get("id"))
                    if waiting:
                        waiting.put_nowait(message)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, queue.Full):
            self.stats["protocol_errors"] += 1
        finally:
            self.failed = True
            self._fail_pending()

    def _server_message(self, message):
        method, params = message["method"], message.get("params") or {}
        if not isinstance(params, dict):
            return
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri")
            if isinstance(uri, str) and isinstance(params.get("diagnostics"), list):
                with self.lock:
                    self.diagnostics[uri] = (
                        params.get("version"),
                        params["diagnostics"],
                    )
                self.diagnostic_ready.set()
        if "id" not in message:
            return
        if method == "workspace/configuration":
            result = [{} for _ in params.get("items", [])]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root.as_uri(), "name": self.root.name}]
        elif method in (
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
        ):
            result = None
        elif method == "workspace/applyEdit":
            result = {
                "applied": False,
                "failureReason": "Aider does not accept server-initiated edits",
            }
        else:
            self._write(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Unsupported client method"},
                }
            )
            return
        self._write({"id": message["id"], "result": result})

    def sync(self, path, language):
        text = path.read_bytes().decode("utf-8")
        uri = path.as_uri()
        previous = self.documents.get(uri)
        if previous and previous[1] == text:
            return True
        sync = self.capabilities.get("textDocumentSync", 0)
        kind = sync.get("change", 0) if isinstance(sync, dict) else sync
        if previous:
            if kind not in (1, 2):
                return False
            version = previous[0] + 1
            change = (
                incremental_change(previous[1], text) if kind == 2 else {"text": text}
            )
            method = "textDocument/didChange"
            params = {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [change],
            }
        else:
            version = 1
            method = "textDocument/didOpen"
            params = {
                "textDocument": {
                    "uri": uri,
                    "languageId": language,
                    "version": version,
                    "text": text,
                }
            }
        self.documents[uri] = (version, text)
        with self.lock:
            self.diagnostics.pop(uri, None)
        self.diagnostic_ready.clear()
        self.notify(method, params)
        return True

    def close(self, graceful=True):
        if self.closed:
            return
        if graceful and self.process.poll() is None:
            self.request("shutdown", None, time.monotonic() + 0.1)
            try:
                self.notify("exit")
            except OSError:
                pass
        self.closed = True
        try:
            self.outgoing.put_nowait(None)
        except queue.Full:
            pass
        if self.process.poll() is None:
            if os.name == "nt":
                # npm/Python launchers can own child processes; only terminate our tree.
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=0.5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    self.process.terminate()
            else:
                self.process.terminate()
        try:
            self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=0.2)
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except OSError:
                pass


class LSPBridge:
    def __init__(self, root, timeout=2.0, commands=None, initialization_options=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LSP timeout must be positive and finite")
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.commands = dict(SERVERS if commands is None else commands)
        # The TS syntax-only server can return import aliases while semantic
        # indexing is incomplete. Headless graph queries require the full server.
        self.initialization_options = {
            "typescript": {
                "hostInfo": "aider",
                "disableAutomaticTypingAcquisition": True,
                "tsserver": {"useSyntaxServer": "never"},
            }
        }
        for name, options in (initialization_options or {}).items():
            defaults = self.initialization_options.setdefault(name, {})
            for key, value in options.items():
                if isinstance(value, dict) and isinstance(defaults.get(key), dict):
                    defaults[key] = {**defaults[key], **value}
                else:
                    defaults[key] = value
        self.session_id = uuid.uuid4().hex
        self.stats = Counter()
        self.errors = {}
        self.deadline = None
        self.servers = {}
        self.unavailable = set()
        self.closed = False
        self.lock = threading.RLock()
        atexit.register(self.close)

    def _path(self, rel_fname):
        path = (self.root / rel_fname).resolve()
        path.relative_to(self.root)
        return path

    @contextmanager
    def _query_lock(self, deadline):
        acquired = self.lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            self.stats["lock_timeouts"] += 1
        try:
            yield acquired
        finally:
            if acquired:
                self.lock.release()

    def _command(self, language):
        command = self.commands.get(language)
        if not command:
            return None
        executable = shutil.which(str(command[0]))
        if not executable:
            executable = shutil.which(
                str(command[0]), path=str(Path(sys.executable).parent)
            )
        if not executable:
            return None
        result = [executable, *command[1:]]
        if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
            launcher = subprocess.list2cmdline(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
            )
            # cmd /s /c needs an outer quote pair around an already-quoted
            # executable path. Passing this as a list would escape those quotes.
            return launcher + ' "' + subprocess.list2cmdline(result) + '"'
        return result

    def _server(self, path, deadline):
        description = LANGUAGES.get(path.suffix.lower())
        if not description or self.closed:
            return None
        name, language = description
        if name in self.unavailable:
            return None
        if name not in self.servers:
            command = self._command(name)
            if not command:
                self.unavailable.add(name)
                self.stats["missing_servers"] += 1
                return None
            server = None
            try:
                server = _Server(command, self.root, self.stats)
                result = server.request(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "initializationOptions": self.initialization_options.get(
                            name, {}
                        ),
                        "rootUri": self.root.as_uri(),
                        "rootPath": str(self.root),
                        "clientInfo": {"name": "Better Aider"},
                        "workspaceFolders": [
                            {"uri": self.root.as_uri(), "name": self.root.name}
                        ],
                        "capabilities": {
                            "general": {"positionEncodings": ["utf-16"]},
                            "workspace": {
                                "configuration": True,
                                "workspaceFolders": True,
                            },
                            "textDocument": {
                                "definition": {"linkSupport": True},
                                "publishDiagnostics": {"versionSupport": True},
                            },
                        },
                    },
                    deadline,
                )
                capabilities = (
                    result.get("capabilities") if isinstance(result, dict) else None
                )
                if (
                    not isinstance(capabilities, dict)
                    or capabilities.get("positionEncoding", "utf-16") != "utf-16"
                ):
                    raise ValueError(
                        "Server initialization unavailable or unsupported position encoding"
                    )
                server.capabilities = capabilities
                server.notify("initialized")
                self.servers[name] = server
                self.stats["started_servers"] += 1
            except (OSError, ValueError) as exc:
                if server:
                    self.errors[name] = server.last_error or str(exc)
                    server.close(graceful=False)
                else:
                    self.errors[name] = str(exc)
                self.unavailable.add(name)
                self.stats["startup_failures"] += 1
                return None
        server = self.servers[name]
        if server.failed or server.process.poll() is not None:
            self.stats["crashed_servers"] += 1
            self.unavailable.add(name)
            return None
        if not server.sync(path, language):
            return None
        return server

    def _query(self, method, rel_fname, line=0, col=0):
        if type(line) is not int or type(col) is not int or min(line, col) < 0:
            return []
        deadline = min(time.monotonic() + self.timeout, self.deadline or float("inf"))
        with self._query_lock(deadline) as acquired:
            if not acquired:
                return []
            try:
                if time.monotonic() >= deadline:
                    return []
                path = self._path(rel_fname)
                server = self._server(path, deadline)
                if not server or time.monotonic() >= deadline:
                    return []
                self.stats["queries"] += 1
                params = {
                    "textDocument": {"uri": path.as_uri()},
                    "position": {"line": line, "character": col},
                }
                if method == "textDocument/references":
                    params["context"] = {"includeDeclaration": False}
                return locations(server.request(method, params, deadline))
            except (OSError, ValueError, UnicodeError):
                self.stats["fallbacks"] += 1
                return []

    def resolve_definition(self, rel_fname, line, col):
        return self._query("textDocument/definition", rel_fname, line, col)

    def find_references(self, rel_fname, line, col):
        return self._query("textDocument/references", rel_fname, line, col)

    def workspace_changed(self, files):
        """Invalidate open buffers as well as disk-based dependency indexes."""
        for server in self.servers.values():
            changes = []
            for fname in files:
                try:
                    path = self._path(fname)
                    changes.append(
                        {"uri": path.as_uri(), "type": 2 if path.exists() else 3}
                    )
                    if path.as_uri() in server.documents and path.exists():
                        language = LANGUAGES.get(path.suffix.lower())
                        if language:
                            server.sync(path, language[1])
                except (OSError, ValueError, UnicodeError):
                    continue
            if changes:
                try:
                    server.notify(
                        "workspace/didChangeWatchedFiles", {"changes": changes}
                    )
                except OSError:
                    pass

    def get_diagnostics(self, rel_fname):
        deadline = min(time.monotonic() + self.timeout, self.deadline or float("inf"))
        with self._query_lock(deadline) as acquired:
            if not acquired:
                return []
            try:
                path = self._path(rel_fname)
                server = self._server(path, deadline)
                if not server:
                    return []
                uri = path.as_uri()
                server.diagnostic_ready.wait(
                    timeout=max(0, min(0.1, deadline - time.monotonic()))
                )
                with server.lock:
                    version, rows = server.diagnostics.get(uri, (None, []))
                if version is not None and version != server.documents[uri][0]:
                    return []
                result = []
                for item in rows:
                    try:
                        start = item["range"]["start"]
                        severity = item.get("severity", 3)
                        if (
                            type(start["line"]) is int
                            and type(start["character"]) is int
                            and min(start["line"], start["character"]) >= 0
                            and type(severity) is int
                            and 1 <= severity <= 4
                            and isinstance(item["message"], str)
                        ):
                            result.append(
                                Diagnostic(
                                    start["line"],
                                    start["character"],
                                    severity,
                                    item["message"],
                                    item.get("code"),
                                )
                            )
                    except (KeyError, TypeError):
                        continue
                return sorted(
                    result,
                    key=lambda item: (
                        item.line,
                        item.character,
                        item.severity,
                        item.message,
                    ),
                )
            except (OSError, ValueError, UnicodeError):
                return []

    def close(self):
        if self.closed:
            return
        self.closed = True
        for server in self.servers.values():
            server.close()
        atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
