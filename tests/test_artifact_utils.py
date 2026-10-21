import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from artifact_utils import (
    ValidationError,
    _migrate_fields,
    apply_defaults,
    compute_strat_labels,
    label_category,
    read_frontmatter,
    update_frontmatter,
    validate,
    write_frontmatter,
)
from jira_utils import strip_metadata

# ─── strat-task schema validation ─────────────────────────────────────────────


class TestStratTaskSchema:

    def test_valid_strat_id_local(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        assert validate(data, "strat-task") == []

    def test_valid_strat_id_jira(self):
        data = {
            "strat_id": "RHAISTRAT-123",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        assert validate(data, "strat-task") == []

    def test_invalid_strat_id_wrong_prefix(self):
        data = {
            "strat_id": "FOO-1",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("does not match" in e for e in errors)

    def test_invalid_strat_id_missing_digits(self):
        data = {
            "strat_id": "STRAT-",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("does not match" in e for e in errors)

    @pytest.mark.parametrize("priority", [
        "Blocker", "Critical", "Major", "Normal", "Minor", "Undefined",
    ])
    def test_valid_priorities(self, priority):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": priority,
            "status": "Draft",
        }
        assert validate(data, "strat-task") == []

    def test_invalid_priority(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Low",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("not in" in e for e in errors)

    @pytest.mark.parametrize("status", ["Draft", "Ready", "Refined", "Reviewed"])
    def test_valid_statuses(self, status):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": status,
        }
        assert validate(data, "strat-task") == []

    def test_invalid_status(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Submitted",
        }
        errors = validate(data, "strat-task")
        assert any("not in" in e for e in errors)

    def test_missing_required_strat_id(self):
        data = {
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("strat_id" in e for e in errors)

    def test_missing_required_title(self):
        data = {
            "strat_id": "STRAT-001",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("title" in e for e in errors)

    def test_missing_required_source_rfe(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("source_rfe" in e for e in errors)

    def test_missing_required_priority(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("priority" in e for e in errors)

    def test_missing_required_status(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
        }
        errors = validate(data, "strat-task")
        assert any("status" in e for e in errors)

    def test_unknown_field_rejected(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "bogus": "field",
        }
        errors = validate(data, "strat-task")
        assert any("Unknown field" in e for e in errors)

    def test_source_rfe_valid_rfe_prefix(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RFE-042",
            "priority": "Major",
            "status": "Draft",
        }
        assert validate(data, "strat-task") == []

    def test_source_rfe_invalid_prefix(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "JIRA-999",
            "priority": "Major",
            "status": "Draft",
        }
        errors = validate(data, "strat-task")
        assert any("does not match" in e for e in errors)

    def test_jira_key_valid_pattern(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "jira_key": "RHAISTRAT-500",
        }
        assert validate(data, "strat-task") == []

    def test_jira_key_invalid_pattern(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "jira_key": "STRAT-001",
        }
        errors = validate(data, "strat-task")
        assert any("does not match" in e for e in errors)

    @pytest.mark.parametrize("workflow", ["local", "ci"])
    def test_workflow_valid_enum(self, workflow):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "workflow": workflow,
        }
        assert validate(data, "strat-task") == []

    def test_workflow_invalid_enum(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "workflow": "invalid",
        }
        errors = validate(data, "strat-task")
        assert any("not in" in e for e in errors)

    def test_workflow_defaults_to_none(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        apply_defaults(data, "strat-task")
        assert data.get("workflow") is None
        assert validate(data, "strat-task") == []


# ─── strat-review schema validation ──────────────────────────────────────────


class TestStratReviewSchema:

    def _valid_review(self, **overrides):
        data = {
            "strat_id": "STRAT-001",
            "recommendation": "approve",
            "needs_attention": False,
            "scores": {
                "feasibility": 3,
                "testability": 3,
                "scope": 2,
                "architecture": 4,
                "total": 12,
            },
            "reviewers": {
                "feasibility": "approve",
                "testability": "approve",
                "scope": "revise",
                "architecture": "approve",
            },
        }
        data.update(overrides)
        return data

    def test_valid_review(self):
        assert validate(self._valid_review(), "strat-review") == []

    def test_scores_nested_int_validation(self):
        data = self._valid_review()
        data["scores"]["feasibility"] = "high"
        errors = validate(data, "strat-review")
        assert any("feasibility" in e and "expected int" in e for e in errors)

    def test_scores_unknown_nested_field_rejected(self):
        data = self._valid_review()
        data["scores"]["bogus_metric"] = 5
        errors = validate(data, "strat-review")
        assert any("unknown field 'bogus_metric'" in e for e in errors)

    def test_scores_missing_required_field(self):
        data = self._valid_review()
        del data["scores"]["total"]
        errors = validate(data, "strat-review")
        assert any("total" in e for e in errors)

    @pytest.mark.parametrize("verdict", ["approve", "revise", "reject"])
    def test_reviewer_valid_enum(self, verdict):
        data = self._valid_review()
        data["reviewers"]["feasibility"] = verdict
        assert validate(data, "strat-review") == []

    def test_reviewer_invalid_enum(self):
        data = self._valid_review()
        data["reviewers"]["feasibility"] = "pass"
        errors = validate(data, "strat-review")
        assert any("not in" in e for e in errors)

    @pytest.mark.parametrize("rec", ["approve", "revise", "reject"])
    def test_recommendation_valid_enum(self, rec):
        data = self._valid_review(recommendation=rec)
        assert validate(data, "strat-review") == []

    def test_recommendation_invalid_enum(self):
        data = self._valid_review(recommendation="submit")
        errors = validate(data, "strat-review")
        assert any("not in" in e for e in errors)

    def test_bool_wrong_type_int(self):
        data = self._valid_review(needs_attention=1)
        errors = validate(data, "strat-review")
        assert any("expected bool" in e for e in errors)

    def test_reviewers_unknown_field_rejected(self):
        data = self._valid_review()
        data["reviewers"]["performance"] = "approve"
        errors = validate(data, "strat-review")
        assert any("unknown field 'performance'" in e for e in errors)

    def test_strat_id_with_rhaistrat_prefix(self):
        data = self._valid_review(strat_id="RHAISTRAT-400")
        assert validate(data, "strat-review") == []

    @pytest.mark.parametrize(
        "status", ["clear", "contradictions-found", "insufficient-context"])
    def test_consistency_status_valid(self, status):
        data = self._valid_review(consistency_status=status,
                                  consistency_severities=["high"])
        assert validate(data, "strat-review") == []

    def test_consistency_status_invalid(self):
        data = self._valid_review(consistency_status="unknown")
        errors = validate(data, "strat-review")
        assert any("consistency_status" in e and "not in" in e
                   for e in errors)

    def test_consistency_severities_must_be_list(self):
        data = self._valid_review(consistency_severities="high")
        errors = validate(data, "strat-review")
        assert any("consistency_severities" in e and "expected list" in e
                   for e in errors)

    def test_consistency_severity_enum(self):
        data = self._valid_review(consistency_severities=["urgent"])
        errors = validate(data, "strat-review")
        assert any("consistency_severities.0" in e and "not in" in e
                   for e in errors)


# ─── Frontmatter R/W ─────────────────────────────────────────────────────────


class TestFrontmatterReadWrite:

    def _strat_task_data(self):
        return {
            "strat_id": "STRAT-001",
            "title": "Enable GPU sharing",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }

    def _strat_review_data(self):
        return {
            "strat_id": "STRAT-001",
            "recommendation": "approve",
            "needs_attention": False,
            "scores": {
                "feasibility": 3,
                "testability": 3,
                "scope": 2,
                "architecture": 4,
                "total": 12,
            },
            "reviewers": {
                "feasibility": "approve",
                "testability": "approve",
                "scope": "approve",
                "architecture": "approve",
            },
        }

    def test_write_and_read_strat_task(self, tmp_path):
        path = str(tmp_path / "strat-tasks" / "STRAT-001.md")
        data = self._strat_task_data()
        write_frontmatter(path, data, "strat-task")
        read_data, body = read_frontmatter(path)
        assert read_data["strat_id"] == "STRAT-001"
        assert read_data["title"] == "Enable GPU sharing"
        assert read_data["priority"] == "Major"
        assert read_data["status"] == "Draft"

    def test_write_and_read_strat_review(self, tmp_path):
        path = str(tmp_path / "strat-reviews" / "STRAT-001-review.md")
        data = self._strat_review_data()
        write_frontmatter(path, data, "strat-review")
        read_data, body = read_frontmatter(path)
        assert read_data["recommendation"] == "approve"
        assert read_data["scores"]["total"] == 12
        assert read_data["reviewers"]["architecture"] == "approve"

    def test_write_preserves_body(self, tmp_path):
        path = str(tmp_path / "strat-tasks" / "STRAT-001.md")
        data = self._strat_task_data()
        write_frontmatter(path, data, "strat-task")
        with open(path, "a") as f:
            f.write("\n## Strategy\n\nSome content here.\n")

        read_data, body = read_frontmatter(path)
        assert "Some content here." in body

    def test_write_invalid_data_raises(self, tmp_path):
        path = str(tmp_path / "strat-tasks" / "STRAT-001.md")
        data = {"strat_id": "STRAT-001"}  # missing required fields
        with pytest.raises(ValidationError):
            write_frontmatter(path, data, "strat-task")

    def test_update_frontmatter_merges(self, tmp_path):
        path = str(tmp_path / "strat-tasks" / "STRAT-001.md")
        data = self._strat_task_data()
        write_frontmatter(path, data, "strat-task")
        update_frontmatter(path, {"status": "Refined"}, "strat-task")
        read_data, _ = read_frontmatter(path)
        assert read_data["status"] == "Refined"
        assert read_data["title"] == "Enable GPU sharing"

    def test_update_frontmatter_merges_nested_dict(self, tmp_path):
        path = str(tmp_path / "strat-reviews" / "STRAT-001-review.md")
        data = self._strat_review_data()
        write_frontmatter(path, data, "strat-review")
        update_frontmatter(
            path,
            {"scores": {"total": 15}},
            "strat-review",
        )
        read_data, _ = read_frontmatter(path)
        assert read_data["scores"]["total"] == 15
        assert read_data["scores"]["feasibility"] == 3

    def test_update_invalid_raises(self, tmp_path):
        path = str(tmp_path / "strat-tasks" / "STRAT-001.md")
        data = self._strat_task_data()
        write_frontmatter(path, data, "strat-task")
        with pytest.raises(ValidationError):
            update_frontmatter(path, {"status": "Bogus"}, "strat-task")

    def test_read_frontmatter_no_frontmatter(self, tmp_path):
        path = str(tmp_path / "plain.md")
        with open(path, "w") as f:
            f.write("# Just a heading\n\nSome text.\n")
        data, body = read_frontmatter(path)
        assert data == {}
        assert "Just a heading" in body

    def test_write_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "dir" / "STRAT-002.md")
        data = self._strat_task_data()
        data["strat_id"] = "STRAT-002"
        write_frontmatter(path, data, "strat-task")
        assert os.path.exists(path)


# ─── apply_defaults ───────────────────────────────────────────────────────────


class TestApplyDefaults:

    def test_strat_task_default_jira_key(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
        }
        apply_defaults(data, "strat-task")
        assert "jira_key" in data
        assert data["jira_key"] is None

    def test_strat_review_default_needs_attention(self):
        data = {
            "strat_id": "STRAT-001",
            "recommendation": "approve",
            "scores": {
                "feasibility": 3,
                "testability": 3,
                "scope": 2,
                "architecture": 4,
                "total": 12,
            },
            "reviewers": {
                "feasibility": "approve",
                "testability": "approve",
                "scope": "approve",
                "architecture": "approve",
            },
        }
        apply_defaults(data, "strat-review")
        assert data["needs_attention"] is False

    def test_apply_defaults_does_not_overwrite_existing(self):
        data = {
            "strat_id": "STRAT-001",
            "title": "Test",
            "source_rfe": "RHAIRFE-100",
            "priority": "Major",
            "status": "Draft",
            "jira_key": "RHAISTRAT-500",
        }
        apply_defaults(data, "strat-task")
        assert data["jira_key"] == "RHAISTRAT-500"


# ─── Field migration ─────────────────────────────────────────────────────────


class TestFieldMigration:

    def test_revised_renamed_to_auto_revised(self):
        data = {"revised": True}
        _migrate_fields(data)
        assert "revised" not in data
        assert data["auto_revised"] is True

    def test_migration_does_not_overwrite_existing(self):
        data = {"revised": True, "auto_revised": False}
        _migrate_fields(data)
        assert data["auto_revised"] is False
        assert "revised" in data

    def test_read_frontmatter_applies_migration(self, tmp_path):
        path = str(tmp_path / "test.md")
        with open(path, "w") as f:
            f.write("---\nrevised: true\nrfe_id: RFE-001\n---\nBody\n")
        data, _ = read_frontmatter(path)
        assert "revised" not in data
        assert data["auto_revised"] is True


# ─── Label derivation ────────────────────────────────────────────────────────


class TestComputeStratLabels:

    def test_draft_only_auto_created(self):
        labels = compute_strat_labels("Draft", None)
        assert labels == ["strat-creator-auto-created"]

    def test_refined_approve_rubric_pass(self):
        labels = compute_strat_labels("Refined", "approve")
        assert "strat-creator-auto-created" in labels
        assert "strat-creator-auto-refined" in labels
        assert "strat-creator-rubric-pass" in labels
        assert "strat-creator-needs-attention" not in labels

    def test_refined_revise_needs_attention(self):
        labels = compute_strat_labels("Refined", "revise")
        assert "strat-creator-auto-created" in labels
        assert "strat-creator-auto-refined" in labels
        assert "strat-creator-needs-attention" in labels
        assert "strat-creator-rubric-pass" not in labels

    def test_refined_reject_needs_attention(self):
        labels = compute_strat_labels("Refined", "reject")
        assert "strat-creator-needs-attention" in labels

    def test_reviewed_approve(self):
        labels = compute_strat_labels("Reviewed", "approve")
        assert "strat-creator-auto-refined" in labels
        assert "strat-creator-rubric-pass" in labels

    def test_draft_with_approve_no_refined_label(self):
        labels = compute_strat_labels("Draft", "approve")
        assert "strat-creator-auto-refined" not in labels
        assert "strat-creator-rubric-pass" in labels


class TestLabelCategory:

    def test_provenance_labels(self):
        assert label_category("strat-creator-auto-created") == "provenance"
        assert label_category("strat-creator-auto-refined") == "provenance"
        assert label_category("strat-creator-auto-revised") == "provenance"

    def test_gate_label(self):
        assert label_category("strat-creator-rubric-pass") == "gate"
        assert label_category("strat-creator-human-sign-off") == "gate"

    def test_escalation_label(self):
        assert label_category("strat-creator-needs-attention") == "escalation"

    def test_exclusion_label(self):
        assert label_category("strat-creator-ignore") == "exclusion"

    def test_unknown_label(self):
        assert label_category("some-random-label") == "unknown"


# ─── strip_metadata ──────────────────────────────────────────────────────────


class TestStripMetadata:

    def test_strips_frontmatter(self):
        md = "---\nstrat_id: STRAT-001\n---\n## Strategy\n\nContent."
        result = strip_metadata(md)
        assert "strat_id" not in result
        assert "---" not in result
        assert "Content." in result

    def test_strips_strat_title_heading(self):
        md = "# STRAT-001: Enable GPU sharing\n\n## Strategy\n\nContent."
        result = strip_metadata(md)
        assert "# STRAT-001:" not in result
        assert "Content." in result

    def test_strips_rhaistrat_title_heading(self):
        md = "# RHAISTRAT-1234: Big feature\n\nBody text."
        result = strip_metadata(md)
        assert "# RHAISTRAT-1234:" not in result
        assert "Body text." in result

    def test_strips_rfe_title_heading(self):
        md = "# RFE-001: Some RFE\n\nBody."
        result = strip_metadata(md)
        assert "# RFE-001:" not in result

    def test_strips_rhairfe_title_heading(self):
        md = "# RHAIRFE-999: Another RFE\n\nBody."
        result = strip_metadata(md)
        assert "# RHAIRFE-999:" not in result

    def test_strips_html_comments(self):
        md = "Before\n<!-- This is a comment -->\nAfter"
        result = strip_metadata(md)
        assert "<!--" not in result
        assert "comment" not in result
        assert "Before" in result
        assert "After" in result

    def test_strips_multiline_html_comments(self):
        md = "Before\n<!-- This is\na multiline\ncomment -->\nAfter"
        result = strip_metadata(md)
        assert "<!--" not in result
        assert "multiline" not in result

    def test_strips_all_at_once(self):
        md = (
            "---\nstrat_id: STRAT-001\n---\n"
            "# STRAT-001: GPU Sharing\n\n"
            "<!-- hidden -->\n\n"
            "## Strategy\n\nContent."
        )
        result = strip_metadata(md)
        assert "strat_id" not in result
        assert "# STRAT-001:" not in result
        assert "hidden" not in result
        assert "Content." in result

    def test_preserves_non_metadata_content(self):
        md = "## Business Need\n\nWe need GPUs.\n\n## Scope\n\nSmall."
        result = strip_metadata(md)
        assert "We need GPUs." in result
        assert "Small." in result
