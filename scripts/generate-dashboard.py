#!/usr/bin/env python3
"""Generate a multi-run executive dashboard from strategy pipeline data.

Scans timestamped and identified runs in a RHAISTRAT/ data directory,
extracts aggregate stats from each run, and produces a self-contained
HTML dashboard with trend charts (Chart.js) and per-run drill-down.

Usage:
    python3 scripts/generate-dashboard.py \
        --data-dir /path/to/RHAISTRAT \
        --config config/test-rfes.yaml \
        --output /tmp/dashboard/index.html
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Add scripts/ to path for artifact_utils
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
# extract-pipeline-data.py is hyphenated (not importable by name); load it so the
# dashboard shares a single source of truth for the metric parsers/validators
# (has_sme_input, valid_refine_count) rather than re-implementing them here.
import importlib.util as _il

from artifact_utils import compute_strat_labels, read_frontmatter

_epd_path = os.path.join(os.path.dirname(__file__), "extract-pipeline-data.py")
_epd_spec = _il.spec_from_file_location("extract_pipeline_data", _epd_path)
_epd = _il.module_from_spec(_epd_spec)
_epd_spec.loader.exec_module(_epd)
has_sme_input = _epd.has_sme_input
valid_refine_count = _epd.valid_refine_count


# ─── Helper functions (shared with generate-report.py) ────────────────────────

def is_approve(v):
    return v in ("approve", "approved")

def is_revise(v):
    return v in ("revise", "needs revision", "needs_revision")

def is_reject(v):
    return v in ("reject", "rejected", "infeasible")

def is_split(v):
    return v == "split"

def pct(n, total):
    return round(100 * n / total) if total > 0 else 0


# ─── Metric helpers (SME Input + refine loop count) ──────────────────────────
# These mirror the client-side logic in recomputeExec()/renderExecutiveSummary()
# so the contract can be unit-tested in Python. Keep the two in sync. See
# docs/plans/001 and docs/decisions/ADR-0001-refine-loop-count-source.md.

def count_pipeline_runs(runs):
    """Map strat_id -> number of distinct runs that contain that strategy.

    Used as the fallback iteration signal when a strategy has no authoritative
    refine_count.
    """
    counts = {}
    for run in runs:
        for sid in {s["strat_id"] for s in run.get("strategies", [])}:
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def refine_iterations(refine_count, pipeline_run_count):
    """Resolve one strategy's refine-iteration count.

    Returns (value, approximate). An authoritative refine_count (a non-boolean
    int >= 0, including an explicit 0) wins verbatim with approximate=False.
    Otherwise - absent, null, or malformed - fall back to the pipeline-run
    baseline max(0, distinct_runs - 1) and mark it approximate. See ADR-0001.
    """
    v = valid_refine_count(refine_count)
    if v is not None:
        return v, False
    runs = pipeline_run_count if isinstance(pipeline_run_count, int) and pipeline_run_count > 0 else 1
    return max(0, runs - 1), True


def empty_sme_rubric_pass(strategies):
    """Return (empty_sme_count, rubric_pass_known) for the SME Input KPI.

    Rubric-pass is defined solely by is_approve(recommendation) - never the
    strat-creator-rubric-pass label (see docs/plans/001, Task 1.2).

    has_sme_input may be None for runs that predate SME instrumentation; those
    are unknown, not empty, so they are excluded from both the numerator and the
    denominator. The denominator is therefore rubric-pass STRATs with a KNOWN
    SME status, keeping the reported percentage honest.
    """
    rubric_pass = [s for s in strategies if is_approve(s.get("recommendation"))]
    known = [s for s in rubric_pass if s.get("has_sme_input") is not None]
    empty = [s for s in known if s.get("has_sme_input") is False]
    return len(empty), len(known)


def compute_metric_trends(runs):
    """Attach per-run SME and refine-iteration aggregates for the trend charts.

    Both series are self-contained per run (no cross-run state):
      - sme_empty_rate: percent of rubric-pass STRATs with a KNOWN SME status
        that had empty SME input in that run; None when the run has no known
        rubric-pass STRAT (plotted as a gap rather than a misleading 0%).
      - avg_refine_iter: mean instrumented refine_count in that run. This trend
        is authoritative-only: fallback (pipeline-run) estimates are deliberately
        excluded so the line is a clean regression signal for RHAIFIRST-325 and
        not polluted by CI churn. None when no STRAT in the run is instrumented.
        The Executive Summary KPI keeps the blended (authoritative + fallback)
        snapshot; the trend and the summary answer different questions. See
        docs/plans/002.

    Mutates each run dict in place. Call after compute_deltas().
    """
    for run in runs:
        strategies = run.get("strategies", [])
        empty, known = empty_sme_rubric_pass(strategies)
        run["sme_empty_count"] = empty
        run["sme_rubric_known"] = known
        run["sme_empty_rate"] = pct(empty, known) if known else None

        vals = [valid_refine_count(s.get("refine_count")) for s in strategies]
        vals = [v for v in vals if v is not None]
        run["avg_refine_iter"] = round(sum(vals) / len(vals), 1) if vals else None
        run["refine_instrumented_count"] = len(vals)


def health_color(rate):
    if rate >= 70:
        return "#3fb950"
    elif rate >= 40:
        return "#d29922"
    return "#f85149"

def verdict_class(verdict):
    if verdict in ("approve", "approved"):
        return "verdict-approve"
    elif verdict in ("revise", "needs revision", "needs_revision"):
        return "verdict-revise"
    elif verdict in ("reject", "rejected", "infeasible"):
        return "verdict-reject"
    elif verdict == "split":
        return "verdict-split"
    return "verdict-unknown"

def verdict_label(verdict):
    if verdict in ("approve", "approved"):
        return "Approve"
    elif verdict in ("revise", "needs revision", "needs_revision"):
        return "Revise"
    elif verdict in ("reject", "rejected", "infeasible"):
        return "Reject"
    elif verdict in ("split",):
        return "Split"
    return verdict or "—"

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def inline_format(text):
    text = escape_html(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text


def split_strategy_body(body):
    """Split strategy body into RFE content and AI-generated strategy."""
    marker = "## Strategy (AI Generated by Agentic SDLC Pipeline)"
    idx = body.find(marker)
    if idx == -1:
        return body, ""
    return body[:idx].rstrip(), body[idx:].strip()


def extract_size(body):
    """Extract T-shirt size (S/M/L/XL) from the Effort Estimate section."""
    in_section = False
    for line in body.split("\n"):
        if re.match(r'^###?\s+Effort Estimate', line):
            in_section = True
            continue
        if in_section and line.strip():
            m = re.match(r'\*?\*?(XL|L|M|S)\b', line.strip())
            if m:
                return m.group(1)
            return "—"
    return "—"


def md_to_html(md_text):
    lines = md_text.split("\n")
    html_lines = []
    in_list = False
    in_code = False
    in_table = False
    code_block = []
    table_rows = []

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            return
        html_lines.append('<table>')
        for idx, row_cells in enumerate(table_rows):
            tag = "th" if idx == 0 else "td"
            html_lines.append("<tr>")
            for cell in row_cells:
                html_lines.append(f"<{tag}>{inline_format(cell.strip())}</{tag}>")
            html_lines.append("</tr>")
        html_lines.append("</table>")
        table_rows = []
        in_table = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_table:
                flush_table()
            if in_code:
                html_lines.append("<pre><code>" + escape_html("\n".join(code_block)) + "</code></pre>")
                code_block = []
                in_code = False
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                in_code = True
            continue
        if in_code:
            code_block.append(line)
            continue
        stripped = line.strip()
        # Markdown table rows
        if stripped.startswith("|") and stripped.endswith("|"):
            if re.match(r'^\|[\s\-:|]+\|$', stripped):
                continue
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            in_table = True
            cells = [c for c in stripped.split("|")[1:-1]]
            table_rows.append(cells)
            continue
        if in_table:
            flush_table()
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue
        heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            level = len(heading_match.group(1))
            text = inline_format(heading_match.group(2))
            html_lines.append(f"<h{level}>{text}</h{level}>")
            continue
        list_match = re.match(r'^[\s]*[-*]\s+(.*)', line)
        if list_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{inline_format(list_match.group(1))}</li>")
            continue
        num_match = re.match(r'^[\s]*\d+\.\s+(.*)', line)
        if num_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{inline_format(num_match.group(1))}</li>")
            continue
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        html_lines.append(f"<p>{inline_format(stripped)}</p>")

    if in_table:
        flush_table()
    if in_list:
        html_lines.append("</ul>")
    if in_code:
        html_lines.append("<pre><code>" + escape_html("\n".join(code_block)) + "</code></pre>")
    return "\n".join(html_lines)


# ─── Data extraction ──────────────────────────────────────────────────────────

def load_yaml_config(path):
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f)
    result = {}
    for rfe in data.get("test_rfes", []):
        result[rfe["id"]] = rfe
    return result


def load_run_artifacts(run_dir):
    """Load tasks, reviews, and review comments from a single run directory."""
    tasks = {}
    reviews = {}
    review_comments = {}

    for pattern in ["STRAT-*.md", "RHAISTRAT-*.md"]:
        for path in sorted(glob.glob(os.path.join(run_dir, "strat-tasks", pattern))):
            try:
                meta, body = read_frontmatter(path)
                strat_id = meta.get("strat_id", Path(path).stem)
                tasks[strat_id] = {"meta": meta, "body": body}
            except Exception as e:
                print(f"  Warning: {path}: {e}", file=sys.stderr)

    for pattern in ["STRAT-*-review.md", "RHAISTRAT-*-review.md"]:
        for path in sorted(glob.glob(os.path.join(run_dir, "strat-reviews", pattern))):
            if path.endswith("-review-comment.md"):
                continue
            try:
                meta, body = read_frontmatter(path)
                strat_id = meta.get("strat_id", Path(path).stem.replace("-review", ""))
                reviews[strat_id] = {"meta": meta, "body": body}
            except Exception as e:
                print(f"  Warning: {path}: {e}", file=sys.stderr)

    for pattern in ["STRAT-*-review-comment.md", "RHAISTRAT-*-review-comment.md"]:
        for path in sorted(glob.glob(os.path.join(run_dir, "strat-reviews", pattern))):
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
                strat_id = Path(path).stem.replace("-review-comment", "")
                review_comments[strat_id] = body
            except Exception as e:
                print(f"  Warning: {path}: {e}", file=sys.stderr)

    # Load skipped RFEs
    skipped = []
    skipped_path = os.path.join(run_dir, "strat-skipped.md")
    if os.path.exists(skipped_path):
        with open(skipped_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("|") and not line.startswith("| RFE") and not line.startswith("|--"):
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cols) >= 4:
                        skipped.append({
                            "rfe_key": cols[0],
                            "title": cols[1],
                            "labels": cols[2],
                            "missing": cols[3],
                        })

    return tasks, reviews, review_comments, skipped


def extract_run_stats(run_dir, config):
    """Extract aggregate stats and per-strategy detail from a run."""
    tasks, reviews, review_comments, skipped = load_run_artifacts(run_dir)
    if not tasks:
        return None

    strategies = []
    for strat_id in sorted(tasks.keys()):
        task = tasks[strat_id]
        review = reviews.get(strat_id, {})
        meta = task["meta"]
        rev_meta = review.get("meta", {})
        reviewers = rev_meta.get("reviewers", {})
        source_rfe = meta.get("source_rfe", "")
        cfg = config.get(source_rfe, {})

        scores = rev_meta.get("scores", {})
        strategies.append({
            "strat_id": strat_id,
            "title": meta.get("title", ""),
            "source_rfe": source_rfe,
            "priority": meta.get("priority", "—"),
            "size": cfg.get("size") or extract_size(task.get("body", "")),
            "baseline": cfg.get("baseline", False),
            "cross_component": False,
            "has_sme_input": has_sme_input(task.get("body", "")),
            "refine_count": valid_refine_count(meta.get("refine_count")),
            "recommendation": rev_meta.get("recommendation", "—"),
            "needs_attention": rev_meta.get("needs_attention", False),
            "feasibility": reviewers.get("feasibility", "—"),
            "testability": reviewers.get("testability", "—"),
            "scope": reviewers.get("scope", "—"),
            "architecture": reviewers.get("architecture", "—"),
            "scores": {
                "feasibility": scores.get("feasibility"),
                "testability": scores.get("testability"),
                "scope": scores.get("scope"),
                "architecture": scores.get("architecture"),
                "total": scores.get("total"),
            } if scores else None,
            "rfe_html": md_to_html(split_strategy_body(task.get("body", ""))[0]),
            "strategy_html": md_to_html(split_strategy_body(task.get("body", ""))[1]),
            "comment_html": md_to_html(review_comments.get(strat_id, "")),
            "review_html": md_to_html(review.get("body", "")),
            "labels": compute_strat_labels(
                meta.get("status", ""),
                rev_meta.get("recommendation", ""),
                reviewers,
            ),
        })

    reviewed = [s for s in strategies if s["recommendation"] not in ("—", "")]
    total = len(strategies)
    total_reviewed = len(reviewed)

    # Skip runs where review stage didn't complete (no reviewed strategies)
    if total_reviewed == 0:
        return None

    approved = sum(1 for s in reviewed if is_approve(s["recommendation"]))
    revise = sum(1 for s in reviewed if is_revise(s["recommendation"]))

    dimensions = ["feasibility", "testability", "scope", "architecture"]
    dim_stats = {}
    for dim in dimensions:
        scored_vals = [s["scores"][dim] for s in strategies
                       if s.get("scores") and s["scores"].get(dim) is not None]
        dim_total = len(scored_vals)
        dim_pass = sum(1 for v in scored_vals if v == 2)
        dim_partial = sum(1 for v in scored_vals if v == 1)
        dim_fail = sum(1 for v in scored_vals if v == 0)
        dim_sum = sum(scored_vals)
        dim_max = dim_total * 2
        dim_stats[dim] = {
            "total": dim_total,
            "pass": dim_pass,
            "partial": dim_partial,
            "fail": dim_fail,
            "rate": pct(dim_sum, dim_max),
        }

    weakest_dim = min(dimensions, key=lambda d: dim_stats[d]["rate"])
    strongest_dim = max(dimensions, key=lambda d: dim_stats[d]["rate"])

    # First-pass quality score: % of all dimension scores that are 2/2
    total_checks = sum(dim_stats[d]["total"] for d in dimensions)
    total_passes = sum(dim_stats[d]["pass"] for d in dimensions)
    quality_score = pct(total_passes, total_checks)

    # Numeric score aggregates (when scores are available)
    scored = [s for s in strategies if s.get("scores") and s["scores"].get("total") is not None]
    has_scores = len(scored) > 0
    avg_total_score = round(sum(s["scores"]["total"] for s in scored) / len(scored), 1) if scored else None
    dim_avg_scores = {}
    for dim in dimensions:
        vals = [s["scores"][dim] for s in scored if s["scores"].get(dim) is not None]
        dim_avg_scores[dim] = round(sum(vals) / len(vals), 2) if vals else None

    # Verdict distribution (4-way)
    split_count = sum(1 for s in reviewed if s["recommendation"] == "split")
    reject_count = sum(1 for s in reviewed if is_reject(s["recommendation"]))
    needs_attention = sum(1 for s in reviewed if s.get("needs_attention", False))

    return {
        "total": total,
        "reviewed": total_reviewed,
        "approved": approved,
        "revise": revise,
        "split": split_count,
        "reject": reject_count,
        "needs_attention": needs_attention,
        "approval_rate": pct(approved, total_reviewed),
        "revision_rate": pct(revise, total_reviewed),
        "quality_score": quality_score,
        "has_scores": has_scores,
        "avg_total_score": avg_total_score,
        "dim_avg_scores": dim_avg_scores,
        "dimensions": dim_stats,
        "weakest_dim": weakest_dim,
        "weakest_rate": dim_stats[weakest_dim]["rate"],
        "strongest_dim": strongest_dim,
        "strongest_rate": dim_stats[strongest_dim]["rate"],
        "strategies": strategies,
        "skipped": skipped,
    }


def load_skipped_file(path):
    """Parse a strat-skipped.md file into a list of dicts."""
    skipped = []
    if not os.path.exists(path):
        return skipped
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("|") and not line.startswith("| RFE") and not line.startswith("|--"):
                cols = [c.strip() for c in line.split("|")[1:-1]]
                if len(cols) >= 4:
                    skipped.append({
                        "rfe_key": cols[0],
                        "title": cols[1],
                        "labels": cols[2],
                        "missing": cols[3],
                    })
    return skipped


def load_run_from_json(run_dir, config):
    """Load run data from pipeline-data.json, converting to dashboard format."""
    json_path = os.path.join(run_dir, "pipeline-data.json")
    if not os.path.exists(json_path):
        return None

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    strategies = []
    for s in data.get("strategies", []):
        source_rfe = s.get("source_rfe", "")
        cfg = config.get(source_rfe, {})
        body = s.get("body", {})
        reviewers = s.get("reviewers") or {}

        strategies.append({
            "strat_id": s.get("strat_id", ""),
            "title": s.get("title", ""),
            "source_rfe": source_rfe,
            "priority": s.get("priority", "—"),
            "size": cfg.get("size") or s.get("size") or "—",
            "baseline": cfg.get("baseline", False),
            "cross_component": False,
            # None (absent) means the run predates SME instrumentation - keep it
            # distinct from an explicit False so it can be excluded from the KPI
            # rather than miscounted as empty. See CodeRabbit PR #42.
            "has_sme_input": s.get("has_sme_input"),
            "refine_count": valid_refine_count(s.get("refine_count")),
            "recommendation": s.get("recommendation", "—"),
            "needs_attention": s.get("needs_attention", False),
            "feasibility": reviewers.get("feasibility", "—"),
            "testability": reviewers.get("testability", "—"),
            "scope": reviewers.get("scope", "—"),
            "architecture": reviewers.get("architecture", "—"),
            "scores": s.get("scores"),
            "rfe_html": md_to_html(body.get("rfe", "")) if body else "",
            "strategy_html": md_to_html(body.get("strategy", "")) if body else "",
            "review_html": md_to_html(body.get("review", "")) if body else "",
            "comment_html": md_to_html(body.get("review_comment", "")) if body else "",
            "labels": s.get("labels", []),
        })

    stats = data.get("stats", {})
    return {
        "total": stats.get("total", len(strategies)),
        "reviewed": stats.get("reviewed", 0),
        "approved": stats.get("approved", 0),
        "revise": stats.get("revise", 0),
        "split": stats.get("split", 0),
        "reject": stats.get("reject", 0),
        "needs_attention": stats.get("needs_attention", 0),
        "approval_rate": stats.get("approval_rate", 0),
        "revision_rate": pct(stats.get("revise", 0), stats.get("reviewed", 1)),
        "quality_score": 0,
        "has_scores": any(s.get("scores") for s in strategies),
        "avg_total_score": stats.get("avg_total_score"),
        "dim_avg_scores": {},
        "dimensions": stats.get("dimensions", {}),
        "weakest_dim": min(stats.get("dimensions", {"feasibility": {"rate": 0}}),
                          key=lambda d: stats.get("dimensions", {}).get(d, {}).get("rate", 0))
                      if stats.get("dimensions") else "—",
        "weakest_rate": min((stats.get("dimensions", {}).get(d, {}).get("rate", 0)
                            for d in stats.get("dimensions", {"feasibility": {"rate": 0}})),
                           default=0)
                      if stats.get("dimensions") else 0,
        "strongest_dim": max(stats.get("dimensions", {"feasibility": {"rate": 0}}),
                            key=lambda d: stats.get("dimensions", {}).get(d, {}).get("rate", 0))
                        if stats.get("dimensions") else "—",
        "strongest_rate": max((stats.get("dimensions", {}).get(d, {}).get("rate", 0)
                              for d in stats.get("dimensions", {"feasibility": {"rate": 0}})),
                             default=0)
                        if stats.get("dimensions") else 0,
        "strategies": strategies,
        "skipped": data.get("skipped", []),
        "dry_run": data.get("dry_run", False),
        "cost": data.get("cost"),
    }


def load_cost_backfill(data_dir):
    """Load historical cost data keyed by run_id."""
    path = os.path.join(data_dir, "cost-backfill.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def scan_all_runs(data_dir, config, max_runs=None):
    """Discover published runs using the same contract as the summary extractor."""
    runs = []
    current_target = None

    current_link = os.path.join(data_dir, "current")
    if os.path.islink(current_link):
        current_target = os.readlink(current_link)

    cost_backfill = load_cost_backfill(data_dir)

    for entry, entry_path, ts in _epd.discover_runs(data_dir):
        print(f"  Scanning run {entry}...")
        stats = load_run_from_json(entry_path, config)
        if stats is None:
            stats = extract_run_stats(entry_path, config)
        if stats is None:
            print("    Skipped (no artifacts)")
            continue

        stats["run_id"] = entry
        stats["timestamp"] = ts.isoformat()
        stats["label"] = ts.strftime("%b %d, %Y %H:%M")
        stats["is_current"] = (entry == current_target)
        # Skip runs explicitly marked as dry
        if stats.get("dry_run") is True:
            print("    Skipped (dry run)")
            continue

        # Attach cost data from pipeline-data.json or backfill
        if "cost" not in stats:
            cost = cost_backfill.get(entry)
            if cost:
                stats["cost"] = cost

        runs.append(stats)

    # Sort chronologically (oldest first) and cap if requested
    runs.sort(key=lambda r: (r["timestamp"], r["run_id"]))
    if max_runs and len(runs) > max_runs:
        runs = runs[-max_runs:]

    return runs


def compute_deltas(runs):
    """Add delta fields comparing each run to its predecessor."""
    cumulative = 0
    cumulative_cost = 0.0
    for i, run in enumerate(runs):
        cumulative += run["reviewed"]
        run["cumulative_reviewed"] = cumulative
        cost = run.get("cost") or {}
        cumulative_cost += cost.get("total_usd", 0)
        run["cumulative_cost"] = round(cumulative_cost, 2)
        if i == 0:
            run["delta_approval"] = None
            run["delta_revision"] = None
        else:
            prev = runs[i - 1]
            run["delta_approval"] = run["approval_rate"] - prev["approval_rate"]
            run["delta_revision"] = run["revision_rate"] - prev["revision_rate"]


def compute_executive_summary(runs):
    """Dedup strategies across all runs, keeping the most recent version of each.

    For each unique strategy ID, picks the latest run that contains it.
    Returns aggregate KPIs and the deduped strategy list.
    """
    # Runs are sorted oldest-first; iterate newest-first for dedup
    seen = {}
    for run in reversed(runs):
        for s in run["strategies"]:
            strat_id = s["strat_id"]
            if strat_id not in seen:
                seen[strat_id] = {
                    **s,
                    "run_id": run["run_id"],
                    "run_label": run["label"],
                }

    strategies = sorted(seen.values(), key=lambda s: s["strat_id"])

    total = len(strategies)
    reviewed = [s for s in strategies if s["recommendation"] not in ("—", "")]
    total_reviewed = len(reviewed)
    approved = sum(1 for s in reviewed if is_approve(s["recommendation"]))
    revise = sum(1 for s in reviewed if is_revise(s["recommendation"]))
    reject = sum(1 for s in reviewed if is_reject(s["recommendation"]))
    split_count = sum(1 for s in reviewed if is_split(s["recommendation"]))
    needs_attention = sum(1 for s in reviewed if s.get("needs_attention", False))

    dimensions = ["feasibility", "testability", "scope", "architecture"]
    dim_stats = {}
    for dim in dimensions:
        scored_vals = [s["scores"][dim] for s in strategies
                       if s.get("scores") and s["scores"].get(dim) is not None]
        dim_total = len(scored_vals)
        dim_pass = sum(1 for v in scored_vals if v == 2)
        dim_partial = sum(1 for v in scored_vals if v == 1)
        dim_fail = sum(1 for v in scored_vals if v == 0)
        dim_sum = sum(scored_vals)
        dim_max = dim_total * 2
        dim_stats[dim] = {
            "total": dim_total,
            "pass": dim_pass,
            "partial": dim_partial,
            "fail": dim_fail,
            "rate": pct(dim_sum, dim_max),
        }

    scored = [s for s in strategies
              if s.get("scores") and s["scores"].get("total") is not None]
    avg_total_score = (round(sum(s["scores"]["total"] for s in scored) / len(scored), 1)
                       if scored else None)

    weakest_dim = min(dimensions, key=lambda d: dim_stats[d]["rate"]) if strategies else "—"
    strongest_dim = max(dimensions, key=lambda d: dim_stats[d]["rate"]) if strategies else "—"

    # Aggregate skipped RFEs — dedup by rfe_key, keeping most recent
    skipped_seen = {}
    for run in reversed(runs):
        for s in run.get("skipped", []):
            rfe_key = s.get("rfe_key", "")
            if rfe_key and rfe_key not in skipped_seen:
                skipped_seen[rfe_key] = s
    skipped = sorted(skipped_seen.values(), key=lambda s: s.get("rfe_key", ""))

    # Count unique source RFEs (strategies + skipped, no double-counting)
    all_rfe_keys = set()
    for s in strategies:
        rfe = s.get("source_rfe", "")
        if rfe:
            all_rfe_keys.add(rfe)
    for s in skipped:
        rfe = s.get("rfe_key", "")
        if rfe:
            all_rfe_keys.add(rfe)
    total_rfes = len(all_rfe_keys)

    return {
        "total": total,
        "total_rfes": total_rfes,
        "reviewed": total_reviewed,
        "approved": approved,
        "revise": revise,
        "reject": reject,
        "split": split_count,
        "needs_attention": needs_attention,
        "approval_rate": pct(approved, total_reviewed),
        "revision_rate": pct(revise, total_reviewed),
        "reject_rate": pct(reject, total_reviewed),
        "avg_total_score": avg_total_score,
        "dimensions": dim_stats,
        "weakest_dim": weakest_dim,
        "strongest_dim": strongest_dim,
        "strategies": strategies,
        "skipped": skipped,
        "total_runs": len(runs),
    }


# ─── HTML generation ──────────────────────────────────────────────────────────

def _delta_html(current, prev, field, is_pct=True):
    """Render a delta comparison vs previous run. Green if +, red if -."""
    if not current or not prev:
        return '<span style="color:#6e7681">first run</span>' if current else ''
    delta = current[field] - prev[field]
    if delta == 0:
        suffix = '%' if is_pct else ''
        return f'<span style="color:#6e7681">0{suffix} vs prev</span>'
    color = '#3fb950' if delta > 0 else '#f85149'
    sign = '+' if delta > 0 else ''
    suffix = '%' if is_pct else ''
    return f'<span style="color:{color}">{sign}{delta}{suffix} vs prev</span>'


def generate_dashboard(runs, exec_summary, output_path, jira_counts=None,
                       processing_issues=None):
    """Generate the full dashboard HTML."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Prepare JSON data (strip strategy HTML bodies for overview; keep for detail)
    runs_json = json.dumps(runs, indent=None)
    jira_counts_json = json.dumps(jira_counts) if jira_counts else "null"
    processing_json = json.dumps(processing_issues) if processing_issues else "[]"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgenticCI — Strat Refinement & Review Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }}

