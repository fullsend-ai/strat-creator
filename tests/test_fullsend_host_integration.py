"""Integration tests for the fullsend host side, against jira-emulator.

Covers what runs on the runner around the sandbox: the pre-script (pinned
clone, vendoring, context check, resume points, human gates, locking), the
validator, the post-script handoff and report, and the CI lock release. The
sandbox side (the strat-pipeline Workflow) is covered by
test_strat_pipeline_workflow.py.

Each test builds a "remote" strat-creator from this working tree (a git repo
with one commit), so the pre-script clones and verifies a real sha.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / ".fullsend" / "shared"
SCHEMA = SHARED / "result.schema.json"
CONTEXT = ROOT / "tests" / "fixtures" / "workflow-smoke" / "architecture-context"
PROCESSING = "strat-creator-processing"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A strat-creator repository at one commit, built from this working tree."""
    src = tmp_path_factory.mktemp("strat-creator-src")
    files = _git(ROOT, "ls-files", "-co", "--exclude-standard").split("\n")
    for rel in filter(None, files):
        path = ROOT / rel
        if path.is_symlink():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(path), src / rel)
        elif path.is_file():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, src / rel)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "snapshot")
    return src, _git(src, "rev-parse", "HEAD")


@pytest.fixture
def host(tmp_path, source, jira):
    """Environment for one run: an empty consumer repo and a host state dir."""
    src, ref = source
    target, state = tmp_path / "consumer", tmp_path / "state"
    target.mkdir()
    _git(target, "init", "-q")
    (target / "README.md").write_text("consumer repository\n")
    prescript_out = tmp_path / "prescript-output"
    prescript_out.write_text("")
    env = {**os.environ, "TARGET_REPO_DIR": str(target), "STRAT_STATE_DIR": str(state),
           "STRAT_CREATOR_REF": ref, "STRAT_CREATOR_REPO": str(src), "STRAT_RUN_ID": "run-1",
           "STRAT_ARCHITECTURE_CONTEXT_PATH": str(CONTEXT), "STRAT_DRY_RUN": "",
           "STRAT_PREVIOUS_PROGRESS": "", "FULLSEND_PRESCRIPT_OUTPUT": str(prescript_out),
           "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
    env.pop("RFE_SKIP_BOOTSTRAP", None)
    return {"env": env, "target": target, "state": state, "ref": ref, "src": src,
            "prescript_out": prescript_out, "tmp": tmp_path}


def _pre(host, mode, **extra):
    env = {**host["env"], "STRAT_MODE": mode, **extra}
    return subprocess.run(["bash", str(SHARED / "pre-strategy.sh")], env=env,
                          capture_output=True, text=True)


def _labels(jira, key):
    return set(jira.get(key)["fields"].get("labels", []))


def _strat(jira, strat, rfe, labels, status=None):
    jira.create(strat, f"Strategy for {rfe}", "Strategy.", labels=labels, issue_type="Feature",
                status=status)
    jira.request("POST", "/rest/api/3/issueLink", {
        "type": {"name": "Cloners"}, "inwardIssue": {"key": strat}, "outwardIssue": {"key": rfe}})


def _run_record(host):
    return json.loads((host["state"] / "run.json").read_text())


# ── pre-script ────────────────────────────────────────────────────────────────

def test_pre_script_vendors_locks_and_writes_the_input(host, jira):
    jira.create("RHAIRFE-7001", "An approved RFE", "Business need.")
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7001")
    print(result.stdout, result.stderr)
    assert result.returncode == 0, result.stderr

    target = host["target"]
    assert (target / ".claude/skills/strategy-review/SKILL.md").is_file()
    assert (target / ".claude/skills/strategy-review/scripts/apply_scores.py").is_file()
    assert (target / ".claude/agents/strat-scorer.md").is_file()
    assert (target / "scripts/strat_pipeline_state.py").is_file()
    assert (target / "workflows/strat-pipeline.js").is_file()
    assert not [p for p in target.rglob("*") if p.is_symlink()]
    marker = json.loads((target / ".strat-creator-vendor.json").read_text())
    assert marker["ref"] == host["ref"]

    assert PROCESSING in _labels(jira, "RHAIRFE-7001")
    assert (host["state"] / "owned-rfe-ids.txt").read_text().split() == ["RHAIRFE-7001"]
    run = _run_record(host)
    assert run["acquired_keys"] == ["RHAIRFE-7001"]
    assert run["resume_points"] == {"RHAIRFE-7001": "create"}
    assert run["resume_from"] == "create"
    assert run["architecture_version"] == "rhoai-9.9"
    assert json.loads((target / "tmp/strat-input.json").read_text()) == run
    assert (host["state"] / "strat-creator/.git").is_dir()


def test_pre_script_refuses_an_unpinned_or_wrong_ref(host, jira):
    jira.create("RHAIRFE-7002", "RFE", "Need.")
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7002", STRAT_CREATOR_REF="main")
    assert result.returncode != 0
    assert "full 40-character commit sha" in result.stderr
    wrong = _pre(host, "single", RFE_KEY="RHAIRFE-7002", STRAT_CREATOR_REF="0" * 40)
    assert wrong.returncode != 0
    assert PROCESSING not in _labels(jira, "RHAIRFE-7002")


def test_missing_context_fails_before_any_lock(host, jira, tmp_path):
    jira.create("RHAIRFE-7003", "RFE", "Need.")
    empty = tmp_path / "no-context"
    (empty / "architecture").mkdir(parents=True)
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7003", STRAT_ARCHITECTURE_CONTEXT_PATH=str(empty))
    assert result.returncode == 1
    assert "architecture context incomplete; no RFE was locked" in result.stderr
    assert PROCESSING not in _labels(jira, "RHAIRFE-7003")
    assert not (host["state"] / "owned-rfe-ids.txt").exists()


def test_strat_at_a_human_gate_is_skipped_without_a_lock(host, jira):
    jira.create("RHAIRFE-7004", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8004", "RHAIRFE-7004",
           ["strat-creator-auto-created", "strat-creator-auto-refined", "strat-creator-needs-attention"])
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7004")
    print(result.stdout)
    assert result.returncode == 78
    assert "skipped=true" in host["prescript_out"].read_text()
    assert PROCESSING not in _labels(jira, "RHAIRFE-7004")
    run = _run_record(host)
    assert run["skipped"] == [{"key": "RHAIRFE-7004",
                               "reason": "RHAISTRAT-8004 waiting for a human (needs-attention)"}]


def test_resume_from_a_strat_key_after_an_interrupted_review(host, jira, tmp_path):
    jira.create("RHAIRFE-7005", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8005", "RHAIRFE-7005",
           ["strat-creator-auto-created", "strat-creator-auto-refined"])
    previous = tmp_path / "previous-progress.json"
    previous.write_text(json.dumps({
        "run_id": "run-0",
        "create": {"strategies": [{"rfe": "RHAIRFE-7005", "strat": "RHAISTRAT-8005"}]},
        "refine": {"RHAISTRAT-8005": "2026-10-01T00:00:00Z"},
        "push": {"done_at": "2026-10-01T00:01:00Z", "skipped": False, "strategies": ["RHAISTRAT-8005"]},
        "review": {}}))
    result = _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8005",
                  STRAT_PREVIOUS_PROGRESS=str(previous))
    print(result.stdout)
    assert result.returncode == 0, result.stderr
    run = _run_record(host)
    assert run["mode"] == "resume"
    assert run["input_keys"] == ["RHAIRFE-7005"]
    assert run["resume_points"] == {"RHAIRFE-7005": "review"}
    assert PROCESSING in _labels(jira, "RHAIRFE-7005")


