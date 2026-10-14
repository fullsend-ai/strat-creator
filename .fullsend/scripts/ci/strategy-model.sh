#!/usr/bin/env bash
# POC-only model selection; never source this for unrelated harnesses.
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6
export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-6
export CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1
unset ANTHROPIC_MODEL
