# Local development — OpenClaw workspace template

These commands follow the current repository CI. Local tests do not require a
live workspace or production credential.

## Prerequisites

- Python 3.11+
- Git
- Access to `git.moleculesai.app` and its package registry
- Docker only when reproducing the image/conformance jobs

## Clone and install test dependencies

```bash
git clone https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-openclaw.git
cd molecule-ai-workspace-template-openclaw
git switch -c fix/describe-the-change

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install pytest pytest-asyncio pyyaml python-multipart jsonschema packaging

rm -rf .molecule-ci-canonical
git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci-canonical
python3 .molecule-ci-canonical/scripts/install_workspace_dependencies.py --allow-missing
python3 -m pip install "molecule-ai-sdk @ git+https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git@da42c7f2dae122aaa6f34a74c13e598a87870586"
```

The canonical installer acquires the private runtime from the Gitea package
registry. Do not select it from an untrusted public source.

## Run the checks

```bash
PROVIDERS_MANIFEST_FILE=internal/providers/providers.yaml \
  python3 .molecule-ci-canonical/scripts/validate-workspace-template.py --static-only
python3 -m pytest tests/test_platform_routing.py -q
python3 -m pytest tests/test_openclaw_concierge_mcp_live_e2e.py -v
python3 -m pytest tests/ -v
```

The last suite includes provider routing, session-id, persona, plugin, MCP,
provenance, and documentation contracts. The live-concierge layer skips unless
CI explicitly supplies a live container; its local logic layer still runs.

## Build the image

With Docker and package access available:

```bash
docker build -t workspace-template-openclaw:dev .
```

Do not use `python -m adapter` as a standalone runtime. The supported image
entrypoint expects `/configs` and `/workspace`, executes `molecule-runtime`, and
lets `OpenClawAdapter.setup()` start the gateway. CI owns the platform-shaped
smoke and privilege-conformance checks.

There is no `openclaw_smoke_test`, `mock_platform_server`, `tests/unit`, or
`tests/integration` module in the current tree. Use the commands above rather
than copying old examples.

## Before opening a pull request

```bash
git diff --check
python3 -m pytest tests/test_current_documentation.py -q
```

Never commit `.env` files, provider keys, platform tokens, OpenClaw auth
profiles, or generated credential files.