def test_previous_progress_in_the_target_survives_the_clean_up(host, jira):
    jira.create("RHAIRFE-7010", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8010", "RHAIRFE-7010",
           ["strat-creator-auto-created", "strat-creator-auto-refined"])
    kept = host["target"] / "artifacts" / "strat-progress.json"
    kept.parent.mkdir(parents=True)
    kept.write_text(json.dumps({
        "run_id": "run-0", "create": {"strategies": [{"rfe": "RHAIRFE-7010", "strat": "RHAISTRAT-8010"}]},
        "refine": {"RHAISTRAT-8010": "t"},
        "push": {"done_at": "t", "skipped": False, "strategies": ["RHAISTRAT-8010"]}, "review": {}}))
    result = _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8010", STRAT_PREVIOUS_PROGRESS=str(kept))
    assert result.returncode == 0, result.stderr
    assert not kept.exists()  # cleaned from the target...
    assert _run_record(host)["resume_points"] == {"RHAIRFE-7010": "review"}  # ...after it was read


def test_a_dry_run_push_does_not_count_as_pushed(host, jira, tmp_path):
    jira.create("RHAIRFE-7011", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8011", "RHAIRFE-7011",
           ["strat-creator-auto-created", "strat-creator-auto-refined"])
    previous = tmp_path / "dry-progress.json"
    previous.write_text(json.dumps({
        "run_id": "run-0", "create": {"strategies": [{"rfe": "RHAIRFE-7011", "strat": "RHAISTRAT-8011"}]},
        "refine": {"RHAISTRAT-8011": "t"},
        "push": {"done_at": "t", "skipped": True, "strategies": ["RHAISTRAT-8011"]}, "review": {}}))
    result = _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8011", STRAT_PREVIOUS_PROGRESS=str(previous))
    assert result.returncode == 0, result.stderr
    assert _run_record(host)["resume_points"] == {"RHAIRFE-7011": "refine"}


