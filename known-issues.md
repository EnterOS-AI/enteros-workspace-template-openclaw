# Support notes — OpenClaw workspace template

The live issue tracker is the source of truth for open defects:

<https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-openclaw/issues>

The former contents described a different Python runtime, nonexistent methods,
invented ticket identifiers, and environment flags not implemented by the
current adapter. They remain in git history but are not current workarounds.

## Runtime installation

OpenClaw is a Node CLI installed in the image. `adapter.py` only performs a
user-local install if the executable is absent, then installs the optional
packages required by the gateway. Python's `requirements.txt` contains the
Molecule runtime integration, not an `openclaw-runtime` package.

## Provider resolution

The supported direct providers and credentials are defined by
`OPENCLAW_PROVIDERS` in `adapter.py` and the template `config.yaml`. Platform
routes use the injected resolved provider/base URL/usage token. Setup fails
before gateway start when the selected model cannot be served.

## Session continuity

`OpenClawA2AExecutor` inherits the shared `SubprocessA2AExecutor` session
contract and supplies only the OpenClaw shell-out plus trace handling. There is
no `OPENCLAW_FORWARD_CONTEXT` or `OPENCLAW_NO_RESUME_REINJECT` workaround in
the code.

## Skills and MCP plugins

Native prompt files are materialized into the workspace OpenClaw actually
reads. Declared plugins are installed through the runtime registry and native
MCP config hook; there is no `OPENCLAW_SKILL_LIST` directory scanner in this
template.
