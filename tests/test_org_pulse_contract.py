"""Completed feature export preserves numeric verdicts, dissent and real skips."""
import copy
import importlib.util
import json
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
