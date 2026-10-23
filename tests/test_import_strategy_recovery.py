"""Prepared imports require full strategy content and all supporting context."""
import json
import os
import signal
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import import_strategy as importer  # noqa: E402
import pull_strategy as puller  # noqa: E402
from artifact_utils import write_frontmatter  # noqa: E402
from jira_utils import add_attachment, markdown_to_adf  # noqa: E402

KEY, RFE = "RHAISTRAT-7921", "RHAIRFE-7921"
REVIEW = b"## Prior review\nPreserve the earlier recovery findings.\n"


@pytest.fixture
def prepared(tmp_path, jira, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRAT_MANAGED_RUN", raising=False)
    monkeypatch.delenv("STRAT_EFFECTS_DIR", raising=False)
    for name, value in {"JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}.items():
        monkeypatch.setenv(name, value)
    jira.create(RFE, "Recover import context", "The complete source business need.")
    jira.create(KEY, "Recover context before refining", (
        "## Business Need (from RFE)\n\nThe complete source business need.\n\n"
        + puller.STRATEGY_HEADING + "\n\nPreserve the technical recovery plan.\n\n"
        "## Staff Engineer / SME Input\n\nKeep the SME-selected recovery policy.\n"))
    jira.request("POST", "/rest/api/3/issueLink", {
        "type": {"name": "Cloners"}, "inwardIssue": {"key": KEY}, "outwardIssue": {"key": RFE}})
    for key, body in [(RFE, "SME guidance from the source comments."),
                      (KEY, "[Strat Creator] Saved review summary.")]:
        jira.request("POST", f"/rest/api/3/issue/{key}/comment", {"body": markdown_to_adf(body)})
    upload = tmp_path / f"{KEY}-review.md"
    upload.write_bytes(REVIEW)
    add_attachment(jira.url, "admin", "admin", KEY, str(upload))
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp/strat-input.json").write_text(json.dumps({
        "run_id": "import-recovery", "dry_run": False, "acquired_keys": [RFE]}))
    return tmp_path


def existing_task(root):
    task = root / f"artifacts/strat-tasks/{KEY}.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(puller.STRATEGY_HEADING + "\n\nKeep this already refined strategy unchanged.\n")
    write_frontmatter(task, {"strat_id": KEY, "source_rfe": RFE, "jira_key": KEY,
                            "title": "Refined local work", "priority": "Major", "status": "Refined"}, "strat-task")
    return task


def assert_context(root):
    artifacts = root / "artifacts"
    assert "complete source business need" in (artifacts / f"strat-originals/{RFE}.md").read_text()
    assert "Keep the SME-selected recovery policy" in (artifacts / f"strat-originals/{KEY}.md").read_text()
    assert "SME guidance" in (artifacts / f"strat-originals/{RFE}-comments.md").read_text()
    assert "Saved review summary" in (artifacts / f"strat-reviews/{KEY}-review-summary.md").read_text()
    assert (artifacts / f"strat-reviews/{KEY}-review.md").read_bytes() == REVIEW


