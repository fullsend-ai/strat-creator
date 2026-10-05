import csv
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from apply_scores import ensure_review_file, extract_feedback, extract_score_table

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
APPLY_SCORES_SCRIPT = os.path.join(SCRIPTS_DIR, "apply_scores.py")


# ─── extract_score_table ─────────────────────────────────────────────────────


class TestExtractScoreTable:

    def test_finds_table_with_criterion_header(self):
        text = (
            "Some preamble\n"
            "\n"
            "| Criterion | Score | Notes |\n"
            "|-----------|-------|-------|\n"
            "| Feasibility | 2/2 | Good |\n"
            "| Testability | 1/2 | Needs work |\n"
            "\n"
            "More text after\n"
        )
        result = extract_score_table(text)
        assert result is not None
        assert "Criterion" in result
        assert "Feasibility" in result
        assert "Testability" in result

    def test_stops_at_non_table_line(self):
        text = (
            "| Criterion | Score |\n"
            "|-----------|-------|\n"
            "| Scope | 2/2 |\n"
            "Not a table line\n"
            "| Architecture | 1/2 |\n"
        )
        result = extract_score_table(text)
        assert result is not None
        assert "Scope" in result
        assert "Architecture" not in result

    def test_returns_none_if_no_table(self):
        text = "Just some text\nNo tables here\n"
        result = extract_score_table(text)
        assert result is None

    def test_returns_none_for_table_without_criterion(self):
        text = (
            "| Name | Value |\n"
            "|------|-------|\n"
            "| foo  | bar   |\n"
        )
        result = extract_score_table(text)
        assert result is None

    def test_includes_separator_row(self):
        text = (
            "| Criterion | Score |\n"
            "|-----------|-------|\n"
            "| Feasibility | 2/2 |\n"
        )
        result = extract_score_table(text)
        lines = result.split("\n")
        assert len(lines) == 3
        assert "---" in lines[1]


# ─── extract_feedback ─────────────────────────────────────────────────────────


class TestExtractFeedback:

    def test_finds_h2_feedback_section(self):
        text = (
            "## Scores\n"
            "Some scores.\n"
            "\n"
            "## Feedback\n"
            "This is the feedback.\n"
            "Multiple lines.\n"
            "\n"
            "## Next Section\n"
            "Not feedback.\n"
        )
        result = extract_feedback(text)
        assert result is not None
        assert "This is the feedback." in result
        assert "Multiple lines." in result
        assert "Not feedback." not in result

    def test_finds_h3_feedback_section(self):
        text = (
            "### Feedback\n"
            "H3 level feedback.\n"
        )
        result = extract_feedback(text)
        assert result is not None
        assert "H3 level feedback." in result

    def test_stops_at_next_h2(self):
        text = (
            "## Feedback\n"
            "Feedback content.\n"
            "## Another Section\n"
            "Should not appear.\n"
        )
        result = extract_feedback(text)
        assert "Feedback content." in result
        assert "Should not appear." not in result

    def test_does_not_stop_at_h3_within_feedback(self):
        text = (
            "## Feedback\n"
            "Intro.\n"
            "### Subsection\n"
            "Subsection content.\n"
        )
        result = extract_feedback(text)
        assert "Subsection" in result
        assert "Subsection content." in result

    def test_returns_none_if_no_feedback_section(self):
        text = "## Scores\nSome scores.\n## Summary\nDone.\n"
        result = extract_feedback(text)
        assert result is None

    def test_returns_none_if_feedback_section_empty(self):
        text = "## Feedback\n\n## Next\nStuff.\n"
        result = extract_feedback(text)
        assert result is None


# ─── ensure_review_file ──────────────────────────────────────────────────────


