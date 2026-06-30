"""Unit tests for adapter.resolve_platform_routing — the platform provider
routing override.

Selection is flag-free: routing kicks in when the resolved provider is
``platform`` (``LLM_PROVIDER=platform`` — core injects this for platform-routed
workspaces — or a ``platform/``/``platform:`` model namespace), NOT a
``MOLECULE_LLM_BILLING_MODE`` env. For the platform arm the tenant has no BYOK
key (the workspace-server strips them), so the per-vendor colon registry
(resolve_provider_routing) would raise "No API key found".
resolve_platform_routing short-circuits that: route everything through the
Molecule proxy's OpenAI-compat surface.

adapter.py is loaded by tests/conftest.py straight from the repo root; its
top-level imports require molecule_runtime. If that's unavailable the module
isn't in sys.modules and these tests skip (mirrors test_model_fallback.py).
"""
from __future__ import annotations

import sys

import pytest

adapter = sys.modules.get("adapter")
if adapter is None:
    pytest.skip(
        "adapter module unavailable (molecule_runtime not installed)",
        allow_module_level=True,
    )

PROXY = "https://api.moleculesai.app/api/v1/internal/llm/openai/v1"


def test_not_platform_returns_none():
    # provider != platform / unset -> no override; the colon registry path
    # runs as before. A bare vendor model with no platform signal is BYOK.
    assert adapter.resolve_platform_routing("minimax:MiniMax-M2.7", {}) is None
    assert adapter.resolve_platform_routing(
        "moonshot:kimi-k2.6", {"LLM_PROVIDER": "minimax"}
    ) is None


def test_platform_provider_routes_to_proxy_openai_surface():
    # provider==platform via LLM_PROVIDER (the signal core injects for
    # platform-routed workspaces) selects the platform arm.
    env = {
        "LLM_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "tok-123",
    }
    key, url, model, compat = adapter.resolve_platform_routing("moonshot/kimi-k2.6", env)
    assert (key, url, model, compat) == ("tok-123", PROXY, "moonshot/kimi-k2.6", "openai")


def test_model_provider_legacy_alias_also_selects_platform():
    env = {
        "MODEL_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "t",
    }
    assert adapter.resolve_platform_routing("moonshot/kimi-k2.6", env) is not None


def test_platform_model_namespace_selects_platform_and_is_stripped():
    # A "platform/" model namespace marker is itself the provider==platform
    # signal — no env needed — and the marker is stripped for the proxy.
    env = {
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "t",
    }
    _, _, model, _ = adapter.resolve_platform_routing("platform/moonshot/kimi-k2.6", env)
    assert model == "moonshot/kimi-k2.6"


def test_empty_model_falls_back_to_platform_default():
    env = {
        "LLM_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "t",
    }
    _, _, model, _ = adapter.resolve_platform_routing("", env)
    assert model == adapter.OPENCLAW_PLATFORM_DEFAULT_MODEL


def test_openai_base_url_and_anthropic_token_fallbacks():
    env = {
        "LLM_PROVIDER": "platform",
        "OPENAI_BASE_URL": PROXY,
        "ANTHROPIC_API_KEY": "sk-ant-xx",
    }
    key, url, _, _ = adapter.resolve_platform_routing("moonshot/kimi-k2.6", env)
    assert key == "sk-ant-xx" and url == PROXY


def test_fail_closed_when_platform_unconfigured():
    # provider==platform but no base/token -> raise, do NOT fall back to a
    # keyless BYOK route.
    with pytest.raises(RuntimeError):
        adapter.resolve_platform_routing(
            "moonshot/kimi-k2.6", {"LLM_PROVIDER": "platform"}
        )


# --- SSOT signal: MOLECULE_RESOLVED_PROVIDER (TOP PRECEDENCE) ---------------
# Core's provisioner resolves the provider ONCE and publishes the registry arm
# name here. When set it is authoritative: platform iff value == "platform";
# any other arm is BYOK and must NOT be re-derived from LLM_PROVIDER/MODEL_
# PROVIDER or the model namespace. Only when ABSENT do the legacy signals apply.

def test_resolved_provider_platform_routes_to_proxy():
    env = {
        "MOLECULE_RESOLVED_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "tok-ssot",
    }
    key, url, model, compat = adapter.resolve_platform_routing("moonshot/kimi-k2.6", env)
    assert (key, url, model, compat) == ("tok-ssot", PROXY, "moonshot/kimi-k2.6", "openai")


def test_resolved_provider_byok_arm_is_not_platform():
    # The SSOT signal names a byok arm -> not platform, even though LLM_PROVIDER
    # and the model namespace would otherwise (legacy) say platform. The SSOT
    # value wins; resolve_platform_routing returns None so the BYOK colon
    # registry path runs.
    env = {
        "MOLECULE_RESOLVED_PROVIDER": "minimax",
        "LLM_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "tok",
    }
    assert adapter.resolve_platform_routing("platform/moonshot/kimi-k2.6", env) is None


def test_resolved_provider_absent_falls_back_to_legacy_signal():
    # No SSOT signal -> legacy LLM_PROVIDER=platform still selects platform.
    env = {
        "LLM_PROVIDER": "platform",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "tok",
    }
    assert adapter.resolve_platform_routing("moonshot/kimi-k2.6", env) is not None


def test_resolved_provider_platform_fail_closed_when_unconfigured():
    # SSOT signal == platform but proxy env unset -> fail closed (no keyless
    # BYOK fall-through), same invariant as the legacy-signal path.
    with pytest.raises(RuntimeError):
        adapter.resolve_platform_routing(
            "moonshot/kimi-k2.6", {"MOLECULE_RESOLVED_PROVIDER": "platform"}
        )
