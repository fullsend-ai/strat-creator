"""Human intervention during owned acquisition never authorizes pipeline work."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_side_effect_recovery import workspace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lock_issues  # noqa: E402
from jira_utils import add_comment, markdown_to_adf  # noqa: E402
from strat_freshness import initialize  # noqa: E402
from strat_side_effects import owner_label  # noqa: E402


@pytest.mark.parametrize("gate", ["strat-creator-human-sign-off", "strat-creator-needs-attention"])
@pytest.mark.parametrize("phase", ["fresh", "recovered", "already-gated"])
def test_gate_race_refuses_work_and_retains_every_owned_cleanup_candidate(
    tmp_path, jira, monkeypatch, capsys, gate, phase
):
    owner = "acquisition-gate-run"
    good, target, later = "RHAIRFE-7901", "RHAIRFE-7902", "RHAIRFE-7903"
    for key in (good, target, later):
        labels = ["team-context"]
        if key == target and phase != "fresh":
            labels += [lock_issues.PROCESSING_LABEL, owner_label(owner)]
        if key == target and phase == "already-gated":
            labels.append(gate)
        jira.create(key, "Authored RFE", "Distinct business need.", labels=labels)
    state = tmp_path / "state"
    record = state / "owned-rfe-ids.txt"
    original = lock_issues._get_labels
    injected = []

    def intervene(server, user, token, key):
        labels = original(server, user, token, key)
        if key == target and not injected and phase != "already-gated":
            # A genuine Jira write after the first read, before acquisition's
            # write/final read. Recovery already holds this run's remote claim.
            jira.request("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": gate}]}})
            injected.append(True)
        return labels

    monkeypatch.setattr(lock_issues, "_get_labels", intervene)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="human gate"):
        lock_issues.lock_owned(jira.url, "admin", "admin", [good, target, later], record, owner)
    assert capsys.readouterr().out == ""  # No acquired subset is returned to preparation.
    assert record.read_text().split() == [good, target]
    assert json.loads(record.with_name(record.name + ".owner.json").read_text()) == {"owner": owner}
    for key in (good, target):
        assert {lock_issues.PROCESSING_LABEL, owner_label(owner)} <= set(jira.get(key)["fields"]["labels"])
    assert set(jira.get(later)["fields"]["labels"]) == {"team-context"}
    assert gate in jira.get(target)["fields"]["labels"]
    env = {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
    env.pop("STRAT_MANAGED_RUN", None)  # Host-side cleanup, outside the managed agent.
    for _ in range(2):
        released = subprocess.run(["bash", str(ROOT / ".fullsend/shared/release-locks.sh"), str(state)],
                                  cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert released.returncode == 0, released.stdout + released.stderr
    assert record.with_name(record.name + ".released").read_text().split() == [good, target]
    assert set(jira.get(target)["fields"]["labels"]) == {"team-context", gate}
    assert set(jira.get(good)["fields"]["labels"]) == {"team-context"}


@pytest.mark.parametrize("malformed", [None, "strat-creator-human-sign-off", {}, ["ordinary", 7], "missing"])
def test_acquisition_refuses_malformed_requested_labels_before_claim(tmp_path, jira, monkeypatch, malformed):
    key = "RHAIRFE-7904"
    jira.create(key, "Authored RFE", "Need.")
    original = lock_issues.get_issue

    def bad_response(*args, **kwargs):
        issue = original(*args, **kwargs)
        if malformed == "missing":
            issue["fields"].pop("labels", None)
        else:
            issue["fields"]["labels"] = malformed
        return issue

    monkeypatch.setattr(lock_issues, "get_issue", bad_response)
    monkeypatch.chdir(tmp_path)
    record = tmp_path / "owned-rfe-ids.txt"
    with pytest.raises(ValueError, match="labels.*list of strings"):
        lock_issues.lock_owned(jira.url, "admin", "admin", [key], record, "gate-run")
    assert not record.exists()
    assert jira.get(key)["fields"]["labels"] == []


def test_conflicting_owner_still_blocks_acquisition_and_cleanup(tmp_path, jira, monkeypatch):
    key, owner = "RHAIRFE-7905", "gate-run"
    gate = "strat-creator-needs-attention"
    jira.create(key, "Authored RFE", "Need.")
    original = lock_issues._get_labels
    calls = []

    def conflict(server, user, token, target):
        labels = original(server, user, token, target)
        if not calls:
            jira.request("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [
                {"add": gate}, {"add": owner_label("other-run")}]}})
        calls.append(target)
        return labels

    monkeypatch.setattr(lock_issues, "_get_labels", conflict)
    monkeypatch.chdir(tmp_path)
    record = tmp_path / "owned-rfe-ids.txt"
    with pytest.raises(RuntimeError, match="conflicting ownership"):
        lock_issues.lock_owned(jira.url, "admin", "admin", [key], record, owner)
    assert record.read_text().split() == [key]
    before = jira.get(key)
    with pytest.raises(RuntimeError, match="foreign or conflicting"):
        lock_issues.unlock_owned(jira.url, "admin", "admin", [key], owner)
    assert jira.get(key) == before


@pytest.mark.parametrize("gate", ["strat-creator-human-sign-off", "strat-creator-needs-attention"])
@pytest.mark.parametrize("target", ["RHAIRFE-7701", "RHAISTRAT-7701"])
def test_gate_in_initial_baseline_does_not_authorize_owned_mutations(tmp_path, jira, monkeypatch, gate, target):
    env = workspace(tmp_path, jira)
    for name in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.chdir(tmp_path)
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-7701", {"update": {"labels": [{"add": gate}]}})
    path = tmp_path / "tmp/strat-input.json"
    data = json.loads(path.read_text())
    initialize(tmp_path, data, live=True)
    path.write_text(json.dumps(data))  # Deliberately captures the gate in a matching baseline.
    before = {key: jira.get(key) for key in ("RHAIRFE-7701", "RHAISTRAT-7701")}
    with pytest.raises((ValueError, RuntimeError), match="human gate"):
        add_comment(jira.url, "admin", "admin", target, markdown_to_adf("No work authorized."))
    assert {key: jira.get(key) for key in before} == before
    assert not list((tmp_path / "tmp/strat-effects").glob("*.json"))
