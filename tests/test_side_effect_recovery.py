"""Real process death / lost-response tests against the isolated Jira emulator.

No model/provider calls. Set S1_RECOVERY_CAPTURE=1 to retain HTTP records,
strace and child outputs in pytest's controlled temp directory for archival.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from jira_utils import add_comment, get_comments, markdown_to_adf  # noqa: E402
from strat_side_effects import UncertainWrite, digest, owner_label, perform  # noqa: E402

CHILD = r'''
import json, os, runpy, signal, sys, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import jira_utils as j
operation, mode = sys.argv[2:4]
server, user, token = j.require_env()
log = Path('http-' + operation + '-' + mode + '-' + str(os.getpid()) + '.jsonl')
def event(value):
    with log.open('a') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')
        stream.flush(); os.fsync(stream.fileno())
def fault():
    if mode == 'kill':
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == 'lost':
        raise urllib.error.HTTPError(server, 503, 'authored lost response', {}, None)
original = j.api_call
def api(server, path, user, token, body=None, method=None):
    event({'kind':'request', 'path':path, 'method':method or ('POST' if body else 'GET'), 'body':body})
    result = original(server, path, user, token, body, method)
    event({'kind':'response', 'path':path, 'body':result})
    hit = ((operation == 'clone' and path == '/issue' and body is not None) or
           (operation == 'link' and path == '/issueLink') or
           (operation in ('comment','review') and path.endswith('/comment') and body is not None) or
           (operation == 'description' and method == 'PUT' and 'description' in body.get('fields',{})) or
           (operation in ('label','lock','unlock') and method == 'PUT' and 'update' in body))
    if hit:
        fault()
    return result
j.api_call = api
urlopen = urllib.request.urlopen
def upload(req, *args, **kwargs):
    hit = (isinstance(req, urllib.request.Request) and req.get_method() == 'POST'
           and req.full_url.endswith('/attachments'))
    if hit:
        event({'kind':'attachment_request','bytes':len(req.data)})
    result = urlopen(req, *args, **kwargs)
    if hit:
        event({'kind':'attachment_committed','status':result.status})
        if mode != 'normal':
            result.close(); fault()
    return result
urllib.request.urlopen = upload
if operation == 'clone':
    sys.argv = ['clone_issue.py','RHAIRFE-7701','--target-project','RHAISTRAT']
    runpy.run_path(str(Path(sys.path[0]) / 'clone_issue.py'), run_name='__main__')
elif operation == 'link':
    j.create_issue_link(server,user,token,'Cloners','RHAISTRAT-7701','RHAIRFE-7701')
elif operation == 'comment':
    j.add_comment(server,user,token,'RHAISTRAT-7701',j.markdown_to_adf('Authored numeric review summary.'))
elif operation == 'attachment':
    j.add_attachment(server,user,token,'RHAISTRAT-7701','report.md')
elif operation == 'description':
    j.set_fields_verified(server,user,token,'RHAISTRAT-7701',{'description':j.markdown_to_adf('Completed strategy.')})
elif operation == 'label':
    j.add_labels(server,user,token,'RHAISTRAT-7701',['strat-creator-rubric-pass'])
elif operation == 'review':
    from publish_review import publish
    print(json.dumps(publish(Path.cwd(),'RHAISTRAT-7701')))
elif operation == 'lock':
    from lock_issues import lock_owned
    lock_owned(server,user,token,['RHAIRFE-7701'],'owned-rfe-ids.txt','recovery-run')
elif operation == 'unlock':
    from lock_issues import unlock_owned
    unlock_owned(server,user,token,['RHAIRFE-7701'],'recovery-run')
'''


def workspace(tmp_path, jira, linked=True):
    owner = "recovery-run"
    jira.create("RHAIRFE-7701", "Authored recovery RFE", "Source.",
                labels=["strat-creator-processing", owner_label(owner)])
    jira.create("RHAISTRAT-7701", "Authored strategy", "Original.", issue_type="Feature")
    if linked:
        jira.request("POST", "/rest/api/3/issueLink", {"type": {"name": "Cloners"},
                     "inwardIssue": {"key": "RHAISTRAT-7701"}, "outwardIssue": {"key": "RHAIRFE-7701"}})
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp/strat-input.json").write_text(json.dumps({"run_id": owner, "lock_owner": owner,
                                                          "dry_run": False, "acquired_keys": ["RHAIRFE-7701"]}))
    (tmp_path / "report.md").write_text("Authored immutable attachment.\n")
    return {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}


def child(root, env, operation, mode):
    command = [sys.executable, "-c", CHILD, str(ROOT / "scripts"), operation, mode]
    if os.environ.get("S1_RECOVERY_CAPTURE") and shutil.which("strace"):
        command = ["strace", "-ff", "-o", str(root / f"strace-{operation}-{mode}"), *command]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    (root / f"child-{operation}-{mode}-{len(list(root.glob('child-*.json')))}.json").write_text(
        json.dumps({"operation": operation, "fault": mode, "exit": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr}, indent=2))
    return result


@pytest.mark.parametrize("operation", ["clone", "link", "comment", "attachment", "description", "label"])
@pytest.mark.parametrize("fault", ["kill", "lost"])
def test_commit_before_process_death_or_response_loss(tmp_path, jira, operation, fault):
    env = workspace(tmp_path, jira, linked=operation not in ("clone", "link"))
    first = child(tmp_path, env, operation, fault)
    assert first.returncode != 0 if fault == "kill" else first.returncode == 0, first.stderr
    # Exact same operation in a new process / agent restart.
    recovered = child(tmp_path, env, operation, "normal")
    assert recovered.returncode == 0, recovered.stderr
    effect_records = [json.loads(p.read_text()) for p in (tmp_path / "tmp/strat-effects").glob("*.json")]
    assert all(record["status"] == "verified" for record in effect_records)
    assert len(get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")) == (1 if operation == "comment" else 0)
    attachments = jira.get("RHAISTRAT-7701")["fields"].get("attachment", [])
    assert len(attachments) == (1 if operation == "attachment" else 0)
    links = jira.get("RHAIRFE-7701")["fields"].get("issuelinks", [])
    assert len(links) == 1
    if operation == "clone":
        assert recovered.stdout.strip().startswith("RHAISTRAT-")
        if fault == "lost":
            assert recovered.stdout.strip() == first.stdout.strip()
    requests = [json.loads(line) for path in tmp_path.glob("http-*.jsonl") for line in path.read_text().splitlines()]
    target = {"clone": "/issue", "link": "/issueLink", "comment": "/issue/RHAISTRAT-7701/comment"}.get(operation)
    if target:
        assert sum(r.get("kind") == "request" and r.get("path") == target and r.get("body") is not None
                   for r in requests) == 1
    if operation == "attachment":
        assert sum(r["kind"] == "attachment_request" for r in requests) == 1


def test_concurrent_recovery_posts_one_comment(tmp_path, jira):
    env = workspace(tmp_path, jira)
    command = [sys.executable, "-c", CHILD, str(ROOT / "scripts"), "comment", "normal"]
    processes = [subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(8)]
    results = [(p.communicate(timeout=60), p.returncode) for p in processes]
    assert all(code == 0 for _, code in results), results
    assert len(get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")) == 1


def test_owned_lock_recovery_and_cleanup_after_process_death(tmp_path, jira):
    env = workspace(tmp_path, jira)
    (tmp_path / "tmp/strat-input.json").unlink()  # Host pre-script, before prepared input exists.
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-7701", {"fields": {"labels": []}})
    killed = child(tmp_path, env, "lock", "kill")
    assert killed.returncode != 0
    assert (tmp_path / "owned-rfe-ids.txt").read_text().split() == ["RHAIRFE-7701"]
    recovered = child(tmp_path, env, "lock", "normal")
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "RHAIRFE-7701"
    assert (tmp_path / "owned-rfe-ids.txt").read_text().split() == ["RHAIRFE-7701"]
    killed = child(tmp_path, env, "unlock", "kill")
    assert killed.returncode != 0
    recovered = child(tmp_path, env, "unlock", "normal")
    assert recovered.returncode == 0, recovered.stderr
    assert not set(jira.get("RHAIRFE-7701")["fields"]["labels"]) & {
        "strat-creator-processing", owner_label("recovery-run")}


def test_duplicate_remote_comments_are_not_silently_adopted(tmp_path, jira, monkeypatch):
    workspace(tmp_path, jira)
    body = markdown_to_adf("Duplicated authored summary.")
    for _ in range(2):
        jira.request("POST", "/rest/api/3/issue/RHAISTRAT-7701/comment", {"body": body})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="duplicate"):
        add_comment(jira.url, "admin", "admin", "RHAISTRAT-7701", body)
    assert len(get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")) == 2


def test_http_read_retries_are_distinct_from_write_reconciliation(monkeypatch):
    import urllib.error

    import jira_utils
    calls = []
    def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            raise urllib.error.HTTPError("local", 503, "authored", {}, None)
        return {"ok": True}
    monkeypatch.setattr(jira_utils, "api_call", flaky)
    monkeypatch.setattr(jira_utils.time, "sleep", lambda _: None)
    assert jira_utils.api_call_with_retry("local", "/issue/key", "user", "placeholder") == {"ok": True}
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(urllib.error.HTTPError):
        jira_utils.api_call_with_retry("local", "/issue", "user", "placeholder", body={"fields": {}})
    assert len(calls) == 1


def test_uncertain_absence_does_not_repost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("STRAT_RUN_ID", "run")
    writes = []
    def interrupted():
        writes.append(1)
        raise ConnectionError("lost response")
    with pytest.raises(ConnectionError):
        perform("local", "comment", "key", "body", lambda: None, interrupted)
    with pytest.raises(UncertainWrite, match="not visible"):
        perform("local", "comment", "key", "body", lambda: None, interrupted)
    assert writes == [1]


def test_changed_retry_summary_is_rejected(tmp_path, jira, monkeypatch):
    workspace(tmp_path, jira)
    monkeypatch.chdir(tmp_path)
    add_comment(jira.url, "admin", "admin", "RHAISTRAT-7701", markdown_to_adf("Original summary."))
    with pytest.raises(UncertainWrite, match="identity mismatch"):
        add_comment(jira.url, "admin", "admin", "RHAISTRAT-7701", markdown_to_adf("Regenerated summary."))
    assert len(get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")) == 1


def test_wrong_owner_blocks_publication_and_cleanup(tmp_path, jira, monkeypatch):
    from lock_issues import unlock_owned
    env = workspace(tmp_path, jira)
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-7701", {"update": {"labels": [{"add": owner_label("other")}]}})
    result = child(tmp_path, env, "comment", "normal")
    assert result.returncode != 0
    assert not get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")
    with pytest.raises(RuntimeError, match="foreign or conflicting"):
        unlock_owned(jira.url, "admin", "admin", ["RHAIRFE-7701"], "recovery-run")
    assert "strat-creator-processing" in jira.get("RHAIRFE-7701")["fields"]["labels"]


def test_completed_review_recovers_publication_without_regeneration(tmp_path, jira):
    from artifact_utils import write_frontmatter
    from review_composition_support import prepare
    env = workspace(tmp_path, jira)
    task = tmp_path / "artifacts/strat-tasks/RHAISTRAT-7701.md"
    task.parent.mkdir(parents=True)
    task.write_text("## Strategy\nAuthored strategy.\n")
    write_frontmatter(task, {"strat_id": "RHAISTRAT-7701", "title": "Authored", "source_rfe": "RHAIRFE-7701",
                            "priority": "Major", "status": "Refined", "jira_key": "RHAISTRAT-7701"}, "strat-task")
    prepare(tmp_path, "RHAISTRAT-7701", "RHAIRFE-7701", [2, 2, 2, 2],
            verdicts={"feasibility": "approve", "testability": "approve", "scope": "revise", "architecture": "approve"})
    before = {str(p): digest(p.read_text()) for p in (tmp_path / "artifacts").rglob("*") if p.is_file()}
    first = child(tmp_path, env, "review", "kill")
    assert first.returncode != 0
    recovered = child(tmp_path, env, "review", "normal")
    assert recovered.returncode == 0, recovered.stderr
    repeated = child(tmp_path, env, "review", "normal")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(recovered.stdout) == {"key": "RHAISTRAT-7701", "recommendation": "approve",
                                          "needs_attention": False}
    assert all(digest(Path(p).read_text()) == expected for p, expected in before.items())
    assert len(get_comments(jira.url, "admin", "admin", "RHAISTRAT-7701")) == 1
    assert len(jira.get("RHAISTRAT-7701")["fields"]["attachment"]) == 1
    text = (tmp_path / "artifacts/strat-reviews/RHAISTRAT-7701-review-comment.md").read_text()
    assert "Score: 8/8" in text and "scope" in text


def load_publisher():
    spec = importlib.util.spec_from_file_location("result_publisher", ROOT / ".fullsend/shared/publish/push-results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def test_git_publication_retry_reuses_run_and_commit(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "test")
    git(work, "config", "user.email", "test@example.com")
    (work / "README.md").write_text("Authored publication fixture.\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "initial")
    git(work, "push", "origin", "main")
    task = work / "strat-tasks/RHAISTRAT-7701.md"
    task.parent.mkdir()
    task.write_text("Immutable strategy.\n")
    script = ROOT / ".fullsend/shared/publish/push-results.py"
    env = {**os.environ, "RESULTS_PUSH_TOKEN": "isolated-local-git-placeholder"}
    command = [sys.executable, str(script), "--results-dir", str(work), "--results-repo", str(remote),
               "--run-id", "run-7"]
    first = subprocess.run(command + ["--dry-run"], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr  # Simulate death after commit, before push.
    committed = git(work, "rev-parse", "HEAD")
    for _ in range(2):  # Resume once, then reconcile an already accepted push.
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert git(work, "rev-parse", "HEAD") == committed
    assert git(remote, "rev-parse", "main") == committed
    assert [p.name for p in (work / "RHAISTRAT").iterdir() if p.is_dir() and not p.is_symlink()] == ["run-run-7"]
    # A fresh clone after the remote accepted a push must reconcile the same bundle.
    second = tmp_path / "fresh"
    git(tmp_path, "clone", str(remote), str(second))
    (second / "strat-tasks").mkdir()
    shutil.copyfile(work / "RHAISTRAT/run-run-7/strat-tasks/RHAISTRAT-7701.md",
                    second / "strat-tasks/RHAISTRAT-7701.md")
    command[3] = str(second)
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "main") == committed


def test_publication_conflict_and_partial_copy_are_reconciled(tmp_path):
    pub = load_publisher()
    (tmp_path / ".git").mkdir()
    (tmp_path / "strat-tasks").mkdir()
    task = tmp_path / "strat-tasks/STRAT-1.md"
    task.write_text("Immutable.\n")
    original = pub.shutil.copyfile
    def crash_after_copy(*args):
        original(*args)
        raise RuntimeError("death after first copy")
    pub.shutil.copyfile = crash_after_copy
    with pytest.raises(RuntimeError):
        pub.organize_run(tmp_path, "run")
    pub.shutil.copyfile = original
    run = pub.organize_run(tmp_path, "run")
    assert (run / "strat-tasks/STRAT-1.md").read_text() == "Immutable.\n"
    task.parent.mkdir()
    task.write_text("Changed.\n")
    with pytest.raises(ValueError, match="changed|different"):
        pub.organize_run(tmp_path, "run")


def test_death_after_manifest_before_current_pointer_can_resume(tmp_path):
    pub = load_publisher()
    (tmp_path / ".git").mkdir()
    (tmp_path / "strat-tasks").mkdir()
    (tmp_path / "strat-tasks/STRAT-1.md").write_text("Immutable.\n")
    previous = tmp_path / "RHAISTRAT/previous"
    previous.mkdir(parents=True)
    (previous.parent / "current").symlink_to("previous")
    original = pub.atomic
    def die_after_manifest(path, value):
        original(path, value)
        if path.name == ".publication.json":
            raise RuntimeError("death before pointer update")
    pub.atomic = die_after_manifest
    with pytest.raises(RuntimeError):
        pub.organize_run(tmp_path, "run")
    pub.atomic = original
    resumed = pub.organize_run(tmp_path, "run")
    assert (resumed.parent / "current").readlink() == Path("run-run")
