"""The independently maintained type descriptor matches runtime reviewer values."""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from artifact_utils import SCHEMAS  # noqa: E402


@pytest.mark.parametrize("role", ["feasibility", "testability", "scope", "architecture"])
def test_reviewer_descriptor_matches_runtime_contract(role):
    descriptor = yaml.safe_load((ROOT / "types/rfe-strategy/type.yaml").read_text())
    declared = descriptor["schema"]["review"]["extra_fields"]["reviewers"]["fields"][role]
    runtime = SCHEMAS["strat-review"]["reviewers"]["fields"][role]
    assert declared == runtime


def test_provisional_reviewer_values_do_not_expand_numeric_recommendations():
    descriptor = yaml.safe_load((ROOT / "types/rfe-strategy/type.yaml").read_text())
    assert descriptor["schema"]["review"]["recommendation_enum"] == SCHEMAS["strat-review"]["recommendation"]["enum"]
    assert set(descriptor["schema"]["review"]["recommendation_enum"]) == {"approve", "revise", "reject"}