def test_expected_create_skips_follow_the_pipeline_gates(host, jira):
    jira.create("RHAIRFE-7012", "In scope", "Need.", labels=["strat-creator-3.7", "tech-reviewed"])
    jira.create("RHAIRFE-7013", "Out of scope", "Need.")
    jira.create("RHAIRFE-7014", "Two open STRATs", "Need.", labels=["strat-creator-3.7", "tech-reviewed"])
    _strat(jira, "RHAISTRAT-8014", "RHAIRFE-7014", [])
    _strat(jira, "RHAISTRAT-8015", "RHAIRFE-7014", [])
    for key in ("RHAIRFE-7012", "RHAIRFE-7013", "RHAIRFE-7014"):
        shutil.rmtree(host["state"], ignore_errors=True)
        assert _pre(host, "single", RFE_KEY=key, STRAT_DRY_RUN="1").returncode == 0
        expected = {s["key"]: s["reason"] for s in _run_record(host)["expected_skips"]}
        if key == "RHAIRFE-7012":
            assert expected == {}
        elif key == "RHAIRFE-7013":
            assert expected == {key: "release scope: no required label or target version"}
        else:
            assert expected == {key: "multiple open STRATs"}


def test_a_planted_vendor_marker_cannot_reach_outside_the_target(host, jira, tmp_path):
    jira.create("RHAIRFE-7016", "RFE", "Need.")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    (host["target"] / ".strat-creator-vendor.json").write_text(
        json.dumps({"ref": "x", "files": ["../victim.txt"]}))
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7016")
    assert result.returncode == 1
    assert "lists paths outside the target" in result.stderr
    assert victim.read_text() == "keep me\n"
    assert PROCESSING not in _labels(jira, "RHAIRFE-7016")


def test_vendoring_refuses_a_symlinked_directory_in_the_target(host, jira, tmp_path):
    jira.create("RHAIRFE-7017", "RFE", "Need.")
    outside = tmp_path / "outside-config"
    outside.mkdir()
    (host["target"] / "config").symlink_to(outside)
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7017")
    assert result.returncode == 1
    assert "go through a symlink" in result.stderr
    assert not any(outside.iterdir())


def test_resume_after_a_human_cleared_the_gate_refines_again(host, jira):
    jira.create("RHAIRFE-7006", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8006", "RHAIRFE-7006",
           ["strat-creator-auto-created", "strat-creator-auto-refined"])
    result = _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8006")
    assert result.returncode == 0, result.stderr
    assert _run_record(host)["resume_points"] == {"RHAIRFE-7006": "refine"}


def test_dry_run_takes_no_lock(host, jira):
    jira.create("RHAIRFE-7007", "RFE", "Need.")
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7007", STRAT_DRY_RUN="1")
    assert result.returncode == 0, result.stderr
    assert "selected (dry run) RHAIRFE-7007:create" in result.stdout
    assert PROCESSING not in _labels(jira, "RHAIRFE-7007")
    assert _run_record(host)["dry_run"] is True


def test_vendoring_never_overwrites_consumer_files(host, jira):
    jira.create("RHAIRFE-7008", "RFE", "Need.")
    (host["target"] / "scripts").mkdir()
    (host["target"] / "scripts" / "state.py").write_text("# the consumer's own file\n")
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7008")
    assert result.returncode == 1
    assert "refusing to overwrite them" in result.stderr
    assert (host["target"] / "scripts" / "state.py").read_text() == "# the consumer's own file\n"
    assert PROCESSING not in _labels(jira, "RHAIRFE-7008")


