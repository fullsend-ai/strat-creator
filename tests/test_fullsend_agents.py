"""Tests for the native Fullsend strategy agents' host-side boundaries.

The agents themselves are prompts; these tests cover the deterministic parts
that decide what they work on and whether their work counts: host-side
discovery and locking, the pre-script, result validation, artifact handoff
and lock release.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FULLSEND = ROOT / ".fullsend"
SHARED = FULLSEND / "shared"
SCHEMA = SHARED / "result.schema.json"

sys.path.insert(0, str(SHARED))
import validate_result  # noqa: E402
from handoff import handoff  # noqa: E402

RUN_ID = "job-4100"
RFE = "RHAIRFE-12"
STRAT = "RHAISTRAT-40"
PROCESSING = "strat-creator-processing"
ELIGIBLE = ["strat-creator-3.6", "rfe-creator-autofix-rubric-pass"]


# ---------------------------------------------------------------- fixtures

def write_md(path, frontmatter, body="Body.\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + body)


def add_strategy(repo, rfe, strat, recommendation="approve"):
    art = repo / "artifacts"
    write_md(art / "strat-tasks" / f"{strat}.md", {
        "strat_id": strat, "title": "Strategy", "source_rfe": rfe,
        "priority": "Major", "status": "Refined", "jira_key": strat,
    })
    # Scores consistent with the skill's rule: 8 -> approve, 4 -> revise.
    each = 2 if recommendation == "approve" else 1
    write_md(art / "strat-reviews" / f"{strat}-review.md", {
        "strat_id": strat, "recommendation": recommendation,
        "needs_attention": recommendation != "approve",
        "scores": {"feasibility": each, "testability": each, "scope": each,
                   "architecture": each, "total": 4 * each},
        "reviewers": {"feasibility": "approve", "testability": "approve",
                      "scope": "approve", "architecture": "approve"},
    })
    (art / "strat-originals").mkdir(parents=True, exist_ok=True)
    (art / "strat-originals" / f"{rfe}.md").write_text("Business need.\n")


def make_repo(tmp_path, pairs=((RFE, STRAT),), recommendation="approve",
              context=True):
    """A downloaded repository after the agent ran."""
    repo = tmp_path / "repo"
    (repo / "artifacts").mkdir(parents=True, exist_ok=True)
    for rfe, strat in pairs:
        add_strategy(repo, rfe, strat, recommendation)
    if context:
        ctx = repo / ".context" / "architecture-context"
        (ctx / "architecture" / "rhoai-3.6").mkdir(parents=True)
        (ctx / "LATEST_VERSION").write_text("rhoai-3.6\n")
        (ctx / "architecture" / "rhoai-3.6" / "PLATFORM.md").write_text("x")
    return repo


def make_state(tmp_path, inputs=(RFE,), acquired=(RFE,), skipped=(),
               mode="single", owned=None):
    """The host run record prepare_run.py writes."""
    state = tmp_path / "state"
    state.mkdir()
    record = {"run_id": RUN_ID, "mode": mode, "input_keys": list(inputs),
              "acquired_keys": list(acquired),
              "skipped": [{"key": k, "reason": "blocked"} for k in skipped]}
    (state / "run.json").write_text(json.dumps(record))
    keys = acquired if owned is None else owned
    (state / "owned-rfe-ids.txt").write_text("".join(f"{k}\n" for k in keys))
    return state


def make_output(tmp_path, phases=("create", "refine", "push", "review"),
                mapping=((RFE, STRAT),), run_id=RUN_ID):
    out = tmp_path / "output"
    out.mkdir()
    lines = [f"run_id: {run_id}"]
    for i, phase in enumerate(phases):
        lines.append(f"phase_{phase}: 2026-10-07T12:0{i}:00Z")
        if phase in ("refine", "review"):
            lines += [f"{phase}_{strat}: 2026-10-07T12:0{i}:00Z"
                      for _, strat in mapping]
    lines += [f"map_{rfe}: {strat}" for rfe, strat in mapping]
    (out / "strat-progress.yaml").write_text("\n".join(lines) + "\n")
    return out


def strategy(rfe=RFE, strat=STRAT, recommendation="approve"):
    return {"rfe": rfe, "strat": strat, "recommendation": recommendation,
            "needs_attention": recommendation != "approve"}


def completed(**overrides):
    result = {
        "action": "completed", "mode": "single", "run_id": RUN_ID,
        "summary": f"{RFE} -> {STRAT}: approve.",
        "input_keys": [RFE], "acquired_keys": [RFE], "skipped": [],
        "strategies": [strategy()],
        "completed_phases": ["create", "refine", "push", "review"],
        "artifacts": [f"artifacts/strat-tasks/{STRAT}.md",
                      f"artifacts/strat-reviews/{STRAT}-review.md"],
        "architecture_context": "available",
        "publication_ready": True, "errors": [],
    }
    result.update(overrides)
    return result


ENV = {"STRAT_MODE": "single"}


def check(result, out, repo, state, env=ENV):
    return validate_result.validate(result, out, repo, state, str(SCHEMA),
                                    use_jira=False, env=env)


def single(tmp_path, **result_overrides):
    """Default single-run fixture set, returned as check() arguments."""
    return (completed(**result_overrides), make_output(tmp_path),
            make_repo(tmp_path), make_state(tmp_path))


# ------------------------------------------------------- validator: offline

class TestValidateCompleted:
    def test_valid_completed_passes(self, tmp_path):
        assert check(*single(tmp_path)) == []

    def test_schema_violation_reported(self, tmp_path):
        errors = check(*single(tmp_path, action="done"))
        assert errors and errors[0].startswith("schema:")

    def test_false_verdict_rejected(self, tmp_path):
        repo = make_repo(tmp_path, recommendation="revise")
        errors = check(completed(), make_output(tmp_path), repo,
                       make_state(tmp_path))
        assert any("review file says revise" in e for e in errors)

    def test_missing_review_rejected(self, tmp_path):
        result, out, repo, state = single(
            tmp_path, artifacts=[f"artifacts/strat-tasks/{STRAT}.md"])
        (repo / "artifacts/strat-reviews" / f"{STRAT}-review.md").unlink()
        errors = check(result, out, repo, state)
        assert any("review file missing" in e for e in errors)

    def test_claimed_phase_not_recorded_rejected(self, tmp_path):
        out = make_output(tmp_path, phases=("create", "refine"))
        errors = check(completed(), out, make_repo(tmp_path),
                       make_state(tmp_path))
        assert any("phase push claimed but not recorded" in e for e in errors)

    def test_phase_order_rejected(self, tmp_path):
        errors = check(*single(tmp_path, completed_phases=[
            "create", "push", "refine", "review"]))
        assert any("out of order" in e for e in errors)

    def test_phase_recorded_out_of_time_order_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        text = (out / "strat-progress.yaml").read_text()
        (out / "strat-progress.yaml").write_text(text.replace(
            "phase_review: 2026-10-07T12:03:00Z",
            "phase_review: 2026-10-07T11:00:00Z"))
        errors = check(result, out, repo, state)
        assert any("review recorded before push" in e for e in errors)

    def test_old_artifact_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        write_md(repo / "artifacts/strat-tasks/RHAISTRAT-7.md",
                 {"strat_id": "RHAISTRAT-7"})
        errors = check(result, out, repo, state)
        assert any("not produced by this run" in e for e in errors)

    def test_other_run_progress_rejected(self, tmp_path):
        out = make_output(tmp_path, run_id="job-1")
        errors = check(completed(), out, make_repo(tmp_path),
                       make_state(tmp_path))
        assert any("belongs to run job-1" in e for e in errors)

    def test_missing_per_strategy_marker_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        text = (out / "strat-progress.yaml").read_text()
        (out / "strat-progress.yaml").write_text(
            text.replace(f"review_{STRAT}:", "reviewed_nothing:"))
        errors = check(result, out, repo, state)
        assert any(f"review of {STRAT} not recorded" in e for e in errors)

    def test_marker_for_foreign_strat_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        with open(out / "strat-progress.yaml", "a") as f:
            f.write("review_RHAISTRAT-99: 2026-10-07T12:09:00Z\n")
        errors = check(result, out, repo, state)
        assert any("review_RHAISTRAT-99 for a STRAT not in the result" in e
                   for e in errors)

    def test_missing_mapping_rejected(self, tmp_path):
        out = make_output(tmp_path, mapping=())
        errors = check(completed(), out, make_repo(tmp_path),
                       make_state(tmp_path))
        assert any("does not map" in e for e in errors)

    def test_missing_context_rejected(self, tmp_path):
        repo = make_repo(tmp_path, context=False)
        errors = check(completed(), make_output(tmp_path), repo,
                       make_state(tmp_path))
        assert any("repository has missing" in e for e in errors)
        assert any("without architecture context" in e for e in errors)

    def test_missing_context_cannot_be_hidden(self, tmp_path):
        repo = make_repo(tmp_path, context=False)
        errors = check(completed(architecture_context="missing"),
                       make_output(tmp_path), repo, make_state(tmp_path))
        assert any("without architecture context" in e for e in errors)

    def test_artifact_outside_artifacts_rejected(self, tmp_path):
        errors = check(*single(tmp_path,
                               artifacts=["artifacts/../../etc/passwd"]))
        assert any("does not exist" in e for e in errors)

    def test_revise_is_a_valid_completion(self, tmp_path):
        repo = make_repo(tmp_path, recommendation="revise")
        result = completed(strategies=[strategy(recommendation="revise")])
        assert check(result, make_output(tmp_path), repo,
                     make_state(tmp_path)) == []

    def test_approve_with_dissenting_reviewers_passes(self, tmp_path):
        # Pipeline 2077/job 4038: score 6/8 -> approve while three prose
        # reviewers said revise. Prose verdicts are informational only.
        result, out, repo, state = single(tmp_path)
        path = repo / "artifacts/strat-reviews" / f"{STRAT}-review.md"
        fm = {"strat_id": STRAT, "recommendation": "approve",
              "needs_attention": False,
              "scores": {"feasibility": 1, "testability": 1, "scope": 2,
                         "architecture": 2, "total": 6},
              "reviewers": {"feasibility": "revise", "testability": "revise",
                            "scope": "approve", "architecture": "revise"}}
        write_md(path, fm)
        assert check(result, out, repo, state) == []

    def test_recommendation_must_follow_scores(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        path = repo / "artifacts/strat-reviews" / f"{STRAT}-review.md"
        text = path.read_text()
        for dim in ("feasibility: 2", "testability: 2"):
            text = text.replace(dim, dim[:-1] + "0", 1)
        path.write_text(text.replace("total: 8", "total: 4"))
        errors = check(result, out, repo, state)
        assert any("does not follow from scores" in e for e in errors)

    def test_inconsistent_total_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        path = repo / "artifacts/strat-reviews" / f"{STRAT}-review.md"
        path.write_text(path.read_text().replace("total: 8", "total: 7"))
        errors = check(result, out, repo, state)
        assert any("is not the sum" in e for e in errors)

    def test_missing_scores_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        path = repo / "artifacts/strat-reviews" / f"{STRAT}-review.md"
        path.write_text(path.read_text().replace("  scope: 2\n", ""))
        errors = check(result, out, repo, state)
        assert any("review scores incomplete" in e for e in errors)

    def test_malformed_frontmatter_is_a_finding(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        (repo / "artifacts/strat-tasks" / f"{STRAT}.md").write_text(
            "---\nstrat_id: [unclosed\n---\n")
        errors = check(result, out, repo, state)
        assert any("unreadable frontmatter" in e for e in errors)

    def test_create_gate_skip_is_completed_without_strategies(self, tmp_path):
        repo = make_repo(tmp_path, pairs=(), context=False)
        out = make_output(tmp_path, phases=("create",), mapping=())
        result = completed(
            strategies=[], artifacts=[],
            skipped=[{"key": RFE, "reason": "quality gate"}],
            completed_phases=["create"], architecture_context="missing")
        assert check(result, out, repo, make_state(tmp_path)) == []


class TestValidateHostRecord:
    def test_claims_must_match_host_record(self, tmp_path):
        errors = check(*single(tmp_path, run_id="job-other",
                               input_keys=["RHAIRFE-99"]))
        assert any("run_id job-other differs from the host run record" in e
                   for e in errors)
        assert any("input_keys ['RHAIRFE-99'] differs" in e for e in errors)

    def test_agent_cannot_claim_extra_locks(self, tmp_path):
        state = make_state(tmp_path, inputs=(RFE, "RHAIRFE-13"),
                           skipped=("RHAIRFE-13",))
        result = completed(input_keys=[RFE, "RHAIRFE-13"],
                           acquired_keys=[RFE, "RHAIRFE-13"],
                           skipped=[{"key": "RHAIRFE-13", "reason": "x"}])
        errors = check(result, make_output(tmp_path), make_repo(tmp_path),
                       state)
        assert any("acquired_keys" in e and "differs" in e for e in errors)

    def test_strategy_for_unlocked_rfe_rejected(self, tmp_path):
        state = make_state(tmp_path, inputs=(RFE, "RHAIRFE-13"),
                           skipped=("RHAIRFE-13",), mode="batch")
        repo = make_repo(tmp_path, pairs=((RFE, STRAT),
                                          ("RHAIRFE-13", "RHAISTRAT-41")))
        out = make_output(tmp_path, mapping=((RFE, STRAT),
                                             ("RHAIRFE-13", "RHAISTRAT-41")))
        result = completed(mode="batch", input_keys=[RFE, "RHAIRFE-13"],
                           strategies=[strategy(),
                                       strategy("RHAIRFE-13", "RHAISTRAT-41")])
        errors = check(result, out, repo, state, env={})
        assert any("which this run did not lock" in e for e in errors)

    def test_missing_state_dir_rejected(self, tmp_path):
        result, out, repo, _ = single(tmp_path)
        errors = check(result, out, repo, None)
        assert errors == ["STRAT_STATE_DIR is not set; the run record is "
                          "needed to check the result"]

    def test_tampered_lock_record_rejected(self, tmp_path):
        result, out, repo, _ = single(tmp_path)
        state = tmp_path / "state2"
        state.mkdir()
        shutil.copy(tmp_path / "state" / "run.json", state / "run.json")
        (state / "owned-rfe-ids.txt").write_text("")
        errors = check(result, out, repo, state)
        assert any("host lock record [] differs" in e for e in errors)

    def test_invalid_lock_record_rejected(self, tmp_path):
        result, out, repo, state = single(tmp_path)
        (state / "owned-rfe-ids.txt").write_text("RHAIRFE-12; rm -rf /\n")
        errors = check(result, out, repo, state)
        assert any("invalid entries" in e for e in errors)


class TestValidateBatch:
    KEYS = ("RHAIRFE-12", "RHAIRFE-13", "RHAIRFE-14", "RHAIRFE-15")

    def fixture(self, tmp_path):
        """4 discovered: 12 and 13 strategized, 14 gate-skipped, 15 unlocked."""
        pairs = (("RHAIRFE-12", "RHAISTRAT-40"), ("RHAIRFE-13", "RHAISTRAT-41"))
        repo = make_repo(tmp_path, pairs=pairs)
        state = make_state(tmp_path, inputs=self.KEYS, acquired=self.KEYS[:3],
                           skipped=("RHAIRFE-15",), mode="batch")
        out = make_output(tmp_path, mapping=pairs)
        result = completed(
            mode="batch", input_keys=list(self.KEYS),
            acquired_keys=list(self.KEYS[:3]),
            skipped=[{"key": "RHAIRFE-14", "reason": "quality gate"},
                     {"key": "RHAIRFE-15", "reason": "blocked"}],
            strategies=[strategy(*p) for p in pairs], artifacts=[])
        return result, out, repo, state

    def test_partial_batch_passes(self, tmp_path):
        assert check(*self.fixture(tmp_path), env={"STRAT_MODE": "batch"}) \
            == []

    def test_unaccounted_rfe_rejected(self, tmp_path):
        result, out, repo, state = self.fixture(tmp_path)
        result["skipped"] = result["skipped"][:1]
        errors = check(result, out, repo, state, env={})
        assert any("RHAIRFE-15 must appear exactly once" in e for e in errors)

    def test_double_counted_rfe_rejected(self, tmp_path):
        result, out, repo, state = self.fixture(tmp_path)
        result["skipped"].append({"key": "RHAIRFE-12", "reason": "x"})
        errors = check(result, out, repo, state, env={})
        assert any("RHAIRFE-12 must appear exactly once" in e for e in errors)

    def test_foreign_rfe_rejected(self, tmp_path):
        result, out, repo, state = self.fixture(tmp_path)
        result["skipped"].append({"key": "RHAIRFE-99", "reason": "x"})
        errors = check(result, out, repo, state, env={})
        assert any("RHAIRFE-99 was not an input" in e for e in errors)

    def test_missing_strategy_file_in_batch_rejected(self, tmp_path):
        result, out, repo, state = self.fixture(tmp_path)
        (repo / "artifacts/strat-tasks/RHAISTRAT-41.md").unlink()
        errors = check(result, out, repo, state, env={})
        assert any("RHAISTRAT-41: strategy file missing" in e for e in errors)

    def test_mode_must_match_harness(self, tmp_path):
        errors = check(*self.fixture(tmp_path), env={"STRAT_MODE": "single"})
        assert any("harness runs single" in e for e in errors)


class TestValidateOtherActions:
    def test_preflight_skip_passes(self, tmp_path):
        state = make_state(tmp_path, acquired=(), skipped=(RFE,))
        result = completed(
            action="skipped", acquired_keys=[], strategies=[],
            skipped=[{"key": RFE, "reason": "blocked"}], completed_phases=[],
            artifacts=[], publication_ready=False, summary="Blocked.",
            architecture_context="missing")
        empty = tmp_path / "empty"
        empty.mkdir()
        assert check(result, empty, empty, state) == []

    def test_agent_skip_with_locks_rejected(self, tmp_path):
        result = completed(action="skipped", strategies=[],
                           skipped=[{"key": RFE, "reason": "x"}],
                           completed_phases=[], artifacts=[],
                           publication_ready=False)
        out = make_output(tmp_path, phases=(), mapping=())
        errors = check(result, out, make_repo(tmp_path, pairs=()),
                       make_state(tmp_path))
        assert any("must not hold locks" in e for e in errors)

    def test_failed_never_validates(self, tmp_path):
        errors = check(*single(tmp_path, action="failed",
                               errors=["refine crashed"],
                               publication_ready=False))
        assert any("agent reported failure: refine crashed" in e
                   for e in errors)


class TestValidateCli:
    def run(self, tmp_path, result):
        out = make_output(tmp_path)
        repo = make_repo(tmp_path)
        state = make_state(tmp_path)
        (out / "agent-result.json").write_text(json.dumps(result))
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith("JIRA_")}
        environ.update(ENV, FULLSEND_OUTPUT_SCHEMA=str(SCHEMA),
                       TARGET_REPO_DIR=str(repo), STRAT_STATE_DIR=str(state))
        return subprocess.run(["bash", str(SHARED / "validate-output.sh")],
                              cwd=tmp_path, capture_output=True, text=True,
                              env=environ)

    def test_pass(self, tmp_path):
        proc = self.run(tmp_path, completed())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PASS:")

    def test_fail_lists_reasons(self, tmp_path):
        proc = self.run(tmp_path, completed(run_id="job-other"))
        assert proc.returncode == 1
        assert "FAIL: run_id job-other differs" in proc.stdout

    def test_missing_result_asks_for_resume(self, tmp_path):
        (tmp_path / "output").mkdir()
        proc = subprocess.run(
            ["bash", str(SHARED / "validate-output.sh")], cwd=tmp_path,
            capture_output=True, text=True,
            env={**os.environ, "FULLSEND_OUTPUT_SCHEMA": str(SCHEMA),
                 "TARGET_REPO_DIR": str(tmp_path)})
        assert proc.returncode == 1
        assert "stopped before finishing" in proc.stdout
        assert "progress.sh read" in proc.stdout

    def test_not_json(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "agent-result.json").write_text("```json")
        proc = subprocess.run(
            ["bash", str(SHARED / "validate-output.sh")], cwd=tmp_path,
            capture_output=True, text=True,
            env={**os.environ, "FULLSEND_OUTPUT_SCHEMA": str(SCHEMA),
                 "TARGET_REPO_DIR": str(tmp_path)})
        assert proc.returncode == 1
        assert "cannot read" in proc.stdout


# ---------------------------------------------------------------- progress

class TestProgress:
    """progress.sh survives Fullsend clearing the output dir between tries."""

    def run(self, repo, out, *args):
        return subprocess.run(
            ["bash", str(SHARED / "progress.sh"), *args], cwd=repo,
            capture_output=True, text=True,
            env={**os.environ, "FULLSEND_OUTPUT_DIR": str(out)})

    def setup(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "state.py", repo / "scripts")
        out = tmp_path / "output"
        out.mkdir()
        return repo, out

    def test_records_and_mirrors(self, tmp_path):
        repo, out = self.setup(tmp_path)
        assert self.run(repo, out, "init", "run_id=r1", "mode=batch").returncode == 0
        assert self.run(repo, out, "set", "map_RHAIRFE-1=RHAISTRAT-1").returncode == 0
        assert self.run(repo, out, "mark", "refine_RHAISTRAT-1", "phase_refine").returncode == 0
        canonical = (repo / "tmp/strat-progress.yaml").read_text()
        assert canonical == (out / "strat-progress.yaml").read_text()
        progress = validate_result.read_progress(repo / "tmp/strat-progress.yaml")
        assert progress["map_RHAIRFE-1"] == "RHAISTRAT-1"
        assert progress["refine_RHAISTRAT-1"] == progress["phase_refine"]

    def test_retry_resumes_from_repository_copy(self, tmp_path):
        repo, out = self.setup(tmp_path)
        self.run(repo, out, "init", "run_id=r1", "mode=batch")
        self.run(repo, out, "mark", "phase_create")
        # Fullsend 0.43.0 runs `rm -rf <workspace>/output/*` before iteration 2.
        for f in out.iterdir():
            f.unlink()
        again = self.run(repo, out, "init", "run_id=r1", "mode=batch")
        assert again.returncode == 1 and "this is a resume" in again.stderr
        read = self.run(repo, out, "read")
        assert "phase_create" in read.stdout
        assert (out / "strat-progress.yaml").is_file()

    def test_read_without_progress(self, tmp_path):
        repo, out = self.setup(tmp_path)
        read = self.run(repo, out, "read")
        assert read.returncode == 0 and "no progress recorded" in read.stdout
        assert not (out / "strat-progress.yaml").exists()


# ------------------------------------------------------------------ handoff

def test_handoff_copies_regular_artifacts_only(tmp_path):
    repo = make_repo(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    (repo / "artifacts/strat-tasks/leak.md").symlink_to(secret)
    (repo / "artifacts/strat-skipped.md").write_text("| key |\n")
    dest = tmp_path / "publish" / "handoff"
    assert handoff(repo, dest) == 4
    assert (dest / "strat-tasks" / f"{STRAT}.md").is_file()
    assert not (dest / "strat-tasks" / "leak.md").exists()
    assert (dest / "strat-skipped.md").is_file()


# ------------------------------------------------- Jira-backed boundaries

def jira_env(jira, **extra):
    env = {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin",
           "JIRA_TOKEN": "admin", "TARGET_REPO_DIR": str(ROOT)}
    for key in ("RFE_KEY", "BATCH_SIZE", "BATCH_OFFSET",
                "BATCH_EXPECTED_KEYS", "RFE_SKIP_BOOTSTRAP"):
        env.pop(key, None)
    env.update(extra)
    return env


def labels(jira, key):
    return set(jira.get(key)["fields"].get("labels", []))


def link(jira, strat_key, rfe_key):
    jira.request("POST", "/rest/api/3/issueLink", {
        "type": {"name": "Cloners"},
        "inwardIssue": {"key": strat_key},
        "outwardIssue": {"key": rfe_key},
    })


def eligible_rfe(jira, key, extra_labels=()):
    jira.create(key, f"RFE {key}", "Need.",
                labels=ELIGIBLE + list(extra_labels))


def strat_clone(jira, strat, rfe, strat_labels):
    jira.create(strat, "Strategy", "Body.", issue_type="Feature",
                labels=list(strat_labels))
    link(jira, strat, rfe)


def prepare(jira, tmp_path, mode, repo=ROOT, **extra):
    state = tmp_path / "state"
    prescript_out = tmp_path / "prescript.out"
    prescript_out.write_text("")
    env = jira_env(jira, **{"STRAT_RUN_ID": RUN_ID,
                            "STRAT_STATE_DIR": str(state),
                            "TARGET_REPO_DIR": str(repo),
                            "FULLSEND_PRESCRIPT_OUTPUT": str(prescript_out),
                            **extra})
    proc = subprocess.run([sys.executable, str(SHARED / "prepare_run.py"),
                           mode], capture_output=True, text=True, env=env)
    return proc, state, prescript_out


def scratch_repo(tmp_path):
    """A disposable copy of the checkout for scripts that write into it."""
    repo = tmp_path / "checkout"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
        "artifacts", "tmp", ".context", "eval", "tests"))
    return repo


class TestPrepareSingle:
    def test_locks_records_and_writes_agent_input(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.")
        repo = scratch_repo(tmp_path)
        proc, state, out = prepare(jira, tmp_path, "single", repo=repo,
                                   RFE_KEY=RFE)
        assert proc.returncode == 0, proc.stderr
        assert PROCESSING in labels(jira, RFE)
        assert (state / "owned-rfe-ids.txt").read_text() == f"{RFE}\n"
        record = json.loads((state / "run.json").read_text())
        assert record["acquired_keys"] == [RFE]
        agent_input = json.loads((repo / "tmp/strat-input.json").read_text())
        assert agent_input == record
        assert out.read_text() == ""

    @pytest.mark.parametrize("label", [PROCESSING,
                                       "strat-creator-needs-attention",
                                       "strat-creator-human-sign-off"])
    def test_blocked_rfe_skips_natively(self, jira, tmp_path, label):
        jira.create(RFE, "RFE", "Need.", labels=[label])
        proc, state, out = prepare(jira, tmp_path, "single", RFE_KEY=RFE)
        assert proc.returncode == 78, proc.stderr
        assert "skipped=true" in out.read_text()
        assert (state / "owned-rfe-ids.txt").read_text() == ""
        result = json.loads((state / "preflight-result.json").read_text())
        assert result["skipped"][0]["reason"].endswith(label)
        # The no-model skip record is itself a valid skipped result.
        empty = tmp_path / "empty"
        empty.mkdir()
        assert validate_result.validate(
            result, empty, empty, state, str(SCHEMA), env=ENV) == []

    def test_bad_key_is_an_error(self, jira, tmp_path):
        proc, _, _ = prepare(jira, tmp_path, "single", RFE_KEY="RHAIRFE-1;id")
        assert proc.returncode == 1

    def test_missing_run_id_is_an_error(self, jira, tmp_path):
        proc, _, _ = prepare(jira, tmp_path, "single", RFE_KEY=RFE,
                             STRAT_RUN_ID="")
        assert proc.returncode == 1

    def test_live_lock_record_is_not_overwritten(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.")
        state = tmp_path / "state"
        state.mkdir()
        (state / "owned-rfe-ids.txt").write_text("RHAIRFE-1\n")
        proc, _, _ = prepare(jira, tmp_path, "single", RFE_KEY=RFE)
        assert proc.returncode == 1
        assert "already records locks" in proc.stderr
        assert (state / "owned-rfe-ids.txt").read_text() == "RHAIRFE-1\n"
        assert PROCESSING not in labels(jira, RFE)


class TestPrepareBatch:
    def test_discovers_excludes_processed_and_locks_partially(self, jira,
                                                              tmp_path):
        eligible_rfe(jira, "RHAIRFE-20")
        eligible_rfe(jira, "RHAIRFE-21", ["strat-creator-needs-attention"])
        eligible_rfe(jira, "RHAIRFE-22")
        strat_clone(jira, "RHAISTRAT-90", "RHAIRFE-22",
                    ["strat-creator-auto-created",
                     "strat-creator-rubric-pass"])
        eligible_rfe(jira, "RHAIRFE-23", [PROCESSING])
        jira.create("RHAIRFE-24", "Not in scope", "Need.")
        repo = scratch_repo(tmp_path)
        proc, state, _ = prepare(jira, tmp_path, "batch", repo=repo)
        assert proc.returncode == 0, proc.stderr
        record = json.loads((state / "run.json").read_text())
        # 22 is processed (rubric-pass STRAT), 23 is locked elsewhere and 24
        # fails the release/quality JQL, so discovery leaves them out.
        assert record["input_keys"] == ["RHAIRFE-20", "RHAIRFE-21"]
        assert record["acquired_keys"] == ["RHAIRFE-20"]
        assert record["skipped"] == [{
            "key": "RHAIRFE-21",
            "reason": "blocked by label(s): strat-creator-needs-attention"}]
        assert PROCESSING in labels(jira, "RHAIRFE-20")
        assert PROCESSING not in labels(jira, "RHAIRFE-21")

    def test_size_and_offset_bound_discovery(self, jira, tmp_path):
        for n in range(30, 34):
            eligible_rfe(jira, f"RHAIRFE-{n}")
        proc, state, _ = prepare(jira, tmp_path, "batch",
                                 repo=scratch_repo(tmp_path),
                                 BATCH_SIZE="2", BATCH_OFFSET="1")
        assert proc.returncode == 0, proc.stderr
        record = json.loads((state / "run.json").read_text())
        assert record["acquired_keys"] == ["RHAIRFE-31", "RHAIRFE-32"]
        assert PROCESSING not in labels(jira, "RHAIRFE-30")

    def test_expected_bound_mismatch_takes_no_locks(self, jira, tmp_path):
        eligible_rfe(jira, "RHAIRFE-40")
        proc, state, _ = prepare(jira, tmp_path, "batch",
                                 BATCH_EXPECTED_KEYS="RHAIRFE-41")
        assert proc.returncode == 1
        assert "differs from the expected bound" in proc.stderr
        assert PROCESSING not in labels(jira, "RHAIRFE-40")
        assert not (state / "owned-rfe-ids.txt").exists()

    def test_nothing_discovered_skips_natively(self, jira, tmp_path):
        proc, state, out = prepare(jira, tmp_path, "batch")
        assert proc.returncode == 78, proc.stderr
        assert "skipped=true" in out.read_text()
        result = json.loads((state / "preflight-result.json").read_text())
        assert result["input_keys"] == [] and result["mode"] == "batch"

    def test_failed_run_is_rediscovered_for_resume(self, jira, tmp_path):
        # An earlier run cloned the STRAT and then failed: the STRAT has the
        # provenance label but no verdict, and the lock was released.
        eligible_rfe(jira, "RHAIRFE-50")
        strat_clone(jira, "RHAISTRAT-95", "RHAIRFE-50",
                    ["strat-creator-auto-created",
                     "strat-creator-auto-refined"])
        proc, state, _ = prepare(jira, tmp_path, "batch",
                                 repo=scratch_repo(tmp_path))
        assert proc.returncode == 0, proc.stderr
        record = json.loads((state / "run.json").read_text())
        assert record["acquired_keys"] == ["RHAIRFE-50"]


class TestPreScript:
    """pre-strategy.sh end to end, with a local architecture context."""

    def run(self, jira, tmp_path, repo, **extra):
        ctx = tmp_path / "arch"
        (ctx / "architecture" / "rhoai-3.6").mkdir(parents=True)
        (ctx / "architecture" / "rhoai-3.6" / "PLATFORM.md").write_text("x")
        (ctx / "overlays").mkdir()
        env = jira_env(jira, **{
            "TARGET_REPO_DIR": str(repo), "STRAT_RUN_ID": RUN_ID,
            "STRAT_STATE_DIR": str(tmp_path / "state"),
            "STRAT_ARCHITECTURE_CONTEXT_PATH": str(ctx), **extra})
        return subprocess.run(
            ["bash", str(FULLSEND / "strat-single" / "pre-strat-single.sh")],
            capture_output=True, text=True, env=env)

    def test_cleans_fetches_context_and_locks(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.")
        repo = scratch_repo(tmp_path)
        write_md(repo / "artifacts/strat-tasks/RHAISTRAT-7.md",
                 {"strat_id": "RHAISTRAT-7"})
        (repo / "tmp").mkdir()
        (repo / "tmp/strat-input.json").write_text("{}")
        proc = self.run(jira, tmp_path, repo, RFE_KEY=RFE)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not (repo / "artifacts/strat-tasks").exists()
        assert (repo / ".context/architecture-context/LATEST_VERSION"
                ).read_text().strip() == "rhoai-3.6"
        assert json.loads((repo / "tmp/strat-input.json").read_text()
                          )["acquired_keys"] == [RFE]
        settings = json.loads((repo / ".claude/settings.json").read_text())
        assert "/tmp/strat-assess" not in settings["permissions"].get(
            "additionalDirectories", [])
        assert PROCESSING in labels(jira, RFE)

    def test_context_failure_takes_no_locks(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.")
        proc = self.run(jira, tmp_path, scratch_repo(tmp_path), RFE_KEY=RFE,
                        STRAT_ARCHITECTURE_CONTEXT_PATH=str(tmp_path / "nope"))
        assert proc.returncode == 1
        assert PROCESSING not in labels(jira, RFE)

    def test_blocked_exits_78(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.", labels=[PROCESSING])
        proc = self.run(jira, tmp_path, scratch_repo(tmp_path), RFE_KEY=RFE)
        assert proc.returncode == 78, proc.stdout + proc.stderr


class TestReleaseLocks:
    def run(self, jira, state):
        return subprocess.run(
            ["bash", str(SHARED / "release-locks.sh"), str(state), str(ROOT)],
            capture_output=True, text=True, env=jira_env(jira))

    def test_releases_recorded_keys_only(self, jira, tmp_path):
        for key in ("RHAIRFE-20", "RHAIRFE-21", "RHAIRFE-22"):
            jira.create(key, key, "Need.", labels=[PROCESSING])
        state = tmp_path / "state"
        state.mkdir()
        (state / "owned-rfe-ids.txt").write_text("RHAIRFE-20\nRHAIRFE-21\n")
        proc = self.run(jira, state)
        assert proc.returncode == 0, proc.stderr
        assert "releasing RHAIRFE-20 RHAIRFE-21" in proc.stdout
        assert PROCESSING not in labels(jira, "RHAIRFE-20")
        assert PROCESSING not in labels(jira, "RHAIRFE-21")
        # Not recorded as owned, so another job's lock is left alone.
        assert PROCESSING in labels(jira, "RHAIRFE-22")
        assert (state / "owned-rfe-ids.txt.released").is_file()
        assert not (state / "owned-rfe-ids.txt").exists()

    def test_lock_then_release_round_trip(self, jira, tmp_path):
        jira.create(RFE, "RFE", "Need.")
        proc, state, _ = prepare(jira, tmp_path, "single",
                                 repo=scratch_repo(tmp_path), RFE_KEY=RFE)
        assert proc.returncode == 0, proc.stderr
        assert self.run(jira, state).returncode == 0
        assert PROCESSING not in labels(jira, RFE)

    def test_no_record_is_not_an_error(self, jira, tmp_path):
        proc = self.run(jira, tmp_path)
        assert proc.returncode == 0
        assert "no lock record" in proc.stdout

    def test_invalid_record_refused(self, jira, tmp_path):
        (tmp_path / "owned-rfe-ids.txt").write_text("RHAIRFE-1\n--help\n")
        proc = self.run(jira, tmp_path)
        assert proc.returncode == 1
        assert "invalid key" in proc.stderr


class TestValidateJira:
    def seed(self, jira, verdict_label="strat-creator-rubric-pass",
             locked=True):
        jira.create(RFE, "RFE", "Need.",
                    labels=[PROCESSING] if locked else None)
        strat_clone(jira, STRAT, RFE, ["strat-creator-auto-created",
                                       "strat-creator-auto-refined",
                                       verdict_label])

    def check(self, jira, tmp_path):
        keys = ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update({"JIRA_SERVER": jira.url, "JIRA_USER": "admin",
                           "JIRA_TOKEN": "admin"})
        try:
            return validate_result.validate(
                completed(), make_output(tmp_path), make_repo(tmp_path),
                make_state(tmp_path), str(SCHEMA), use_jira=True, env=ENV)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_matching_jira_passes(self, jira, tmp_path):
        self.seed(jira)
        assert self.check(jira, tmp_path) == []

    def test_released_lock_rejected(self, jira, tmp_path):
        self.seed(jira, locked=False)
        errors = self.check(jira, tmp_path)
        assert any("not locked although recorded" in e for e in errors)

    def test_missing_verdict_label_rejected(self, jira, tmp_path):
        self.seed(jira, verdict_label="unrelated")
        errors = self.check(jira, tmp_path)
        assert any("lacks label strat-creator-rubric-pass" in e
                   for e in errors)

    def test_resume_must_reuse_existing_clone(self, jira, tmp_path):
        self.seed(jira)
        strat_clone(jira, "RHAISTRAT-41", RFE, [])
        errors = self.check(jira, tmp_path)
        assert any("more than one open STRAT clone" in e for e in errors)

    def test_closed_earlier_clone_is_not_a_duplicate(self, jira, tmp_path):
        self.seed(jira)
        jira.create("RHAISTRAT-41", "Old", "Body.", issue_type="Feature",
                    status="Closed")
        link(jira, "RHAISTRAT-41", RFE)
        assert self.check(jira, tmp_path) == []


# ------------------------------------------------------- harness wiring

@pytest.mark.parametrize("name", ["strat-single", "strat-batch"])
def test_harness_wiring(name):
    config = yaml.safe_load((FULLSEND / "config.yaml").read_text())
    assert f"{name}/{name}.yaml" in [a["source"] for a in config["agents"]]
    harness = yaml.safe_load((FULLSEND / name / f"{name}.yaml").read_text())
    assert harness["agent"] == f"{name}/prompt.md"
    assert harness["model"] == "claude-opus-4-6"
    assert harness["env"]["sandbox"]["CLAUDE_CODE_SUBAGENT_MODEL"] == \
        "claude-opus-4-6"
    assert harness["env"]["sandbox"]["STRAT_MODE"] == name.split("-")[1]
    assert harness["validation_loop"]["max_iterations"] == 2
    assert harness["validation_loop"]["feedback_mode"] == "append"
    # The host record and token-bearing values stay out of the sandbox.
    assert "STRAT_STATE_DIR" not in harness["env"]["sandbox"]
    assert "STRAT_STATE_DIR" in harness["env"]["runner"]
    assert "RESULTS_PUSH_TOKEN" not in harness["env"]["sandbox"]
    for key in ("agent", "pre_script", "post_script", "policy"):
        assert (FULLSEND / harness[key]).is_file(), harness[key]
    for key in ("script", "schema"):
        assert (FULLSEND / harness["validation_loop"][key]).is_file()
    for entry in harness["host_files"]:
        if not entry["src"].startswith("${"):
            assert (FULLSEND / entry["src"]).is_file(), entry["src"]
    assert "entrypoint" not in harness
    prompt = (FULLSEND / harness["agent"]).read_text()
    assert "tmp/strat-input.json" in prompt
    assert "Never run `scripts/lock_issues.py`" in prompt
    assert "Do not end your turn until `agent-result.json` is written" in prompt
    assert "progress.sh read" in prompt
    assert "state.py set \"$P\"" not in prompt
