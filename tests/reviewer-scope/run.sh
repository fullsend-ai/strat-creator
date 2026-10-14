#!/usr/bin/env bash
# Diagnostic job only; production still uses Fullsend's standard Workflow agent.
set -euo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/../reviewer-delivery/run.sh" reviewer-scope
