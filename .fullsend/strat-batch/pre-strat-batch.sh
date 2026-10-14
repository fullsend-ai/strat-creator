#!/usr/bin/env bash
# Host pre-script for strat-batch; see ../shared/pre-strategy.sh.
set -euo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/../shared/pre-strategy.sh" batch