def test_release_locks_after_the_run(host, jira):
    jira.create("RHAIRFE-7009", "RFE", "Need.")
    assert _pre(host, "single", RFE_KEY="RHAIRFE-7009").returncode == 0
    assert PROCESSING in _labels(jira, "RHAIRFE-7009")
    release = host["state"] / "strat-creator/.fullsend/shared/release-locks.sh"
    result = subprocess.run(["bash", str(release), str(host["state"])], env=host["env"],
                            capture_output=True, text=True)
    print(result.stdout)
    assert result.returncode == 0, result.stderr
    assert PROCESSING not in _labels(jira, "RHAIRFE-7009")
    assert (host["state"] / "owned-rfe-ids.txt.released").is_file()
    again = subprocess.run(["bash", str(release), str(host["state"])], env=host["env"],
                           capture_output=True, text=True)
    assert again.returncode == 0 and "no lock record" in again.stdout


# ── a downloaded repository as the agent leaves it ──────────────────────────────

def _state(repo, *args):
    env = {**os.environ, "FULLSEND_OUTPUT_DIR": str(repo / "output")}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "strat_pipeline_state.py"), *args],
                          cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def _frontmatter(repo, path, *pairs):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "frontmatter.py"), "set", path, *pairs],
                   cwd=repo, check=True, capture_output=True, text=True)


SCORES = {"approve": (2, 2, 1, 2), "revise": (1, 1, 1, 1)}


def _downloaded_repo(tmp_path, run, verdicts):
    """Build what the sandbox leaves behind, through the same helper the Workflow uses."""
    repo = tmp_path / "downloaded"
    (repo / "tmp").mkdir(parents=True)
    (repo / "tmp" / "strat-input.json").write_text(json.dumps(run))
    for rfe, (strat, verdict) in verdicts.items():
        task, review = f"artifacts/strat-tasks/{strat}.md", f"artifacts/strat-reviews/{strat}-review.md"
        comments = f"artifacts/strat-originals/{rfe}-comments.md"
        for rel in (task, review, comments):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("## Strategy\nDone.\n")
        _frontmatter(repo, task, f"strat_id={strat}", "title=T", f"source_rfe={rfe}",
                     "priority=Major", "status=Refined", f"jira_key={strat}")
        f, t, s, a = SCORES[verdict]
        _frontmatter(repo, review, f"strat_id={strat}", f"recommendation={verdict}",
                     f"needs_attention={'false' if verdict == 'approve' else 'true'}",
                     f"scores.feasibility={f}", f"scores.testability={t}", f"scores.scope={s}",
                     f"scores.architecture={a}", f"scores.total={f + t + s + a}",
                     *(f"reviewers.{d}={verdict}" for d in ("feasibility", "testability", "scope",
                                                            "architecture")))
    _state(repo, "record-create")
    for strat, _ in verdicts.values():
        _state(repo, "mark", "refine", strat)
    _state(repo, "mark", "push")
    for strat, _ in verdicts.values():
        _state(repo, "record-review", strat)
    _state(repo, "result")
    return repo


def _validate(repo, run_path, env):
    return subprocess.run([sys.executable, str(SHARED / "validate_result.py"),
                           "--result", str(repo / "output" / "agent-result.json"), "--repo", str(repo),
                           "--schema", str(SCHEMA), "--run", str(run_path)],
                          env=env, capture_output=True, text=True)


