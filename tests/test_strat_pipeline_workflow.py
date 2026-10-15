"""Headless smoke test of the strat-pipeline Workflow under fullsend's launch shape.

Runs the strat-pipeline Workflow from the harness plugin
(.fullsend/plugins/strat-pipeline) in a temporary copy of this repository,
through the same command line fullsend builds for an agent harness:

    claude --print --verbose --output-format stream-json --settings <hooks>
        --model <model> --plugin-dir <plugin> --agent strat-batch
        --dangerously-skip-permissions 'Run the agent task'

strategy-create, strategy-refine, the four reviewer skills and the scoring
rubric are replaced by stubs from tests/fixtures/workflow-smoke/, so no Jira
and no real review work is involved. The strat-scorer agent, parse_results.py,
apply_scores.py, strat_pipeline_state.py and strategy-review itself (with --scores-from
and --dry-run) are the repository's own.

This needs an authenticated `claude` CLI (2.1.154 or later) and calls the
model, so it is opt-in:

    STRAT_WORKFLOW_SMOKE=1 uv run pytest tests/test_strat_pipeline_workflow.py -v -s

Optional: CLAUDE_BIN (default `claude`), STRAT_SMOKE_MODEL (default
claude-opus-4-6), STRAT_SMOKE_KEEP=<dir> to keep the copy and stream there.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "workflow-smoke"
DEFAULT_PROMPT = "Run the agent task"  # fullsend's prompt when there is no feedback

pytestmark = pytest.mark.skipif(
    os.environ.get("STRAT_WORKFLOW_SMOKE") != "1",
    reason="set STRAT_WORKFLOW_SMOKE=1 to run the headless workflow smoke test (calls the model)",
)

RUN_INPUT = {
    "run_id": "smoke-1",
    "mode": "batch",
    "dry_run": True,
    "input_keys": ["RHAIRFE-9001", "RHAIRFE-9002", "RHAIRFE-9003", "RHAIRFE-9004"],
    "acquired_keys": ["RHAIRFE-9001", "RHAIRFE-9002", "RHAIRFE-9003"],
    "skipped": [{"key": "RHAIRFE-9004",
                 "reason": "blocked by label(s): strat-creator-needs-attention"}],
    "resume_points": {"RHAIRFE-9001": "create", "RHAIRFE-9002": "create", "RHAIRFE-9003": "create"},
    "resume_from": "create",
    "expected_skips": [{"key": "RHAIRFE-9003", "reason": "release scope: no required label or target version"}],
    "architecture_context": "available",
    "architecture_version": "rhoai-9.9",
}


def _copy_repo(dest):
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
        check=True, capture_output=True, text=True).stdout.split("\n")
    for rel in filter(None, files):
        src = ROOT / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def _install_stubs(repo, mode):
    skills = repo / ".claude" / "skills"
    for stub in (FIXTURES / "skills").iterdir():
        shutil.rmtree(skills / stub.name, ignore_errors=True)
        shutil.copytree(stub, skills / stub.name)
    shutil.copy2(FIXTURES / "agent_prompt.md", repo / "scripts" / "assess-strat" / "agent_prompt.md")
    context = repo / ".context" / "architecture-context"
    shutil.copytree(FIXTURES / "architecture-context", context)
    # fullsend installs the agent into $CLAUDE_CONFIG_DIR/agents; a project-scope
    # copy resolves the same --agent name without a separate config directory.
    agents = repo / ".claude" / "agents"
    shutil.copy2(repo / ".fullsend" / f"strat-{mode}" / "prompt.md", agents / f"strat-{mode}.md")
    (repo / "tmp").mkdir()
    (repo / "tmp" / "strat-input.json").write_text(json.dumps(RUN_INPUT, indent=2) + "\n")


def _events(stream):
    return [json.loads(line) for line in stream.splitlines() if line.startswith("{")]


def test_workflow_runs_headless_under_fullsend_launch_shape(tmp_path):
    keep = os.environ.get("STRAT_SMOKE_KEEP")
    base = Path(keep) if keep else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    repo, output = base / "repo", base / "output"
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(output, ignore_errors=True)
    for path in Path("/tmp/strat-assess").glob("STRAT-900*"):
        shutil.rmtree(path)
    output.mkdir()
    _copy_repo(repo)
    _install_stubs(repo, "batch")
    hooks = base / "hooks.json"
    hooks.write_text("{}\n")

    env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
    env.update({"FULLSEND_OUTPUT_DIR": str(output), "RFE_SKIP_BOOTSTRAP": "1",
                "DISABLE_AUTOUPDATER": "1"})
    cmd = [os.environ.get("CLAUDE_BIN", "claude"), "--print", "--verbose",
           "--output-format", "stream-json", "--settings", str(hooks),
           "--model", os.environ.get("STRAT_SMOKE_MODEL", "claude-opus-4-6"),
           "--plugin-dir", str(repo / ".fullsend" / "plugins" / "strat-pipeline"),
           "--agent", "strat-batch", "--dangerously-skip-permissions", DEFAULT_PROMPT]
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=3600)
    wall = time.monotonic() - started
    (base / "stream.jsonl").write_text(proc.stdout)
    (base / "stderr.txt").write_text(proc.stderr)
    events = _events(proc.stdout)
    assert proc.returncode == 0, proc.stderr[-2000:]

    tool_uses = [c for e in events if e.get("type") == "assistant"
                 for c in e["message"]["content"] if c.get("type") == "tool_use"]
    assert tool_uses and tool_uses[0]["name"] == "Workflow"
    assert tool_uses[0]["input"].get("name") == "strat-pipeline:strat-pipeline"
    notices = [i for i, e in enumerate(events) if e.get("subtype") == "task_notification"]
    results = [i for i, e in enumerate(events) if e.get("type") == "result"]
    assert notices and results and results[-1] > notices[-1], "turn ended before the workflow"

    result = json.loads((output / "agent-result.json").read_text())
    print(json.dumps(result, indent=2))
    print(f"wall {wall:.0f}s, result events at {results}, workflow notice at {notices}")
    assert result["action"] == "completed", result["errors"]
    assert result["errors"] == []
    assert result["completed_phases"] == ["create", "refine", "push", "review"]
    verdicts = {s["strat"]: s["recommendation"] for s in result["strategies"]}
    assert verdicts == {"STRAT-9001": "revise", "STRAT-9002": "approve"}
    assert {s["key"] for s in result["skipped"]} == {"RHAIRFE-9003", "RHAIRFE-9004"}

    for strat in ("STRAT-9001", "STRAT-9002"):
        review = repo / "artifacts" / "strat-reviews" / f"{strat}-review.md"
        assert review.is_file()
        assert (Path("/tmp/strat-assess") / strat / "scores.csv").is_file()
        # --dry-run: the Jira comment is saved locally instead of posted.
        assert (repo / "artifacts" / "strat-reviews" / f"{strat}-review-comment.md").is_file()
    progress = json.loads((repo / "tmp" / "strat-progress.json").read_text())
    assert set(progress["review"]) == {"STRAT-9001", "STRAT-9002"}
    assert progress["push"]["skipped"] is True
