#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Storage adapter used by the remote Memory Trigger MCP wrapper.

The original Memory Trigger core remains filesystem-first. This module adds a
small persistence boundary around that filesystem so a hosted process can keep
its state in a GitHub repository instead of trusting an ephemeral disk.

Backends:
- local: existing behavior; MEMORY_TRIGGER_REFS_DIR points at a real directory.
- github: mirrors the runtime state directory to a path in a GitHub repository.

The GitHub backend intentionally lives outside write_pipeline.py so upstream
Memory Trigger logic can keep evolving without being coupled to deployment.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


_SKIP_NAMES = {"__pycache__", ".git"}
_SKIP_SUFFIXES = {".lock", ".tmp"}


class StorageError(RuntimeError):
    pass


class StorageBackend:
    def prepare(self) -> str:
        raise NotImplementedError

    def flush(self) -> None:
        return None


class LocalBackend(StorageBackend):
    def __init__(self, fallback_dir: str):
        self.refs_dir = os.environ.get("MEMORY_TRIGGER_REFS_DIR") or fallback_dir

    def prepare(self) -> str:
        Path(self.refs_dir).mkdir(parents=True, exist_ok=True)
        return self.refs_dir


class GitHubArchiveBackend(StorageBackend):
    """Mirror Memory Trigger runtime files into a GitHub repository path.

    This is an optimistic, small-state mirror. It is intentionally not a
    database replacement. The memory engine still reads/writes local files;
    this adapter pulls before calls and pushes changed files after calls.
    """

    def __init__(self):
        self.repo = os.environ.get("MEMORY_TRIGGER_GITHUB_REPO", "tusumy/amao-aevren")
        self.branch = os.environ.get("MEMORY_TRIGGER_GITHUB_BRANCH", "main")
        self.prefix = os.environ.get("MEMORY_TRIGGER_GITHUB_PREFIX", "记忆引擎/state").strip("/")
        self.token = os.environ.get("MEMORY_TRIGGER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.cache_dir = Path(os.environ.get("MEMORY_TRIGGER_CACHE_DIR", "/tmp/memory-trigger-state"))
        self.pull_ttl = float(os.environ.get("MEMORY_TRIGGER_GITHUB_PULL_TTL", "3"))
        self._last_pull = 0.0
        self._lock = threading.RLock()
        self._remote_sha: dict[str, str] = {}

    def _require_token(self) -> None:
        if not self.token:
            raise StorageError(
                "GitHub storage backend is enabled but no token is configured. "
                "Set MEMORY_TRIGGER_GITHUB_TOKEN (or GITHUB_TOKEN) with Contents read/write access."
            )

    def _api(self, path: str) -> str:
        safe = urllib.parse.quote(path, safe="/")
        return f"https://api.github.com/repos/{self.repo}/contents/{safe}"

    def _request(self, method: str, url: str, payload: dict | None = None):
        self._require_token()
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "memory-trigger-remote")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                return json.loads(body.decode("utf-8")) if body else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise StorageError(f"GitHub API {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise StorageError(f"GitHub API unavailable: {exc}") from exc

    def _remote_path(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def _walk_remote(self, path: str) -> list[dict]:
        url = self._api(path) + f"?ref={urllib.parse.quote(self.branch)}"
        node = self._request("GET", url)
        if node is None:
            return []
        if isinstance(node, dict) and node.get("type") == "file":
            return [node]
        out: list[dict] = []
        for item in node if isinstance(node, list) else []:
            if item.get("type") == "dir":
                out.extend(self._walk_remote(item["path"]))
            elif item.get("type") == "file":
                out.append(item)
        return out

    def _pull(self) -> None:
        now = time.monotonic()
        if now - self._last_pull < self.pull_ttl:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for item in self._walk_remote(self.prefix):
            remote_path = item["path"]
            rel = remote_path[len(self.prefix):].lstrip("/") if self.prefix else remote_path
            if not rel:
                continue
            detail = self._request(
                "GET",
                self._api(remote_path) + f"?ref={urllib.parse.quote(self.branch)}",
            )
            if not detail or detail.get("type") != "file":
                continue
            raw = base64.b64decode(detail.get("content", "").replace("\n", ""))
            target = self.cache_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            self._remote_sha[rel] = detail.get("sha", "")
        self._last_pull = now

    def _iter_local_files(self):
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.cache_dir).parts
            if any(part in _SKIP_NAMES for part in rel_parts):
                continue
            if path.suffix in _SKIP_SUFFIXES:
                continue
            yield path, path.relative_to(self.cache_dir).as_posix()

    def _get_remote_meta(self, rel: str):
        remote_path = self._remote_path(rel)
        return self._request(
            "GET",
            self._api(remote_path) + f"?ref={urllib.parse.quote(self.branch)}",
        )

    def _push_file(self, path: Path, rel: str) -> None:
        raw = path.read_bytes()
        meta = self._get_remote_meta(rel)
        if meta and meta.get("type") == "file":
            remote_raw = base64.b64decode(meta.get("content", "").replace("\n", ""))
            if remote_raw == raw:
                self._remote_sha[rel] = meta.get("sha", "")
                return
        payload = {
            "message": f"memory-state: sync {rel}",
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": self.branch,
        }
        if meta and meta.get("sha"):
            payload["sha"] = meta["sha"]
        result = self._request("PUT", self._api(self._remote_path(rel)), payload)
        if result and result.get("content"):
            self._remote_sha[rel] = result["content"].get("sha", "")

    def prepare(self) -> str:
        with self._lock:
            self._require_token()
            self._pull()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            return str(self.cache_dir)

    def flush(self) -> None:
        with self._lock:
            self._require_token()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            for path, rel in self._iter_local_files() or []:
                self._push_file(path, rel)


def build_backend(fallback_dir: str) -> StorageBackend:
    kind = (os.environ.get("MEMORY_TRIGGER_STORAGE_BACKEND") or "local").strip().lower()
    if kind in {"", "local", "filesystem", "file"}:
        return LocalBackend(fallback_dir)
    if kind in {"github", "github_archive", "archive"}:
        return GitHubArchiveBackend()
    raise StorageError(f"Unknown MEMORY_TRIGGER_STORAGE_BACKEND={kind!r}")
