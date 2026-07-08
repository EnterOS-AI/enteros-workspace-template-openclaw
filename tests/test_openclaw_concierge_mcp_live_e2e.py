"""E2E: an openclaw CONCIERGE's MCP tool set must be LOADED + CALLABLE per runtime.

This is the STRENGTHENED test that closes the shallow-check flaw the mocked
``test_openclaw_mgmt_mcp.py`` could not catch: those tests mock
``_list_mcp_tools`` and assert render/parse/publish in isolation, so a live
concierge with an EMPTY ``mcp.servers`` map ("mcpServers:NONE"), a
declared-but-dead management MCP, or a server advertising the WRONG tool names
all pass the unit suite while a real concierge boots without
``provision_workspace`` / ``commit_memory``.

Two layers, both driving REAL MCP handshakes (no mocks of the wire):

  1. LOGIC layer (always runs, hermetic, no private deps): stands up a REAL
     stdio MCP server (``_fake_mcp_stdio_server.py``) and points a synthetic
     ``openclaw.json`` at it, then asserts ``assert_concierge_mcp_loaded`` —
       * FAILS on empty ``mcp.servers`` (the mcpServers:NONE regression),
       * FAILS when a required tool (``provision_workspace``) is not advertised,
       * FAILS on a declared-but-dead server (answers initialize, never
         tools/list),
       * PASSES when a live server advertises the full REQUIRED set.
     This is the fail-before / pass-after guarantee, locked in CI.

  2. LIVE layer (opt-in, deploy gate): when ``MOLECULE_LIVE_CONCIERGE_CONTAINER``
     is set, ``docker exec`` the asserter INSIDE a real, running openclaw
     concierge and require exit 0 — proving the REQUIRED privileged tools are
     advertised by live-connected servers on an actually-provisioned concierge.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ASSERTER = REPO / "scripts" / "assert_concierge_mcp_loaded.py"
FAKE_SERVER = Path(__file__).resolve().parent / "_fake_mcp_stdio_server.py"

# Ground-truth REQUIRED set (mirrors assert_concierge_mcp_loaded.REQUIRED_TOOLS).
ALL_REQUIRED = [
    "provision_workspace", "list_workspaces",
    "list_peers", "get_workspace_info", "commit_memory", "recall_memory",
]


def _write_openclaw_json(tmp_path: Path, servers: dict) -> Path:
    import json
    p = tmp_path / "openclaw.json"
    p.write_text(json.dumps({"mcp": {"servers": servers}}))
    return p


def _stdio_spec(tools: list[str], *, dead: bool = False) -> dict:
    env = {"FAKE_MCP_TOOLS": ",".join(tools)}
    if dead:
        env["FAKE_MCP_DEAD"] = "1"
    return {"command": sys.executable, "args": [str(FAKE_SERVER)], "env": env}


def _run_asserter(openclaw_json: Path, *, timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ASSERTER), "--openclaw-json", str(openclaw_json),
         "--per-server-timeout", str(timeout)],
        capture_output=True, text=True, timeout=120,
    )


# ── LOGIC layer — fail-before / pass-after, real handshake, hermetic ──

def test_fails_on_empty_mcp_servers(tmp_path):
    """mcpServers:NONE — the exact live regression the mocked suite missed."""
    oc = _write_openclaw_json(tmp_path, {})
    r = _run_asserter(oc)
    assert r.returncode == 1, r.stdout + r.stderr
    for t in ALL_REQUIRED:
        assert t in r.stdout


def test_passes_when_all_required_tools_advertised_live(tmp_path):
    """A single REAL stdio server advertising the full REQUIRED set → PASS."""
    oc = _write_openclaw_json(tmp_path, {"molecule-platform": _stdio_spec(ALL_REQUIRED)})
    r = _run_asserter(oc)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS:" in r.stdout


def test_fails_when_required_tool_missing_despite_declared_server(tmp_path):
    """The declared-but-WRONG-tools case: server is live but omits
    provision_workspace (e.g. an old server exposing only `create_workspace`).
    A mock that lists the assumed name would pass; the live handshake catches it."""
    oc = _write_openclaw_json(
        tmp_path,
        {"molecule-platform": _stdio_spec([t for t in ALL_REQUIRED if t != "provision_workspace"])},
    )
    r = _run_asserter(oc)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "provision_workspace" in r.stdout
    assert "MISS" in r.stdout


def test_fails_on_declared_but_dead_server(tmp_path):
    """Server answers initialize then never answers tools/list — must be
    treated as NOT loaded (callable, not merely declared)."""
    oc = _write_openclaw_json(tmp_path, {"molecule-platform": _stdio_spec(ALL_REQUIRED, dead=True)})
    r = _run_asserter(oc, timeout=2.0)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "dead" in r.stdout.lower() or "UNREACHABLE" in r.stdout


# ── LIVE layer — opt-in, runs against a real provisioned concierge ──

@pytest.mark.skipif(
    not os.environ.get("MOLECULE_LIVE_CONCIERGE_CONTAINER"),
    reason="set MOLECULE_LIVE_CONCIERGE_CONTAINER=<docker name> to run against a live concierge",
)
def test_live_concierge_has_required_mcp_tools_loaded():
    """docker exec the asserter INSIDE a running openclaw concierge; require PASS.

    Wired into the deploy gate: a concierge whose management MCP or memory tools
    are not actually loaded+callable FAILS the promote, catching the
    mcpServers:NONE / declared-but-dead class of regression on the real image."""
    container = os.environ["MOLECULE_LIVE_CONCIERGE_CONTAINER"]
    # Copy the asserter into the container and run it as the agent user.
    subprocess.run(["docker", "cp", str(ASSERTER),
                    f"{container}:/tmp/assert_concierge_mcp_loaded.py"], check=True, timeout=60)
    r = subprocess.run(
        ["docker", "exec", "-u", "agent", container, "sh", "-lc",
         'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; '
         "python3 /tmp/assert_concierge_mcp_loaded.py --per-server-timeout 60"],
        capture_output=True, text=True, timeout=240,
    )
    assert r.returncode == 0, f"live concierge MCP tools not loaded+callable:\n{r.stdout}\n{r.stderr}"
