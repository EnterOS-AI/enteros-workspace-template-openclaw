#!/usr/bin/env python3
"""A minimal REAL stdio MCP server used by the concierge live-e2e test.

Answers the exact ``initialize`` -> ``tools/list`` JSON-RPC handshake the
concierge (and ``assert_concierge_mcp_loaded.py``) drive, advertising whatever
tool names are passed in ``FAKE_MCP_TOOLS`` (comma-separated). Set
``FAKE_MCP_DEAD=1`` to answer ``initialize`` then hang on ``tools/list`` — the
"declared-but-dead" server the live check must treat as not-loaded.

This lets the e2e exercise a GENUINE MCP wire handshake (not a mock) with no
private ``@molecule-ai/mcp-server`` dependency, so it runs hermetically in CI.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    tools = [t for t in (os.environ.get("FAKE_MCP_TOOLS", "")).split(",") if t]
    dead = os.environ.get("FAKE_MCP_DEAD") == "1"
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake-mcp", "version": "1"},
                "capabilities": {"tools": {}}}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if dead:
                time.sleep(3600)  # hang: declared-but-dead
                return 0
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                "tools": [{"name": n, "description": n, "inputSchema": {"type": "object"}} for n in tools]}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
