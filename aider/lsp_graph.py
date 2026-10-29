"""Semantic evidence for repo-map edges, with workspace-scoped caches and one time budget."""

import hashlib
import math
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from diskcache import Cache, Timeout

from aider.lsp_bridge import LSPBridge


def uri_key(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != "file":
        return uri
    path = url2pathname(parsed.path)
    # Pyright may percent-encode a Windows drive colon. Decode it once, and
    # remove the extra leading slash left by nturl2path in that case.
    if (
        os.name == "nt"
        and len(path) >= 3
        and path[0] in ("/", "\\")
        and path[1].isalpha()
        and path[2] == ":"
    ):
        path = path[1:]
    if parsed.netloc and parsed.netloc != "localhost":
        path = "//" + parsed.netloc + path
    return os.path.normcase(os.path.normpath(path))


class SemanticGraph:
    def __init__(self, root, timeout=2.0, bridge=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LSP timeout must be positive and finite")
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.bridge = bridge or LSPBridge(self.root, timeout=timeout)
        self.owns_bridge = bridge is None
        self.cache = None
        self.memory_cache = {}
        self.file_hashes = {}
        self.position_cache = {}
        self.resolutions = {}
        self.stats = Counter()

    def _snapshot(self, fnames, deadline):
        hashes = {}
        for fname in sorted(fnames):
            if time.monotonic() >= deadline:
                return None
            path = Path(fname).resolve()
            try:
                relative = str(path.relative_to(self.root))
                hasher = hashlib.sha256()
                with path.open("rb") as source:
                    while True:
                        chunk = source.read(256 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        if time.monotonic() >= deadline:
                            return None
                hashes[relative] = hasher.hexdigest()
            except (OSError, ValueError):
                continue
        changed = sorted(
            key
            for key in hashes.keys() | self.file_hashes.keys()
            if hashes.get(key) != self.file_hashes.get(key)
        )
        if changed and (self.file_hashes or getattr(self.bridge, "servers", None)):
            self.bridge.workspace_changed(changed)
            self.memory_cache.clear()
        self.file_hashes = hashes
        self.position_cache = {
            key: value
            for key, value in self.position_cache.items()
            if key[0] in hashes and key[1] == hashes[key[0]]
        }
        digest = hashlib.sha256()
        for relative, content in sorted(hashes.items()):
            digest.update(
                relative.encode("utf-8") + b"\0" + content.encode("ascii") + b"\0"
            )
        return digest.hexdigest()

    def _cached(self, key):
        if key in self.memory_cache:
            return self.memory_cache[key]
        try:
            if self.cache is None:
                self.cache = Cache(self.root / ".aider.lsp.cache.v1", timeout=0.01)
            return self.cache.get(key)
        except (sqlite3.Error, OSError, Timeout):
            self.stats["cache_errors"] += 1
            return None

    def _store(self, key, result):
        self.memory_cache[key] = result
        try:
            if self.cache is not None:
                self.cache.set(key, result, expire=3600)
        except (sqlite3.Error, OSError, Timeout):
            self.stats["cache_errors"] += 1

    def prepare(
        self, fnames, defines, references, chat_fnames, mentioned_fnames, get_positions
    ):
        self.stats = Counter()
        self.resolutions = {}
        deadline = time.monotonic() + self.timeout
        old_deadline = getattr(self.bridge, "deadline", None)
        self.bridge.deadline = deadline
        try:
            workspace = self._snapshot(fnames, deadline)
            if workspace is None:
                self.stats["budget_exhausted"] += 1
                return
            files = {
                str(Path(fname).resolve().relative_to(self.root)): fname
                for fname in fnames
                if Path(fname).resolve().is_relative_to(self.root)
            }
            relevant = {}
            for symbol in sorted(set(defines) & set(references)):
                for relative in set(references[symbol]):
                    relevant.setdefault(relative, set()).add(symbol)
            chat = {
                str(Path(fname).resolve().relative_to(self.root))
                for fname in chat_fnames
                if Path(fname).resolve().is_relative_to(self.root)
            }
            ordered = sorted(
                relevant,
                key=lambda name: (name not in chat, name not in mentioned_fnames, name),
            )
            queries = 0
            for relative in ordered:
                if time.monotonic() >= deadline or queries >= 64:
                    self.stats["budget_exhausted"] += 1
                    return
                if relative not in files or relative not in self.file_hashes:
                    continue
                position_key = (relative, self.file_hashes[relative])
                if position_key not in self.position_cache:
                    self.position_cache[position_key] = get_positions(
                        files[relative], relative
                    )
                positions = self.position_cache[position_key]
                # Query ambiguous names first, while retaining deterministic traversal.
                symbols = sorted(
                    relevant[relative], key=lambda name: (len(defines[name]) < 2, name)
                )
                for symbol in symbols:
                    sites = positions.get(symbol, [])
                    evidence = [None] * len(sites)
                    for site_index, (line, col) in enumerate(sites):
                        if time.monotonic() >= deadline or queries >= 64:
                            self.stats["budget_exhausted"] += 1
                            break
                        key = (
                            "definition",
                            1,
                            str(self.root),
                            getattr(self.bridge, "session_id", id(self.bridge)),
                            workspace,
                            relative,
                            self.file_hashes[relative],
                            symbol,
                            line,
                            col,
                        )
                        result = self._cached(key)
                        if result is None:
                            queries += 1
                            result = self.bridge.resolve_definition(relative, line, col)
                            if result:
                                result = tuple(
                                    (item.uri, item.line, item.character)
                                    for item in result
                                )
                                self._store(key, result)
                        else:
                            self.stats["cache_hits"] += 1
                        if result:
                            evidence[site_index] = tuple(
                                uri_key(item[0]) for item in result
                            )
                            self.stats["resolved_sites"] += 1
                        else:
                            self.stats["unknown_sites"] += 1
                    if evidence:
                        self.resolutions[(relative, symbol)] = evidence
        except Exception:
            # This optional enrichment must never prevent the stock map from being built.
            self.stats["fallback_errors"] += 1
            self.resolutions = {}
        finally:
            self.bridge.deadline = old_deadline
            self.stats["queries"] = queries if "queries" in locals() else 0

    def multiplier(self, referencer, definer, symbol):
        evidence = self.resolutions.get((referencer, symbol))
        if not evidence:
            return 1.0
        target = uri_key((self.root / definer).resolve().as_uri())
        factors = [
            1.0 if item is None else 2.5 if target in item else 0.05
            for item in evidence
        ]
        return sum(factors) / len(factors)

    def close(self):
        if self.owns_bridge:
            self.bridge.close()
        if self.cache is not None:
            self.cache.close()
