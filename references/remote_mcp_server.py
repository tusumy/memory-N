#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hosted Streamable HTTP entrypoint for Memory Trigger.

This keeps the original stdio server untouched and exposes only tools through a
fresh FastMCP HTTP server. The hosted process mirrors runtime state into the
configured GitHub repository via storage_backend.py.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mcp.server.fastmcp import FastMCP  # noqa: E402
import mcp_server as base  # noqa: E402
from storage_backend import StorageError, build_backend  # noqa: E402

backend = build_backend(_HERE)
_original_resolve_refs = base._resolve_refs
_original_call = base._call
_original_promise_notify_due = base.wp.promise_notify_due


def _backend_refs() -> str:
    return backend.prepare()


def _resolve_refs(refs_dir: str | None, skip_selfcheck: bool = False) -> str:
    if refs_dir:
        return _original_resolve_refs(refs_dir, skip_selfcheck=skip_selfcheck)
    resolved = _backend_refs()
    os.environ["MEMORY_TRIGGER_REFS_DIR"] = resolved
    return _original_resolve_refs(resolved, skip_selfcheck=skip_selfcheck)


def _call(fn, *args, **kwargs) -> dict:
    try:
        result = _original_call(fn, *args, **kwargs)
        backend.flush()
        return result
    except StorageError as exc:
        return {"ok": False, "error": f"StorageError: {exc}"}


def _promise_notify_due(refs: str):
    result = _original_promise_notify_due(refs)
    backend.flush()
    return result


# Patch persistence boundary used by all original tool functions.
base._resolve_refs = _resolve_refs
base._call = _call
base.wp.promise_notify_due = _promise_notify_due

host = os.environ.get("HOST", "0.0.0.0")
port = int(os.environ.get("PORT", "8000"))

# Fresh HTTP server instead of mutating the stdio server after construction.
# This mirrors the deployment shape already proven by amao-aevren and avoids
# advertising MCP prompt capabilities that ChatGPT custom connectors do not need.
mcp = FastMCP(
    "memory-trigger",
    host=host,
    port=port,
    stateless_http=True,
    json_response=True,
)

_TOOL_NAMES = [
    "memory_write",
    "memory_search",
    "memory_forget",
    "memory_stats",
    "memory_decay",
    "memory_vacuum",
    "memory_backup",
    "memory_selfcheck",
    "memory_recover",
    "memory_wellness",
    "memory_deny",
    "memory_expire_check",
    "memory_recall",
    "memory_promise",
    "memory_promise_done",
    "memory_promise_list",
    "memory_promise_check",
    "memory_init",
    "memory_promise_watch_status",
]

for _name in _TOOL_NAMES:
    mcp.tool()(getattr(base, _name))


if __name__ == "__main__":
    try:
        refs = backend.prepare()
        os.environ["MEMORY_TRIGGER_REFS_DIR"] = refs
    except StorageError as exc:
        sys.stderr.write(f"[storage] {exc}\n")

    base._maybe_start_promise_watch()
    mcp.run(transport="streamable-http")
