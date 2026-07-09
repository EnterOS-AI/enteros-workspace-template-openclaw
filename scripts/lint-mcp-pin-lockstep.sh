#!/usr/bin/env bash
# lint-mcp-pin-lockstep.sh — GUARD D: lockstep pin<->version gate.
#
# WHY THIS EXISTS  (#228 / task #229 Guard D)
# -------------------------------------------
# The kind=platform concierge's management MCP is delivered as a plugin
# (molecule-ai-plugin-molecule-platform-mcp) whose settings-fragment.json
# launches:
#
#     npx --prefer-offline @molecule-ai/mcp-server@<PIN>
#
# The runtime image pre-bakes that SAME mcp-server version into the agent's
# npm cache (Dockerfile `ARG MCP_SERVER_VERSION=<BAKED>`) so the first agent
# launch resolves ENTIRELY FROM CACHE with zero network dependency.
#
# When the plugin bumps <PIN> ahead of the image's <BAKED> (e.g. plugin pins
# 1.8.1 but this image baked 1.7.0), `npx --prefer-offline @...@1.8.1` MISSES
# the cache (which only holds 1.7.0) and COLD-PULLS 1.8.1 from the CF-fronted
# registry. Under CF-WAF throttling that pull can hard-fail `ETARGET` — the
# concierge then FALSE-READYs / degrades on every version bump (#228, a
# recurring deterministic bug, NOT flakiness).
#
# This lint makes <BAKED> == <PIN> a HARD, machine-checked invariant: the two
# can never silently drift. RED when the image's baked version is behind (or
# otherwise != ) the plugin's pinned version; GREEN only in lockstep.
#
# SSOT: the pinned version is READ from the plugin fragment — never hand-typed
# here. Pass the fragment via --fragment (a local checkout/clone) or the exact
# version via --ssot-version (used by the self-test).
#
# Usage:
#   lint-mcp-pin-lockstep.sh --dockerfile ./Dockerfile --fragment ./settings-fragment.json
#   lint-mcp-pin-lockstep.sh --dockerfile ./Dockerfile --ssot-version 1.8.1
#
# Exit 0  => lockstep (baked == pinned)         [GREEN]
# Exit 1  => skew / unresolvable input          [RED, fail-closed]
set -euo pipefail

DOCKERFILE="./Dockerfile"
FRAGMENT=""
SSOT_VERSION=""
PRINT_SSOT=0
# The npm package whose pin<->bake must stay in lockstep.
PKG="@molecule-ai/mcp-server"

die() { echo "::error::$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dockerfile)         DOCKERFILE="${2:-}"; shift 2 ;;
    --fragment)           FRAGMENT="${2:-}"; shift 2 ;;
    --ssot-version)       SSOT_VERSION="${2:-}"; shift 2 ;;
    --package)            PKG="${2:-}"; shift 2 ;;
    # Resolve the plugin-pinned version from --fragment and print it to stdout,
    # then exit 0 — no Dockerfile compare. Used by the publish workflow to
    # DERIVE the --build-arg MCP_SERVER_VERSION from the SSOT (point 2), reusing
    # this one canonical parse instead of a second copy of it.
    --print-ssot-version) PRINT_SSOT=1; shift ;;
    -h|--help)            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                    die "unknown arg: $1" ;;
  esac
done

# ── Resolve the SSOT (plugin-pinned) version ────────────────────────────────
# Precedence: explicit --ssot-version wins (self-test / override); otherwise
# parse it out of the plugin settings-fragment.json. Fail-closed if neither
# yields a version — the guard must NEVER silently pass on a missing SSOT.
if [ -z "${SSOT_VERSION}" ]; then
  [ -n "${FRAGMENT}" ] || die "no --ssot-version and no --fragment given — cannot resolve the plugin pin (fail-closed)"
  [ -f "${FRAGMENT}" ] || die "fragment file not found: ${FRAGMENT} (fail-closed)"
  # Parse the args array in mcpServers.*.args for the '<PKG>@<version>' token.
  # Use python3 for correct JSON parsing (available on every CI runner + locally).
  SSOT_VERSION="$(
    PKG="${PKG}" python3 - "${FRAGMENT}" <<'PY'
