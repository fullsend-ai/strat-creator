#!/usr/bin/env bash
# Diagnostic job only; production uses Fullsend's standard Workflow entrypoint.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
model=rits/zai-org/glm-5-3
[[ "${PIPELINE_MODEL:-}" == "$model" ]] || { echo "Exact hosted GLM required" >&2; exit 2; }
out=${JOB_ARTIFACT_DIR:?dashboard artifacts required}
fixture="$root/tests/fixtures/refine-receipts"
git archive --format=tar.gz HEAD > "$out/candidate-source.tar.gz"
export ANTHROPIC_MODEL="$model" CLAUDE_CODE_SUBAGENT_MODEL="$model"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$model" ANTHROPIC_DEFAULT_SONNET_MODEL="$model"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$model" ANTHROPIC_DEFAULT_FABLE_MODEL="$model"
export CLAUDE_CODE_EFFORT_LEVEL=medium DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=1200000
unset JIRA_SERVER JIRA_USER JIRA_TOKEN
cp "$fixture/strategy-refine.md" .claude/skills/strategy-refine/SKILL.md
python3 tests/refine-receipts/fixture.py setup STRAT-9401 STRAT-9402
cp "$fixture/plugin/workflows/refine-receipts.js" "$out/fixture-workflow.js"
cp "$fixture/strategy-refine.md" "$out/fixture-skill.md"
printf '%s\n' '{"enableWorkflows":true,"effortLevel":"medium"}' > "$out/fixture-settings.json"
claude --version > "$out/claude-version.txt"
sha256sum "$(command -v claude)" > "$out/claude-sha256.txt"
git rev-parse HEAD > "$out/source-ref.txt"
set +e
timeout 1200 claude --print --verbose --output-format stream-json --max-turns 20 \
  --model "$model" --effort medium --dangerously-skip-permissions \
  --disallowed-tools Edit Write Agent \
  --settings "$out/fixture-settings.json" \
  --plugin-dir "$root/.fullsend/plugins/strat-pipeline" \
  --plugin-dir "$fixture/plugin" "/refine-receipts:refine-receipts" \
  2> >(tee "$out/claude-stderr.log" >&2) | tee "$out/stream.jsonl"
code=${PIPESTATUS[0]}
set -e
printf '%s\n' "$code" > "$out/claude-exit.txt"
config_root=${CLAUDE_CONFIG_DIR:-"$HOME/.claude"}
for name in projects tasks; do
  [[ ! -d "$config_root/$name" ]] || cp -a "$config_root/$name" "$out/native-$name"
done
cp -a tmp "$out/fixture-state"
cp -a artifacts/strat-tasks "$out/fixture-strategies"
exit "$code"
