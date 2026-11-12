"""Unit tests for dashboard metric contracts.

Covers the two metrics added by docs/plans/001 and ADR-0001:
  - Empty SME Input detection (has_sme_input)
  - Refine loop count source: refine_count validation + absent-vs-zero, and the
    rubric-pass predicate (is_approve) used by the SME Input KPI.

extract-pipeline-data.py is hyphenated, so it is loaded via importlib.
"""
import importlib.util
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS_DIR))

from artifact_utils import (  # noqa: E402
    read_frontmatter,
    validate,
    write_frontmatter,
)


def _load_module(filename, modname):
    path = os.path.join(SCRIPTS_DIR, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


extract = _load_module("extract-pipeline-data.py", "extract_pipeline_data")
dashboard = _load_module("generate-dashboard.py", "generate_dashboard")


# The exact default placeholder shipped in the strat templates.
SME_BOILERPLATE = (
    "## Staff Engineer / SME Input\n\n"
    "*Add technical corrections, architectural direction, component "
    "preferences, or domain expertise below. Write in declarative, cumulative "
    "form - statements that remain valid across refinement iterations. This "
    "input takes priority over architecture context when they conflict. After "
    "review: address findings, then remove the needs-attention label from "
    "Jira.*\n"
)


# ─── has_sme_input ──────────────────────────────────────────────────────────


class TestHasSmeInput:

    def test_empty_boilerplate_only(self):
        assert extract.has_sme_input(SME_BOILERPLATE) is False

    def test_real_content(self):
        body = SME_BOILERPLATE + "\nUse the Kueue controller for quota.\n"
        assert extract.has_sme_input(body) is True

    def test_missing_section(self):
        assert extract.has_sme_input("## Business Need\nsome text\n") is False

    def test_boilerplate_plus_additional_text(self):
        body = SME_BOILERPLATE + "\nPrefer the existing operator pattern.\n"
        assert extract.has_sme_input(body) is True

    def test_html_comment_placeholder_only(self):
        body = "## Staff Engineer / SME Input\n<!-- placeholder -->\n"
        assert extract.has_sme_input(body) is False

    def test_content_stops_at_next_header(self):
        # Real content lives in a later section, not the SME one.
        body = SME_BOILERPLATE + "\n## Next Section\nreal content here\n"
        assert extract.has_sme_input(body) is False


# ─── valid_refine_count (fail-closed normalization) ─────────────────────────


class TestValidRefineCount:

    @pytest.mark.parametrize("value,expected", [
        (0, 0),          # explicit zero is authoritative, not absent
        (3, 3),
        (10, 10),
        (None, None),    # absent -> fallback
        (-1, None),      # negative is malformed -> fallback
        (-99, None),
        (True, None),    # bool is not a valid int here -> fallback
        (False, None),
        ("2", None),     # string -> fallback
        (2.5, None),     # float -> fallback
    ])
    def test_normalization(self, value, expected):
        assert extract.valid_refine_count(value) == expected


# ─── rubric-pass predicate (SME Input KPI) ──────────────────────────────────


class TestIsApprovePredicate:

    @pytest.mark.parametrize("value,expected", [
        ("approve", True),
        ("approved", True),
        ("revise", False),
        ("reject", False),
        ("", False),
        ("split", False),
    ])
    def test_is_approve_mapping(self, value, expected):
        assert extract.is_approve(value) is expected


# ─── extract_strategy field wiring ──────────────────────────────────────────


def _make_task(body, meta=None):
    m = {
        "strat_id": "STRAT-001",
        "title": "Test",
        "source_rfe": "RHAIRFE-1",
        "priority": "Major",
        "status": "Draft",
    }
    if meta:
        m.update(meta)
    return {"meta": m, "body": body}


class TestExtractStrategyFields:

    def test_informational_reviewer_is_carried_through(self):
        review = {"meta": {"reviewers": {"feasibility": "approve", "testability": "approve",
                                         "scope": "approve", "architecture": "approve",
                                         "uxresearch": "revise"}}}
        s = extract.extract_strategy("STRAT-001", _make_task("body"), review, "")
        assert s["reviewers"]["uxresearch"] == "revise"
        assert s["scores"] is None, "an unscored reviewer must not create scores"

    def test_absent_informational_reviewer_is_blank(self):
        review = {"meta": {"reviewers": {"feasibility": "approve", "testability": "approve",
                                         "scope": "approve", "architecture": "approve"}}}
        s = extract.extract_strategy("STRAT-001", _make_task("body"), review, "")
        assert s["reviewers"]["uxresearch"] == ""

    def test_has_sme_input_true(self):
        task = _make_task(SME_BOILERPLATE + "\nReal SME guidance.\n")
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["has_sme_input"] is True

    def test_has_sme_input_false(self):
        task = _make_task(SME_BOILERPLATE)
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["has_sme_input"] is False

    def test_refine_count_absent_is_null(self):
        task = _make_task("body")
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["refine_count"] is None

    def test_refine_count_explicit_zero_preserved(self):
        task = _make_task("body", {"refine_count": 0})
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["refine_count"] == 0

    def test_refine_count_positive_preserved(self):
        task = _make_task("body", {"refine_count": 4})
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["refine_count"] == 4

    def test_refine_count_malformed_is_null(self):
        task = _make_task("body", {"refine_count": -3})
        s = extract.extract_strategy("STRAT-001", task, None, "")
        assert s["refine_count"] is None


# ─── strat-task schema: refine_count absent-vs-zero round-trip ──────────────


class TestRefineCountSchema:

    def _base(self):
        return {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-1",
            "priority": "Major",
            "status": "Draft",
        }

    def test_absent_stays_absent(self, tmp_path):
        path = str(tmp_path / "STRAT-001.md")
        write_frontmatter(path, self._base(), "strat-task")
        data, _ = read_frontmatter(path)
        assert "refine_count" not in data

    def test_explicit_zero_round_trips(self, tmp_path):
        path = str(tmp_path / "STRAT-001.md")
        d = self._base()
        d["refine_count"] = 0
        write_frontmatter(path, d, "strat-task")
        data, _ = read_frontmatter(path)
        assert data.get("refine_count") == 0

    def test_positive_round_trips(self, tmp_path):
        path = str(tmp_path / "STRAT-001.md")
        d = self._base()
        d["refine_count"] = 5
        write_frontmatter(path, d, "strat-task")
        data, _ = read_frontmatter(path)
        assert data.get("refine_count") == 5

    def test_int_type_enforced(self):
        d = self._base()
        d["refine_count"] = "not-an-int"
        errors = validate(d, "strat-task")
        assert any("refine_count" in e for e in errors)


# ─── dashboard: refine_iterations fallback selection + provenance ────────────


class TestRefineIterations:

    def test_authoritative_value_used_plain(self):
        # refine_count present -> used verbatim, not approximate.
        value, approx = dashboard.refine_iterations(3, 99)
        assert value == 3
        assert approx is False

    def test_authoritative_zero_beats_fallback(self):
        # explicit 0 is authoritative even when runs suggest iterations.
        value, approx = dashboard.refine_iterations(0, 5)
        assert value == 0
        assert approx is False

    def test_absent_falls_back_to_run_baseline(self):
        # null refine_count -> max(0, runs - 1), marked approximate.
        value, approx = dashboard.refine_iterations(None, 4)
        assert value == 3
        assert approx is True

    def test_malformed_falls_back(self):
        for bad in (-1, True, "2", 2.5):
            value, approx = dashboard.refine_iterations(bad, 3)
            assert value == 2
            assert approx is True

    def test_single_run_fallback_is_zero(self):
        # baseline run is creation, not a refine iteration.
        value, approx = dashboard.refine_iterations(None, 1)
        assert value == 0
        assert approx is True

    def test_missing_run_count_defaults_to_one(self):
        value, approx = dashboard.refine_iterations(None, 0)
        assert value == 0
        assert approx is True


# ─── dashboard: distinct pipeline run counting ──────────────────────────────


class TestCountPipelineRuns:

    def test_counts_distinct_runs_per_strat(self):
        runs = [
            {"strategies": [{"strat_id": "A"}, {"strat_id": "B"}]},
            {"strategies": [{"strat_id": "A"}]},
            {"strategies": [{"strat_id": "A"}, {"strat_id": "B"}]},
        ]
        counts = dashboard.count_pipeline_runs(runs)
        assert counts == {"A": 3, "B": 2}

    def test_duplicate_strat_in_one_run_counts_once(self):
        runs = [{"strategies": [{"strat_id": "A"}, {"strat_id": "A"}]}]
        assert dashboard.count_pipeline_runs(runs) == {"A": 1}

    def test_empty(self):
        assert dashboard.count_pipeline_runs([]) == {}


# ─── dashboard: SME Input KPI counting ──────────────────────────────────────


class TestEmptySmeRubricPass:

    def test_counts_only_rubric_pass_with_empty_sme(self):
        strategies = [
            {"recommendation": "approve", "has_sme_input": False},   # counts
            {"recommendation": "approved", "has_sme_input": False},  # counts
            {"recommendation": "approve", "has_sme_input": True},    # rubric-pass, not empty
            {"recommendation": "revise", "has_sme_input": False},    # not rubric-pass
            {"recommendation": "reject", "has_sme_input": False},    # not rubric-pass
        ]
        empty, total = dashboard.empty_sme_rubric_pass(strategies)
        assert empty == 2
        assert total == 3

    def test_no_rubric_pass(self):
        strategies = [{"recommendation": "revise", "has_sme_input": False}]
        assert dashboard.empty_sme_rubric_pass(strategies) == (0, 0)

    def test_unknown_sme_excluded_from_both(self):
        # has_sme_input None (un-instrumented historical run) is unknown, not
        # empty: excluded from numerator AND denominator.
        strategies = [
            {"recommendation": "approve", "has_sme_input": None},    # unknown -> excluded
            {"recommendation": "approve", "has_sme_input": False},   # counts
            {"recommendation": "approve", "has_sme_input": True},    # known, not empty
        ]
        empty, total = dashboard.empty_sme_rubric_pass(strategies)
        assert empty == 1
        assert total == 2

    def test_missing_key_treated_as_unknown(self):
        # A dict with no has_sme_input key at all is also unknown.
        strategies = [{"recommendation": "approve"}]
        assert dashboard.empty_sme_rubric_pass(strategies) == (0, 0)


# ─── dashboard: per-run trend aggregation ────────────────────────────────────


class TestComputeMetricTrends:

    def _run(self, strategies):
        return {"strategies": strategies}

    def test_sme_rate_and_count_per_run(self):
        run = self._run([
            {"recommendation": "approve", "has_sme_input": False},   # empty
            {"recommendation": "approved", "has_sme_input": True},   # known, not empty
            {"recommendation": "revise", "has_sme_input": False},    # not rubric-pass
        ])
        dashboard.compute_metric_trends([run])
        assert run["sme_empty_count"] == 1
        assert run["sme_rubric_known"] == 2
        assert run["sme_empty_rate"] == 50

    def test_sme_rate_none_when_no_known_rubric_pass(self):
        # No rubric-pass STRAT with known SME status -> gap, not a misleading 0%.
        run = self._run([
            {"recommendation": "revise", "has_sme_input": False},
            {"recommendation": "approve", "has_sme_input": None},    # unknown
        ])
        dashboard.compute_metric_trends([run])
        assert run["sme_rubric_known"] == 0
        assert run["sme_empty_rate"] is None

    def test_avg_refine_authoritative_only(self):
        # Only instrumented refine_count contributes; absent is excluded (no
        # fallback in the trend).
        run = self._run([
            {"refine_count": 2},
            {"refine_count": 0},     # explicit zero counts
            {"refine_count": None},  # un-instrumented -> excluded
        ])
        dashboard.compute_metric_trends([run])
        assert run["avg_refine_iter"] == 1.0   # mean of [2, 0]
        assert run["refine_instrumented_count"] == 2

    def test_avg_refine_none_when_uninstrumented(self):
        run = self._run([{"refine_count": None}, {"recommendation": "approve"}])
        dashboard.compute_metric_trends([run])
        assert run["avg_refine_iter"] is None
        assert run["refine_instrumented_count"] == 0

    def test_avg_refine_excludes_malformed(self):
        # Malformed values normalize to null -> excluded, same as absent.
        run = self._run([
            {"refine_count": 4},
            {"refine_count": -1},     # negative
            {"refine_count": True},   # bool
            {"refine_count": "3"},    # string
        ])
        dashboard.compute_metric_trends([run])
        assert run["avg_refine_iter"] == 4.0
        assert run["refine_instrumented_count"] == 1

    def test_runs_computed_independently(self):
        runs = [
            self._run([{"recommendation": "approve", "has_sme_input": False,
                        "refine_count": 1}]),
            self._run([{"recommendation": "approve", "has_sme_input": True,
                        "refine_count": 3}]),
        ]
        dashboard.compute_metric_trends(runs)
        assert runs[0]["sme_empty_rate"] == 100
        assert runs[0]["avg_refine_iter"] == 1.0
        assert runs[1]["sme_empty_rate"] == 0
        assert runs[1]["avg_refine_iter"] == 3.0

    def test_empty_run_gets_null_fields(self):
        run = self._run([])
        dashboard.compute_metric_trends([run])
        assert run["sme_empty_rate"] is None
        assert run["avg_refine_iter"] is None
        assert run["refine_instrumented_count"] == 0
