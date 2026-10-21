"""Live publication cannot become a local-only completion after partial writes."""

import argparse
import json
import sys
import urllib.error
from pathlib import Path

import pytest
from review_composition_support import prepare
from test_side_effect_recovery import workspace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jira_utils  # noqa: E402
import publish_review  # noqa: E402
import strat_pipeline_state as state  # noqa: E402
from artifact_utils import write_frontmatter  # noqa: E402

KEY = "RHAISTRAT-7701"


@pytest.fixture
def prepared(tmp_path, jira, monkeypatch):
    env = workspace(tmp_path, jira)
    for name in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FULLSEND_OUTPUT_DIR", str(tmp_path / "output"))
    data = json.loads(state.INPUT.read_text())
    data.update(mode="single", input_keys=["RHAIRFE-7701"], skipped=[], architecture_context="available")
    state.INPUT.write_text(json.dumps(data))
    task = tmp_path / f"artifacts/strat-tasks/{KEY}.md"
    task.parent.mkdir(parents=True)
    task.write_text("## Strategy\nAuthored refined strategy.\n")
    write_frontmatter(task, {"strat_id": KEY, "source_rfe": "RHAIRFE-7701", "jira_key": KEY,
                            "title": "Authored", "priority": "Major", "status": "Refined"}, "strat-task")
    prepare(tmp_path, KEY, "RHAIRFE-7701", (2, 2, 2, 2),
            verdicts={"feasibility": "approve", "testability": "revise", "scope": "approve", "architecture": "approve"})
    when = "2026-10-21T14:00:00Z"
    state.save_progress({"run_id": data["run_id"], "create": {"done_at": when, "pending": [], "skipped": [],
                         "strategies": [{"rfe": "RHAIRFE-7701", "strat": KEY}]},
                         "refine": {KEY: when}, "push": {"done_at": when, "strategies": [KEY]}, "review": {}})
    return tmp_path


def test_live_publisher_rejects_explicit_dry_run(prepared, jira):
    with pytest.raises(ValueError, match="dry.run.*live"):
        publish_review.publish(prepared, KEY, dry_run=True)
    assert not jira_utils.get_comments(jira.url, "admin", "admin", KEY)
    assert not jira.get(KEY)["fields"].get("attachment")


def test_live_publisher_requires_credentials(prepared, monkeypatch):
    monkeypatch.delenv("JIRA_TOKEN")
    with pytest.raises(ValueError, match="credentials"):
        publish_review.publish(prepared, KEY)


def test_partial_publication_cannot_record_completion_and_recovers_without_duplicates(
    prepared, jira, monkeypatch, capsys
):
    original = jira_utils.download_attachment

    def blocked(*args):
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(jira_utils, "download_attachment", blocked)
    with pytest.raises(urllib.error.URLError):
        publish_review.publish(prepared, KEY)
    assert len(jira_utils.get_comments(jira.url, "admin", "admin", KEY)) == 1
    assert len(jira.get(KEY)["fields"]["attachment"]) == 1
    assert "strat-creator-rubric-pass" not in jira.get(KEY)["fields"]["labels"]
    progress = state.PROGRESS.read_bytes()
    with pytest.raises(SystemExit):
        state.cmd_record_review(argparse.Namespace(key=KEY))
    assert "publication" in capsys.readouterr().err
    assert state.PROGRESS.read_bytes() == progress
    state.cmd_result(argparse.Namespace(failed=None))
    assert json.loads((prepared / "output/agent-result.json").read_text())["action"] == "failed"
    monkeypatch.setattr(jira_utils, "download_attachment", original)
    before = (prepared / f"artifacts/strat-reviews/{KEY}-review.md").read_bytes()
    for _ in range(2):
        assert publish_review.publish(prepared, KEY)["recommendation"] == "approve"
        state.cmd_record_review(argparse.Namespace(key=KEY))
    assert (prepared / f"artifacts/strat-reviews/{KEY}-review.md").read_bytes() == before
    assert len(jira_utils.get_comments(jira.url, "admin", "admin", KEY)) == 1
    assert len(jira.get(KEY)["fields"]["attachment"]) == 1
    assert "strat-creator-rubric-pass" in jira.get(KEY)["fields"]["labels"]
    state.cmd_result(argparse.Namespace(failed=None))
    assert json.loads((prepared / "output/agent-result.json").read_text())["action"] == "completed"


@pytest.mark.parametrize("missing", ["comment", "attachment", "label"])
def test_live_review_recording_reads_back_all_publication_effects(prepared, jira, missing, monkeypatch, capsys):
    assert publish_review.publish(prepared, KEY)["recommendation"] == "approve"
    if missing == "comment":
        # The emulator has no comment DELETE endpoint. Model a later read
        # that no longer finds the comment, without allowing another write.
        monkeypatch.setattr(jira_utils, "get_comments", lambda *args: [])
    elif missing == "attachment":
        attachment = jira.get(KEY)["fields"]["attachment"][0]
        jira.request("DELETE", f"/rest/api/3/attachment/{attachment['id']}")
    else:
        jira.request("PUT", f"/rest/api/3/issue/{KEY}",
                     {"update": {"labels": [{"remove": "strat-creator-rubric-pass"}]}})
    progress = state.PROGRESS.read_bytes()
    with pytest.raises(SystemExit):
        state.cmd_record_review(argparse.Namespace(key=KEY))
    assert "publication" in capsys.readouterr().err
    assert state.PROGRESS.read_bytes() == progress


@pytest.mark.parametrize("standalone", [False, True])
def test_intentional_dry_run_still_saves_locally(prepared, monkeypatch, standalone):
    if standalone:
        state.INPUT.unlink()
    else:
        data = json.loads(state.INPUT.read_text())
        data["dry_run"] = True
        state.INPUT.write_text(json.dumps(data))
    for name in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN"):
        monkeypatch.delenv(name)
    assert publish_review.publish(prepared, KEY, dry_run=True)["recommendation"] == "approve"
    assert (prepared / f"artifacts/strat-reviews/{KEY}-review-comment.md").is_file()
