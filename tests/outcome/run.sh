#!/usr/bin/env bash
# Diagnostic job, using the actual final Workflow step; no custom production entrypoint.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
model=rits/zai-org/glm-5-3
[[ "${PIPELINE_MODEL:-}" == "$model" ]] || { echo "Exact hosted GLM required" >&2; exit 2; }
out=${JOB_ARTIFACT_DIR:?dashboard artifacts required}
# Fresh per-test native state; never copy the developer's global sessions.
export CLAUDE_CONFIG_DIR
CLAUDE_CONFIG_DIR=$(mktemp -d "$out/claude-config.XXXXXX")
git archive --format=tar.gz HEAD > "$out/candidate-source.tar.gz"
export ANTHROPIC_MODEL="$model" CLAUDE_CODE_SUBAGENT_MODEL="$model"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$model" ANTHROPIC_DEFAULT_SONNET_MODEL="$model"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$model" ANTHROPIC_DEFAULT_FABLE_MODEL="$model"
export CLAUDE_CODE_EFFORT_LEVEL=medium DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=600000
export FULLSEND_OUTPUT_DIR="$root/output"
unset JIRA_SERVER JIRA_USER JIRA_TOKEN
python3 tests/outcome/fixture.py setup
cp tests/fixtures/outcome/plugin/workflows/outcome.js "$out/fixture-workflow.js"
printf '%s\n' '{"enableWorkflows":true,"effortLevel":"medium"}' > "$out/fixture-settings.json"
claude --version > "$out/claude-version.txt"
sha256sum "$(command -v claude)" > "$out/claude-sha256.txt"
git rev-parse HEAD > "$out/source-ref.txt"
set +e
timeout 600 claude --print --verbose --output-format stream-json --max-turns 12 \
  --model "$model" --effort medium --dangerously-skip-permissions \
  --disallowed-tools Edit Write Agent Skill \
  --settings "$out/fixture-settings.json" \
  --plugin-dir "$root/.fullsend/plugins/strat-pipeline" \
  --plugin-dir "$root/tests/fixtures/outcome/plugin" "/outcome:outcome" \
  2> >(tee "$out/claude-stderr.log" >&2) | tee "$out/stream.jsonl"
code=${PIPESTATUS[0]}
set -e
printf '%s\n' "$code" > "$out/claude-exit.txt"
config_root=${CLAUDE_CONFIG_DIR:-"$HOME/.claude"}
for name in projects tasks; do
  [[ ! -d "$config_root/$name" ]] || cp -a "$config_root/$name" "$out/native-$name"
done
mkdir -p "$out/fixture"
cp -a tmp artifacts output "$out/fixture/"
python3 tests/outcome/fixture.py inspect --artifacts "$out" --cli-exit "$code"
