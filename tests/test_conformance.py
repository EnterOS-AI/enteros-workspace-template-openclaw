"""ADR-004 §4 conformance opt-in for the openclaw adapter.

Inherits the SDK-owned conformance battery
(``molecule_plugin.adapter_conformance.AdapterConformance``) and points it at
THIS template's ``Adapter``. pytest collects every ``test_*`` the base class
defines against ``OpenClawAdapter`` — proving it satisfies the runtime-adapter
socket (identity, lifecycle, the MCP-config render→read→present round-trip,
enumerate tri-state, persona, and fail-closed-on-unmapped) with a STUBBED spawn.

``molecule_plugin`` (the SDK) + ``molecule_runtime`` (the runtime engine) must be
importable — both are test-time deps (see requirements-test.txt / CI PYTHONPATH).
"""

from molecule_plugin.adapter_conformance import AdapterConformance

from adapter import Adapter


class TestOpenClawAdapterConformance(AdapterConformance):
    adapter_class = Adapter
