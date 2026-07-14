# Coding discipline

1. Think before coding: verify assumptions against current files and tests.
2. Prefer the smallest change that satisfies the task.
3. Keep edits surgical and match the existing style.
4. Define the validation that proves the change before implementing it.

# Repository guide

This is the `openclaw` workspace image, not a generic Python agent scaffold.
The supported path is `entrypoint.sh` → `molecule-runtime` →
`OpenClawAdapter` → loopback OpenClaw gateway. A2A turns are executed through
the runtime's shared subprocess/session contract.

Treat these files as the sources of truth:

| Concern | Source |
|---|---|
| Models/providers | `config.yaml` and `OPENCLAW_PROVIDERS` in `adapter.py` |
| Container boot | `entrypoint.sh` |
| OpenClaw setup, persona, plugins, gateway | `adapter.py` |
| Native prompt modules | `SOUL.md`, `BOOTSTRAP.md`, `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md` |
| Runtime dependency | `.runtime-version`, `requirements.txt` |
| Delivery behavior | `.gitea/workflows/publish-image.yml` |
| Supported local checks | `runbooks/local-dev-setup.md` and `.gitea/workflows/ci.yml` |

Do not document nonexistent `OPENCLAW_*` tuning flags, a standalone mock server,
or `openclaw_smoke_test`; none is present in this repository. Keep credential
values out of logs/examples. Open a branch and pull request; never push directly
to `main`, tag a release, or manually publish an image as part of routine work.
