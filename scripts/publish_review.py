#!/usr/bin/env python3
"""Publish/recover the verified review without regenerating its content."""

import argparse
import json
import re
from pathlib import Path

from artifact_utils import read_frontmatter_validated
from jira_utils import add_attachment, add_comment, add_labels, markdown_to_adf, remove_labels, require_env
from review_composition import regular, verify_review


def publish(root, key, check_only=False, dry_run=False):
    if not re.fullmatch(r"(?:RHAISTRAT|STRAT)-[0-9]+", key):
        raise ValueError("invalid strategy key")
    from strat_freshness import verify_progress
    input_path, progress_path = root / "tmp/strat-input.json", root / "tmp/strat-progress.json"
    if input_path.exists():
        data = json.loads(input_path.read_text())
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
        verify_progress(root, data, progress)
    path = root / "artifacts/strat-reviews" / f"{key}-review.md"
    if not path.exists():
        return None
    provenance = path.with_name(f"{key}-review-provenance.json")
    if not provenance.exists():
        # apply_scores writes a provisional review before any role reports.
        # Absence is recoverable only before a completed/recorded composition.
        if progress_path.exists():
            progress = json.loads(progress_path.read_text())
            if key in progress.get("review", {}):
                raise ValueError("recorded review has no provenance")
        active = root / "tmp/review-composition" / key / "active.json"
        if active.exists():
            identity = json.loads(regular(root, active).read_text())["session_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError("invalid composition session")
            session = json.loads(regular(root, active.parent / identity / "session.json").read_text())
            if session.get("complete"):
                raise ValueError("completed review has no provenance")
        return None
    manifest = verify_review(root, key)
    run_path = root / "tmp/strat-input.json"
    data = json.loads(run_path.read_text()) if run_path.exists() else {}
    if data and manifest.get("workflow_run_id") != data["run_id"]:
        raise ValueError("saved review belongs to another run")
    verdict = {"key": key, **manifest["decision"]}
    if check_only:
        return verdict
    fm, _ = read_frontmatter_validated(path, "strat-review")
    scores = fm["scores"]
    dissent = [role for role, assessment in fm.get("reviewers", {}).items()
               if assessment != verdict["recommendation"]]
    lines = [f"*[Strat Creator]* Strategy Review — {verdict['recommendation'].upper()} "
             f"(Score: {scores['total']}/8)", "", "| Criterion | Score |", "| --- | --- |"]
    lines += [f"| {role.title()} | {scores[role]}/2 |"
              for role in ("feasibility", "testability", "scope", "architecture")]
    lines += ["", "**Specialist dissent or skip:** " + (", ".join(dissent) if dissent else "none"),
              "The numeric scores alone determine the approval gate. See the full attached review "
              "for all findings, source attribution and differing recommendations.", "",
              "**Action:** " + ("Ready for human sign-off; inspect any specialist dissent."
                                if verdict["recommendation"] == "approve" else
                                "Address the review findings before rerunning the pipeline.")]
    comment = "\n".join(lines) + "\n"
    comment_path = path.with_name(f"{key}-review-comment.md")
    if comment_path.exists() and comment_path.read_text() != comment:
        raise ValueError("saved review summary differs; reconcile before publication")
    comment_path.write_text(comment)
    if dry_run or data.get("dry_run") or not key.startswith("RHAISTRAT-") or not all(require_env()):
        return verdict
    server, user, token = require_env()
    add_comment(server, user, token, key, markdown_to_adf(comment))
    add_attachment(server, user, token, key, str(path))
    label = "strat-creator-needs-attention" if verdict["needs_attention"] else "strat-creator-rubric-pass"
    other = "strat-creator-rubric-pass" if verdict["needs_attention"] else "strat-creator-needs-attention"
    remove_labels(server, user, token, key, [other])
    add_labels(server, user, token, key, [label])
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    value = publish(Path.cwd().resolve(), args.key, args.check_only, args.dry_run)
    if value is None:
        return 3  # No composed review: generation is still needed.
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
