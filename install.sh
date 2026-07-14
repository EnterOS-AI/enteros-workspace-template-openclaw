#!/usr/bin/env bash
# install.sh — legacy self-managed host-install hook for the openclaw runtime.
#
# Hosted workspaces use the published image and entrypoint.sh. This hook is
# retained for self-managed bare-host installations that explicitly invoke it.
#
# Why this exists
# ---------------
# openclaw's npm package (openclaw@2026.4.29 at time of writing) pins
# `engines.node >= 22.14.0`. Older host images shipped Node 18.19.1
# from apt. When adapter.py:setup() ran
# `npm install --prefix ~/.local -g openclaw` against Node 18, npm
# either fails outright or surfaces a successful install whose
# postinstall scripts crash on first invocation — both end as
# `RuntimeError: Failed to install OpenClaw` from adapter.py:71, the
# molecule-runtime never binds port 8000, and the workspace flips to
# status=failed within ~2 minutes of provision.
#
# Diagnosed 2026-05-01 from the legacy host runtime logs:
#
#     RuntimeError: Failed to install OpenClaw: npm WARN EBADENGINE
#       package: 'openclaw@2026.4.29',
#       required: { node: '>=22.14.0' },
#       current: { node: 'v18.19.1', npm: '9.2.0' }
#
# What this does
# --------------
# Installs Node 22 LTS from nodesource into the system PATH. apt is
# the standard way to install Node on Ubuntu and matches openclaw's
# own README. The runtime user has passwordless sudo on the AMI, so
# we don't need to fall back to a user-local tarball install. Node 22
# is the LTS line that openclaw upstream targets.
#
# Idempotent — if Node ≥22 is already on PATH (e.g. AMI gets bumped
# in a future cycle and this hook becomes a no-op), early-exits 0
# without touching apt.
#
# Tracked: this is template-local "system deps" workaround. The
# durable fix is bumping the workspace AMI to ship Node 22 directly,
# which would also benefit future Node-based runtimes. Until then,
# this hook keeps openclaw provisioning green.

set -euo pipefail

current_major() {
  command -v node >/dev/null 2>&1 || { echo 0; return; }
  node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

if [ "$(current_major)" -ge 22 ]; then
  echo "Node $(node --version) already satisfies >=22 — skipping install"
  exit 0
fi

echo "Installing Node 22 from nodesource (had: $(node --version 2>/dev/null || echo 'none'))"

# nodesource setup_22.x: configures /etc/apt/sources.list.d/nodesource.list
# and refreshes apt cache. Re-running on an already-configured host is a
# no-op, so this stays idempotent across reboots / re-runs.
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -

# `apt-get install -y nodejs` from nodesource provides Node 22 + npm 10.
# --no-install-recommends keeps the install slim (no recommended-but-unused
# build toolchain pulled in transitively).
sudo apt-get install -y --no-install-recommends nodejs

echo "Node $(node --version) installed; npm $(npm --version)"
