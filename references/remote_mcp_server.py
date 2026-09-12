#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hosted Streamable HTTP entrypoint for Memory Trigger.

This keeps the original stdio server untouched and wraps it for cloud hosting.
When MEMORY_TRIGGER_STORAGE_BACKEND=github, runtime files are mirrored into
`tusumy/amao-aevren` (or another configured repository) through
storage_backend.py.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import mcp_server as base  # noqa: E402
from storage_backend import StorageError, build_backend  # noqa: E402


backend = build_backend(_HERE)
_original_resolve_refs = base._resolve_refs
_original_call = base._call
_original_promise_notify_due = base.wp.promise_notify_due


def _backend_refs() -> str:
    return backend.prepare()


def _resolve_refs(refs_dir: str | None, skip_selfcheck: bool = False) -> str:
    # Explicit refs_dir always wins. Hosted/default calls use the configured
    # backend so the existing 19 tools do not need to be rewritten.
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
    # The original promise watcher bypasses _call(), so persist its changes too.
    result = _original_promise_notify_due(refs)
    backend.flush()
    return result


# Monkey-patch only the deployment boundary. Tool implementations and memory
# semantics stay exactly where upstream Memory Trigger expects them.
base._resolve_refs = _resolve_refs
base._call = _call
base.wp.promise_notify_due = _promise_notify_due


host = os.environ.get("HOST", "0.0.0.0")
port = int(os.environ.get("PORT", "8000"))

# FastMCP v1 stores its HTTP serving config on settings. The stdio entrypoint
# remains untouched; this file is exclusively the hosted HTTP entrypoint.
base.mcp.settings.host = host
base.mcp.settings.port = port
base.mcp.settings.stateless_http = True
base.mcp.settings.json_response = True


if __name__ == "__main__":
    # Do not fail process startup only because a secret is absent. Tool calls
    # will return a clear StorageError until the GitHub token is configured.
    try:
        refs = backend.prepare()
        os.environ["MEMORY_TRIGGER_REFS_DIR"] = refs
    except StorageError as exc:
        sys.stderr.write(f"[storage] {exc}\n")

    base._maybe_start_promise_watch()
    base.mcp.run(transport="streamable-http")
