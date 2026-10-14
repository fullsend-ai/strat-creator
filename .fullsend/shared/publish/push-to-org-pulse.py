#!/usr/bin/env python3
"""Push strategy pipeline data to the org pulse feature bulk API.

Reads the summary-production.json from the strat-pipeline-data repo and
POSTs strategy entries to the org pulse bulk upsert endpoint in batches.

This script is designed to be non-blocking: it always exits 0, logging
warnings on failure so the CI pipeline continues.

Auth: reads ORG_PULSE_API_TOKEN from the environment (never passed as a
CLI argument to avoid exposing it in /proc/cmdline or job traces).

Usage:
    # In CI (token and URL from env vars):
    python3 ci-scripts/push-to-org-pulse.py --results-dir /tmp/claude-workdir/artifacts

    # Local testing:
    ORG_PULSE_URL=https://... ORG_PULSE_API_TOKEN=tt_... \
        python3 ci-scripts/push-to-org-pulse.py --results-dir ./strat-pipeline-data

    # Dry run (no env vars needed):
    python3 ci-scripts/push-to-org-pulse.py --results-dir ./strat-pipeline-data --dry-run

Dependencies: Python stdlib only (no pip packages).
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# Source: strat-pipeline ci-scripts/push-to-org-pulse.py at fd36b15c5095c9f20a270b1d69933c578c04d9da.
# Adapted: retain normal certificate and hostname verification; the CI image
# must trust the configured service CA through its system store/SSL_CERT_FILE.

BATCH_SIZE = 25
REQUEST_TIMEOUT = 60


def find_summary_file(results_dir):
    """Find summary-production.json in the RHAISTRAT directory."""
    path = os.path.join(results_dir, "RHAISTRAT", "summary-production.json")
    if os.path.isfile(path):
        return path
    return None


def load_strategies(summary_path):
    """Load strategies from summary-production.json."""
    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    strategies = data.get("summary", {}).get("strategies", [])
    return strategies


def build_features(strategies):
    """Transform strategy entries into org pulse feature objects.

    The org pulse validation layer handles snake_case -> camelCase normalization
    (strat_id -> key, source_rfe -> sourceRfe, needs_attention -> needsAttention,
    run_id -> runId, run_timestamp -> runTimestamp).

    We pass the data through as-is since the API accepts snake_case fields.
    """
    features = []
    for strat in strategies:
        # The API requires 'key' — map strat_id to key
        feature = {
            "key": strat.get("strat_id", ""),
            "title": strat.get("title", ""),
            "source_rfe": strat.get("source_rfe", ""),
            "priority": strat.get("priority", "Normal"),
            "status": strat.get("status", ""),
            "size": strat.get("size"),
            "recommendation": strat.get("recommendation", ""),
            "needs_attention": strat.get("needs_attention", False),
            "scores": strat.get("scores", {}),
            "reviewers": strat.get("reviewers", {}),
            "labels": strat.get("labels", []),
            "run_id": strat.get("run_id", ""),
            "run_timestamp": strat.get("run_timestamp", ""),
        }
        features.append(feature)

    return features


def push_batch(api_url, api_token, batch, batch_num, num_batches):
    """POST a single batch of features to the org pulse bulk endpoint."""
    url = f"{api_url.rstrip('/')}/api/modules/ai-impact/features/bulk"
    payload = json.dumps({"features": batch}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        },
        method="POST",
    )

    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return False, f"HTTP {resp.status}: Response is not valid JSON (first 200 chars): {raw[:200]}"
            return True, body
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body = "(could not read response)"
        if status in (401, 403):
            return False, f"Authentication failed (HTTP {status}). Check ORG_PULSE_API_TOKEN CI variable."
        return False, f"HTTP {status}: {body}"
    except urllib.error.URLError as e:
        return False, f"Network error: {e.reason}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def push_to_org_pulse(api_url, api_token, features):
    """Push features to org pulse in batches."""
    totals = {"created": 0, "updated": 0, "unchanged": 0}
    entry_errors = []
    batch_errors = []

    num_batches = (len(features) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(features), BATCH_SIZE):
        batch = features[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        print(f"  Sending batch {batch_num}/{num_batches} ({len(batch)} entries)...")
        success, result = push_batch(api_url, api_token, batch, batch_num, num_batches)

        if success:
            totals["created"] += result.get("created", 0)
            totals["updated"] += result.get("updated", 0)
            totals["unchanged"] += result.get("unchanged", 0)
            entry_errors.extend(result.get("errors", []))
        else:
            batch_errors.append(f"Batch {batch_num}: {result}")
            if "Authentication failed" in str(result):
                break

    return totals, entry_errors, batch_errors


def main():
    parser = argparse.ArgumentParser(description="Push strategy data to org pulse")
    parser.add_argument("--results-dir", default="artifacts",
                        help="Base directory of the cloned strat-pipeline-data repo")
    parser.add_argument("--api-url", default=os.environ.get("ORG_PULSE_URL", ""),
                        help="Org pulse base URL (default: $ORG_PULSE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Transform data and print sample, skip API call")
    args = parser.parse_args()

    api_token = os.environ.get("ORG_PULSE_API_TOKEN", "")

    if not args.api_url and not args.dry_run:
        print("INFO: ORG_PULSE_URL not set, skipping org pulse push")
        sys.exit(0)

    if not api_token and not args.dry_run:
        print("INFO: ORG_PULSE_API_TOKEN not set, skipping org pulse push")
        sys.exit(0)

    # Find summary-production.json
    summary_path = find_summary_file(args.results_dir)
    if summary_path is None:
        print(f"INFO: {args.results_dir}/RHAISTRAT/summary-production.json not found, skipping org pulse push")
        sys.exit(0)

    # Load strategies
    strategies = load_strategies(summary_path)
    if not strategies:
        print("INFO: No strategies found in summary-production.json")
        sys.exit(0)

    # Build feature payloads
    features = build_features(strategies)
    print(f"Prepared {len(features)} features for org pulse")

    if args.dry_run:
        print("DRY RUN: Skipping API call")
        print(f"  Total entries: {len(features)}")
        print(f"  Would send in {(len(features) + BATCH_SIZE - 1) // BATCH_SIZE} batches of up to {BATCH_SIZE}")
        print(f"  Sample entry:\n{json.dumps(features[0], indent=2)[:600]}")
        sys.exit(0)

    totals, entry_errors, batch_errors = push_to_org_pulse(args.api_url, api_token, features)

    print(f"Org pulse push complete: {totals['created']} created, {totals['updated']} updated, {totals['unchanged']} unchanged")

    if batch_errors:
        print(f"WARNING: {len(batch_errors)} batch(es) failed:")
        for err in batch_errors:
            print(f"  - {err}")

    if entry_errors:
        print(f"WARNING: {len(entry_errors)} entries had validation errors:")
        for err in entry_errors[:5]:
            print(f"  - {err.get('key', '?')}: {err.get('errors', err.get('error', '?'))}")
        if len(entry_errors) > 5:
            print(f"  ... and {len(entry_errors) - 5} more")

    sys.exit(0)


if __name__ == "__main__":
    main()
