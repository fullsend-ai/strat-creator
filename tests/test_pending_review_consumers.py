"""Provisional specialist values survive JSON and remain neutral in both UIs."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(filename):
    spec = importlib.util.spec_from_file_location("pending_" + filename, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename", ["generate-report.py", "generate-dashboard.py"])
def test_pending_is_rendered_as_provisional_rather_than_a_specialist_verdict(filename):
    renderer = load(filename)
    assert renderer.verdict_class("pending") == "verdict-unknown"
    assert renderer.verdict_label("pending") == "pending"
    assert not renderer.is_approve("pending")


def test_json_export_preserves_pending_separately_from_the_numeric_decision():
    exporter = load("extract-pipeline-data.py")
    reviewers = dict.fromkeys(("feasibility", "testability", "scope", "architecture"), "pending")
    task = {"meta": {"status": "Refined", "source_rfe": "RHAIRFE-9703"}, "body": "## Strategy\nBounded queues.\n"}
    review = {"meta": {"recommendation": "approve", "needs_attention": False, "reviewers": reviewers}, "body": ""}
    exported = exporter.extract_strategy("STRAT-9703", task, review, "")
    assert exported["reviewers"] == reviewers
    assert exported["recommendation"] == "approve"
