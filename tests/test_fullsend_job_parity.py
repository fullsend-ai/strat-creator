"""Real host preparation against the Jira emulator, without model calls."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_fullsend_host_integration import (
    _git,
    _labels,
    _pre,
    _run_record,
    _strat,
)
from test_fullsend_host_integration import (
    host as host,
)
from test_fullsend_host_integration import (
    source as source,
)

ROOT = Path(__file__).resolve().parents[1]
PROCESSING = "strat-creator-processing"
AUTO = "strat-creator-auto-created"
PASS = "strat-creator-rubric-pass"


def config_source(host):
    """A separate committed source fixture, never modify the shared source."""
    checkout = host["tmp"] / "config-source"
    _git(host["tmp"], "clone", "-q", str(host["src"]), str(checkout))
    path = checkout / "config/cutover-test.yaml"
    path.write_text(yaml.safe_dump({"test_rfes": [
        {"id": "RHAIRFE-7601", "baseline": True},
        {"id": "RHAIRFE-7602", "baseline": False},
        {"id": "RHAIRFE-7602", "baseline": False},
    ]}))
    _git(checkout, "add", str(path.relative_to(checkout)))
    _git(checkout, "-c", "user.name=test", "-c", "user.email=test@example.com",
         "commit", "-qm", "synthetic curated batch")
    return {"STRAT_CREATOR_REPO": str(checkout), "STRAT_CREATOR_REF": _git(checkout, "rev-parse", "HEAD")}


@pytest.mark.parametrize("rfe_filter,offset,expected", [
    ("--baseline", "0", ["RHAIRFE-7601"]),
    ("--no-baseline", "0", ["RHAIRFE-7602"]),
    ("", "1", ["RHAIRFE-7602"]),
])
def test_curated_batch_uses_config_filter_and_offset_then_owned_claims(host, jira, rfe_filter, offset, expected):
    jira.create("RHAIRFE-7601", "Bounded inference request cancellation", "Stop work at the request deadline.")
    jira.create("RHAIRFE-7602", "Offline artifact signature verification", "Verify signatures without network access.")
    result = _pre(host, "batch", **config_source(host), CONFIG_FILE="config/cutover-test.yaml",
                  RFE_FILTER=rfe_filter, BATCH_SIZE="10", BATCH_OFFSET=offset)
    assert result.returncode == 0, result.stdout + result.stderr
    run = _run_record(host)
    assert run["input_keys"] == expected
    assert run["acquired_keys"] == expected
    assert run["request"]["CONFIG_FILE"] == "config/cutover-test.yaml"
    assert run["request"]["RFE_FILTER"] == rfe_filter
    assert (host["state"] / "owned-rfe-ids.txt").read_text().split() == expected
    for key in expected:
        assert PROCESSING in _labels(jira, key)


@pytest.mark.parametrize("path", ["../outside.yaml", "/tmp/outside.yaml", "config/missing.yaml"])
def test_invalid_config_stops_before_acquiring_tickets(host, jira, path):
    result = _pre(host, "batch", CONFIG_FILE=path)
    assert result.returncode != 0
    assert not (host["state"] / "owned-rfe-ids.txt").exists()


def make_strategy(jira, extra=()):
    jira.create("RHAIRFE-7603", "Air-gapped signature verification", "Users verify signed artifacts offline.")
    _strat(jira, "RHAISTRAT-8603", "RHAIRFE-7603", [AUTO, PASS, *extra])


def reprocess(host, **extra):
    return _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8603",
                STRAT_REPROCESS_KEY="RHAISTRAT-8603", **extra)


@pytest.mark.parametrize("dry", ["0", "1"])
def test_explicit_reprocess_imports_exact_strategy_and_forces_new_refinement(host, jira, dry):
    make_strategy(jira)
    result = reprocess(host, STRAT_DRY_RUN=dry)
    assert result.returncode == 0, result.stdout + result.stderr
    run = _run_record(host)
    assert run["reprocess"] == {"RHAIRFE-7603": "RHAISTRAT-8603"}
    assert run["resume_points"] == {"RHAIRFE-7603": "refine"}
    assert run["expected_skips"] == []
    progress = json.loads((host["target"] / "tmp/strat-progress.json").read_text())
    assert progress["create"]["done_at"]
    assert progress["create"]["strategies"] == [{"rfe": "RHAIRFE-7603", "strat": "RHAISTRAT-8603"}]
    assert progress["refine"] == {}
    assert PASS in _labels(jira, "RHAISTRAT-8603")  # Never clear labels to bypass the gate.
    assert (PROCESSING in _labels(jira, "RHAIRFE-7603")) == (dry == "0")
    command = [sys.executable, str(ROOT / "scripts/strat_reprocess.py"), "RHAISTRAT-8603"]
    checked = subprocess.run(command, cwd=host["target"], env={**host["env"], "STRAT_DRY_RUN": dry},
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["reprocess"] == "RHAISTRAT-8603"
    before = (host["target"] / "tmp/strat-progress.json").read_bytes()
    again = reprocess(host, STRAT_DRY_RUN=dry)
    assert again.returncode == 0, again.stderr
    assert (host["target"] / "tmp/strat-progress.json").read_bytes() == before
    wrong = subprocess.run([*command[:-1], "RHAISTRAT-9999", "--prepare"], cwd=host["target"],
                           env=host["env"], capture_output=True, text=True)
    assert wrong.returncode != 0
    assert (host["target"] / "tmp/strat-progress.json").read_bytes() == before


@pytest.mark.parametrize("gate", ["strat-creator-needs-attention", "strat-creator-human-sign-off"])
def test_explicit_reprocess_does_not_override_human_gates(host, jira, gate):
    make_strategy(jira, [gate])
    result = reprocess(host)
    assert result.returncode != 0
    assert PROCESSING not in _labels(jira, "RHAIRFE-7603")
    assert not (host["target"] / "artifacts/strat-tasks/RHAISTRAT-8603.md").exists()


def test_normal_resume_still_skips_approved_strategies(host, jira):
    make_strategy(jira)
    result = _pre(host, "resume", STRAT_RESUME_KEY="RHAISTRAT-8603")
    assert result.returncode == 78
    assert PROCESSING not in _labels(jira, "RHAIRFE-7603")


def test_reprocess_rejects_multiple_open_clones(host, jira):
    make_strategy(jira)
    _strat(jira, "RHAISTRAT-8604", "RHAIRFE-7603", [AUTO])
    result = reprocess(host)
    assert result.returncode != 0
    assert PROCESSING not in _labels(jira, "RHAIRFE-7603")


def test_reprocess_rejects_non_pipeline_strategy(host, jira):
    jira.create("RHAIRFE-7603", "RFE", "Need.")
    _strat(jira, "RHAISTRAT-8603", "RHAIRFE-7603", [PASS])
    result = reprocess(host)
    assert result.returncode != 0
    assert PROCESSING not in _labels(jira, "RHAIRFE-7603")


def test_reprocess_stops_if_a_human_gate_arrives_after_preparation(host, jira):
    make_strategy(jira)
    result = reprocess(host)
    assert result.returncode == 0, result.stderr
    labels = sorted(_labels(jira, "RHAISTRAT-8603") | {"strat-creator-human-sign-off"})
    jira.request("PUT", "/rest/api/3/issue/RHAISTRAT-8603", {"fields": {"labels": labels}})
    checked = subprocess.run([sys.executable, str(ROOT / "scripts/strat_reprocess.py"), "RHAISTRAT-8603"],
                             cwd=host["target"], env=host["env"], capture_output=True, text=True)
    assert checked.returncode != 0
