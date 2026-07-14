# Molecule AI workspace template — OpenClaw

This repository builds the `openclaw` workspace image used by Molecule AI. The
canonical source is this Gitea repository; create workspaces through the canvas
runtime picker.

## Runtime shape

- `entrypoint.sh` loads projected secrets, prepares `/configs` and the agent
  home, drops to uid 1000, configures private package access for the management
  MCP, and executes `molecule-runtime`.
- `adapter.py` resolves the provider, runs OpenClaw's non-interactive setup,
  materializes native workspace files, installs declared plugins, starts the
  loopback OpenClaw gateway, and registers Molecule MCP tools.
- `OpenClawA2AExecutor` inherits the common subprocess/session contract and
  executes turns through `openclaw agent --json`.
- `config.yaml` defines the template's models/providers. The files under
  `internal/providers/` are a CI-checked registry projection.

The OpenClaw gateway listens on loopback port 18789. Molecule's A2A runtime is
the platform-facing service on port 8000.

## Native workspace files

OpenClaw reads its prompt/persona modules from its resolved workspace directory:

- `SOUL.md`
- `BOOTSTRAP.md`
- `AGENTS.md`
- `TOOLS.md`
- `HEARTBEAT.md`

`adapter.py` materializes these files and the current platform persona before
the gateway serves a turn. Do not replace this with an invented
`system-prompt.md` or environment-driven skill-fragment mechanism.

## Authentication and providers

Provider selection follows `MOLECULE_RESOLVED_PROVIDER` when the platform has
already resolved a route. Otherwise `adapter.py` uses the provider registry in
this repository. Supported direct prefixes currently include OpenAI, Groq,
OpenRouter, Qianfan, MiniMax, and Moonshot/Kimi, with the credential names
declared in `OPENCLAW_PROVIDERS` and `config.yaml`.

Never commit credentials or put them in command-line examples. Configure them
through workspace/platform secret surfaces.

## Development and delivery

See [`runbooks/local-dev-setup.md`](runbooks/local-dev-setup.md) for the commands
that mirror CI. Pull requests run static, unit, image, and conformance checks. A
push to `main` invokes `publish-image`, which builds the image, pushes it to the
Gitea OCI registry, and runs the configured pin verification. Do not substitute
a manual registry script or direct-main-push procedure.

The current config contains `template_schema_version: 1`; change it only with a
corresponding platform contract change and validation.

## License

Business Source License 1.1 — © Molecule AI.
