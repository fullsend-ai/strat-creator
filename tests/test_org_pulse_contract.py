"""Completed feature export preserves numeric verdicts, dissent and real skips."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "org_pulse_publisher", ROOT / ".fullsend/shared/publish/push-to-org-pulse.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def feature():
    return json.loads((ROOT / "tests/fixtures/org-pulse-feature-contract.json").read_text())["features"][0]


def strategy():
    value = feature()
    value["strat_id"] = value.pop("key")
    return value


def test_completed_export_matches_consumer_fixture_without_hiding_dissent_or_skip():
    assert publisher.build_features([strategy()]) == [feature()]


@pytest.mark.parametrize("role", ["feasibility", "testability", "scope", "architecture"])
def test_pending_assessment_is_not_exported_as_a_completed_review(role, capsys):
    provisional = strategy()
    provisional["reviewers"][role] = "pending"
    original = copy.deepcopy(provisional)
    assert publisher.build_features([provisional, strategy()]) == [feature()]
    assert provisional == original
    assert "provisional review" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [True, False])
def test_all_provisional_cli_exits_without_a_sample_or_api_call(tmp_path, monkeypatch, capsys, dry_run):
    provisional = strategy()
    provisional["reviewers"]["scope"] = "pending"
    directory = tmp_path / "RHAISTRAT"
    directory.mkdir()
    (directory / "summary-production.json").write_text(json.dumps({"summary": {"strategies": [provisional]}}))
    monkeypatch.setenv("ORG_PULSE_API_TOKEN", "isolated-placeholder")
    args = ["push-to-org-pulse.py", "--results-dir", str(tmp_path), "--api-url", "https://orgpulse.invalid"]
    monkeypatch.setattr(sys, "argv", args + (["--dry-run"] if dry_run else []))

    def unexpected(*args):
        pytest.fail("provisional-only export must not call the API")

    monkeypatch.setattr(publisher, "push_to_org_pulse", unexpected)
    with pytest.raises(SystemExit) as stopped:
        publisher.main()
    assert stopped.value.code == 0
    output = capsys.readouterr()
    assert "No completed" in output.out and "Sample entry" not in output.out
