#!/usr/bin/env python3
"""Assert an OpenClaw concierge's MCP tool set is LOADED + CALLABLE (not just declared).

WHY THIS EXISTS — the shallow-check flaw
----------------------------------------
The pre-existing openclaw MCP tests (``tests/test_openclaw_mgmt_mcp.py``) are
100% unit-level: they mock ``_list_mcp_tools`` and assert the *render / parse /
publish* logic in isolation. They CANNOT catch a live concierge whose
``~/.openclaw/openclaw.json`` ``mcp.servers`` map is empty (the "mcpServers:NONE"
regression), whose declared management MCP is declared-but-dead, or whose server
advertises the *wrong* tool names. (Concretely: those unit tests even assert a
mocked ``create_workspace`` tool — the REAL ``@molecule-ai/mcp-server`` exposes
``provision_workspace``; a mock can enshrine a name that does not exist.)

This module closes that gap. Run INSIDE (or via ``docker exec`` against) a live
openclaw concierge, it:

  1. reads the concierge's OWN ``~/.openclaw/openclaw.json`` ``mcp.servers`` map;
  2. for EACH declared server, performs a REAL MCP JSON-RPC handshake
     (``initialize`` -> ``notifications/initialized`` -> ``tools/list``) against
     the exact transport the concierge uses — stdio (``{command,args?,env?}``)
     or streamable-http (``{url|type:http}``) — so a declared-but-dead server
     contributes NOTHING;
  3. asserts the UNION of live-advertised tools covers the REQUIRED privileged
     concierge tool set (management + peer + memory), and reports which server
     served each required tool.

A tool that is merely *declared* but whose server never answers ``tools/list``
does NOT count — this is the "callable, not just present/no-op-text" bar the
platform online/degraded gate (core#3082) actually needs.

EXIT CODES
  0  every required tool was advertised by a live-connected server (PASS)
  1  one or more required tools missing / a server unreachable (FAIL)
  2  could not read the concierge config at all (usage / environment error)

This is intentionally dependency-light (stdlib only) so it runs unchanged inside
the minimal concierge container image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# The REQUIRED privileged tool set a healthy openclaw CONCIERGE must expose.
# Grouped by the server that is expected to serve them so a failure report can
# point at the right wiring (management MCP vs the molecule A2A sidecar). The
# names are the GROUND TRUTH verified live against a staging concierge
# (@molecule-ai/mcp-server@1.7.0 + the a2a_mcp_server sidecar), NOT guesses:
#   * provision_workspace  — the org-admin "create a team/workspace" tool a
#     concierge orchestrates with (there is NO `create_workspace`; that mocked
#     name in the old unit test never existed on the real server).
#   * commit_memory/recall_memory — the durable-memory tools; their ABSENCE is
#     what forces the concierge to fall back to editing MEMORY.md by hand.
# ---------------------------------------------------------------------------
REQUIRED_TOOLS: dict[str, list[str]] = {
    "management-mcp (org admin)": [
        "provision_workspace",
        "list_workspaces",
    ],
    "molecule sidecar (peers + memory)": [
        "list_peers",
        "get_workspace_info",
        "commit_memory",
        "recall_memory",
    ],
}


def default_openclaw_json() -> Path:
    return Path(os.path.expanduser("~/.openclaw/openclaw.json"))


def read_declared_servers(openclaw_json: Path) -> dict:
    """Return the ``mcp.servers`` map from openclaw.json ({} if absent/malformed)."""
    try:
        data = json.loads(Path(openclaw_json).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get("servers")
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def _parse_tools_list_body(body: str) -> list[str]:
    """Extract ``result.tools[].name`` from a JSON / NDJSON / SSE tools/list reply."""
    candidates: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if line.startswith("{") or line.startswith("["):
            candidates.append(line)
    stripped = body.strip()
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        result = data.get("result") if isinstance(data, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            names = [
                t["name"] for t in tools
                if isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("name")
            ]
            if names:
                return names
    return []


async def _list_stdio(spec: dict, *, timeout: float) -> list[str]:
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return []
    args = [str(a) for a in (spec.get("args") or [])]
    child_env = os.environ.copy()
    child_env.setdefault("PATH", f"{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin")
    child_env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "concierge-mcp-livecheck", "version": "1"}}}
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    payload = (json.dumps(init) + "\n" + json.dumps(initialized) + "\n"
               + json.dumps(tools_list) + "\n").encode()

    proc = await asyncio.create_subprocess_exec(
        command, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=child_env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()  # reap so the transport is closed cleanly (no ResourceWarning)
        except Exception:
            pass
        return []
    return _parse_tools_list_body(stdout.decode(errors="replace"))


def _list_http(url: str, *, timeout: float) -> list[str]:
    """Streamable-http handshake: initialize (capture mcp-session-id) then tools/list."""
    import urllib.request

    def _post(body: dict, session: str | None) -> tuple[str, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["mcp-session-id"] = session
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode(errors="replace"), resp.headers.get("mcp-session-id")

    try:
        _, session = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "concierge-mcp-livecheck", "version": "1"}}}, None)
        try:
            _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session)
        except Exception:
            pass
        body, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
        return _parse_tools_list_body(body)
    except Exception:
        return []


async def enumerate_live_tools(servers: dict, *, per_server_timeout: float = 30.0) -> dict[str, list[str]]:
    """Return ``{server_name: [live tool names]}`` by really handshaking each server.

    A server that does not answer contributes an empty list (NOT skipped) so the
    report can distinguish "declared but dead" from "not declared at all".
    """
    out: dict[str, list[str]] = {}
    for name, spec in servers.items():
        url = spec.get("url")
        if not url and spec.get("type") == "http":
            url = spec.get("url")
        if url:
            out[name] = _list_http(url, timeout=min(per_server_timeout, 12.0))
        else:
            out[name] = await _list_stdio(spec, timeout=per_server_timeout)
    return out


def evaluate(live_by_server: dict[str, list[str]]) -> tuple[bool, list[str], dict[str, str]]:
    """Return (ok, missing_tools, provided_by) for the REQUIRED set over the live union."""
    provided_by: dict[str, str] = {}
    for server, tools in live_by_server.items():
        for t in tools:
            provided_by.setdefault(t, server)
    missing: list[str] = []
    for _group, tools in REQUIRED_TOOLS.items():
        for t in tools:
            if t not in provided_by:
                missing.append(t)
    return (not missing), missing, provided_by


def run(openclaw_json: Path | None = None, *, per_server_timeout: float = 30.0) -> int:
    path = Path(openclaw_json) if openclaw_json else default_openclaw_json()
    if not path.is_file():
        print(f"FAIL(usage): concierge openclaw.json not found at {path}", file=sys.stderr)
        return 2
    servers = read_declared_servers(path)
    print(f"declared mcp.servers: {sorted(servers.keys()) or '<EMPTY -- mcpServers:NONE>'}")
    live_by_server = asyncio.run(enumerate_live_tools(servers, per_server_timeout=per_server_timeout))
    for server, tools in sorted(live_by_server.items()):
        state = f"{len(tools)} tool(s)" if tools else "UNREACHABLE / no tools (dead)"
        print(f"  live[{server}]: {state}")
    ok, missing, provided_by = evaluate(live_by_server)
    print("\nREQUIRED privileged concierge tools:")
    for group, tools in REQUIRED_TOOLS.items():
        for t in tools:
            src = provided_by.get(t)
            mark = "OK  " if src else "MISS"
            print(f"  [{mark}] {t:<22} <- {src or 'NOT ADVERTISED BY ANY LIVE SERVER'}   ({group})")
    if not ok:
        print(f"\nFAIL: {len(missing)} required tool(s) not loaded+callable: {missing}", file=sys.stderr)
        return 1
    print("\nPASS: every required privileged tool is advertised by a live-connected MCP server")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--openclaw-json", default=None,
                    help="path to the concierge openclaw.json (default: ~/.openclaw/openclaw.json)")
    ap.add_argument("--per-server-timeout", type=float, default=30.0)
    ns = ap.parse_args(argv)
    return run(Path(ns.openclaw_json) if ns.openclaw_json else None,
               per_server_timeout=ns.per_server_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