.header {{ margin-bottom: 24px; }}
.header h1 {{ font-size: 28px; color: #f0f6fc; margin-bottom: 4px; }}
.header .subtitle {{ color: #8b949e; font-size: 13px; }}
.header-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
.header-links {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.header-link {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; color: #58a6ff; font-size: 13px; font-weight: 600; padding: 8px 14px; text-decoration: none; white-space: nowrap; }}
.header-link:hover {{ background: #1f2630; border-color: #58a6ff; }}

/* Report cards */
.report-cards {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 24px; }}
.report-card {{ display: flex; flex-direction: column; background: #1c1f26; border: 1px solid #30363d; border-left: 4px solid #58a6ff; border-radius: 8px; padding: 20px 24px; line-height: 1.7; text-decoration: none; }}
.report-card:hover {{ background: #21252e; border-color: #58a6ff; }}
.report-card-title {{ font-size: 15px; font-weight: 600; color: #58a6ff; margin-bottom: 8px; }}
.report-card-body {{ font-size: 13px; color: #8b949e; flex: 1; }}
.report-card-body strong {{ color: #c9d1d9; }}
.report-card-cta {{ font-size: 13px; font-weight: 600; color: #58a6ff; margin-top: 10px; }}

/* Nav */
.nav-tabs {{ display: flex; gap: 0; margin-bottom: 24px; border-bottom: 2px solid #21262d; }}
.nav-tab {{ padding: 12px 24px; cursor: pointer; color: #8b949e; font-size: 15px; font-weight: 600; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; }}
.nav-tab:hover {{ color: #c9d1d9; background: #161b22; }}
.nav-tab.active {{ color: #f0f6fc; border-bottom-color: #f78166; }}
.nav-page {{ display: none; }}
.nav-page.active {{ display: block; }}

/* Hero */
.hero {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 32px; margin-bottom: 24px; text-align: center; }}
.hero-statement {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
.hero-delta {{ font-size: 16px; margin-bottom: 4px; }}
.hero-sub {{ color: #8b949e; font-size: 13px; }}

/* KPI cards */
.kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
.kpi {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px 24px; text-align: center; }}
.kpi-value {{ font-size: 36px; font-weight: 700; line-height: 1.1; }}
.kpi-label {{ font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; }}
.kpi-detail {{ font-size: 12px; color: #6e7681; margin-top: 4px; }}

/* Charts */
.charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }}
.chart-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }}
.chart-card h3 {{ color: #f0f6fc; font-size: 14px; margin-bottom: 12px; }}
.chart-card.full-width {{ grid-column: 1 / -1; }}

/* Run list */
.run-list {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px; }}
.run-list h3 {{ color: #f0f6fc; font-size: 16px; margin-bottom: 16px; }}
.run-item {{ display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-radius: 8px; cursor: pointer; border-bottom: 1px solid #21262d; transition: background 0.15s; }}
.run-item:hover {{ background: #1c2128; }}
.run-item:last-child {{ border-bottom: none; }}
.run-item .run-date {{ font-size: 14px; color: #c9d1d9; font-weight: 500; flex: 1; }}
.run-item .run-badge {{ font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 12px; }}
.run-item .run-count {{ font-size: 12px; color: #8b949e; }}
.run-item .run-current {{ font-size: 10px; color: #58a6ff; text-transform: uppercase; font-weight: 600; }}

/* Detail page */
.run-selector {{ margin-bottom: 24px; }}
.run-selector select {{ background: #161b22; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 16px; border-radius: 8px; font-size: 14px; cursor: pointer; }}
.run-selector select:focus {{ outline: none; border-color: #58a6ff; }}

/* Dimension bars */
.dim-section {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px; margin-bottom: 24px; }}
.dim-section h3 {{ color: #f0f6fc; font-size: 16px; margin-bottom: 16px; }}
.dim-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
.dim-label {{ width: 110px; font-size: 13px; color: #8b949e; text-align: right; flex-shrink: 0; }}
.dim-bar-container {{ flex: 1; }}
.dim-bar-track {{ display: flex; height: 24px; border-radius: 6px; overflow: hidden; background: #21262d; }}
.dim-bar-seg {{ transition: width 0.3s ease; }}
.dim-rate {{ width: 48px; font-size: 14px; font-weight: 700; text-align: right; flex-shrink: 0; }}

/* Verdict grid */
.grid-section {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px; margin-bottom: 24px; }}
.grid-section h3 {{ color: #f0f6fc; font-size: 16px; margin-bottom: 8px; }}
.grid-header {{ display: flex; align-items: center; gap: 4px; padding: 0 0 8px; border-bottom: 1px solid #21262d; margin-bottom: 8px; }}
.grid-header-id {{ width: 90px; font-size: 11px; color: #6e7681; text-transform: uppercase; }}
.grid-header-dim {{ width: 40px; text-align: center; font-size: 10px; color: #6e7681; text-transform: uppercase; }}
.grid-header-verdict {{ width: 72px; text-align: right; font-size: 11px; color: #6e7681; text-transform: uppercase; }}
.grid-row {{ display: flex; align-items: center; gap: 4px; padding: 4px 0; }}
.grid-id {{ width: 90px; font-size: 13px; color: #c9d1d9; font-weight: 500; }}
.grid-cell {{ width: 40px; height: 24px; border-radius: 4px; border: 2px solid; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; }}
.grid-verdict {{ width: 72px; text-align: right; font-size: 12px; font-weight: 600; }}

/* Two column */
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }}

/* Table */
table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }}
thead {{ background: #21262d; }}
th {{ text-align: left; padding: 12px 16px; font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; border-bottom: 1px solid #30363d; }}
td {{ padding: 12px 16px; border-bottom: 1px solid #21262d; font-size: 14px; }}
tr:hover {{ background: #1c2128; }}
tr.clickable {{ cursor: pointer; }}

/* Verdicts */
.verdict-approve {{ color: #3fb950; font-weight: 600; }}
.verdict-revise {{ color: #d29922; font-weight: 600; }}
.verdict-reject {{ color: #f85149; font-weight: 600; }}
.verdict-unknown {{ color: #8b949e; }}

/* Badges */
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
.badge-baseline {{ background: #1f3a5f; color: #58a6ff; }}
.badge-cross {{ background: #3d2e00; color: #d29922; }}
.badge-size {{ background: #21262d; color: #8b949e; border: 1px solid #30363d; }}

/* Detail panels */
.detail-panel {{ display: none; background: #0d1117; border: 1px solid #30363d; border-radius: 8px; margin: 8px 16px 16px; padding: 24px; }}
.detail-panel.open {{ display: block; }}
.detail-panel h2 {{ color: #f0f6fc; font-size: 20px; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #21262d; }}
.detail-panel h3 {{ color: #c9d1d9; font-size: 16px; margin: 16px 0 8px; }}
.detail-panel p {{ line-height: 1.6; margin-bottom: 8px; }}
.detail-panel ul {{ padding-left: 24px; margin-bottom: 8px; }}
.detail-panel li {{ line-height: 1.6; margin-bottom: 4px; }}
.detail-panel code {{ background: #21262d; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
.detail-panel pre {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; overflow-x: auto; margin: 8px 0; }}
.detail-panel pre code {{ background: none; padding: 0; }}
.detail-tabs {{ display: flex; gap: 0; margin-bottom: 16px; border-bottom: 1px solid #30363d; }}
.detail-tab {{ padding: 8px 16px; cursor: pointer; color: #8b949e; font-size: 14px; border-bottom: 2px solid transparent; }}
.detail-tab:hover {{ color: #c9d1d9; }}
.detail-tab.active {{ color: #f0f6fc; border-bottom-color: #f78166; }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.expand-icon {{ color: #8b949e; transition: transform 0.2s; display: inline-block; margin-right: 8px; }}
.expand-icon.open {{ transform: rotate(90deg); }}

.zoom-btn {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; width: 32px; height: 32px; border-radius: 6px; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }}
.zoom-btn:hover {{ background: #30363d; color: #f0f6fc; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #21262d; color: #484f58; font-size: 12px; }}

/* Pipeline labels */
.label-bar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }}
.label-badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
.label-provenance {{ background: #1c2333; color: #8b949e; }}
.label-stage {{ background: #0d419d; color: #58a6ff; }}
.label-gate {{ background: #23302a; color: #3fb950; }}
.label-gate-pending {{ background: #2d2400; color: #d29922; }}
.label-escalation {{ background: #2d1418; color: #f85149; }}
.label-legend {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-top: 24px; }}
.label-legend h3 {{ color: #f0f6fc; font-size: 14px; margin-bottom: 12px; }}
.label-legend table {{ margin-bottom: 0; }}
.label-legend td, .label-legend th {{ font-size: 12px; padding: 6px 12px; }}

@media (max-width: 900px) {{
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .charts-grid {{ grid-template-columns: 1fr; }}
    .two-col {{ grid-template-columns: 1fr; }}
    .report-cards {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
    <div class="header-row">
        <div>
            <h1>AgenticCI — AI First — Strat Refinement & Review Dashboard</h1>
            <div class="subtitle">Generated {timestamp} | {len(runs)} run(s) tracked</div>
        </div>
        <div class="header-links">
            <a class="header-link" href="value-report.html">Strategy Value Report ↗</a>
            <a class="header-link" href="evals-report.html">Agent Eval Report ↗</a>
        </div>
    </div>
</div>

<div class="report-cards">
    <a class="report-card" href="value-report.html">
        <div class="report-card-title">Strategy Value Report — 2026-08-06</div>
        <div class="report-card-body">
            Was first-pass AI strategy generation worth it? Every concept in every draft — <strong>1,812 concepts across 150 strategies</strong> — audited against the final signed text.
            <strong>80% of first-pass strategies held</strong>; a majority of their concepts survived engaged review.
            The correction tax is real but concentrated: <strong>337 false assertions</strong> across 61% of issues, ~19% of review statements.
        </div>
        <div class="report-card-cta">Read the full report →</div>
    </a>
    <a class="report-card" href="evals-report.html">
        <div class="report-card-title">Agent Eval Report — 2026-08-10 · claude-opus-4-6</div>
        <div class="report-card-body">
            The pipeline's own scoring, checked against an <strong>independent judge</strong> over a 24-case eval suite.
            Latest run: <strong>24/24 cases green</strong> (exit 0), all <strong>11 judge thresholds passed</strong>, pipeline total <strong>5.04/8</strong> vs independent judge <strong>5.46/8</strong>.
            Open gap: Feasibility is saturated at 1 (23/24) — refine documents critical-path open questions instead of retiring them.
        </div>
        <div class="report-card-cta">Read the full report →</div>
    </a>
</div>

<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
<div class="nav-tabs" style="margin-bottom:0">
    <div class="nav-tab active" onclick="switchPage('executive')">Executive Summary</div>
    <div class="nav-tab" onclick="switchPage('overview')">Per-Run Trends</div>
    <div class="nav-tab" onclick="switchPage('run-detail')">Run Detail</div>
    <div class="nav-tab" onclick="switchPage('attention')">Needs Attention</div>
    <div class="nav-tab" onclick="switchPage('blocked')">Blocked RFEs</div>
    <div class="nav-tab" onclick="switchPage('processing')">Currently Processing</div>
    <div class="nav-tab" onclick="switchPage('pipeline')">Pipeline</div>
</div>
<select id="time-range" onchange="applyTimeRange()" style="font-size:13px;color:#c9d1d9;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 12px;cursor:pointer">
    <option value="0">All time</option>
    <option value="10" selected>Last 10 days</option>
    <option value="20">Last 20 days</option>
    <option value="30">Last 30 days</option>
</select>
</div>

<!-- ═══ EXECUTIVE SUMMARY PAGE ═══ -->
<div class="nav-page active" id="page-executive">
<div id="exec-content"></div>
</div>

<!-- ═══ OVERVIEW PAGE ═══ -->
<div class="nav-page" id="page-overview">

<div id="overview-kpis"></div>

<div class="charts-grid">
    <div class="chart-card">
        <h3>Approval Rate Over Time</h3>
        <canvas id="chart-approval"></canvas>
    </div>
    <div class="chart-card">
        <h3>Strategies Per Run</h3>
        <canvas id="chart-volume"></canvas>
    </div>
    <div class="chart-card">
        <h3>Review Dimensions Over Time</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Per-dimension approval rate. Shows which dimension is the bottleneck.</p>
        <canvas id="chart-dimensions"></canvas>
    </div>
    <div class="chart-card">
        <h3>Cumulative Strategies</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Total strategies reviewed over time (running sum across pipeline runs).</p>
        <canvas id="chart-cumulative"></canvas>
    </div>
    <div class="chart-card">
        <h3>Average Score Trend</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Mean total score per run (0-8). Threshold for approval: 6/8.</p>
        <canvas id="chart-avg-score"></canvas>
    </div>
    <div class="chart-card">
        <h3>Empty SME Input Rate Over Time</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Percent of rubric-pass STRATs (known SME status) that passed with empty Staff Engineer / SME Input. Runs with no known rubric-pass STRAT are shown as gaps.</p>
        <canvas id="chart-sme-empty"></canvas>
    </div>
    <div class="chart-card">
        <h3>Avg Refine Iterations Over Time</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Mean instrumented refine_count per run (authoritative only; fallback estimates excluded). Regression signal for refine-loop convergence. Runs with no instrumented STRAT are shown as gaps.</p>
        <canvas id="chart-refine-iter"></canvas>
    </div>
    <div class="chart-card">
        <h3>Cost Per Run</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Total USD per run, broken down by phase (create / refine / review).</p>
        <canvas id="chart-cost"></canvas>
    </div>
    <div class="chart-card">
        <h3>Cost Per Strategy</h3>
        <p style="color:#6e7681;font-size:12px;margin-bottom:8px">Average cost per strategy. Lower is better.</p>
        <canvas id="chart-cost-per-strat"></canvas>
    </div>
</div>

<div class="run-list">
    <h3>All Runs</h3>
    <div id="run-list-container"></div>
</div>

</div><!-- end overview -->

<!-- ═══ RUN DETAIL PAGE ═══ -->
<div class="nav-page" id="page-run-detail">
    <div class="run-selector">
        <select id="run-select" onchange="renderRunDetail(this.value)">
        </select>
    </div>
    <div id="run-detail-content"></div>
</div>

<!-- ═══ NEEDS ATTENTION PAGE ═══ -->
<div class="nav-page" id="page-attention">
<div id="attention-content"></div>
</div>

<!-- ═══ BLOCKED RFES PAGE ═══ -->
<div class="nav-page" id="page-blocked">
<div id="blocked-content"></div>
</div>

<!-- ═══ CURRENTLY PROCESSING PAGE ═══ -->
<div class="nav-page" id="page-processing">
<div id="processing-content"></div>
</div>

<!-- ═══ PIPELINE PAGE ═══ -->
<div class="nav-page" id="page-pipeline">
<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;position:relative">
    <h2 style="color:#f0f6fc;font-size:16px;margin-bottom:16px;flex-shrink:0">RHAI Agentic SDLC Pipeline</h2>
    <div style="position:absolute;top:16px;right:16px;display:flex;gap:4px;z-index:10">
        <button class="zoom-btn" onclick="zoomDiagram(1.2)" title="Zoom in">+</button>
        <button class="zoom-btn" onclick="zoomDiagram(0.8)" title="Zoom out">&minus;</button>
        <button class="zoom-btn" onclick="resetDiagram()" title="Reset">&#8634;</button>
    </div>
    <div id="diagram-container" style="overflow:auto;position:relative">
    <div id="diagram-inner" style="transform-origin:0 0;transition:transform 0.1s ease;display:flex;align-items:center;justify-content:center;padding:24px 0;font-size:16px">
    <pre class="mermaid">
graph LR
    subgraph P1["Phase 1: RFE Assessment"]
        A[rfe.create] --> B[rfe.review]
        B --> C[rfe.auto-fix]
        C --> D[rfe.submit]
    end

    D -->|"Auto job\\ntrigger"| E0

    subgraph P2["Phase 2: Strategy Refinement"]

        subgraph SC["strategy-create"]
            E0["strategy-create"] --> GATE{{{{Pipeline label gate\\nstrat-creator-3.5 +\\nrfe-creator-autofix-rubric-pass\\nor tech-reviewed}}}}
            GATE -->|"Fail"| SKIP["Skipped RFEs\\nstrat-skipped.md"]
            GATE -->|"Pass"| E1["Fetch RFE\\nfrom Jira"]
            E1 --> E2["Check existing\\nSTRATs via Cloners"]
            E2 --> EG{{{{"Pipeline label gate\\nSkip if rubric-pass\\nor needs-attention"}}}}
            EG -->|"Fail"| ESKIP["Skipped STRATs"]
            EG -->|"Pass"| E3["Clone or import\\nRHAISTRAT from Jira"]
            E3 --> E4["Save originals\\n& fetch comments"]
            E4 --> E5["Create strategy\\nstubs"]
            E5 --> E6["Add label\\nstrat-creator-auto-created"]
        end

        E6 --> SF

        subgraph SR["strategy-refine"]
            SF["strategy-refine"] --> FG{{{{"Pipeline label gate\\nSkip if rubric-pass\\nor needs-attention"}}}}
            FG -->|"Fail"| FSKIP["Skipped STRATs"]
            FG -->|"Pass"| F0["Fetch arch\\ncontext"]
            F0 --> F1["HOW context\\n&#8226; removed-context (RFE)\\n&#8226; Staff Eng Input\\n&#8226; Arch overlays"]
            F1 --> F2["Technical approach\\n& components"]
            F2 --> F3["Dependencies,\\nNFRs & effort"]
            F3 --> F5["Push strategy\\nto Jira"]
            F5 --> F4["Add label\\nstrat-creator-auto-refined"]
        end

        F4 --> SRV

        subgraph SV["strategy-review"]
            SRV["strategy-review"] --> RG{{{{"Pipeline label gate\\nSkip if rubric-pass\\nor needs-attention"}}}}
            RG -->|"Fail"| RSKIP["Skipped STRATs"]
            RG -->|"Pass"| R1[feasibility]
            RG -->|"Pass"| R2[testability]
            RG -->|"Pass"| R3[scope]
            RG -->|"Pass"| R4[architecture]
            RG -->|"Pass"| R5["future:\\nUX, migration,\\ndeps, ..."]
            R1 & R2 & R3 & R4 & R5 --> SC1["assess-strat\\nscorer agents\\nF/T/S/A 0-2"]
            SC1 --> SCRIPTS["parse_results.py &#8594; apply_scores.py\\n(deterministic verdicts)"]
            SCRIPTS --> CON["Write review\\nscores + prose"]
            CON --> JIRA["Attach review to Jira\\n& post summary\\nas comment"]
            JIRA --> Q{{{{&#8805;6/8\\nno zeros?}}}}
            Q -->|"APPROVE"| LA["Add label\\nstrat-creator-rubric-pass"]
            Q -->|"REVISE / REJECT"| LR["Add label\\nstrat-creator-needs-attention"]
        end

        LA --> PA{{"AI Strategy HOW\\nReady\\n(optional &#128100; review)"}}
        LR --> HR["&#128100; Human Review\\nStaff Eng or Architect"]
        HR -->|"Path A: Update\\narchitecture context"| RL["Remove label\\nneeds-attention"]
        HR -->|"Path B: Edit\\nStaff Engineer / SME Input"| RL
        RL -->|"Re-trigger pipeline\\nvia CI or strategy-submit"| SF
    end

    PA --> FR

    subgraph P3["Phase 3: Feature Dev"]
        FR[feature.ready] --> J[Feature Ready]
        J --> K[Prioritize]
        K --> L[AI-Assisted Dev]
        L --> M[PR Review]
    end

    PA -.->|"Optional: human\\ndecides to improve STRAT"| HR

    style A fill:#2d6a2d,color:#fff
    style B fill:#2d6a2d,color:#fff
    style C fill:#2d6a2d,color:#fff
    style D fill:#2d6a2d,color:#fff
    style GATE fill:#1f3a5f,color:#58a6ff,stroke:#58a6ff
    style SKIP fill:#3d1f00,color:#d29922,stroke:#d29922
    style EG fill:#1f3a5f,color:#58a6ff,stroke:#58a6ff
    style ESKIP fill:#3d1f00,color:#d29922,stroke:#d29922
    style E0 fill:#2d6a2d,color:#fff
    style E6 fill:#6e40c9,color:#fff
    style E1 fill:#c77d1a,color:#fff
    style E2 fill:#c77d1a,color:#fff
    style E3 fill:#c77d1a,color:#fff
    style E4 fill:#c77d1a,color:#fff
    style E5 fill:#c77d1a,color:#fff
    style SF fill:#2d6a2d,color:#fff
    style FSKIP fill:#3d1f00,color:#d29922,stroke:#d29922
    style FG fill:#1f3a5f,color:#58a6ff,stroke:#58a6ff
    style F0 fill:#c77d1a,color:#fff
    style F1 fill:#c77d1a,color:#fff
    style F2 fill:#c77d1a,color:#fff
    style F3 fill:#c77d1a,color:#fff
    style F5 fill:#c77d1a,color:#fff
    style F4 fill:#6e40c9,color:#fff
    style SC1 fill:#c77d1a,color:#fff
    style SCRIPTS fill:#1f6feb,color:#fff
    style R1 fill:#c77d1a,color:#fff
    style R2 fill:#c77d1a,color:#fff
    style R3 fill:#c77d1a,color:#fff
    style R4 fill:#c77d1a,color:#fff
    style R5 fill:#555,color:#8b949e,stroke:#6e7681,stroke-dasharray:5
    style CON fill:#c77d1a,color:#fff
    style JIRA fill:#c77d1a,color:#fff
    style SRV fill:#2d6a2d,color:#fff
    style RG fill:#1f3a5f,color:#58a6ff,stroke:#58a6ff
    style RSKIP fill:#3d1f00,color:#d29922,stroke:#d29922
    style Q fill:#1f3a5f,color:#58a6ff,stroke:#58a6ff
    style LA fill:#6e40c9,color:#fff
    style LR fill:#6e40c9,color:#fff
    style PA fill:#2d6a2d,color:#fff,stroke:#3fb950,stroke-width:3px
    style HR fill:#3d1f00,color:#f0883e,stroke:#f0883e
    style RL fill:#6e40c9,color:#fff
    style FR fill:#555,color:#fff
    style J fill:#555,color:#fff
    style K fill:#555,color:#fff
    style L fill:#555,color:#fff
    style M fill:#555,color:#fff
    </pre>
    </div>
    </div>

    <div class="label-legend">
    <h3>Pipeline Labels (strat-creator-*)</h3>
    <table>
    <thead><tr><th>Label</th><th>Category</th><th>Applied When</th></tr></thead>
    <tbody>
    <tr><td><span class="label-badge label-provenance">strat-creator-auto-created</span></td><td>Provenance</td><td>strategy.create generates the RHAISTRAT ticket</td></tr>
    <tr><td><span class="label-badge label-provenance">strat-creator-auto-refined</span></td><td>Provenance</td><td>strategy.refine enriches with technical approach</td></tr>
    <tr><td><span class="label-badge label-provenance">strat-creator-auto-revised</span></td><td>Provenance</td><td>strategy.revise modifies content after review feedback</td></tr>
    <tr><td><span class="label-badge label-gate">strat-creator-rubric-pass</span></td><td>Gate</td><td>Approved; excluded from re-processing in future runs</td></tr>
    <tr><td><span class="label-badge label-escalation">strat-creator-needs-attention</span></td><td>Escalation</td><td>REVISE / REJECT — human review required</td></tr>
    <tr><td><span class="label-badge label-escalation">strat-creator-ignore</span></td><td>Exclusion</td><td>Permanent exclusion from pipeline (human-set only)</td></tr>
    </tbody>
    </table>
    </div>

</div>
</div><!-- end pipeline -->

<div class="footer">
    strat-creator pipeline | RHAI Agentic SDLC
</div>

<script>
const ALL_RUNS = {runs_json};
const JIRA_COUNTS = {jira_counts_json};
const PROCESSING_ISSUES = {processing_json};
let RUNS = filterByDays(ALL_RUNS, 10);
let EXEC = recomputeExec(RUNS);

function filterByDays(runs, days) {{
    if (!days) return [...runs];
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    return runs.filter(r => new Date(r.timestamp) >= cutoff);
}}

// ─── Dry-run filtering ─────────────────────────────────────────────────────
function recomputeExec(runs) {{
    // Distinct pipeline runs per strat_id - fallback signal for refine iterations.
    // Count across ALL loaded runs, not the date-filtered view, so the count
    // does not shrink when older runs fall outside the selected range (the
    // contract is "distinct runs across all loaded runs"). `runs` still governs
    // which strategies are displayed.
    const countSource = (typeof ALL_RUNS !== 'undefined' && ALL_RUNS) ? ALL_RUNS : runs;
    const runCounts = {{}};
    for (const run of countSource) {{
        const ids = new Set(run.strategies.map(s => s.strat_id));
        ids.forEach(id => {{ runCounts[id] = (runCounts[id] || 0) + 1; }});
    }}
    const seen = {{}};
    for (let i = runs.length - 1; i >= 0; i--) {{
        const run = runs[i];
        for (const s of run.strategies) {{
            if (!seen[s.strat_id]) {{
                seen[s.strat_id] = {{...s, run_id: run.run_id, run_label: run.label,
                    pipeline_run_count: runCounts[s.strat_id] || 1}};
            }}
        }}
    }}
    const strategies = Object.values(seen).sort((a, b) => a.strat_id.localeCompare(b.strat_id));
    const total = strategies.length;
    const isApprove = v => ['approve','approved'].includes(v);
    const isRevise = v => ['revise','needs revision','needs_revision'].includes(v);
    const isReject = v => ['reject','rejected','infeasible'].includes(v);
    const isSplit = v => v === 'split';
    const reviewed = strategies.filter(s => s.recommendation && s.recommendation !== '—' && s.recommendation !== '');
    const totalReviewed = reviewed.length;
    const approved = reviewed.filter(s => isApprove(s.recommendation)).length;
    const revise = reviewed.filter(s => isRevise(s.recommendation)).length;
    const reject = reviewed.filter(s => isReject(s.recommendation)).length;
    const split = reviewed.filter(s => isSplit(s.recommendation)).length;
    const needsAttention = reviewed.filter(s => s.needs_attention).length;
    const pct = (n, t) => t > 0 ? Math.round(100 * n / t) : 0;
    const dims = ['feasibility','testability','scope','architecture'];
    const dimensions = {{}};
    dims.forEach(dim => {{
        const vals = strategies.filter(s => s.scores && s.scores[dim] != null).map(s => s.scores[dim]);
        const dt = vals.length;
        const dp = vals.filter(v => v === 2).length;
        const dpart = vals.filter(v => v === 1).length;
        const df = vals.filter(v => v === 0).length;
        const dsum = vals.reduce((a, b) => a + b, 0);
        dimensions[dim] = {{total: dt, pass: dp, partial: dpart, fail: df, rate: pct(dsum, dt * 2)}};
    }});
    const scored = strategies.filter(s => s.scores && s.scores.total != null);
    const avgScore = scored.length > 0 ? Math.round(10 * scored.reduce((a, s) => a + s.scores.total, 0) / scored.length) / 10 : null;
    const skippedSeen = {{}};
    for (let i = runs.length - 1; i >= 0; i--) {{
        for (const s of (runs[i].skipped || [])) {{
            if (s.rfe_key && !skippedSeen[s.rfe_key] && s.missing && s.missing.includes('rfe-creator-autofix-rubric-pass')) skippedSeen[s.rfe_key] = s;
        }}
    }}
    const skipped = Object.values(skippedSeen).sort((a, b) => (a.rfe_key || '').localeCompare(b.rfe_key || ''));
    const rfeKeys = new Set();
    strategies.forEach(s => {{ if (s.source_rfe) rfeKeys.add(s.source_rfe); }});
    skipped.forEach(s => {{ if (s.rfe_key) rfeKeys.add(s.rfe_key); }});
    // SME Input KPI: rubric-pass STRATs (is_approve only) with empty SME Input.
    // has_sme_input is null for un-instrumented historical runs - unknown, not
    // empty - so exclude it from both numerator and denominator.
    const rubricPassStrats = strategies.filter(s => isApprove(s.recommendation));
    const smeKnown = rubricPassStrats.filter(s => s.has_sme_input === true || s.has_sme_input === false);
    const smeEmpty = smeKnown.filter(s => s.has_sme_input === false).length;
    // Refine iterations: authoritative refine_count else pipeline-run fallback.
    const iters = strategies.map(refineIterations);
    const avgRefine = iters.length > 0
        ? Math.round(10 * iters.reduce((a, it) => a + it.value, 0) / iters.length) / 10
        : null;
    return {{
        total, total_rfes: rfeKeys.size, total_runs: runs.length,
        reviewed: totalReviewed, approved, revise, reject, split,
        needs_attention: needsAttention,
        approval_rate: pct(approved, totalReviewed),
        revision_rate: pct(revise, totalReviewed),
        avg_total_score: avgScore, dimensions, strategies, skipped,
        sme_empty: smeEmpty, rubric_pass_total: smeKnown.length,
        avg_refine: avgRefine,
        weakest_dim: total > 0 ? dims.reduce((a, b) => dimensions[a].rate < dimensions[b].rate ? a : b) : '—',
        strongest_dim: total > 0 ? dims.reduce((a, b) => dimensions[a].rate > dimensions[b].rate ? a : b) : '—',
    }};
}}

// Resolve one strategy's refine-iteration count. Authoritative refine_count (a
// non-boolean int >= 0, incl. 0) wins; otherwise fall back to the pipeline-run
// baseline max(0, distinct_runs - 1), marked approximate. Mirrors the Python
// refine_iterations() helper. See ADR-0001.
function refineIterations(s) {{
    const rc = s.refine_count;
    if (typeof rc === 'number' && Number.isInteger(rc) && rc >= 0) {{
        return {{value: rc, approx: false}};
    }}
    const runs = (typeof s.pipeline_run_count === 'number' && s.pipeline_run_count > 0)
        ? s.pipeline_run_count : 1;
    return {{value: Math.max(0, runs - 1), approx: true}};
}}

function applyTimeRange() {{
    const days = parseInt(document.getElementById('time-range').value);
    RUNS = filterByDays(ALL_RUNS, days);
    EXEC = recomputeExec(RUNS);
    renderExecutiveSummary();
    renderOverviewKPIs();
    renderAttention();
    renderBlocked();
    buildRunList();
    buildRunSelector();
    initCharts();
}}

// ─── Page switching ──────────────────────────────────────────────────────────
function switchPage(page) {{
    document.querySelectorAll('.nav-page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('page-' + page).classList.add('active');
    event.target.classList.add('active');
    // Chart.js can't size canvases in hidden containers; trigger resize on show
    document.querySelectorAll('#page-' + page + ' canvas').forEach(c => {{
        const chart = Chart.getChart(c);
        if (chart) chart.resize();
    }});
}}

function showRunDetail(idx) {{
    document.getElementById('run-select').value = idx;
    renderRunDetail(idx);
    // Switch to detail tab
    document.querySelectorAll('.nav-page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('page-run-detail').classList.add('active');
    document.querySelectorAll('.nav-tab')[2].classList.add('active');
}}

// ─── Overview KPIs (Per-Run Trends page) ────────────────────────────────────
function renderOverviewKPIs() {{
    const el = document.getElementById('overview-kpis');
    const cur = RUNS.length > 0 ? RUNS[RUNS.length - 1] : null;
    const prev = RUNS.length > 1 ? RUNS[RUNS.length - 2] : null;
    const deltaHtml = (cur, prev, key, isPct) => {{
        if (!cur || !prev) return '';
        const d = cur[key] - prev[key];
        if (d === 0) return '<span style="color:#6e7681">→ no change</span>';
        const arrow = d > 0 ? '↑' : '↓';
        const color = d > 0 ? '#3fb950' : '#f85149';
        return `<span style="color:${{color}}">${{arrow}} ${{Math.abs(d)}}${{isPct !== false ? '%' : ''}}</span>`;
    }};
    const rate = cur ? cur.approval_rate : 0;
    const heroColor = healthColor(rate);
    const prevRate = prev ? prev.approval_rate : null;
    const deltaVal = prevRate !== null ? rate - prevRate : null;
    const deltaArrow = deltaVal === null ? '—' : deltaVal === 0 ? '→ no change'
        : deltaVal > 0 ? `<span style="color:#3fb950">↑ ${{deltaVal}}%</span>`
        : `<span style="color:#f85149">↓ ${{Math.abs(deltaVal)}}%</span>`;
    const avgScore = cur && cur.avg_total_score != null ? cur.avg_total_score : null;
    const avgScorePct = avgScore != null ? Math.round(avgScore / 8 * 100) : 0;
    const avgScoreHtml = avgScore != null ? `${{avgScore}}<span style="font-size:16px;color:#6e7681">/8</span>` : '—';
    const weakDim = cur ? cur.weakest_dim || '—' : '—';
    const weakRate = cur ? cur.weakest_rate || 0 : 0;
    const strongDim = cur ? cur.strongest_dim || '—' : '—';
    const strongRate = cur ? cur.strongest_rate || 0 : 0;
    const costedRuns = RUNS.filter(r => r.cost && r.cost.total_usd > 0);
    const totalSpend = costedRuns.reduce((s, r) => s + r.cost.total_usd, 0);
    const totalStrats = costedRuns.reduce((s, r) => s + r.reviewed, 0);
    const avgPerStrat = totalStrats > 0 ? (totalSpend / totalStrats).toFixed(2) : '—';

    // Empty SME Input (latest run): count + rate of rubric-pass STRATs with
    // known SME status that shipped empty. Up (more empty) is worse -> red.
    const smeEmpty = cur ? (cur.sme_empty_count || 0) : 0;
    const smeKnown = cur ? (cur.sme_rubric_known || 0) : 0;
    const smePct = smeKnown > 0 ? Math.round(100 * smeEmpty / smeKnown) : 0;
    const smeColor = smeEmpty > 0 ? '#d29922' : '#3fb950';
    let smeDelta = '';
    if (cur && prev && cur.sme_empty_count != null && prev.sme_empty_count != null) {{
        const d = cur.sme_empty_count - prev.sme_empty_count;
        smeDelta = d === 0 ? '<span style="color:#6e7681">→ no change</span>'
            : d > 0 ? `<span style="color:#f85149">↑ ${{d}}</span>`
            : `<span style="color:#3fb950">↓ ${{Math.abs(d)}}</span>`;
    }}
    // Avg refine iterations (latest run): authoritative-only. Direction is not
    // inherently good/bad, so the delta is neutral gray.
    const avgRefine = cur && cur.avg_refine_iter != null ? cur.avg_refine_iter : null;
    const avgRefineHtml = avgRefine != null ? avgRefine : '—';
    const refineInstr = cur ? (cur.refine_instrumented_count || 0) : 0;
    let refineDelta = '';
    if (cur && prev && cur.avg_refine_iter != null && prev.avg_refine_iter != null) {{
        const d = Math.round(10 * (cur.avg_refine_iter - prev.avg_refine_iter)) / 10;
        refineDelta = d === 0 ? '<span style="color:#6e7681">→ no change</span>'
            : `<span style="color:#8b949e">${{d > 0 ? '↑' : '↓'}} ${{Math.abs(d)}}</span>`;
    }}

    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{heroColor}}">${{cur ? cur.approved : 0}} of ${{cur ? cur.reviewed : 0}} strategies approved (${{rate}}%)</div>
        <div class="hero-delta">vs previous run: ${{deltaArrow}}</div>
        <div class="hero-sub">${{RUNS.length}} pipeline run(s) | Latest: ${{cur ? cur.label : 'none'}}</div>
    </div>`;
    html += `<div class="kpi-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
        <div class="kpi"><div class="kpi-value" style="color:#58a6ff">${{RUNS.length}}</div><div class="kpi-label">Pipeline Runs</div><div class="kpi-detail">${{RUNS.reduce((a, r) => a + r.reviewed, 0)}} strategies total</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#f0f6fc">${{cur ? cur.reviewed : 0}}</div><div class="kpi-label">Strategies Reviewed</div><div class="kpi-detail">${{deltaHtml(cur, prev, 'reviewed', false)}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{heroColor}}">${{rate}}%</div><div class="kpi-label">Approval Rate</div><div class="kpi-detail">${{deltaHtml(cur, prev, 'approval_rate', true)}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(avgScorePct)}}">${{avgScoreHtml}}</div><div class="kpi-label">Avg Score</div><div class="kpi-detail">Rubric: F+T+S+A (0-2 each)</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#f85149">${{cur ? cur.needs_attention || 0 : 0}}</div><div class="kpi-label">Needs Attention</div><div class="kpi-detail">${{cur && cur.needs_attention > 0 ? 'Human review required' : 'All clear'}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(weakRate)}}">${{weakRate}}%</div><div class="kpi-label">Weakest: ${{weakDim.charAt(0).toUpperCase() + weakDim.slice(1)}}</div><div class="kpi-detail">Strongest: ${{strongDim.charAt(0).toUpperCase() + strongDim.slice(1)}} (${{strongRate}}%)</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#bc8cff">$${{totalSpend.toFixed(0)}}</div><div class="kpi-label">Total Spend</div><div class="kpi-detail">$${{avgPerStrat}}/strategy (${{costedRuns.length}} runs)</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{smeColor}}">${{smeEmpty}}</div><div class="kpi-label">Empty SME Input</div><div class="kpi-detail">${{smePct}}% of ${{smeKnown}} rubric-pass ${{smeDelta}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#58a6ff">${{avgRefineHtml}}</div><div class="kpi-label">Avg Refine Iterations</div><div class="kpi-detail">${{refineInstr}} instrumented ${{refineDelta}}</div></div>
    </div>`;
    el.innerHTML = html;
}}

// ─── Needs Attention page ───────────────────────────────────────────────────
function renderAttention() {{
    const el = document.getElementById('attention-content');
    const attn = EXEC.strategies.filter(s => s.needs_attention);
    const total = attn.length;

    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{total > 0 ? '#f85149' : '#3fb950'}}">${{total}} ${{total === 1 ? 'strategy' : 'strategies'}} need${{total === 1 ? 's' : ''}} attention</div>
        <div class="hero-sub">Strategies with REVISE or REJECT verdict requiring human review</div>
    </div>`;

    if (total === 0) {{
        html += `<div style="text-align:center;padding:48px;color:#3fb950;font-size:16px">All strategies passed review — no action needed.</div>`;
        el.innerHTML = html;
        return;
    }}

    html += `<table><thead><tr>
        <th>Strategy</th><th>Title</th><th>Verdict</th><th>Score</th>
        <th>F</th><th>T</th><th>S</th><th>A</th><th>Run</th>
    </tr></thead><tbody>`;
    attn.forEach((s, idx) => {{
        const v = s.recommendation || '—';
        const sc = s.scores || {{}};
        const total = sc.total != null ? sc.total + '/8' : '—';
        html += `<tr class="clickable" onclick="toggleAttentionDetail(${{idx}})">
            <td>${{s.strat_id}}</td>
            <td>${{s.title}}</td>
            <td><span class="${{verdictClass(v)}}">${{verdictLabel(v)}}</span></td>
            <td>${{total}}</td>
            <td style="${{scoreStyle(sc.feasibility)}};padding:4px 8px;border-radius:4px">${{scoreText(sc.feasibility)}}</td>
            <td style="${{scoreStyle(sc.testability)}};padding:4px 8px;border-radius:4px">${{scoreText(sc.testability)}}</td>
            <td style="${{scoreStyle(sc.scope)}};padding:4px 8px;border-radius:4px">${{scoreText(sc.scope)}}</td>
            <td style="${{scoreStyle(sc.architecture)}};padding:4px 8px;border-radius:4px">${{scoreText(sc.architecture)}}</td>
            <td style="font-size:12px;color:#8b949e">${{s.run_id || '—'}}</td>
        </tr>
        <tr><td colspan="9" style="padding:0">
            <div id="attn-panel-${{idx}}" class="detail-panel">
                <div style="padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
                    <div>
                        <h4 style="color:#58a6ff;margin-bottom:8px">Review Comment</h4>
                        <div style="color:#c9d1d9;font-size:13px;white-space:pre-wrap">${{s.body && s.body.review_comment ? s.body.review_comment : 'No review comment available'}}</div>
                    </div>
                    <div>
                        <h4 style="color:#58a6ff;margin-bottom:8px">Review Summary</h4>
                        <div style="color:#c9d1d9;font-size:13px;white-space:pre-wrap">${{s.body && s.body.review ? s.body.review.substring(0, 1500) : 'No review available'}}</div>
                    </div>
                </div>
            </div>
        </td></tr>`;
    }});
    html += '</tbody></table>';
    el.innerHTML = html;
}}

function toggleAttentionDetail(idx) {{
    const panel = document.getElementById(`attn-panel-${{idx}}`);
    panel.classList.toggle('open');
}}

// ─── Blocked RFEs ───────────────────────────────────────────────────────────
function renderBlocked() {{
    const el = document.getElementById('blocked-content');
    const seen = {{}};
    for (let i = RUNS.length - 1; i >= 0; i--) {{
        for (const s of (RUNS[i].skipped || [])) {{
            if (s.rfe_key && !seen[s.rfe_key]) seen[s.rfe_key] = s;
        }}
    }}
    const all = Object.values(seen).sort((a, b) => (a.rfe_key || '').localeCompare(b.rfe_key || ''));

    const categories = [
        {{
            id: 'missing-release',
            label: 'Missing Release Targeting',
            color: '#f85149',
            icon: '🏷',
            action: 'Set Target Version to rhoai-3.5 or rhoai-3.6 in Jira <strong>or</strong> add <code>strat-creator-3.5</code> label.',
            filter: s => s.labels && s.labels.includes('missing labels') && s.labels.includes('strat-creator-3.5')
        }},
        {{
            id: 'missing-approval',
            label: 'Missing Quality Approval',
            color: '#f85149',
            icon: '✅',
            action: 'RFE needs <code>rfe-creator-autofix-rubric-pass</code> or <code>tech-reviewed</code> label.',
            filter: s => s.labels && s.labels.includes('missing labels') && s.labels.includes('rfe-creator-autofix-rubric-pass')
        }},
        {{
            id: 'needs-attention',
            label: 'STRAT Needs Attention',
            color: '#d29922',
            icon: '⚠',
            action: 'Existing strategy flagged for review. Open the STRAT in Jira and resolve issues or sign off.',
            filter: s => s.labels && s.labels.includes('already processed') && s.labels.includes('needs-attention')
        }},
        {{
            id: 'in-progress',
            label: 'STRAT In Progress',
            color: '#8b949e',
            icon: '🔄',
            action: 'Strategy exists in an active state (Refinement / In Progress). Pipeline will not reprocess until current work completes.',
            filter: s => s.labels && s.labels.includes('existing STRAT')
        }}
    ];

    categories.forEach(c => {{ c.items = all.filter(c.filter); }});
    const blocked = categories.filter(c => c.id !== 'in-progress');
    const totalBlocked = blocked.reduce((n, c) => n + c.items.length, 0);
    const totalAll = categories.reduce((n, c) => n + c.items.length, 0);

    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{totalBlocked > 0 ? '#d29922' : '#3fb950'}}">${{totalBlocked}} RFE${{totalBlocked !== 1 ? 's' : ''}} blocked</div>
        <div class="hero-sub">${{totalAll}} total RFEs not entering the strategy pipeline (${{categories[3].items.length}} already in progress)</div>
    </div>`;

    html += '<div class="kpi-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:24px">';
    categories.forEach(c => {{
        html += `<div class="kpi">
            <div class="kpi-value" style="color:${{c.color}}">${{c.items.length}}</div>
            <div class="kpi-label">${{c.label}}</div>
        </div>`;
    }});
    html += '</div>';

    categories.forEach(c => {{
        if (c.items.length === 0) return;
        html += `<div style="margin-bottom:24px;padding:16px;background:#161b22;border:1px solid #30363d;border-radius:8px;">
            <h3 style="color:${{c.color}};margin:0 0 8px;font-size:15px">${{c.icon}} ${{c.label}} (${{c.items.length}})</h3>
            <p style="color:#8b949e;margin:0 0 12px;font-size:13px">${{c.action}}</p>
            <table><thead><tr>
                <th>RFE Key</th><th>Title</th><th>Reason</th>
            </tr></thead><tbody>`;
        c.items.forEach(s => {{
            html += `<tr>
                <td><a href="https://issues.redhat.com/browse/${{s.rfe_key}}" target="_blank" style="color:#58a6ff">${{s.rfe_key}}</a></td>
                <td>${{s.title}}</td>
                <td style="color:${{c.color}};font-size:13px">${{s.labels}}</td>
            </tr>`;
        }});
        html += '</tbody></table></div>';
    }});

    if (totalAll === 0) {{
        html += '<div style="text-align:center;padding:48px;color:#3fb950;font-size:16px">All in-scope RFEs are entering the pipeline — no blockers.</div>';
    }}

    el.innerHTML = html;
}}

// ─── Currently Processing ────────────────────────────────────────────────────
function renderProcessing() {{
    const el = document.getElementById('processing-content');
    const issues = PROCESSING_ISSUES;
    const now = new Date();

    const heroColor = issues.length > 0 ? '#d29922' : '#3fb950';
    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{heroColor}}">${{issues.length}} RFE${{issues.length !== 1 ? 's' : ''}} currently processing</div>
        <div class="hero-sub">RFEs with <code style="background:#21262d;padding:2px 6px;border-radius:3px">strat-creator-processing</code> label in Jira</div>
    </div>`;

    if (issues.length === 0) {{
        html += '<div style="text-align:center;padding:48px;color:#3fb950;font-size:16px">No RFEs are currently being processed by the pipeline.</div>';
        el.innerHTML = html;
        return;
    }}

    html += `<table><thead><tr>
        <th>RFE Key</th><th>Title</th><th>Processing Since</th><th>Duration</th><th>Status</th>
    </tr></thead><tbody>`;

    issues.forEach(issue => {{
        let since = '—';
        let duration = '—';
        let status = '<span style="color:#3fb950">Running</span>';

        if (issue.started) {{
            const start = new Date(issue.started);
            since = start.toLocaleString();
            const diffMs = now - start;
            const diffMins = Math.floor(diffMs / 60000);
            const diffHrs = Math.floor(diffMins / 60);
            const remainMins = diffMins % 60;

            if (diffHrs > 0) {{
                duration = `${{diffHrs}}h ${{remainMins}}m`;
            }} else {{
                duration = `${{diffMins}}m`;
            }}

            if (diffMins > 120) {{
                status = '<span style="color:#f85149;font-weight:600">⚠ Possibly stuck (>2h)</span>';
            }} else if (diffMins > 60) {{
                status = '<span style="color:#d29922">⏳ Long running (>1h)</span>';
            }}
        }}

        html += `<tr>
            <td><a href="https://issues.redhat.com/browse/${{issue.key}}" target="_blank" style="color:#58a6ff">${{issue.key}}</a></td>
            <td>${{issue.summary}}</td>
            <td style="font-size:13px;color:#8b949e">${{since}}</td>
            <td>${{duration}}</td>
            <td>${{status}}</td>
        </tr>`;
    }});

    html += '</tbody></table>';
    html += '<div style="margin-top:16px;padding:12px 16px;background:#161b22;border:1px solid #30363d;border-radius:8px;font-size:13px;color:#8b949e">';
    html += '<strong style="color:#c9d1d9">How to interpret:</strong> Pipeline runs typically take 20–40 minutes per RFE. ';
    html += 'If an RFE has been processing for over 2 hours, the pipeline job may have failed — check GitLab CI.';
    html += '</div>';

    el.innerHTML = html;
}}

// ─── Executive summary ──────────────────────────────────────────────────────
function renderExecutiveSummary() {{
    const e = EXEC;
    const jc = JIRA_COUNTS;
    const el = document.getElementById('exec-content');
    const dims = ['feasibility','testability','scope','architecture'];
    const rate = e.approval_rate;
    const heroColor = healthColor(rate);
    const avgScoreHtml = e.avg_total_score !== null && e.avg_total_score !== undefined
        ? `${{e.avg_total_score}}<span style="font-size:16px;color:#6e7681">/8</span>`
        : '—';
    const avgScorePct = e.avg_total_score !== null ? Math.round(e.avg_total_score / 8 * 100) : 0;

    const createdCount = jc ? jc['strat-creator-auto-created'] : e.total;
    const createdDetail = jc
        ? `${{e.total}} refined & reviewed`
        : `Across ${{e.total_runs}} runs`;
    const createdSrc = jc ? 'Jira' : '';
    const rubricPass = jc ? jc['strat-creator-rubric-pass'] : e.approved;
    const rubricSrc = jc ? 'Jira' : '';
    const signedOff = jc ? jc['strat-creator-human-sign-off'] : null;
    const attnCount = jc ? jc['strat-creator-needs-attention'] : e.needs_attention;
    const attnSrc = jc ? 'Jira' : '';
    const heroSub = jc
        ? `${{jc['strat-creator-auto-created']}} strategies in Jira | ${{e.total}} refined & reviewed across ${{e.total_runs}} pipeline runs`
        : `${{e.total}} unique strategies across ${{e.total_runs}} pipeline runs | Deduped by most recent run per strategy`;

    const srcBadge = (src) => src ? `<span style="font-size:10px;color:#8b949e;background:#21262d;padding:1px 6px;border-radius:8px;margin-left:4px;vertical-align:middle">${{src}}</span>` : '';

    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{heroColor}}">${{e.approved}} of ${{e.reviewed}} unique strategies approved (${{rate}}%)</div>
        <div class="hero-sub">${{heroSub}}</div>
    </div>`;

    // KPI cards
    const skippedCount = e.skipped ? e.skipped.length : 0;
    // SME Input: how many rubric-pass STRATs shipped with no human SME input.
    const smeEmpty = e.sme_empty || 0;
    const rubricTotal = e.rubric_pass_total || 0;
    const smePct = rubricTotal > 0 ? Math.round(100 * smeEmpty / rubricTotal) : 0;
    const smeColor = smeEmpty > 0 ? '#d29922' : '#3fb950';
    // Refine iterations: avg across all STRATs (authoritative or fallback).
    const avgRefine = e.avg_refine;
    const avgRefineHtml = (avgRefine !== null && avgRefine !== undefined) ? avgRefine : '—';
    const signOffCard = signedOff !== null
        ? `<div class="kpi"><div class="kpi-value" style="color:#bc8cff">${{signedOff}}</div><div class="kpi-label">Human Sign-Off${{srcBadge('Jira')}}</div><div class="kpi-detail">Staff engineer confirmed</div></div>`
        : '';
    html += `<div class="kpi-grid" style="grid-template-columns: repeat(auto-fit, minmax(190px, 1fr))">
        <div class="kpi"><div class="kpi-value" style="color:#f0f6fc">${{e.total_rfes}}</div><div class="kpi-label">Total RFEs</div><div class="kpi-detail">${{e.total}} strategies + ${{skippedCount}} skipped<br><span style="color:#6e7681;font-size:0.85em">(One RFE may contain 1+ strategies)</span></div></div>
        <div class="kpi"><div class="kpi-value" style="color:#58a6ff">${{createdCount}}</div><div class="kpi-label">Strategies Created${{srcBadge(createdSrc)}}</div><div class="kpi-detail">${{createdDetail}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#3fb950">${{rubricPass}}</div><div class="kpi-label">Rubric Pass${{srcBadge(rubricSrc)}}</div><div class="kpi-detail">${{rubricSrc ? 'CI-approved in Jira' : e.approved + ' approved'}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{smeColor}}">${{smeEmpty}}</div><div class="kpi-label">Empty SME Input</div><div class="kpi-detail">${{smePct}}% of ${{rubricTotal}} rubric-pass STRATs</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#58a6ff">${{avgRefineHtml}}</div><div class="kpi-label">Avg Refine Iterations</div><div class="kpi-detail">across ${{e.total}} STRATs</div></div>
        ${{signOffCard}}
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(avgScorePct)}}">${{avgScoreHtml}}</div><div class="kpi-label">Avg Score</div><div class="kpi-detail">Threshold: 6/8</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{attnCount > 0 ? '#f85149' : '#3fb950'}}">${{attnCount}}</div><div class="kpi-label">Needs Attention${{srcBadge(attnSrc)}}</div><div class="kpi-detail">${{attnCount === 0 ? 'All clear' : 'Staff engineer review'}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{skippedCount > 0 ? '#d29922' : '#3fb950'}}">${{skippedCount}}</div><div class="kpi-label">Skipped RFEs</div><div class="kpi-detail">${{skippedCount > 0 ? 'Waiting on entry gate' : 'All RFEs processed'}}</div></div>
    </div>`;

    // Verdict + Dimensions + Funnel — single 3-column row
    html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:24px;margin-bottom:24px">`;

    // Col 1: verdict donut (compact)
    html += `<div class="chart-card" style="padding:16px">
        <h3 style="margin-bottom:8px">Verdict Distribution</h3>
        <div style="display:flex;align-items:center;gap:16px">
        <div style="width:120px;height:120px;flex-shrink:0"><canvas id="exec-donut"></canvas></div>
        <div style="font-size:12px;line-height:1.8">
            <div><span style="display:inline-block;width:10px;height:10px;background:#3fb950;border-radius:2px;margin-right:4px"></span>Approve: <strong>${{e.approved}}</strong></div>
            <div><span style="display:inline-block;width:10px;height:10px;background:#d29922;border-radius:2px;margin-right:4px"></span>Revise: <strong>${{e.revise}}</strong></div>
            <div><span style="display:inline-block;width:10px;height:10px;background:#f85149;border-radius:2px;margin-right:4px"></span>Reject: <strong>${{e.reject}}</strong></div>
        </div>
        </div>
    </div>`;

    // Col 2: dimension health bars
    html += `<div class="chart-card" style="padding:16px">
        <h3 style="margin-bottom:8px">Quality by Dimension</h3>`;
    dims.forEach(dim => {{
        const ds = e.dimensions[dim];
        const drate = ds.rate;
        const pw = ds.total > 0 ? Math.round(100*(ds.pass||0)/ds.total) : 0;
        const partw = ds.total > 0 ? Math.round(100*(ds.partial||0)/ds.total) : 0;
        const fw = ds.total > 0 ? Math.round(100*(ds.fail||0)/ds.total) : 0;
        html += `<div class="dim-row" style="margin-bottom:6px">
            <div class="dim-label" style="width:90px;font-size:12px">${{dim.charAt(0).toUpperCase()+dim.slice(1)}}</div>
            <div class="dim-bar-container"><div class="dim-bar-track" style="height:20px">
                <div class="dim-bar-seg" style="width:${{pw}}%;background:#3fb950" title="${{ds.pass||0}} scored 2/2"></div>
                <div class="dim-bar-seg" style="width:${{partw}}%;background:#d29922" title="${{ds.partial||0}} scored 1/2"></div>
                <div class="dim-bar-seg" style="width:${{fw}}%;background:#f85149" title="${{ds.fail||0}} scored 0/2"></div>
            </div></div>
            <div class="dim-rate" style="width:40px;font-size:13px;color:${{healthColor(drate)}}">${{drate}}%</div>
        </div>`;
    }});
    html += `<div style="display:flex;gap:12px;margin-top:6px;font-size:10px;color:#6e7681">
        <span><span style="display:inline-block;width:8px;height:8px;background:#3fb950;border-radius:2px;margin-right:3px"></span>Pass</span>
        <span><span style="display:inline-block;width:8px;height:8px;background:#d29922;border-radius:2px;margin-right:3px"></span>Gaps</span>
        <span><span style="display:inline-block;width:8px;height:8px;background:#f85149;border-radius:2px;margin-right:3px"></span>Fail</span>
    </div></div>`;

    // Col 3: readiness funnel (proper narrowing)
    const readyCount = e.reviewed - e.needs_attention;
    const funnelMax = Math.max(e.total, 1);
    html += `<div class="chart-card" style="padding:16px">
        <h3 style="margin-bottom:8px">Readiness Funnel</h3>
        <div style="display:flex;flex-direction:column;gap:6px;align-items:center">
            <div style="display:flex;align-items:center;gap:8px;width:100%">
                <div style="background:#23302a;border-radius:6px;height:28px;display:flex;align-items:center;justify-content:center;width:100%">
                    <span style="color:#3fb950;font-weight:600;font-size:13px">${{e.total}} created</span></div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;width:${{Math.round(100*e.reviewed/funnelMax)}}%">
                <div style="background:#1f3a5f;border-radius:6px;height:28px;display:flex;align-items:center;justify-content:center;width:100%">
                    <span style="color:#58a6ff;font-weight:600;font-size:13px">${{e.reviewed}} reviewed</span></div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;width:${{Math.max(Math.round(100*readyCount/funnelMax), 15)}}%">
                <div style="background:#23302a;border-radius:6px;height:28px;display:flex;align-items:center;justify-content:center;width:100%">
                    <span style="color:#3fb950;font-weight:600;font-size:13px">${{readyCount}} ready</span></div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;width:${{Math.max(Math.round(100*e.needs_attention/funnelMax), 15)}}%">
                <div style="background:#2d1418;border-radius:6px;height:28px;display:flex;align-items:center;justify-content:center;width:100%">
                    <span style="color:#f85149;font-weight:600;font-size:12px">${{e.needs_attention}} attention</span></div>
            </div>
        </div>
    </div>`;
    html += `</div>`;

    // Score distribution (full width)
    html += `<div class="chart-card full-width" style="margin-bottom:24px">
        <h3>Score Distribution</h3>
        <div style="max-height:200px"><canvas id="exec-histogram"></canvas></div>
    </div>`;

    // Per-strategy table with expandable detail accordions
    html += `<div class="grid-section"><h3>All Unique Strategies (deduped)</h3>
    <table><thead><tr>
        <th></th><th>Strat ID</th><th>Title</th><th>Source RFE</th><th>Priority</th>
        <th>F</th><th>T</th><th>S</th><th>A</th><th>Score</th><th>Verdict</th><th>Attention</th><th>SME</th><th>Iter</th><th>Run</th>
    </tr></thead><tbody>`;
    e.strategies.forEach((s, i) => {{
        const sc = s.scores;
        let scoreCells = '';
        if (sc) {{
            ['feasibility','testability','scope','architecture'].forEach(d => {{
                const v = sc[d];
                scoreCells += `<td style="${{scoreStyle(v)}};text-align:center;font-weight:600">${{v !== null && v !== undefined ? v : '—'}}</td>`;
            }});
            const totalPct = sc.total !== null ? Math.round(sc.total / 8 * 100) : 0;
            scoreCells += `<td style="color:${{healthColor(totalPct)}};font-weight:600;text-align:center">${{sc.total}}/8</td>`;
        }} else {{
            scoreCells += '<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>';
        }}
        const attentionHtml = s.needs_attention
            ? '<span style="color:#f85149;font-weight:600">&#9679; Yes</span>'
            : '<span style="color:#3fb950">&#10003;</span>';
        const smeHtml = s.has_sme_input === true
            ? '<span style="color:#3fb950" title="Has staff engineer / SME input">&#10003;</span>'
            : s.has_sme_input === false
            ? '<span style="color:#d29922" title="No SME input (boilerplate only)">Empty</span>'
            : '<span style="color:#6e7681" title="SME status unknown - run predates SME instrumentation">?</span>';
        const it = refineIterations(s);
        const iterHtml = it.approx
            ? `<span style="color:#8b949e" title="Approximate: derived from pipeline run count; no instrumented refine_count">~${{it.value}}</span>`
            : `<span title="Instrumented refine_count">${{it.value}}</span>`;
        html += `<tr class="clickable" onclick="toggleExecDetail(${{i}})">
            <td><span class="expand-icon" id="eicon-${{i}}">&#9654;</span></td>
            <td><strong>${{s.strat_id}}</strong></td>
            <td>${{s.title}}</td>
            <td>${{s.source_rfe}}</td>
            <td>${{s.priority}}</td>
            ${{scoreCells}}
            <td class="${{verdictClass(s.recommendation)}}">${{verdictLabel(s.recommendation)}}</td>
            <td style="text-align:center">${{attentionHtml}}</td>
            <td style="text-align:center">${{smeHtml}}</td>
            <td style="text-align:center">${{iterHtml}}</td>
            <td style="font-size:12px;color:#6e7681">${{s.run_label}}</td>
        </tr>
        <tr><td colspan="15" style="padding:0">
            <div class="detail-panel" id="epanel-${{i}}">
                <h2>${{s.strat_id}}: ${{s.title}}</h2>
                <div class="label-bar">${{renderLabelBadges(s.labels || [])}}</div>
                <div class="detail-tabs">
                    <div class="detail-tab active" onclick="switchExecTab(${{i}},'rfe')">Original RFE</div>
                    <div class="detail-tab" onclick="switchExecTab(${{i}},'strategy')">Strategy (AI Generated)</div>
                    <div class="detail-tab" onclick="switchExecTab(${{i}},'comment')">Review Summary (Jira Comment)</div>
                    <div class="detail-tab" onclick="switchExecTab(${{i}},'review')">Detailed Review</div>
                </div>
                <div class="tab-content active" id="etab-${{i}}-rfe">${{s.rfe_html || '<em>No RFE content</em>'}}</div>
                <div class="tab-content" id="etab-${{i}}-strategy">${{s.strategy_html || '<em>No strategy generated yet</em>'}}</div>
                <div class="tab-content" id="etab-${{i}}-comment">${{s.comment_html || '<em>No review comment available</em>'}}</div>
                <div class="tab-content" id="etab-${{i}}-review">${{s.review_html}}</div>
            </div>
        </td></tr>`;
    }});
    html += '</tbody></table></div>';

    // Skipped RFEs section
    if (e.skipped && e.skipped.length > 0) {{
        html += `<div style="margin-top:24px;padding:16px;background:#1c1917;border:1px solid #d29922;border-radius:8px;">
            <h3 style="color:#d29922;margin:0 0 12px;">Skipped RFEs (${{e.skipped.length}})</h3>
            <p style="color:#8b949e;margin-bottom:12px;">RFEs in scope (strat-creator-3.5) but waiting on entry gate (rfe-creator-autofix-rubric-pass or tech-reviewed)</p>
            <table><thead><tr>
                <th>RFE Key</th><th>Title</th><th>Current Labels</th><th style="color:#f85149">Missing</th>
            </tr></thead><tbody>`;
        e.skipped.forEach(s => {{
            html += `<tr><td>${{s.rfe_key}}</td><td>${{s.title}}</td><td>${{s.labels}}</td><td style="color:#f85149">${{s.missing}}</td></tr>`;
        }});
        html += '</tbody></table></div>';
    }}

    el.innerHTML = html;

    // Donut chart
    new Chart(document.getElementById('exec-donut'), {{
        type: 'doughnut',
        data: {{
            labels: ['Approve', 'Revise', 'Reject'],
            datasets: [{{
                data: [e.approved, e.revise, e.reject],
                backgroundColor: ['#3fb950', '#d29922', '#f85149'],
                borderWidth: 0,
                hoverOffset: 4,
            }}]
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: true,
            cutout: '55%',
            plugins: {{
                legend: {{ display: false }},
            }},
        }},
    }});

    // Score histogram
    const scoreBuckets = [0,0,0,0,0,0,0,0,0]; // 0-8
    e.strategies.forEach(s => {{
        if (s.scores && s.scores.total !== null && s.scores.total !== undefined) {{
            scoreBuckets[s.scores.total]++;
        }}
    }});
    new Chart(document.getElementById('exec-histogram'), {{
        type: 'bar',
        data: {{
            labels: ['0','1','2','3','4','5','6','7','8'],
            datasets: [{{
                label: 'Strategies',
                data: scoreBuckets,
                backgroundColor: scoreBuckets.map((_, i) => i >= 6 ? '#3fb950' : i >= 3 ? '#d29922' : '#f85149'),
                borderRadius: 4,
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ display: false }},
                annotation: {{
                    annotations: {{
                        threshold: {{
                            type: 'line',
                            xMin: 5.5, xMax: 5.5,
                            borderColor: '#3fb950',
                            borderWidth: 2,
                            borderDash: [6, 3],
                            label: {{
                                display: true,
                                content: 'Approve threshold',
                                position: 'start',
                                color: '#3fb950',
                                font: {{ size: 10 }},
                                backgroundColor: 'rgba(0,0,0,0.7)',
                            }}
                        }}
                    }}
                }}
            }},
            scales: {{
                x: {{ title: {{ display: true, text: 'Total Score', color: '#6e7681' }}, grid: {{ display: false }} }},
                y: {{ title: {{ display: true, text: 'Count', color: '#6e7681' }}, ticks: {{ stepSize: 1 }}, grid: {{ color: '#161b22' }} }},
            }},
        }},
    }});
}}

// ─── Build run list ──────────────────────────────────────────────────────────
function buildRunList() {{
    const container = document.getElementById('run-list-container');
    let html = '';
    for (let i = RUNS.length - 1; i >= 0; i--) {{
        const r = RUNS[i];
        const rate = r.approval_rate;
        const color = rate >= 70 ? '#3fb950' : rate >= 40 ? '#d29922' : '#f85149';
        const bg = rate >= 70 ? '#23302a' : rate >= 40 ? '#2d2400' : '#2d1418';
        html += `<div class="run-item" onclick="showRunDetail(${{i}})">
            <div class="run-date">${{r.label}}</div>
            ${{r.is_current ? '<div class="run-current">current</div>' : ''}}
            <div class="run-count">${{r.reviewed}} strategies</div>
            <div class="run-badge" style="background:${{bg}};color:${{color}}">${{rate}}% approved</div>
        </div>`;
    }}
    container.innerHTML = html;
}}

// ─── Populate run selector ───────────────────────────────────────────────────
function buildRunSelector() {{
    const sel = document.getElementById('run-select');
    sel.innerHTML = '';
    for (let i = RUNS.length - 1; i >= 0; i--) {{
        const r = RUNS[i];
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = `${{r.label}} — ${{r.approval_rate}}% approved (${{r.reviewed}} strategies)${{r.is_current ? ' [current]' : ''}}`;
        sel.appendChild(opt);
    }}
    if (RUNS.length > 0) {{
        sel.value = RUNS.length - 1;
        renderRunDetail(RUNS.length - 1);
    }}
}}

// ─── Render run detail ───────────────────────────────────────────────────────
function healthColor(rate) {{
    if (rate >= 70) return '#3fb950';
    if (rate >= 40) return '#d29922';
    return '#f85149';
}}

function verdictClass(v) {{
    if (['approve','approved'].includes(v)) return 'verdict-approve';
    if (['revise','needs revision','needs_revision'].includes(v)) return 'verdict-revise';
    if (['reject','rejected','infeasible'].includes(v)) return 'verdict-reject';
    return 'verdict-unknown';
}}

function verdictLabel(v) {{
    if (['approve','approved'].includes(v)) return 'Approve';
    if (['revise','needs revision','needs_revision'].includes(v)) return 'Revise';
    if (['reject','rejected','infeasible'].includes(v)) return 'Reject';
    return v || '—';
}}

function cellStyle(v) {{
    if (['approve','approved'].includes(v)) return 'background:#23302a;border-color:#3fb950';
    if (['revise','needs revision','needs_revision'].includes(v)) return 'background:#2d2400;border-color:#d29922';
    if (['reject','rejected','infeasible'].includes(v)) return 'background:#2d1418;border-color:#f85149';
    return 'background:#161b22;border-color:#30363d';
}}

function scoreStyle(score) {{
    if (score === 2) return 'background:#23302a;border-color:#3fb950;color:#3fb950';
    if (score === 1) return 'background:#2d2400;border-color:#d29922;color:#d29922';
    if (score === 0) return 'background:#2d1418;border-color:#f85149;color:#f85149';
    return 'background:#161b22;border-color:#30363d;color:#6e7681';
}}

function scoreText(score) {{
    if (score === null || score === undefined) return '—';
    return score + '/2';
}}

function labelCssClass(label) {{
    if (label.includes('needs-attention') || label.includes('ignore')) return 'label-escalation';
    if (label.includes('rubric-pass')) return 'label-gate';
    if (label.includes('auto-')) return 'label-provenance';
    return 'label-provenance';
}}

function renderLabelBadges(labels) {{
    if (!labels || !labels.length) return '';
    return labels.map(l => `<span class="label-badge ${{labelCssClass(l)}}">${{l}}</span>`).join(' ');
}}

function renderRunDetail(idx) {{
    const r = RUNS[idx];
    const dims = ['feasibility','testability','scope','architecture'];
    const el = document.getElementById('run-detail-content');

    // Hero
    let html = `<div class="hero">
        <div class="hero-statement" style="color:${{healthColor(r.approval_rate)}}">${{r.approved}} of ${{r.reviewed}} strategies approved (${{r.approval_rate}}%)</div>
        <div class="hero-sub">${{r.label}} | ${{r.total}} strategies processed</div>
    </div>`;

    // KPI cards
    const avgScoreHtml = r.avg_total_score !== null && r.avg_total_score !== undefined
        ? `${{r.avg_total_score}}<span style="font-size:14px;color:#6e7681">/8</span>`
        : '—';
    const avgScorePct = r.avg_total_score !== null ? Math.round(r.avg_total_score / 8 * 100) : 0;
    const runSkippedAll = r.skipped || [];
    const runSkippedGateCount = runSkippedAll.length;
    const runCost = r.cost && r.cost.total_usd > 0 ? r.cost : null;
    const costHtml = runCost ? `$${{runCost.total_usd.toFixed(2)}}` : '—';
    const costDetail = runCost ? `$${{(runCost.total_usd / (r.reviewed || 1)).toFixed(2)}}/strategy` : 'No cost data';
    html += `<div class="kpi-grid" style="grid-template-columns: repeat(8, 1fr)">
        <div class="kpi"><div class="kpi-value" style="color:#f0f6fc">${{r.reviewed}}</div><div class="kpi-label">Reviewed</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(r.approval_rate)}}">${{r.approval_rate}}%</div><div class="kpi-label">Approval Rate</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(avgScorePct)}}">${{avgScoreHtml}}</div><div class="kpi-label">Avg Score</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#f85149">${{r.needs_attention || 0}}</div><div class="kpi-label">Needs Attention</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(100-r.revision_rate)}}">${{r.revision_rate}}%</div><div class="kpi-label">Revision Rate</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{healthColor(r.weakest_rate)}}">${{r.weakest_rate}}%</div><div class="kpi-label">Weakest: ${{r.weakest_dim.charAt(0).toUpperCase()+r.weakest_dim.slice(1)}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:${{runSkippedGateCount > 0 ? '#d29922' : '#3fb950'}}">${{runSkippedGateCount}}</div><div class="kpi-label">Skipped RFEs</div><div class="kpi-detail">${{runSkippedGateCount > 0 ? 'See details below' : 'All RFEs processed'}}</div></div>
        <div class="kpi"><div class="kpi-value" style="color:#bc8cff">${{costHtml}}</div><div class="kpi-label">Run Cost</div><div class="kpi-detail">${{costDetail}}</div></div>
    </div>`;

    // Two-col: dimension bars + verdict grid
    // Dimension bars
    let dimHtml = '<div class="dim-section"><h3>Review Dimensions</h3>';
    dims.forEach(dim => {{
        const ds = r.dimensions[dim];
        const rate = ds.rate;
        const pw = ds.total > 0 ? Math.round(100*(ds.pass||0)/ds.total) : 0;
        const partw = ds.total > 0 ? Math.round(100*(ds.partial||0)/ds.total) : 0;
        const fw = ds.total > 0 ? Math.round(100*(ds.fail||0)/ds.total) : 0;
        dimHtml += `<div class="dim-row">
            <div class="dim-label">${{dim.charAt(0).toUpperCase()+dim.slice(1)}}</div>
            <div class="dim-bar-container"><div class="dim-bar-track">
                <div class="dim-bar-seg" style="width:${{pw}}%;background:#3fb950" title="${{ds.pass||0}} scored 2/2"></div>
                <div class="dim-bar-seg" style="width:${{partw}}%;background:#d29922" title="${{ds.partial||0}} scored 1/2"></div>
                <div class="dim-bar-seg" style="width:${{fw}}%;background:#f85149" title="${{ds.fail||0}} scored 0/2"></div>
            </div></div>
            <div class="dim-rate" style="color:${{healthColor(rate)}}">${{rate}}%</div>
        </div>`;
    }});
    dimHtml += `<div style="display:flex;gap:16px;margin-top:12px;font-size:11px;color:#6e7681">
        <span><span style="display:inline-block;width:10px;height:10px;background:#3fb950;border-radius:2px;margin-right:4px"></span>2/2 (pass)</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#d29922;border-radius:2px;margin-right:4px"></span>1/2 (gaps)</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#f85149;border-radius:2px;margin-right:4px"></span>0/2 (fail)</span>
    </div></div>`;

    // Verdict grid with numeric scores
    let gridHtml = `<div class="grid-section"><h3>Per-Strategy Scores</h3>
        <div class="grid-header">
            <div class="grid-header-id">Strategy</div>
            <div class="grid-header-dim">Feas</div>
            <div class="grid-header-dim">Test</div>
            <div class="grid-header-dim">Scope</div>
            <div class="grid-header-dim">Arch</div>
            <div class="grid-header-dim">Total</div>
            <div class="grid-header-verdict">Verdict</div>
        </div>`;
    r.strategies.forEach(s => {{
        const sid = s.strat_id.replace('STRAT-','');
        const sc = s.scores;
        let cells = '';
        if (sc) {{
            dims.forEach(d => {{
                const v = sc[d];
                cells += `<div class="grid-cell" style="${{scoreStyle(v)}}" title="${{d}}: ${{scoreText(v)}}">${{scoreText(v)}}</div>`;
            }});
            const totalPct = sc.total !== null ? Math.round(sc.total / 8 * 100) : 0;
            cells += `<div class="grid-cell" style="color:${{healthColor(totalPct)}};font-weight:600" title="Total: ${{sc.total}}/8">${{sc.total}}/8</div>`;
        }} else {{
            dims.forEach(d => {{
                cells += `<div class="grid-cell" style="${{cellStyle(s[d])}}" title="${{d}}: ${{s[d]}}"></div>`;
            }});
            cells += `<div class="grid-cell" style="color:#6e7681">—</div>`;
        }}
        gridHtml += `<div class="grid-row">
            <div class="grid-id">STRAT-${{sid}}</div>
            ${{cells}}
            <div class="grid-verdict ${{verdictClass(s.recommendation)}}">${{verdictLabel(s.recommendation)}}</div>
        </div>`;
    }});
    gridHtml += '</div>';

    html += `<div class="two-col">${{dimHtml}}${{gridHtml}}</div>`;

    // Summary table
    html += `<table><thead><tr>
        <th></th><th>Strat ID</th><th>Title</th><th>Source RFE</th><th>Size</th>
        <th>F</th><th>T</th><th>S</th><th>A</th><th>Score</th><th>Verdict</th><th>Attention</th>
    </tr></thead><tbody>`;
    r.strategies.forEach((s, i) => {{
        let badges = '';
        if (s.baseline) badges += ' <span class="badge badge-baseline">baseline</span>';
        if (s.cross_component) badges += ' <span class="badge badge-cross">cross-component</span>';
        const sc = s.scores;
        let scoreCells = '';
        if (sc) {{
            ['feasibility','testability','scope','architecture'].forEach(d => {{
                const v = sc[d];
                scoreCells += `<td style="${{scoreStyle(v)}};text-align:center;font-weight:600">${{v !== null && v !== undefined ? v : '—'}}</td>`;
            }});
            const totalPct = sc.total !== null ? Math.round(sc.total / 8 * 100) : 0;
            scoreCells += `<td style="color:${{healthColor(totalPct)}};font-weight:600;text-align:center">${{sc.total}}/8</td>`;
        }} else {{
            scoreCells += `<td class="${{verdictClass(s.feasibility)}}">${{verdictLabel(s.feasibility)}}</td>`;
            scoreCells += `<td class="${{verdictClass(s.testability)}}">${{verdictLabel(s.testability)}}</td>`;
            scoreCells += `<td class="${{verdictClass(s.scope)}}">${{verdictLabel(s.scope)}}</td>`;
            scoreCells += `<td class="${{verdictClass(s.architecture)}}">${{verdictLabel(s.architecture)}}</td>`;
            scoreCells += `<td style="color:#6e7681;text-align:center">—</td>`;
        }}
        const attentionHtml = s.needs_attention
            ? '<span style="color:#f85149;font-weight:600">&#9679; Yes</span>'
            : '<span style="color:#3fb950">&#10003;</span>';
        html += `<tr class="clickable" onclick="toggleRunDetail(${{idx}},${{i}})">
            <td><span class="expand-icon" id="ricon-${{idx}}-${{i}}">&#9654;</span></td>
            <td><strong>${{s.strat_id}}</strong></td>
            <td>${{s.title}}${{badges}}</td>
            <td>${{s.source_rfe}}</td>
            <td><span class="badge badge-size">${{s.size}}</span></td>
            ${{scoreCells}}
            <td class="${{verdictClass(s.recommendation)}}">${{verdictLabel(s.recommendation)}}</td>
            <td style="text-align:center">${{attentionHtml}}</td>
        </tr>
        <tr><td colspan="10" style="padding:0">
            <div class="detail-panel" id="rpanel-${{idx}}-${{i}}">
                <h2>${{s.strat_id}}: ${{s.title}}</h2>
                <div class="label-bar">${{renderLabelBadges(s.labels || [])}}</div>
                <div class="detail-tabs">
                    <div class="detail-tab active" onclick="switchRunTab(${{idx}},${{i}},'rfe')">Original RFE</div>
                    <div class="detail-tab" onclick="switchRunTab(${{idx}},${{i}},'strategy')">Strategy (AI Generated)</div>
                    <div class="detail-tab" onclick="switchRunTab(${{idx}},${{i}},'comment')">Review Summary (Jira Comment)</div>
                    <div class="detail-tab" onclick="switchRunTab(${{idx}},${{i}},'review')">Detailed Review</div>
                </div>
                <div class="tab-content active" id="rtab-${{idx}}-${{i}}-rfe">${{s.rfe_html || '<em>No RFE content available</em>'}}</div>
                <div class="tab-content" id="rtab-${{idx}}-${{i}}-strategy">${{s.strategy_html}}</div>
                <div class="tab-content" id="rtab-${{idx}}-${{i}}-comment">${{s.comment_html || '<em>No review comment available</em>'}}</div>
                <div class="tab-content" id="rtab-${{idx}}-${{i}}-review">${{s.review_html}}</div>
            </div>
        </td></tr>`;
    }});
    html += '</tbody></table>';

    // Skipped RFEs section — show all skipped for this run (debug view)
    if (runSkippedAll.length > 0) {{
        html += `<div style="margin-top:24px;padding:16px;background:#1c1917;border:1px solid #d29922;border-radius:8px;">
            <h3 style="color:#d29922;margin:0 0 12px;">Skipped RFEs (${{runSkippedAll.length}})</h3>
            <p style="color:#8b949e;margin-bottom:12px;">All RFEs skipped during this run with reason</p>
            <table><thead><tr>
                <th>RFE Key</th><th>Title</th><th>Current Labels</th><th style="color:#f85149">Reason</th>
            </tr></thead><tbody>`;
        runSkippedAll.forEach(s => {{
            html += `<tr><td>${{s.rfe_key}}</td><td>${{s.title}}</td><td>${{s.labels}}</td><td style="color:#f85149">${{s.missing}}</td></tr>`;
        }});
        html += '</tbody></table></div>';
    }}

    el.innerHTML = html;
}}

function toggleRunDetail(runIdx, stratIdx) {{
    const panel = document.getElementById(`rpanel-${{runIdx}}-${{stratIdx}}`);
    const icon = document.getElementById(`ricon-${{runIdx}}-${{stratIdx}}`);
    panel.classList.toggle('open');
    icon.classList.toggle('open');
}}

function toggleExecDetail(idx) {{
    const panel = document.getElementById(`epanel-${{idx}}`);
    const icon = document.getElementById(`eicon-${{idx}}`);
    panel.classList.toggle('open');
    icon.classList.toggle('open');
}}

function switchExecTab(idx, tab) {{
    const panel = document.getElementById(`epanel-${{idx}}`);
    panel.querySelectorAll('.detail-tab').forEach(t => t.classList.remove('active'));
    panel.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(`etab-${{idx}}-${{tab}}`).classList.add('active');
    event.target.classList.add('active');
}}

function switchRunTab(runIdx, stratIdx, tab) {{
    const panel = document.getElementById(`rpanel-${{runIdx}}-${{stratIdx}}`);
    panel.querySelectorAll('.detail-tab').forEach(t => t.classList.remove('active'));
    panel.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(`rtab-${{runIdx}}-${{stratIdx}}-${{tab}}`).classList.add('active');
    event.target.classList.add('active');
}}

// ─── Chart.js initialization ─────────────────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#21262d';

let _chartInstances = [];
function initCharts() {{
    _chartInstances.forEach(c => c.destroy());
    _chartInstances = [];
    const labels = RUNS.map(r => r.label);

    // Approval rate trend
    _chartInstances.push(new Chart(document.getElementById('chart-approval'), {{
        type: 'line',
        data: {{
            labels,
            datasets: [{{
                label: 'Approval Rate %',
                data: RUNS.map(r => r.approval_rate),
                borderColor: '#3fb950',
                backgroundColor: 'rgba(63,185,80,0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 6,
                pointHoverRadius: 8,
                pointBackgroundColor: '#3fb950',
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ display: false }},
            }},
            scales: {{
                y: {{
                    min: 0, max: 100,
                    ticks: {{ callback: v => v + '%' }},
                    grid: {{ color: '#161b22' }},
                }},
                x: {{
                    grid: {{ display: false }},
                }},
            }},
        }},
    }}));

    // Strategies per run
    _chartInstances.push(new Chart(document.getElementById('chart-volume'), {{
        type: 'bar',
        data: {{
            labels,
            datasets: [{{
                label: 'Approved',
                data: RUNS.map(r => r.approved),
                backgroundColor: '#3fb950',
                borderRadius: 4,
            }}, {{
                label: 'Revise',
                data: RUNS.map(r => r.revise),
                backgroundColor: '#d29922',
                borderRadius: 4,
            }}, {{
                label: 'Reject',
                data: RUNS.map(r => r.reject || 0),
                backgroundColor: '#f85149',
                borderRadius: 4,
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }},
            }},
            scales: {{
                x: {{ stacked: true, grid: {{ display: false }} }},
                y: {{ stacked: true, grid: {{ color: '#161b22' }}, ticks: {{ stepSize: 1 }} }},
            }},
        }},
    }}));

    // Dimension trends
    const dimColors = {{
        feasibility: '#58a6ff',
        testability: '#d29922',
        scope: '#3fb950',
        architecture: '#f78166',
    }};
    _chartInstances.push(new Chart(document.getElementById('chart-dimensions'), {{
        type: 'line',
        data: {{
            labels,
            datasets: ['feasibility','testability','scope','architecture'].map(dim => ({{
                label: dim.charAt(0).toUpperCase() + dim.slice(1),
                data: RUNS.map(r => r.dimensions[dim].rate),
                borderColor: dimColors[dim],
                backgroundColor: 'transparent',
                tension: 0.3,
                pointRadius: 5,
                pointHoverRadius: 7,
                pointBackgroundColor: dimColors[dim],
            }}))
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }},
            }},
            scales: {{
                y: {{
                    min: 0, max: 100,
                    ticks: {{ callback: v => v + '%' }},
                    grid: {{ color: '#161b22' }},
                }},
                x: {{ grid: {{ display: false }} }},
            }},
        }},
    }}));

    // Cumulative strategies
    const cumulative = [];
    let cumSum = 0;
    RUNS.forEach(r => {{ cumSum += r.reviewed; cumulative.push(cumSum); }});
    _chartInstances.push(new Chart(document.getElementById('chart-cumulative'), {{
        type: 'line',
        data: {{
            labels,
            datasets: [{{
                label: 'Cumulative Strategies',
                data: cumulative,
                borderColor: '#a371f7',
                backgroundColor: 'rgba(163,113,247,0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 6,
                pointHoverRadius: 8,
                pointBackgroundColor: '#a371f7',
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ display: false }},
            }},
            scales: {{
                y: {{
                    beginAtZero: true,
                    grid: {{ color: '#161b22' }},
                }},
                x: {{ grid: {{ display: false }} }},
            }},
        }},
    }}));

    // Average score trend
    _chartInstances.push(new Chart(document.getElementById('chart-avg-score'), {{
        type: 'line',
        data: {{
            labels,
            datasets: [{{
                label: 'Avg Score',
                data: RUNS.map(r => r.avg_total_score),
                borderColor: '#58a6ff',
                backgroundColor: 'rgba(88,166,255,0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 6,
                pointHoverRadius: 8,
                pointBackgroundColor: '#58a6ff',
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ display: false }},
                annotation: {{
                    annotations: {{
                        threshold: {{
                            type: 'line',
                            yMin: 6, yMax: 6,
                            borderColor: '#3fb950',
                            borderWidth: 2,
                            borderDash: [6, 3],
                            label: {{
                                display: true,
                                content: 'Approval threshold (6)',
                                position: 'start',
                                color: '#3fb950',
                                font: {{ size: 11 }},
                                backgroundColor: 'rgba(0,0,0,0.7)',
                            }}
                        }}
                    }}
                }}
            }},
            scales: {{
                y: {{
                    min: 0, max: 8,
                    ticks: {{ stepSize: 1 }},
                    grid: {{ color: '#161b22' }},
                }},
                x: {{ grid: {{ display: false }} }},
            }},
        }},
    }}));

    // Empty SME Input rate trend (null runs render as gaps, not 0%)
    _chartInstances.push(new Chart(document.getElementById('chart-sme-empty'), {{
        type: 'line',
        data: {{
            labels,
            datasets: [{{
                label: 'Empty SME %',
                data: RUNS.map(r => r.sme_empty_rate),
                borderColor: '#d29922',
                backgroundColor: 'rgba(210,153,34,0.1)',
                fill: true,
                tension: 0.3,
                spanGaps: false,
                pointRadius: 6,
                pointHoverRadius: 8,
                pointBackgroundColor: '#d29922',
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                y: {{
                    min: 0, max: 100,
                    ticks: {{ callback: v => v + '%' }},
                    grid: {{ color: '#161b22' }},
                }},
                x: {{ grid: {{ display: false }} }},
            }},
        }},
    }}));

    // Avg refine iterations trend (authoritative-only; gaps where uninstrumented)
    _chartInstances.push(new Chart(document.getElementById('chart-refine-iter'), {{
        type: 'line',
        data: {{
            labels,
            datasets: [{{
                label: 'Avg Refine Iterations',
                data: RUNS.map(r => r.avg_refine_iter),
                borderColor: '#58a6ff',
                backgroundColor: 'rgba(88,166,255,0.1)',
                fill: true,
                tension: 0.3,
                spanGaps: false,
                pointRadius: 6,
                pointHoverRadius: 8,
                pointBackgroundColor: '#58a6ff',
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                y: {{
                    min: 0,
                    ticks: {{ precision: 1 }},
                    grid: {{ color: '#161b22' }},
                }},
                x: {{ grid: {{ display: false }} }},
            }},
        }},
    }}));

    // Cost per run (stacked bar: create / refine / review)
    const costRuns = RUNS.filter(r => r.cost && r.cost.total_usd > 0);
    if (costRuns.length > 0) {{
        const costLabels = costRuns.map(r => `${{r.label}} (${{r.reviewed}})`);
        _chartInstances.push(new Chart(document.getElementById('chart-cost'), {{
            type: 'bar',
            data: {{
                labels: costLabels,
                datasets: [{{
                    label: 'Create',
                    data: costRuns.map(r => r.cost.create_usd || 0),
                    backgroundColor: '#58a6ff',
                    borderRadius: 2,
                }}, {{
                    label: 'Refine',
                    data: costRuns.map(r => r.cost.refine_usd || 0),
                    backgroundColor: '#d29922',
                    borderRadius: 2,
                }}, {{
                    label: 'Review',
                    data: costRuns.map(r => r.cost.review_usd || 0),
                    backgroundColor: '#f78166',
                    borderRadius: 2,
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }},
                    tooltip: {{
                        callbacks: {{
                            label: ctx => `${{ctx.dataset.label}}: $${{ctx.raw.toFixed(2)}}`,
                            footer: items => {{
                                const r = costRuns[items[0].dataIndex];
                                const total = items.reduce((s, i) => s + i.raw, 0);
                                return `Total: $${{total.toFixed(2)}} | ${{r.reviewed}} strategies ($${{(total / (r.reviewed || 1)).toFixed(2)}}/strat)`;
                            }},
                        }}
                    }}
                }},
                scales: {{
                    x: {{ stacked: true, grid: {{ display: false }} }},
                    y: {{ stacked: true, grid: {{ color: '#161b22' }}, ticks: {{ callback: v => '$' + v }} }},
                }},
            }},
        }}));

        // Cost per strategy
        _chartInstances.push(new Chart(document.getElementById('chart-cost-per-strat'), {{
            type: 'line',
            data: {{
                labels: costLabels,
                datasets: [{{
                    label: '$/Strategy',
                    data: costRuns.map(r => r.cost.cost_per_strategy || (r.cost.total_usd / (r.reviewed || 1))),
                    borderColor: '#bc8cff',
                    backgroundColor: 'rgba(188,140,255,0.1)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 6,
                    pointHoverRadius: 8,
                    pointBackgroundColor: '#bc8cff',
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ display: false }},
                    tooltip: {{
                        callbacks: {{
                            label: ctx => `$${{ctx.raw.toFixed(2)}} per strategy`,
                        }}
                    }}
                }},
                scales: {{
                    y: {{
                        min: 0,
                        grid: {{ color: '#161b22' }},
                        ticks: {{ callback: v => '$' + v }},
                    }},
                    x: {{ grid: {{ display: false }} }},
                }},
            }},
        }}));
    }}
}}

// ─── Diagram zoom/pan ────────────────────────────────────────────────────────
let diagramScale = 1, diagramX = 0, diagramY = 0;
let isDragging = false, startX, startY;

function zoomDiagram(factor) {{
    diagramScale *= factor;
    diagramScale = Math.max(0.3, Math.min(3, diagramScale));
    updateDiagramTransform();
}}
function resetDiagram() {{
    diagramScale = 1; diagramX = 0; diagramY = 0;
    updateDiagramTransform();
}}
function updateDiagramTransform() {{
    const inner = document.getElementById('diagram-inner');
    if (inner) inner.style.transform = `translate(${{diagramX}}px, ${{diagramY}}px) scale(${{diagramScale}})`;
}}

const dContainer = document.getElementById('diagram-container');
if (dContainer) {{
    dContainer.addEventListener('wheel', (e) => {{ e.preventDefault(); zoomDiagram(e.deltaY < 0 ? 1.1 : 0.9); }}, {{ passive: false }});
    dContainer.addEventListener('mousedown', (e) => {{ isDragging = true; startX = e.clientX - diagramX; startY = e.clientY - diagramY; }});
    document.addEventListener('mousemove', (e) => {{
        if (!isDragging) return;
        diagramX = e.clientX - startX; diagramY = e.clientY - startY;
        const inner = document.getElementById('diagram-inner');
        inner.style.transition = 'none'; updateDiagramTransform(); inner.style.transition = 'transform 0.1s ease';
    }});
    document.addEventListener('mouseup', () => {{ isDragging = false; }});
}}

// ─── Init ────────────────────────────────────────────────────────────────────
renderExecutiveSummary();
renderOverviewKPIs();
renderAttention();
renderBlocked();
renderProcessing();
buildRunList();
buildRunSelector();
initCharts();
</script>

<script type="module">
    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
    mermaid.initialize({{ startOnLoad: false, theme: 'dark', flowchart: {{ useMaxWidth: false, htmlLabels: true, curve: 'basis' }} }});
    let mermaidRendered = false;
    const origSwitchPage = window.switchPage || switchPage;
    // Patch switchPage to render mermaid on first Pipeline tab click
    const _origSwitch = switchPage;
    window._renderMermaid = async function() {{
        if (mermaidRendered) return;
        mermaidRendered = true;
        await mermaid.run();
        const svg = document.querySelector('.mermaid svg');
        const ctr = document.getElementById('diagram-container');
        if (svg && ctr) {{
            const svgRect = svg.getBoundingClientRect();
            const ctrRect = ctr.getBoundingClientRect();
            const scaleX = ctrRect.width / svgRect.width;
            const scaleY = ctrRect.height / svgRect.height;
            diagramScale = Math.min(scaleX, scaleY, 2) * 0.9;
            diagramX = (ctrRect.width - svgRect.width * diagramScale) / 2;
            diagramY = (ctrRect.height - svgRect.height * diagramScale) / 2;
            updateDiagramTransform();
        }}
    }};
    // Observe when pipeline page becomes visible
    const obs = new MutationObserver(() => {{
        const pp = document.getElementById('page-pipeline');
        if (pp && pp.classList.contains('active') && !mermaidRendered) window._renderMermaid();
    }});
    obs.observe(document.getElementById('page-pipeline'), {{ attributes: true, attributeFilter: ['class'] }});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    print(f"Dashboard generated: {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def _query_jira_kpis():
    """Query Jira for pipeline label counts. Returns dict or None."""
    try:
        from jira_utils import query_label_counts, require_env
        server, user, token = require_env()
        if not all([server, user, token]):
            print("Warning: JIRA env vars not set, skipping Jira KPIs",
                  file=sys.stderr)
            return None
        labels = [
            "strat-creator-auto-created",
            "strat-creator-auto-refined",
            "strat-creator-needs-attention",
            "strat-creator-rubric-pass",
            "strat-creator-human-sign-off",
        ]
        counts = query_label_counts(server, user, token, labels)
        print(f"Jira KPIs: {counts}")
        return counts
    except Exception as e:
        print(f"Warning: Jira KPI query failed: {e}", file=sys.stderr)
        return None


def _query_processing_issues():
    """Query Jira for RFEs with strat-creator-processing label.

    Returns list of {key, summary, started} dicts where started is the
    ISO timestamp when the label was added (from changelog), or None.
    """
    try:
        from jira_utils import api_call_with_retry, require_env, search_issues
        server, user, token = require_env()
        if not all([server, user, token]):
            print("Warning: JIRA env vars not set, skipping processing query",
                  file=sys.stderr)
            return None
        issues = search_issues(
            server, user, token,
            'labels = "strat-creator-processing"',
            fields=["key", "summary"],
        )
        result = []
        for issue in issues:
            key = issue["key"]
            summary = issue["fields"].get("summary", "")
            started = None
            try:
                data = api_call_with_retry(
                    server,
                    f"/issue/{key}/changelog",
                    user, token,
                )
                for entry in reversed(data.get("values", [])):
                    for item in entry.get("items", []):
                        if (item.get("field") == "labels"
                                and "strat-creator-processing"
                                in (item.get("toString") or "")):
                            started = entry.get("created")
                            break
                    if started:
                        break
            except Exception as e:
                print(f"Warning: changelog fetch failed for {key}: {e}",
                      file=sys.stderr)
            result.append({"key": key, "summary": summary, "started": started})
        print(f"Processing issues: {len(result)}")
        return result
    except Exception as e:
        print(f"Warning: processing query failed: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="Generate multi-run strategy dashboard")
    parser.add_argument("--data-dir", required=True,
                        help="Path to RHAISTRAT/ directory containing timestamped runs")
    parser.add_argument("--config", "-c", default=None,
                        help="Optional config file for size/baseline metadata")
    parser.add_argument("--output", "-o", default="/tmp/dashboard/index.html",
                        help="Output HTML file path")
    parser.add_argument("--max-runs", type=int, default=None,
                        help="Maximum number of runs to include (default: all)")
    parser.add_argument("--jira-kpis", action="store_true",
                        help="Query Jira for live label counts in executive summary")
    args = parser.parse_args()

    # Load config (optional — provides size/baseline metadata)
    config = {}
    if args.config and os.path.exists(args.config):
        try:
            config = load_yaml_config(args.config)
        except Exception as e:
            print(f"Warning: failed to read config: {e}", file=sys.stderr)

    # Query Jira for live KPIs and processing status (optional)
    jira_counts = _query_jira_kpis() if args.jira_kpis else None
    processing_issues = _query_processing_issues() if args.jira_kpis else None

    # Scan all runs
    print(f"Scanning {args.data_dir} for runs...")
    runs = scan_all_runs(args.data_dir, config, max_runs=args.max_runs)

    if not runs:
        print("Error: no valid runs found", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(runs)} run(s)")
    compute_deltas(runs)
    compute_metric_trends(runs)
    exec_summary = compute_executive_summary(runs)
    print(f"Executive summary: {exec_summary['total']} unique strategies "
          f"({exec_summary['approved']} approved, {exec_summary['needs_attention']} need attention)")
    generate_dashboard(runs, exec_summary, args.output, jira_counts,
                       processing_issues)


if __name__ == "__main__":
    main()
