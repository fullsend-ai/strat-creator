#!/usr/bin/env bash
# before_each staging hook for the strat-creator eval. Runs once per case with
# cwd = the harness case workspace, BEFORE the refine/review steps.
#
# The harness symlinks .claude/skills and .context from the project root into each
# case workspace. The strat skills' bootstrap WRITES into those (installs the
# strat-scorer agent, copies assess-strat), so we replace the symlinks with real
# copies/dirs to keep writes inside the throwaway workspace (never back into the repo).
# Then we stage strategy-create's mechanical output (the create->refine handoff) so
# `refine` and `review` run exactly like the prod strat-pipeline.
#
# Assets (assess-strat + architecture-context) are staged into eval/.assets by the
# before_all hook (eval/scripts/stage-assets.sh). Fully offline: --dry-run in the step
# args skips Jira; assess-strat + architecture-context are local.
#
# Harness-injected env: AGENT_EVAL_PROJECT_ROOT, CASE_WORKSPACE, CASE_INPUT.
set -euo pipefail

WS="${CASE_WORKSPACE:?CASE_WORKSPACE not set}"
ROOT="${AGENT_EVAL_PROJECT_ROOT:?AGENT_EVAL_PROJECT_ROOT not set}"
ASSETS="$ROOT/eval/.assets"
ARCH_CTX="$ASSETS/architecture-context"
INPUT="${CASE_INPUT:-$WS/input.yaml}"

log() { echo "[stage-case] $*" >&2; }

cd "$WS" || { log "FATAL: cannot cd $WS"; exit 1; }

# strat_id / rfe_key from the case input.yaml
_field() { python3 -c "import sys,yaml;print((yaml.safe_load(open(sys.argv[1])) or {}).get(sys.argv[2],''))" "$INPUT" "$1"; }
STRAT_ID="$(_field strat_id)"
RFE_KEY="$(_field rfe_key)"
# Validate before composing paths: these come from the case input.yaml and are
# interpolated into destination paths below, so a traversal or metacharacter would
# escape the workspace. Anchored regex, not a `case` glob -- a glob's * matches / and
# ; as well, so STRAT-[0-9]* happily accepts "STRAT-1/../../outside".
[ -n "$RFE_KEY" ] || { log "FATAL: rfe_key missing in $INPUT"; exit 1; }
# Both are <PROJECT>-<n> keys: an rfe-strategy case is STRAT-<n> from RHAIRFE-<n>, an
# initiative-strategy case is RHOAIENG-<n> on the Initiative itself (build_dataset.py --type).
[[ "$STRAT_ID" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || {
  log "FATAL: strat_id must be <PROJECT>-<n>, got '$STRAT_ID'"; exit 1; }
[[ "$RFE_KEY" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || {
  log "FATAL: rfe_key must be <PROJECT>-<n>, got '$RFE_KEY'"; exit 1; }
log "staging $STRAT_ID ($RFE_KEY) in $WS"

# --- 1. Real (not symlinked) project tree so skill bootstrap can't write into the
#        repo. Replace the harness's .claude/skills + .context symlinks with copies.
rm -rf .claude/skills .claude/agents .context
mkdir -p .claude artifacts/strat-tasks artifacts/strat-reviews artifacts/strat-originals .context
cp -R "$ROOT/.claude/skills" .claude/skills
if [ -d "$ROOT/.claude/agents" ]; then cp -R "$ROOT/.claude/agents" .claude/agents; fi

# Pre-register the assess-strat scorer agent. Claude Code registers agent types at
# session STARTUP; the review skill's bootstrap installs strat-scorer mid-session
# (too late), so without this the scorer falls back to a general-purpose agent.
# Staging it here lets the review's `strat-scorer` agent resolve to the real one.
mkdir -p .claude/agents
[ -d "$ASSETS/assess-strat/agents" ] && cp "$ASSETS/assess-strat/agents/"*.md .claude/agents/ 2>/dev/null || true

# Both assets are required: without architecture-context refine cannot ground (and
# the grounding gate is meaningless), without assess-strat the review has no rubric.
# A missing one used to warn and continue, which produced scored-looking but hollow
# results; `[ -d x ] && cmd || log` also always exits 0, so set -e never caught it.
[ -d "$ARCH_CTX" ] || {
  log "FATAL: architecture-context missing under $ASSETS (run eval/scripts/stage-assets.sh)"; exit 1; }
[ -d "$ASSETS/assess-strat" ] || {
  log "FATAL: assess-strat missing under $ASSETS (run eval/scripts/stage-assets.sh)"; exit 1; }
# architecture-context: symlink the shared read-only copy (cheap; skills read it).
ln -sfn "$ARCH_CTX" .context/architecture-context
# assess-strat: real copy — the review skill's bootstrap runs offline and writes here.
cp -R "$ASSETS/assess-strat" .context/assess-strat

# Keep collect.py's git-diff (_modified capture) from flagging the staged project
# tree — .context alone is 900+ arch-context/rubric files.
if [ -d .git ]; then
  printf '%s\n' '.context/' 'output/' '.claude/' '.memsearch/' >> .git/info/exclude 2>/dev/null || true
fi

# --- 2. Stage strategy-create's output (the create->refine handoff) --------------
[ -f stub.md ] || { log "FATAL: stub.md not in workspace"; exit 1; }
cp stub.md "artifacts/strat-tasks/${STRAT_ID}.md"
if [ -f rfe-original.md ]; then cp rfe-original.md "artifacts/strat-originals/${RFE_KEY}.md"; fi
if [ -f rfe-comments.md ]; then cp rfe-comments.md "artifacts/strat-originals/${RFE_KEY}-comments.md"; fi

log "staged: project tree + create stub for $STRAT_ID"
