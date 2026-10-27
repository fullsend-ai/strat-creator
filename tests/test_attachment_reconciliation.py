"""Attachment publication and readers must agree on the newest named content."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jira_utils as jira_api  # noqa: E402
from pull_strategy import pull_strategy  # noqa: E402
from push_strategy import STRATEGY_HEADING, _push_via_attachment  # noqa: E402
from strat_side_effects import UncertainWrite, atomic  # noqa: E402

KEY = "RHAISTRAT-7911"


@pytest.mark.parametrize("reversed_order", [False, True])
@pytest.mark.parametrize("latest_matches", [False, True])
def test_matching_considers_only_newest_named_attachment(monkeypatch, reversed_order, latest_matches):
    attachments = [{"id": "10", "created": "2026-10-22T12:00:00Z", "filename": "report.md", "content": "old"},
                   {"id": "11", "created": "2026-10-22T12:00:00Z", "filename": "report.md", "content": "latest"},
                   {"id": "99", "created": "2026-10-22T12:00:00Z", "filename": "other.md", "content": "other"}]
    latest = attachments[1]
    monkeypatch.setattr(jira_api, "get_issue", lambda *args, **kwargs: {
        "fields": {"attachment": list(reversed(attachments)) if reversed_order else attachments}})
    downloaded = []

    def download(server, user, token, url, destination):
        downloaded.append(url)
        Path(destination).write_bytes(b"A" if url == "old" or latest_matches else b"B")

    monkeypatch.setattr(jira_api, "download_attachment", download)
    actual = jira_api.find_matching_attachments("local", "user", "token", KEY, "report.md", b"A")
    assert actual == ([latest] if latest_matches else None)
    assert downloaded == ["latest"]


@pytest.mark.parametrize("kind", ["strategy", "review"])
def test_revision_back_to_older_bytes_publishes_and_pulls_newest(tmp_path, jira, monkeypatch, kind):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRAT_MANAGED_RUN", raising=False)
    jira.create(KEY, "Intentional content revision", "", labels=["strat-creator-rubric-pass"])
    filename = f"{KEY}-{'strategy' if kind == 'strategy' else 'review'}.md"
    upload = tmp_path / filename
    versions = ["First approved design.", "Second approved design.", "First approved design."]
    for number, text in enumerate(versions, 1):
        # Deliberate revisions are separate invocations, never changed retries
        # of an already journaled write in the same prepared run.
        monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / f"effects-{number}"))
        monkeypatch.setenv("STRAT_RUN_ID", f"revision-{number}")
        if kind == "strategy":
            fields = jira.get(KEY)["fields"]
            _push_via_attachment(jira.url, "admin", "admin", KEY,
                                 jira_api.adf_to_markdown(fields.get("description")),
                                 f"{STRATEGY_HEADING}\n\n{text}", None, fields.get("attachment", []))
        else:
            upload.write_text(f"## Review\n\n{text}\n")
            jira_api.add_attachment(jira.url, "admin", "admin", KEY, str(upload))
        destination = tmp_path / "pulled"
        pull_strategy(jira.url, "admin", "admin", KEY, str(destination))
        path = destination / (f"strat-tasks/{KEY}.md" if kind == "strategy" else f"strat-reviews/{KEY}-review.md")
        assert text in path.read_text()
        assert len(jira.get(KEY)["fields"]["attachment"]) == number
    # Retrying the exact final bytes does not append a fourth copy even though
    # two older/newer historical copies of A now exist.
    if kind == "strategy":
        upload.write_text(f"{STRATEGY_HEADING}\n\n{versions[-1]}\n")
    jira_api.add_attachment(jira.url, "admin", "admin", KEY, str(upload), filename=filename)
    assert len(jira.get(KEY)["fields"]["attachment"]) == 3


@pytest.mark.parametrize("status", ["pending", "verified"])
def test_intervening_attachment_does_not_replay_same_run_write(tmp_path, jira, monkeypatch, status):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRAT_MANAGED_RUN", raising=False)
    monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("STRAT_RUN_ID", "original-run")
    jira.create(KEY, "Attachment recovery", "")
    upload = tmp_path / "report.md"
    upload.write_bytes(b"A")
    jira_api.add_attachment(jira.url, "admin", "admin", KEY, str(upload))
    record_path, = (tmp_path / "effects").glob("*.json")
    record = json.loads(record_path.read_text())
    record["status"] = status  # Also model intent retained after a lost response.
    atomic(record_path, record)
    with monkeypatch.context() as external:
        external.delenv("STRAT_EFFECTS_DIR")
        upload.write_bytes(b"B")
        jira_api.add_attachment(jira.url, "admin", "admin", KEY, str(upload))
    upload.write_bytes(b"A")
    with pytest.raises(UncertainWrite, match="not visible"):
        jira_api.add_attachment(jira.url, "admin", "admin", KEY, str(upload))
    assert len(jira.get(KEY)["fields"]["attachment"]) == 2
    assert json.loads(record_path.read_text()) == record
