"""Phase P4 — openclaw concierge management-MCP wiring (template side).

Proves the template's half of making the org-admin management MCP load on an
OPENCLAW concierge:

  * ``register_mcp_server_hook`` renders the declared MCP into the openclaw-NATIVE
    config (~/.openclaw/openclaw.json ``mcp.servers.<name>`` — the file
    ``openclaw mcp set`` writes), NOT ``.claude/settings.json`` (the #3159 bug),
    and injects the molecule-* env literals the stdio child needs.
  * the runtime present-reader (dispatched via the adapter) agrees with what the
    renderer wrote, and stays fail-closed when nothing is wired.
  * the loaded_mcp_tools producer is fed an INVENTORY (the tools the registered
    servers EXPOSE), NOT per-turn invoked tools — avoiding the #142/#3082-class
    degradation a tool-less turn would otherwise cause.
  * ``_parse_tools_list_body`` reads the JSON / NDJSON / SSE framings an MCP
    server may use for a ``tools/list`` reply.

Import-level only (no openclaw CLI, no network): _list_mcp_tools is mocked so the
inventory path is exercised without the private @molecule-ai/mcp-server.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]

# The runtime modules the wiring dispatches through. Skip cleanly if an older
# base image lacks the P4 renderer (keeps CI green on a not-yet-bumped runtime).
mcp_render = pytest.importorskip("molecule_runtime.mcp_render")
pai = pytest.importorskip("molecule_runtime.platform_agent_identity")

MANAGEMENT_MCP_NAME = "molecule-platform"


def _config(tmp_path):
    from molecule_runtime.adapter_base import AdapterConfig
    return AdapterConfig(
        model="minimax:MiniMax-M2.7",
        config_path=str(tmp_path / "configs"),
        workspace_id="ws-test",
    )


# ── register_mcp_server_hook → openclaw-native config, not claude settings ──

def test_hook_renders_openclaw_native_not_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = adapter.OpenClawAdapter()
    cfg = _config(tmp_path)

    a.register_mcp_server_hook(
        cfg, MANAGEMENT_MCP_NAME,
        {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"],
         "env": {"MOLECULE_MCP_MODE": "management"}},
    )

    # NOT written to a claude settings.json under the config dir.
    assert not (tmp_path / "configs" / ".claude" / "settings.json").exists()
    # Written to the openclaw-native config under mcp.servers.
    oc = tmp_path / ".openclaw" / "openclaw.json"
    server = json.loads(oc.read_text())["mcp"]["servers"][MANAGEMENT_MCP_NAME]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert server["env"]["MOLECULE_MCP_MODE"] == "management"


def test_hook_injects_molecule_env_literals(tmp_path, monkeypatch):
    """The molecule-* runtime env the stdio MCP child needs is merged in as
    literals; descriptor-declared keys are preserved and win."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOLECULE_CP_URL", "https://cp.example")
    monkeypatch.setenv("MOLECULE_ADMIN_TOKEN", "tok-123")
    monkeypatch.setenv("WORKSPACE_ID", "ws-abc")
    a = adapter.OpenClawAdapter()

    a.register_mcp_server_hook(
        _config(tmp_path), MANAGEMENT_MCP_NAME,
        {"command": "npx", "env": {"MOLECULE_MCP_MODE": "management"}},
    )

    env = json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())[
        "mcp"]["servers"][MANAGEMENT_MCP_NAME]["env"]
    assert env["MOLECULE_MCP_MODE"] == "management"  # descriptor key preserved
    assert env["MOLECULE_CP_URL"] == "https://cp.example"
    assert env["MOLECULE_ADMIN_TOKEN"] == "tok-123"
    assert env["WORKSPACE_ID"] == "ws-abc"


def test_present_reader_agrees_after_hook(tmp_path, monkeypatch):
    """The RCA#2970 gate's present-reader (via the adapter) sees the MCP wired
    after the hook runs, and is fail-closed before."""
    monkeypatch.setenv("HOME", str(tmp_path))
    a = adapter.OpenClawAdapter()
    cfg = _config(tmp_path)

    assert a.management_mcp_present(cfg) is False  # nothing wired yet
    a.register_mcp_server_hook(cfg, MANAGEMENT_MCP_NAME, {"command": "npx"})
    assert a.management_mcp_present(cfg) is True


