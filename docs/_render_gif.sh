#!/usr/bin/env bash
# Render docs/pack-cli-demo.gif via vhs running in Docker.
#
# Prerequisites:
#   - Docker daemon running
#
# What it does:
#   1. Spawns the official vhs image (pinned to a specific content digest
#      so future rerenders match the committed GIF byte-for-byte; bump the
#      digest intentionally with `docker pull ghcr.io/charmbracelet/vhs:latest`
#      and updating VHS_IMAGE below when a newer vhs release is desired)
#   2. apt-installs python3-yaml (aa CLI's only runtime dep for pack commands)
#   3. Drops the demo wrapper into /usr/local/bin/anywhere-agents
#   4. Runs vhs against docs/pack-cli-demo.tape
#
# Total runtime: ~1 minute (first run downloads the image; subsequent runs are faster).

# vhs v0.11.0 — latest release as of 2026-04-24. Upgrade by replacing the
# digest below with the output of `docker pull ghcr.io/charmbracelet/vhs:latest`
# when a newer vhs release ships.
VHS_IMAGE="ghcr.io/charmbracelet/vhs@sha256:9d5fc3dc0c160b0fb1d2212baff07e6bdf3fa9438c504a3237484567302fcf93"

set -e
cd "$(dirname "$0")/.."

# MSYS_NO_PATHCONV disables Git Bash's POSIX→Windows path translation for
# container-internal paths like /vhs and /usr/local/bin.
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(pwd):/vhs" \
  -w /vhs \
  --entrypoint bash \
  "$VHS_IMAGE" -c '
    set -e
    apt-get update -qq > /dev/null
    apt-get install -y -qq python3-yaml > /dev/null
    cp /vhs/docs/_demo-helpers/anywhere-agents /usr/local/bin/anywhere-agents
    chmod +x /usr/local/bin/anywhere-agents
    # Setup helpers with real ESC bytes. Kept in helper files to avoid
    # nested quote escaping in bash -c. init.sh orchestrates the rest.
    cp /vhs/docs/_demo-helpers/ps1-setup.sh /tmp/ps1-setup.sh
    cp /vhs/docs/_demo-helpers/banner.sh /tmp/banner.sh
    cp /vhs/docs/_demo-helpers/init.sh /tmp/init.sh
    anywhere-agents --version
    vhs docs/pack-cli-demo.tape
  '

echo
ls -la docs/pack-cli-demo.gif 2>/dev/null && echo "GIF ready."
