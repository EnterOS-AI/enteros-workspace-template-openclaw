#!/usr/bin/env bash
# test-lint-mcp-pin-lockstep.sh — self-test for GUARD D (task #229 / #228).
#
# Proves the lockstep lint FAILS-BEFORE (RED on a pin<->bake skew) and GREENs
# only in lockstep. Deterministic + network-free: every case feeds synthetic
# fixture files, so this runs identically on any runner and forever documents
# the guard's behaviour.
#
# The FIRST case reproduces the exact live skew this guard was built for:
# image baked 1.7.0 vs plugin pinned 1.8.1 (#228) -> MUST be RED.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LINT="${HERE}/lint-mcp-pin-lockstep.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

fails=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; fails=$((fails + 1)); }

mk_dockerfile() { printf 'FROM scratch\nARG MCP_SERVER_VERSION=%s\n' "$1" > "${TMP}/Dockerfile.$2"; }
mk_fragment() {
  cat > "${TMP}/fragment.$2.json" <<JSON
{ "mcpServers": { "molecule-platform": {
  "command": "npx",
  "args": ["-y", "--prefer-offline", "@molecule-ai/mcp-server@$1"],
  "env": { "MOLECULE_MCP_MODE": "management" } } } }
JSON
}

# Expect the lint to exit RED (non-zero).
expect_red() {
  local name="$1"; shift
  if bash "${LINT}" "$@" >/dev/null 2>&1; then
    fail "${name} — expected RED (skew) but lint returned GREEN"
  else
    pass "${name} — correctly RED"
  fi
}
# Expect the lint to exit GREEN (zero).
expect_green() {
  local name="$1"; shift
  if bash "${LINT}" "$@" >/dev/null 2>&1; then
    pass "${name} — correctly GREEN"
  else
    fail "${name} — expected GREEN (lockstep) but lint returned RED"
  fi
}

echo "== GUARD D lockstep lint self-test =="

# 1. THE LIVE SKEW (#228): baked 1.7.0 vs pinned 1.8.1 -> RED (fail-before).
mk_dockerfile "1.7.0" skew ; mk_fragment "1.8.1" skew
expect_red "live skew 1.7.0-vs-1.8.1" \
  --dockerfile "${TMP}/Dockerfile.skew" --fragment "${TMP}/fragment.skew.json"

# 2. LOCKSTEP: baked 1.8.1 vs pinned 1.8.1 -> GREEN (the fix).
mk_dockerfile "1.8.1" ok ; mk_fragment "1.8.1" ok
expect_green "lockstep 1.8.1-vs-1.8.1" \
  --dockerfile "${TMP}/Dockerfile.ok" --fragment "${TMP}/fragment.ok.json"

# 3. SYNTHETIC forward skew (patch ahead): 2.0.0 vs 2.0.1 -> RED.
mk_dockerfile "2.0.0" syn ; mk_fragment "2.0.1" syn
expect_red "synthetic skew 2.0.0-vs-2.0.1" \
  --dockerfile "${TMP}/Dockerfile.syn" --fragment "${TMP}/fragment.syn.json"

# 4. FAIL-CLOSED: Dockerfile missing the ARG -> RED (never silently pass).
printf 'FROM scratch\nRUN true\n' > "${TMP}/Dockerfile.noarg"
mk_fragment "1.8.1" na
expect_red "missing ARG MCP_SERVER_VERSION" \
  --dockerfile "${TMP}/Dockerfile.noarg" --fragment "${TMP}/fragment.na.json"

# 5. FAIL-CLOSED: malformed fragment (no pin token) -> RED.
mk_dockerfile "1.8.1" mf
printf '{ "mcpServers": { "molecule-platform": { "command": "npx", "args": ["-y"] } } }\n' > "${TMP}/fragment.mf.json"
expect_red "fragment missing pin token" \
  --dockerfile "${TMP}/Dockerfile.mf" --fragment "${TMP}/fragment.mf.json"

# 6. --ssot-version override path GREEN (used by the publish build-arg derive).
mk_dockerfile "1.8.1" ov
expect_green "explicit --ssot-version lockstep" \
  --dockerfile "${TMP}/Dockerfile.ov" --ssot-version "1.8.1"

echo "==="
if [ "${fails}" -ne 0 ]; then
  echo "GUARD D self-test: ${fails} case(s) FAILED"
  exit 1
fi
echo "GUARD D self-test: all cases passed"
exit 0