# ── _parse_tools_list_body: JSON / NDJSON / SSE framings ──

def _tools_list_reply(names):
    return {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": n} for n in names]}}


def test_parse_tools_list_plain_json():
    body = json.dumps(_tools_list_reply(["create_workspace", "list_peers"]))
    assert adapter._parse_tools_list_body(body) == ["create_workspace", "list_peers"]


def test_parse_tools_list_ndjson():
    # initialize reply then tools/list reply, one per line (stdio default).
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}})
    lst = json.dumps(_tools_list_reply(["create_workspace"]))
    assert adapter._parse_tools_list_body(init + "\n" + lst + "\n") == ["create_workspace"]


def test_parse_tools_list_sse():
    body = "event: message\n" + "data: " + json.dumps(_tools_list_reply(["x", "y"])) + "\n\n"
    assert adapter._parse_tools_list_body(body) == ["x", "y"]


def test_parse_tools_list_empty_on_garbage():
    assert adapter._parse_tools_list_body("not json at all") == []
    assert adapter._parse_tools_list_body("") == []


# ── _publish_loaded_mcp_inventory: inventory-based, fail-closed ──

@pytest.mark.asyncio
async def test_publish_inventory_from_registered_servers(tmp_path, monkeypatch):
    """Publishes ``mcp__<server>__<tool>`` for the tools the registered servers
    EXPOSE (inventory) — proving loaded, not per-turn invoked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Seed two registered servers in openclaw.json.
    oc = tmp_path / ".openclaw" / "openclaw.json"
    oc.parent.mkdir(parents=True, exist_ok=True)
    oc.write_text(json.dumps({"mcp": {"servers": {
        MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]},
        "molecule": {"type": "http", "url": "http://127.0.0.1:9100/mcp"},
    }}}))

    a = adapter.OpenClawAdapter()

    async def _fake_list(spec):
        if spec.get("command") == "npx":
            return ["create_workspace", "list_workspaces"]
        return ["list_peers"]

    monkeypatch.setattr(a, "_list_mcp_tools", _fake_list)
    pai.set_loaded_mcp_tools(None)  # reset producer
    await a._publish_loaded_mcp_inventory()

    published = pai.loaded_mcp_tools()
    assert published == sorted([
        f"mcp__{MANAGEMENT_MCP_NAME}__create_workspace",
        f"mcp__{MANAGEMENT_MCP_NAME}__list_workspaces",
        "mcp__molecule__list_peers",
    ])


@pytest.mark.asyncio
async def test_publish_inventory_stays_unset_when_nothing_enumerated(tmp_path, monkeypatch):
    """When enumeration yields no tools, the producer is left UNSET (None) so the
    gate stays fail-closed (degraded) — never a guessed/empty list. This is the
    anti-#142/#3082 guarantee: a tool-less enumeration must not publish []."""
    monkeypatch.setenv("HOME", str(tmp_path))
    oc = tmp_path / ".openclaw" / "openclaw.json"
    oc.parent.mkdir(parents=True, exist_ok=True)
    oc.write_text(json.dumps({"mcp": {"servers": {
        MANAGEMENT_MCP_NAME: {"command": "npx"},
    }}}))

    a = adapter.OpenClawAdapter()
    monkeypatch.setattr(a, "_list_mcp_tools", AsyncMock(return_value=[]))
    pai.set_loaded_mcp_tools(None)
    await a._publish_loaded_mcp_inventory()

    assert pai.loaded_mcp_tools() is None  # NOT [] — fail-closed


@pytest.mark.asyncio
async def test_publish_inventory_failsoft_on_missing_config(tmp_path, monkeypatch):
    """No openclaw.json → best-effort no-op (producer untouched), never raises."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.openclaw/openclaw.json
    a = adapter.OpenClawAdapter()
    pai.set_loaded_mcp_tools(None)
    await a._publish_loaded_mcp_inventory()  # must not raise
    assert pai.loaded_mcp_tools() is None
