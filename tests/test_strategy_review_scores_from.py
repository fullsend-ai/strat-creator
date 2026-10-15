"""Contract checks for strategy-review's --scores-from mode.

The Workflow script (.claude/workflows/strat-pipeline.js) launches the
strat-scorer itself and passes its result directory with --scores-from. These
checks keep the skill text and the workflow in step; the end-to-end run is the
headless smoke test in test_strat_pipeline_workflow.py.
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKILL = os.path.join(ROOT, ".claude", "skills", "strategy-review", "SKILL.md")
WORKFLOW = os.path.join(ROOT, ".fullsend", "plugins", "strat-pipeline", "workflows", "strat-pipeline.js")
INTERACTIVE_WORKFLOW = os.path.join(ROOT, "workflows", "strat-pipeline.js")


def _read(path):
    with open(path) as f:
        return f.read()


def _section(text, heading):
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end != -1 else len(text)]


def test_scores_from_skips_scorer_and_keeps_run_dir():
    step4 = _section(_read(SKILL), "## Step 4: Score Strategy")
    assert "--scores-from <dir>" in step4
    assert "Do NOT launch a scorer agent" in step4
    assert "do NOT delete or recreate `<dir>`" in step4
    assert "No scorer result at <dir>/<KEY>.result.md" in step4


def test_default_path_unchanged():
    step4 = _section(_read(SKILL), "## Step 4: Score Strategy")
    # Without the flag the skill still owns a clean run directory and the scorer.
    assert "rm -rf /tmp/strat-assess/review" in step4
    assert "subagent_type: strat-scorer" in step4


def test_step5_uses_the_given_run_dir():
    step5 = _section(_read(SKILL), "## Step 5: Parse Scores")
    assert "replace `/tmp/strat-assess/review` with `<dir>`" in step5
    assert step5.count("/tmp/strat-assess/review") >= 4



def test_workflow_passes_a_per_strategy_run_dir():
    script = _read(WORKFLOW)
    assert "--scores-from ${runDir}" in script
    # One run directory per strategy, so parallel reviews never share one.
    assert re.search(r"const runDir = `/tmp/strat-assess/\$\{p\.strat\}`", script)
    assert "agentType: `${NS}strat-scorer`" in script


def test_the_two_workflow_copies_are_identical():
    # fullsend loads the harness plugin copy; the strat-creator plugin ships the
    # other for interactive use. Plugin trees may not contain symlinks.
    assert _read(WORKFLOW) == _read(INTERACTIVE_WORKFLOW)