@pytest.fixture
def validated_run(tmp_path, jira):
    jira.create("RHAIRFE-7101", "RFE one", "Need.")
    jira.create("RHAIRFE-7102", "RFE two", "Need.")
    _strat(jira, "RHAISTRAT-8101", "RHAIRFE-7101", ["strat-creator-auto-created",
                                                    "strat-creator-auto-refined", "strat-creator-rubric-pass"])
    _strat(jira, "RHAISTRAT-8102", "RHAIRFE-7102", ["strat-creator-auto-created",
                                                    "strat-creator-auto-refined", "strat-creator-needs-attention"])
    run = {"run_id": "run-2", "mode": "batch", "dry_run": False,
           "input_keys": ["RHAIRFE-7101", "RHAIRFE-7102", "RHAIRFE-7103"],
           "acquired_keys": ["RHAIRFE-7101", "RHAIRFE-7102"],
           "skipped": [{"key": "RHAIRFE-7103", "reason": "blocked by label(s): strat-creator-processing"}],
           "resume_points": {"RHAIRFE-7101": "create", "RHAIRFE-7102": "create"}, "resume_from": "create",
           "expected_skips": [],
           "architecture_context": "available", "architecture_version": "rhoai-9.9"}
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(run))
    repo = _downloaded_repo(tmp_path, run, {"RHAIRFE-7101": ("RHAISTRAT-8101", "approve"),
                                            "RHAIRFE-7102": ("RHAISTRAT-8102", "revise")})
    env = {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
    return repo, run_path, env


def test_validator_passes_a_consistent_run_and_reads_jira_back(validated_run):
    repo, run_path, env = validated_run
    result = json.loads((repo / "output" / "agent-result.json").read_text())
    assert result["action"] == "completed"
    # A human gate (needs_attention) is a successful run, not a failure.
    assert {s["strat"]: s["needs_attention"] for s in result["strategies"]} == {
        "RHAISTRAT-8101": False, "RHAISTRAT-8102": True}
    check = _validate(repo, run_path, env)
    print(check.stdout)
    assert check.returncode == 0, check.stdout
    assert check.stdout.strip().endswith("PASS: result matches the run record, progress, artifacts and scores")


def test_validator_rejects_a_claimed_verdict_the_scores_do_not_give(validated_run):
    repo, run_path, env = validated_run
    path = repo / "output" / "agent-result.json"
    result = json.loads(path.read_text())
    result["strategies"][1].update(recommendation="approve", needs_attention=False)
    path.write_text(json.dumps(result))
    check = _validate(repo, run_path, env)
    print(check.stdout)
    assert check.returncode == 1
    assert "RHAISTRAT-8102 is reported approve, its review says revise" in check.stdout
    assert "Resume: call the Workflow tool" in check.stdout


def test_validator_rejects_a_missing_jira_label(validated_run, jira):
    repo, run_path, env = validated_run
    jira.request("PUT", "/rest/api/3/issue/RHAISTRAT-8101",
                 {"update": {"labels": [{"remove": "strat-creator-rubric-pass"}]}})
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "RHAISTRAT-8101 is missing Jira label(s) strat-creator-rubric-pass" in check.stdout


def test_validator_rejects_progress_and_record_mismatches(validated_run):
    repo, run_path, env = validated_run
    progress = json.loads((repo / "tmp" / "strat-progress.json").read_text())
    del progress["review"]["RHAISTRAT-8102"]
    (repo / "tmp" / "strat-progress.json").write_text(json.dumps(progress))
    run = json.loads(run_path.read_text())
    run["acquired_keys"] = ["RHAIRFE-7101"]
    run_path.write_text(json.dumps(run))
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "review of RHAISTRAT-8102 is not recorded" in check.stdout
    assert "acquired_keys is" in check.stdout


def test_validator_rejects_a_symlinked_artifact(validated_run, tmp_path):
    repo, run_path, env = validated_run
    review = repo / "artifacts" / "strat-reviews" / "RHAISTRAT-8101-review.md"
    outside = tmp_path / "outside.md"
    outside.write_text(review.read_text())
    review.unlink()
    review.symlink_to(outside)
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "artifacts/strat-reviews/RHAISTRAT-8101-review.md is not a regular file" in check.stdout


def _claim_skip(repo, key, reason):
    path = repo / "output" / "agent-result.json"
    result = json.loads(path.read_text())
    result["skipped"].append({"key": key, "reason": reason})
    path.write_text(json.dumps(result))


def _with_expected(run_path, expected, acquire=None):
    run = json.loads(run_path.read_text())
    run["expected_skips"] = expected
    if acquire:
        run["acquired_keys"] += [acquire]
        run["input_keys"] += [acquire]
        run["resume_points"][acquire] = "create"
    run_path.write_text(json.dumps(run))


def _also_acquired(repo, key):
    path = repo / "output" / "agent-result.json"
    result = json.loads(path.read_text())
    result["acquired_keys"] += [key]
    result["input_keys"] += [key]
    path.write_text(json.dumps(result))


@pytest.mark.parametrize("expected,reason,message", [
    ([{"key": "RHAIRFE-7104", "reason": "release scope: no required label or target version"}],
     "missing release scope label", None),
    ([], "missing release scope label", "RHAIRFE-7104 is reported skipped by strategy-create"),
    (None, "missing release scope label", "the host could not compute strategy-create's expected skips"),
    ([], "reconstruction failed: RFE content not available for RHAISTRAT-8104",
     "STRAT RHAISTRAT-8104 references a business need the run could not restore; restore the RFE "
     "original or fix the STRAT description, then rerun"),
])
def test_claimed_create_skips_must_match_the_host_gate_check(validated_run, expected, reason, message):
    repo, run_path, env = validated_run
    _with_expected(run_path, expected, acquire="RHAIRFE-7104")
    _also_acquired(repo, "RHAIRFE-7104")
    _claim_skip(repo, "RHAIRFE-7104", reason)
    check = _validate(repo, run_path, env)
    print(check.stdout)
    if message is None:
        assert check.returncode == 0, check.stdout
    else:
        assert check.returncode == 1
        assert message in check.stdout


def test_a_strategy_created_against_the_gate_fails(validated_run):
    repo, run_path, env = validated_run
    _with_expected(run_path, [{"key": "RHAIRFE-7101", "reason": "quality: no quality label"}])
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "RHAIRFE-7101 fails strategy-create's gate (quality: no quality label) but a strategy was created" \
        in check.stdout


@pytest.mark.parametrize("artifacts,message", [
    ([], "artifacts/strat-tasks/RHAISTRAT-8101.md is not in the result's artifacts list"),
    (["artifacts/reports/report.html"], "artifacts/reports/report.html is not a path the handoff publishes"),
])
def test_validator_requires_the_artifacts_the_handoff_will_copy(validated_run, artifacts, message):
    repo, run_path, env = validated_run
    path = repo / "output" / "agent-result.json"
    result = json.loads(path.read_text())
    result["artifacts"] = artifacts
    path.write_text(json.dumps(result))
    if artifacts:
        (repo / artifacts[0]).parent.mkdir(parents=True, exist_ok=True)
        (repo / artifacts[0]).write_text("<html></html>\n")
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert message in check.stdout


def test_validator_reports_a_wrongly_typed_progress_field(validated_run):
    repo, run_path, env = validated_run
    progress = json.loads((repo / "tmp" / "strat-progress.json").read_text())
    progress["review"] = []
    (repo / "tmp" / "strat-progress.json").write_text(json.dumps(progress))
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "field 'review' has the wrong type" in check.stdout


def test_a_failed_run_is_reported_to_the_next_attempt(validated_run):
    repo, run_path, env = validated_run
    _state(repo, "result", "--failed", "review:RHAISTRAT-8102")
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "the run reports failure: review: RHAISTRAT-8102 returned no result" in check.stdout


# ── post-script ─────────────────────────────────────────────────────────────────

def test_post_script_hands_off_regular_files_and_builds_the_report(host, jira, validated_run):
    jira.create("RHAIRFE-7201", "RFE", "Need.")
    assert _pre(host, "single", RFE_KEY="RHAIRFE-7201").returncode == 0
    repo, _, _ = validated_run
    (repo / "artifacts" / "strat-tasks" / "planted.md").symlink_to("/etc/hosts")
    (repo / "artifacts" / "strat-reviews" / "unlisted-review.md").write_text("not in the validated result\n")
    iteration = host["tmp"] / "iteration"
    iteration.mkdir()
    shutil.copy(repo / "output" / "agent-result.json", iteration / "agent-result.json")
    env = {**host["env"], "REPO_DIR": str(repo), "FULLSEND_VALIDATED_ITERATION_DIR": str(iteration),
           "FULLSEND_RUN_DIR": str(host["tmp"] / "run"), "RESULTS_REPO_URL": "", "RESULTS_PUSH_TOKEN": "",
           "ORG_PULSE_URL": "", "ORG_PULSE_API_TOKEN": "", "STRAT_PUBLISH_DIR": ""}
    result = subprocess.run(["bash", str(SHARED / "post-strategy.sh")], env=env, capture_output=True, text=True)
    print(result.stdout, result.stderr)
    assert result.returncode == 0, result.stderr
    target = host["target"] / "artifacts"
    assert (target / "strat-tasks" / "RHAISTRAT-8101.md").is_file()
    assert not (target / "strat-tasks" / "planted.md").exists()
    assert not (target / "strat-reviews" / "unlisted-review.md").exists()
    assert (target / "strat-originals" / "RHAIRFE-7101-comments.md").is_file()
    assert json.loads((target / "strat-progress.json").read_text())["run_id"] == "run-2"
    results = host["tmp"] / "run" / "publish" / "results"
    assert (results / "reports" / "report.html").is_file()
    assert (results / "pipeline-data.json").is_file()
    assert not (results / "agent-result.json").exists()
    assert "RESULTS_REPO_URL not set" in result.stdout


def test_jira_lookup_failure_is_reported_without_a_traceback(host):
    result = _pre(host, "single", RFE_KEY="RHAIRFE-7999")  # not in the emulator
    assert result.returncode == 1
    assert "reading Jira state failed, no RFE was locked" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (host["state"] / "owned-rfe-ids.txt").exists()


@pytest.mark.parametrize("content,message", [
    ("{not json", "tmp/strat-progress.json is unreadable"),
    ("[1, 2]", "tmp/strat-progress.json is not a JSON object"),
])
def test_validator_reports_a_malformed_progress_file(validated_run, content, message):
    repo, run_path, env = validated_run
    (repo / "tmp" / "strat-progress.json").write_text(content)
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert f"FAIL: {message}" in check.stdout
    assert "Traceback" not in check.stderr


def test_expected_skip_mirrors_the_path_a_label_gate(monkeypatch):
    sys.path.insert(0, str(SHARED))
    import prepare_run

    monkeypatch.setattr(prepare_run, "require_env", lambda: ("s", "u", "t"))
    monkeypatch.setattr(prepare_run, "get_issue", lambda *a, **k: {"fields": {
        "status": {"name": "New"}, "labels": ["strat-creator-3.7", "tech-reviewed"]}})
    monkeypatch.setattr(prepare_run, "find_strat_clones", lambda *a: [
        {"key": "RHAISTRAT-1", "status": "New", "labels": ["strat-creator-rubric-pass"]}])
    settings = {"jql": {"required_labels": ["strat-creator-3.7"], "quality_labels": ["tech-reviewed"],
                        "excluded_statuses": ["Closed"], "target_versions": []}}
    assert prepare_run.expected_create_skip("RHAIRFE-1", settings) == \
        "RHAISTRAT-1 already processed (label: strat-creator-rubric-pass)"
    monkeypatch.setattr(prepare_run, "find_strat_clones", lambda *a: [
        {"key": "RHAISTRAT-1", "status": "New", "labels": ["strat-creator-auto-created"]}])
    assert prepare_run.expected_create_skip("RHAIRFE-1", settings) is None


def test_created_strategies_need_the_host_gate_check(validated_run):
    repo, run_path, env = validated_run
    _with_expected(run_path, None)
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "the host could not compute strategy-create's expected skips" in check.stdout


def test_gate_check_failure_locks_nothing(monkeypatch, tmp_path):
    sys.path.insert(0, str(SHARED))
    import prepare_run

    (tmp_path / "target" / ".context/architecture-context").mkdir(parents=True)
    (tmp_path / "target" / ".context/architecture-context/LATEST_VERSION").write_text("rhoai-9.9\n")
    locked = []
    monkeypatch.setattr(prepare_run, "resume_point", lambda key, previous: ("create", None))
    monkeypatch.setattr(prepare_run, "lock", lambda *a: locked.append(a) or [])

    def unreachable(*_):
        raise OSError("Jira unreachable")

    monkeypatch.setattr(prepare_run, "expected_create_skip", unreachable)
    env = {"STRAT_RUN_ID": "r", "STRAT_STATE_DIR": str(tmp_path / "state"),
           "TARGET_REPO_DIR": str(tmp_path / "target"), "RFE_KEY": "RHAIRFE-1"}
    assert prepare_run.main(["prepare_run.py", "single"], env) == 1
    assert locked == []


def test_handoff_works_below_a_symlinked_directory(validated_run, tmp_path):
    repo, _, _ = validated_run
    link = tmp_path / "linked-repo"
    link.symlink_to(repo)
    out = tmp_path / "handoff"
    proc = subprocess.run([sys.executable, str(SHARED / "handoff.py"), str(link), str(out),
                           str(repo / "output" / "agent-result.json")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert (out / "strat-tasks" / "RHAISTRAT-8101.md").is_file()


def test_validator_refuses_a_symlinked_directory_inside_the_repo(validated_run):
    repo, run_path, env = validated_run
    tasks = repo / "artifacts" / "strat-tasks"
    tasks.rename(repo / "artifacts" / "real-tasks")
    tasks.symlink_to("real-tasks")
    check = _validate(repo, run_path, env)
    assert check.returncode == 1
    assert "artifacts/strat-tasks/RHAISTRAT-8101.md is not a regular file in the repository" in check.stdout