import json, os, re, sys
pkg = os.environ["PKG"]
try:
    data = json.load(open(sys.argv[1]))
except Exception as e:
    print("", end="")
    sys.exit(0)
found = ""
# Walk every mcpServers.*.args token looking for '<pkg>@<version>'.
for srv in (data.get("mcpServers") or {}).values():
    for tok in (srv.get("args") or []):
        if isinstance(tok, str) and tok.startswith(pkg + "@"):
            found = tok[len(pkg) + 1:]
m = re.match(r'^[0-9][0-9A-Za-z.\-+]*$', found or "")
print(found if m else "", end="")
PY
  )"
  [ -n "${SSOT_VERSION}" ] || die "could not extract '${PKG}@<version>' pin from ${FRAGMENT} (fail-closed) — the plugin SSOT is malformed or the package name changed"
fi

# ── --print-ssot-version: emit the resolved pin and stop (build-arg derive) ──
if [ "${PRINT_SSOT}" -eq 1 ]; then
  printf '%s\n' "${SSOT_VERSION}"
  exit 0
fi

# ── Resolve the BAKED version from the Dockerfile ARG default ───────────────
[ -f "${DOCKERFILE}" ] || die "Dockerfile not found: ${DOCKERFILE} (fail-closed)"
# Match: `ARG MCP_SERVER_VERSION=1.8.1` (tolerate optional quotes/space). This
# is the DEFAULT baked into a plain `docker build` with no --build-arg; the
# publish workflow ALSO overrides it with the SSOT-derived build-arg, so this
# lint additionally documents/enforces the default for local builds.
BAKED_RAW="$(grep -E '^[[:space:]]*ARG[[:space:]]+MCP_SERVER_VERSION[[:space:]]*=' "${DOCKERFILE}" | head -1 || true)"
[ -n "${BAKED_RAW}" ] || die "no 'ARG MCP_SERVER_VERSION=' found in ${DOCKERFILE} (fail-closed) — the image must declare the baked mcp-server version so the lockstep gate can enforce it against the plugin pin"
BAKED_VERSION="$(printf '%s\n' "${BAKED_RAW}" \
  | sed -E 's/^[[:space:]]*ARG[[:space:]]+MCP_SERVER_VERSION[[:space:]]*=//; s/^["'\'']//; s/["'\'']?[[:space:]]*$//')"
[ -n "${BAKED_VERSION}" ] || die "'ARG MCP_SERVER_VERSION=' has an empty default in ${DOCKERFILE} (fail-closed)"

# ── Assert lockstep ─────────────────────────────────────────────────────────
if [ "${BAKED_VERSION}" != "${SSOT_VERSION}" ]; then
  echo "::error::GUARD D lockstep VIOLATION — the runtime image bakes ${PKG}@${BAKED_VERSION} but the platform-mcp plugin SSOT pins ${PKG}@${SSOT_VERSION}." >&2
  echo "::error::  baked (Dockerfile ARG MCP_SERVER_VERSION): ${BAKED_VERSION}" >&2
  echo "::error::  pinned (plugin settings-fragment.json):     ${SSOT_VERSION}" >&2
  echo "::error::At runtime the concierge launches 'npx --prefer-offline ${PKG}@${SSOT_VERSION}', MISSES the ${BAKED_VERSION}-only prebake cache, and COLD-PULLS ${SSOT_VERSION} — the #228 ETARGET false-ready on every bump." >&2
  echo "::error::FIX: set 'ARG MCP_SERVER_VERSION=${SSOT_VERSION}' in ${DOCKERFILE} (and rebuild so the prebake bakes ${SSOT_VERSION})." >&2
  exit 1
fi

echo "::notice::GUARD D OK — ${PKG} lockstep: image baked == plugin pin == ${BAKED_VERSION}"
exit 0
