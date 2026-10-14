#!/usr/bin/env python3
"""Apply scores from scores.csv to strategy review file frontmatter.

Reads the output of assess-strat's parse_results.py and sets frontmatter
on each review file in artifacts/strat-reviews/. Creates review files if
they don't exist yet.

This is the bridge between the assess-strat scorer (which produces
scores.csv with deterministic verdicts) and strat-creator's review
artifacts (which use frontmatter for pipeline gate decisions).

The gate logic:
    APPROVE → needs_attention=false → auto-approved, done
    REVISE/REJECT → needs_attention=true → human review required

Usage:
    python3 scripts/apply_scores.py /tmp/strat-assess/review/scores.csv
    python3 scripts/apply_scores.py /tmp/strat-assess/review/scores.csv --review-dir artifacts/strat-reviews
    python3 scripts/apply_scores.py /tmp/strat-assess/review/scores.csv --result-dir /tmp/strat-assess/review
"""

import argparse
import csv
import os
import re
import subprocess
import sys

FRONTMATTER_SCRIPT = os.path.join(os.path.dirname(__file__), "frontmatter.py")
REVIEW_DIR_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "artifacts", "strat-reviews")


def extract_score_table(result_text):
    """Extract the score table from a .result.md file for the review body."""
    lines = result_text.split("\n")
    table_lines = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        # Detect score table by looking for the header row
        if not in_table and stripped.startswith("|") and "Criterion" in stripped:
            in_table = True
            table_lines.append(stripped)
            continue
        if in_table:
            if stripped.startswith("|"):
                table_lines.append(stripped)
            else:
                break

    return "\n".join(table_lines) if table_lines else None


def extract_feedback(result_text):
    """Extract the Feedback section from a .result.md file."""
    lines = result_text.split("\n")
    feedback_lines = []
    in_feedback = False

    for line in lines:
        if re.match(r"^###?\s+Feedback", line):
            in_feedback = True
            continue
        if in_feedback:
            # Stop at next top-level heading
            if re.match(r"^##?\s+", line) and not re.match(r"^###", line):
                break
            feedback_lines.append(line)

    text = "\n".join(feedback_lines).strip()
    return text if text else None


def set_frontmatter(review_path, strat_id, verdict, needs_attention, scores):
    """Set frontmatter on a review file using frontmatter.py.

    Sets scores and verdict from the scorer. Specialist verdicts remain pending
    until actual delivered assessments are captured and composed.
    """
    reviewer_default = "pending"

    args = [
        sys.executable, FRONTMATTER_SCRIPT, "set", review_path,
        f"strat_id={strat_id}",
        f"recommendation={verdict.lower()}",
        f"needs_attention={'true' if needs_attention else 'false'}",
        f"scores.feasibility={scores['Feasibility']}",
        f"scores.testability={scores['Testability']}",
        f"scores.scope={scores['Scope']}",
        f"scores.architecture={scores['Architecture']}",
        f"scores.total={scores['Total']}",
        f"reviewers.feasibility={reviewer_default}",
        f"reviewers.testability={reviewer_default}",
        f"reviewers.scope={reviewer_default}",
        f"reviewers.architecture={reviewer_default}",
    ]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR setting frontmatter: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def ensure_review_file(review_path, strat_id, scores, score_table, feedback):
    """Create a review file with the scores table if it doesn't exist yet."""
    if os.path.exists(review_path) and os.path.getsize(review_path) > 0:
        return

    verdict = scores["Verdict"]
    total = scores["Total"]

    body = "## Scores\n\n"
    if score_table:
        body += f"{score_table}\n"
        if "Total" not in score_table:
            body += f"| **Total** | **{total}/8** | **{verdict}** |\n"
    else:
        body += "| Criterion | Score | Notes |\n"
        body += "|-----------|-------|-------|\n"
        body += f"| Feasibility | {scores['Feasibility']}/2 | |\n"
        body += f"| Testability | {scores['Testability']}/2 | |\n"
        body += f"| Scope | {scores['Scope']}/2 | |\n"
        body += f"| Architecture | {scores['Architecture']}/2 | |\n"
        body += f"| **Total** | **{total}/8** | **{verdict}** |\n"

    if feedback:
        body += f"\n## Scorer Feedback\n\n{feedback}\n"

    with open(review_path, "w", encoding="utf-8") as f:
        f.write(body)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("scores_csv", help="Path to scores.csv from parse_results.py")
    parser.add_argument(
        "--review-dir", default=REVIEW_DIR_DEFAULT,
        help="Directory for review files (default: artifacts/strat-reviews)",
    )
    parser.add_argument(
        "--result-dir", default=None,
        help="Directory with .result.md files (for score table extraction)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.scores_csv):
        print(f"ERROR: {args.scores_csv} not found", file=sys.stderr)
        sys.exit(1)

    review_dir = os.path.abspath(args.review_dir)
    os.makedirs(review_dir, exist_ok=True)

    # If result-dir not specified, infer from scores.csv location
    result_dir = args.result_dir or os.path.dirname(os.path.abspath(args.scores_csv))

    # Read scores
    with open(args.scores_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("No rows in scores.csv", file=sys.stderr)
        sys.exit(1)

    applied = 0
    errors = 0

    for row in rows:
        strat_id = row["ID"]
        scores = {
            "Feasibility": int(row["Feasibility"]),
            "Testability": int(row["Testability"]),
            "Scope": int(row["Scope"]),
            "Architecture": int(row["Architecture"]),
            "Total": int(row["Total"]),
            "Verdict": row["Verdict"],
        }
        needs_attention = row["Needs_Attention"].lower() == "true"

        review_filename = f"{strat_id}-review.md"
        review_path = os.path.join(review_dir, review_filename)

        # Try to extract score table from .result.md
        result_path = os.path.join(result_dir, f"{strat_id}.result.md")
        score_table = None
        feedback = None
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as f:
                result_text = f.read()
            score_table = extract_score_table(result_text)
            feedback = extract_feedback(result_text)

        # Create review file if it doesn't exist
        ensure_review_file(review_path, strat_id, scores, score_table, feedback)

        # Set frontmatter
        ok = set_frontmatter(review_path, strat_id, scores["Verdict"], needs_attention, scores)
        if ok:
            applied += 1
            status = "APPROVE" if not needs_attention else scores["Verdict"]
            print(f"  {strat_id}: {scores['Total']}/8 → {status}")
        else:
            errors += 1

    print(f"\nApplied scores to {applied} review files", file=sys.stderr)
    if errors:
        print(f"  Errors: {errors}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