class TestEnsureReviewFile:

    def _scores(self, verdict="APPROVE", total=7, feas=2, test=2, scope=2,
                arch=1):
        return {
            "Feasibility": feas,
            "Testability": test,
            "Scope": scope,
            "Architecture": arch,
            "Total": total,
            "Verdict": verdict,
        }

    def test_creates_file_when_missing(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        scores = self._scores()
        ensure_review_file(path, "STRAT-001", scores, None, None)
        assert os.path.exists(path)
        content = open(path).read()
        assert "## Scores" in content
        assert "Feasibility" in content
        assert "7/8" in content

    def test_skips_if_file_exists_with_content(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        with open(path, "w") as f:
            f.write("Existing content.\n")
        scores = self._scores()
        ensure_review_file(path, "STRAT-001", scores, None, None)
        content = open(path).read()
        assert content == "Existing content.\n"

    def test_creates_when_file_empty(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        with open(path, "w") as f:
            f.write("")
        scores = self._scores()
        ensure_review_file(path, "STRAT-001", scores, None, None)
        content = open(path).read()
        assert "## Scores" in content

    def test_uses_score_table_when_provided(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        table = (
            "| Criterion | Score | Notes |\n"
            "|-----------|-------|-------|\n"
            "| Feasibility | 2/2 | Good |\n"
            "| Total | 7/8 | APPROVE |"
        )
        scores = self._scores()
        ensure_review_file(path, "STRAT-001", scores, table, None)
        content = open(path).read()
        assert "| Feasibility | 2/2 | Good |" in content

    def test_appends_total_if_not_in_score_table(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        table = (
            "| Criterion | Score | Notes |\n"
            "|-----------|-------|-------|\n"
            "| Feasibility | 2/2 | |\n"
        )
        scores = self._scores()
        ensure_review_file(path, "STRAT-001", scores, table, None)
        content = open(path).read()
        assert "**Total**" in content
        assert "**7/8**" in content

    def test_includes_feedback_when_provided(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        scores = self._scores()
        feedback = "The strategy covers all key areas."
        ensure_review_file(path, "STRAT-001", scores, None, feedback)
        content = open(path).read()
        assert "## Scorer Feedback" in content
        assert "The strategy covers all key areas." in content

    def test_generates_fallback_table_when_no_score_table(self, tmp_path):
        path = str(tmp_path / "STRAT-001-review.md")
        scores = self._scores(feas=1, test=2, scope=2, arch=2, total=7)
        ensure_review_file(path, "STRAT-001", scores, None, None)
        content = open(path).read()
        assert "| Feasibility | 1/2 |" in content
        assert "| Testability | 2/2 |" in content


# ─── Integration: main() via subprocess ──────────────────────────────────────


def _write_scores_csv(path, rows):
    """Write a scores.csv with the given rows (list of dicts)."""
    fieldnames = ["ID", "Feasibility", "Testability", "Scope", "Architecture",
                  "Total", "Verdict", "Needs_Attention"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _run_apply_scores(tmp_path, scores_csv, review_dir, result_dir=None):
    """Run apply_scores.py as a subprocess."""
    env = os.environ.copy()
    env["PYTHONPATH"] = SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, APPLY_SCORES_SCRIPT,
        str(scores_csv),
        "--review-dir", str(review_dir),
    ]
    if result_dir:
        cmd += ["--result-dir", str(result_dir)]
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )


def _read_review_frontmatter(review_path):
    """Read frontmatter from a review file using frontmatter.py."""
    env = os.environ.copy()
    env["PYTHONPATH"] = SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    fm_script = os.path.join(SCRIPTS_DIR, "frontmatter.py")
    result = subprocess.run(
        [sys.executable, fm_script, "read", str(review_path),
         "--schema-type", "strat-review"],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"frontmatter read failed: {result.stderr}")
    return json.loads(result.stdout)


class TestMainIntegration:

    def test_creates_review_files_with_frontmatter(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-001", "Feasibility": "2", "Testability": "2",
                "Scope": "1", "Architecture": "1", "Total": "6",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0
        review_file = review_dir / "STRAT-001-review.md"
        assert review_file.exists()
        data = _read_review_frontmatter(str(review_file))
        assert data["strat_id"] == "STRAT-001"
        assert data["recommendation"] == "approve"
        assert data["needs_attention"] is False

    def test_approve_gate(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-010", "Feasibility": "2", "Testability": "2",
                "Scope": "1", "Architecture": "1", "Total": "6",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0
        assert "APPROVE" in result.stdout
        data = _read_review_frontmatter(
            str(review_dir / "STRAT-010-review.md"))
        assert data["needs_attention"] is False
        assert data["recommendation"] == "approve"

    def test_revise_gate(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-020", "Feasibility": "1", "Testability": "1",
                "Scope": "1", "Architecture": "1", "Total": "4",
                "Verdict": "REVISE", "Needs_Attention": "true",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0
        assert "REVISE" in result.stdout
        data = _read_review_frontmatter(
            str(review_dir / "STRAT-020-review.md"))
        assert data["needs_attention"] is True
        assert data["recommendation"] == "revise"

    def test_reject_gate(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-030", "Feasibility": "0", "Testability": "0",
                "Scope": "1", "Architecture": "1", "Total": "2",
                "Verdict": "REJECT", "Needs_Attention": "true",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0
        assert "REJECT" in result.stdout
        data = _read_review_frontmatter(
            str(review_dir / "STRAT-030-review.md"))
        assert data["needs_attention"] is True
        assert data["recommendation"] == "reject"

    def test_multiple_strategies(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-100", "Feasibility": "2", "Testability": "2",
                "Scope": "2", "Architecture": "2", "Total": "8",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
            {
                "ID": "STRAT-101", "Feasibility": "0", "Testability": "1",
                "Scope": "1", "Architecture": "0", "Total": "2",
                "Verdict": "REJECT", "Needs_Attention": "true",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0

        data_100 = _read_review_frontmatter(
            str(review_dir / "STRAT-100-review.md"))
        assert data_100["needs_attention"] is False
        assert data_100["scores"]["total"] == 8

        data_101 = _read_review_frontmatter(
            str(review_dir / "STRAT-101-review.md"))
        assert data_101["needs_attention"] is True
        assert data_101["scores"]["total"] == 2

    def test_picks_up_result_md_score_table(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        result_dir = tmp_path / "results"
        result_dir.mkdir()

        result_md = result_dir / "STRAT-050.result.md"
        result_md.write_text(
            "## Scores\n\n"
            "| Criterion | Score | Notes |\n"
            "|-----------|-------|-------|\n"
            "| Feasibility | 2/2 | Solid |\n"
            "| Testability | 2/2 | Good |\n"
            "| Scope | 1/2 | Large |\n"
            "| Architecture | 1/2 | OK |\n"
            "| **Total** | **6/8** | **APPROVE** |\n"
            "\n"
            "## Feedback\n"
            "Overall strong proposal.\n"
        )

        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-050", "Feasibility": "2", "Testability": "2",
                "Scope": "1", "Architecture": "1", "Total": "6",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
        ])
        result = _run_apply_scores(
            tmp_path, csv_path, review_dir, result_dir=str(result_dir))
        assert result.returncode == 0

        content = (review_dir / "STRAT-050-review.md").read_text()
        assert "| Feasibility | 2/2 | Solid |" in content
        assert "## Scorer Feedback" in content
        assert "Overall strong proposal." in content

    def test_missing_csv_fails(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        result = _run_apply_scores(
            tmp_path, tmp_path / "nonexistent.csv", review_dir)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_does_not_overwrite_existing_review_body(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        review_file = review_dir / "STRAT-060-review.md"
        review_file.write_text("Custom review body.\n")

        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-060", "Feasibility": "2", "Testability": "1",
                "Scope": "1", "Architecture": "1", "Total": "5",
                "Verdict": "REVISE", "Needs_Attention": "true",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0
        content = review_file.read_text()
        assert "Custom review body." in content

    def test_scores_stored_in_frontmatter(self, tmp_path):
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "STRAT-070", "Feasibility": "2", "Testability": "1",
                "Scope": "2", "Architecture": "1", "Total": "6",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
        ])
        _run_apply_scores(tmp_path, csv_path, review_dir)
        data = _read_review_frontmatter(
            str(review_dir / "STRAT-070-review.md"))
        assert data["scores"]["feasibility"] == 2
        assert data["scores"]["testability"] == 1
        assert data["scores"]["scope"] == 2
        assert data["scores"]["architecture"] == 1
        assert data["scores"]["total"] == 6

    def test_initiative_strategy_ids_use_their_types_review_schema(self, tmp_path):
        """A RHOAIENG- review file validates against initiative-strategy's review schema (its file name's id)."""
        review_dir = tmp_path / "artifacts" / "strat-reviews"
        review_dir.mkdir(parents=True)
        csv_path = tmp_path / "scores.csv"
        _write_scores_csv(str(csv_path), [
            {
                "ID": "RHOAIENG-80", "Feasibility": "2", "Testability": "2",
                "Scope": "1", "Architecture": "2", "Total": "7",
                "Verdict": "APPROVE", "Needs_Attention": "false",
            },
        ])
        result = _run_apply_scores(tmp_path, csv_path, review_dir)
        assert result.returncode == 0, result.stderr
        data = _read_review_frontmatter(str(review_dir / "RHOAIENG-80-review.md"))
        assert data["strat_id"] == "RHOAIENG-80"
        assert data["scores"]["total"] == 7
