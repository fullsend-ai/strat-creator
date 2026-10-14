#!/usr/bin/env bash
# One focused GLM-only test. The dashboard wrapper supplies telemetry/auth/strace.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
model=rits/zai-org/glm-5-3
if [[ "${PIPELINE_MODEL:-}" != "$model" ]]; then
  echo "This iterative test requires the exact hosted GLM model." >&2
  exit 2
fi
out=${JOB_ARTIFACT_DIR:?dashboard job artifacts are required}
scenario=${1:-reviewer-delivery}
case "$scenario" in
  reviewer-delivery|reviewer-scope) ;;
  *) echo "Unknown reviewer test scenario." >&2; exit 2 ;;
esac
fixture="$root/tests/fixtures/$scenario"
git archive --format=tar.gz HEAD > "$out/candidate-source.tar.gz"
printf '%s\n' '{"enableWorkflows":true,"effortLevel":"medium"}' > "$out/fixture-settings.json"
export ANTHROPIC_MODEL="$model" CLAUDE_CODE_SUBAGENT_MODEL="$model"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$model" ANTHROPIC_DEFAULT_SONNET_MODEL="$model"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$model" ANTHROPIC_DEFAULT_FABLE_MODEL="$model"
export CLAUDE_CODE_EFFORT_LEVEL=medium DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=1200000
mkdir -p artifacts/strat-tasks artifacts/rfe-tasks .context/architecture-context/architecture/fixture
if [[ "$scenario" == reviewer-scope ]]; then
  cp "$fixture"/strat-tasks/*.md artifacts/strat-tasks/
  cp "$fixture"/rfe-tasks/*.md artifacts/rfe-tasks/
else
  cp "$fixture/strategy.md" artifacts/strat-tasks/RHAISTRAT-9001.md
  cp "$fixture/rfe.md" artifacts/rfe-tasks/RHAIRFE-9001.md
fi
cp "$fixture/platform.md" .context/architecture-context/architecture/fixture/PLATFORM.md
printf '%s\n' fixture > .context/architecture-context/LATEST_VERSION
claude --version > "$out/claude-version.txt"
sha256sum "$(command -v claude)" > "$out/claude-sha256.txt"
git rev-parse HEAD > "$out/source-ref.txt"
set +e
timeout 1200 claude --print --verbose --output-format stream-json --max-turns 20 \
  --model "$model" --effort medium --dangerously-skip-permissions \
  --settings "$out/fixture-settings.json" \
  --plugin-dir "$root/.fullsend/plugins/strat-pipeline" "$fixture/plugin" "/$scenario:$scenario" \
  2> >(tee "$out/claude-stderr.log" >&2) | tee "$out/stream.jsonl"
code=${PIPESTATUS[0]}
set -e
printf '%s\n' "$code" > "$out/claude-exit.txt"
for name in projects tasks; do
  if [[ -d "$HOME/.claude/$name" ]]; then
    cp -a "$HOME/.claude/$name" "$out/native-$name"
  fi
done
if [[ -d tmp/review-scope ]]; then
  cp -a tmp/review-scope "$out/review-scope"
fi
exit "$code"