@pytest.mark.parametrize("entry", ["pull", "import"])
@pytest.mark.parametrize("existing", [False, True])
def test_overflow_without_named_attachment_preserves_workspace(prepared, jira, entry, existing):
    if existing:
        existing_task(prepared)
    notice = puller.STRATEGY_HEADING + "\n\nThis strategy " + puller.ATTACHMENT_NOTICE_MARKER + ".\n"
    jira.request("PUT", f"/rest/api/3/issue/{KEY}", {"fields": {"description": markdown_to_adf(notice)}})
    before = {str(path.relative_to(prepared)): path.read_bytes()
              for path in (prepared / "artifacts").rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="(?i)strategy attachment.*missing"):
        if entry == "import":
            importer.import_strategy(KEY, RFE)
        else:
            puller.pull_strategy(jira.url, "admin", "admin", KEY, str(prepared / "artifacts"), pipeline_rfe=RFE)
    after = {str(path.relative_to(prepared)): path.read_bytes()
             for path in (prepared / "artifacts").rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("phase", ["rfe-comments", "strat-comments", "review-attachment"])
def test_retry_finishes_context_after_late_download_failure(prepared, monkeypatch, phase):
    with monkeypatch.context() as fault:
        if phase == "review-attachment":
            def fail(*args, **kwargs):
                raise urllib.error.URLError("authored prior-review download failure")
            fault.setattr(puller, "download_attachment", fail)
        else:
            original = puller.get_comments

            def fail(server, user, token, key):
                if key == (RFE if phase == "rfe-comments" else KEY):
                    raise urllib.error.URLError("authored comment download failure")
                return original(server, user, token, key)

            fault.setattr(puller, "get_comments", fail)
        with pytest.raises(urllib.error.URLError):
            importer.import_strategy(KEY, RFE)
    importer.import_strategy(KEY, RFE)
    assert_context(prepared)
    assert "Preserve the technical recovery plan" in (prepared / f"artifacts/strat-tasks/{KEY}.md").read_text()


def test_incomplete_legacy_import_recovers_context_without_overwriting_refinement(prepared):
    task = existing_task(prepared)
    before = task.read_bytes()
    comments = prepared / f"artifacts/strat-originals/{RFE}-comments.md"
    comments.parent.mkdir(parents=True)
    comments.write_text("Partial old download.")
    (comments.parent / f"{KEY}.md").write_text("Partial old STRAT snapshot.")
    review = prepared / f"artifacts/strat-reviews/{KEY}-review.md"
    review.parent.mkdir()
    review.write_text("Partial old review download.")
    importer.import_strategy(KEY, RFE)
    assert_context(prepared)
    assert task.read_bytes() == before


def test_complete_import_reuses_context_and_preserves_later_refinement(prepared, monkeypatch):
    importer.import_strategy(KEY, RFE)
    task = existing_task(prepared)
    before = {str(path): path.read_bytes() for path in (prepared / "artifacts").rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("completed import repeated its downloads")

    monkeypatch.setattr(importer, "pull_strategy", forbidden)
    assert importer.import_strategy(KEY, RFE)["already_imported"]
    assert {str(path): path.read_bytes() for path in (prepared / "artifacts").rglob("*") if path.is_file()} == before
    assert task.read_bytes() == before[str(task)]


def test_retry_after_task_publication_finishes_context_and_preserves_refinement(prepared, monkeypatch):
    destination = prepared / f"artifacts/strat-tasks/{KEY}.md"
    replace = os.replace

    def interrupted(source, target):
        replace(source, target)
        if Path(target).resolve() == destination:
            raise OSError("authored interruption after publishing the task")

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupted)
        with pytest.raises(OSError, match="after publishing the task"):
            importer.import_strategy(KEY, RFE)
    task = existing_task(prepared)
    refined = task.read_bytes()
    importer.import_strategy(KEY, RFE)
    assert_context(prepared)
    assert task.read_bytes() == refined


def test_incomplete_import_cannot_replace_context_bound_to_a_review(prepared):
    task = existing_task(prepared)
    before = task.read_bytes()
    session = prepared / f"tmp/review-composition/{KEY}/active.json"
    session.parent.mkdir(parents=True)
    session.write_text('{"session_id":"bound-review"}')
    with pytest.raises(ValueError, match="review already binds"):
        importer.import_strategy(KEY, RFE)
    assert task.read_bytes() == before


def test_complete_import_with_lost_context_stops_instead_of_claiming_success(prepared):
    importer.import_strategy(KEY, RFE)
    (prepared / f"artifacts/strat-originals/{RFE}-comments.md").unlink()
    with pytest.raises(ValueError, match="completed import.*missing"):
        importer.import_strategy(KEY, RFE)


def test_import_completion_belongs_to_the_prepared_run(prepared):
    importer.import_strategy(KEY, RFE)
    path = prepared / "tmp/strat-input.json"
    data = json.loads(path.read_text())
    data["run_id"] = "another-prepared-run"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="import.*identity"):
        importer.import_strategy(KEY, RFE)


def test_process_death_during_context_download_does_not_complete_import(prepared):
    child = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import pull_strategy
from import_strategy import import_strategy
def stopped(*args):
    os.kill(os.getpid(), signal.SIGKILL)
pull_strategy.get_comments = stopped
import_strategy(sys.argv[2], sys.argv[3])
'''
    process = subprocess.run([sys.executable, "-c", child, str(ROOT / "scripts"), KEY, RFE],
                             cwd=prepared, env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    (prepared / "killed-import.json").write_text(json.dumps({
        "exit": process.returncode, "stdout": process.stdout, "stderr": process.stderr}, indent=2))
    assert process.returncode == -signal.SIGKILL
    importer.import_strategy(KEY, RFE)
    assert_context(prepared)
