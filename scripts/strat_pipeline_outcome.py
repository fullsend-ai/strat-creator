#!/usr/bin/env python3
"""Business completion and read-only native runtime outcome, without model calls.

All repository files are data. Host callers import this from their trusted
checkout, never from the downloaded agent repository. Runtime observations are
separate from business validation; CLI exit alone is never a completion proof.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from artifact_utils import read_frontmatter_validated
from review_composition import verify_review

RECEIPT = "tmp/strat-completion.json"
CONTRACT = "strat-business-validation-v1"


def regular(root, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError(f"unsafe input path: {relative}")
    path = root / relative
    if any((root / Path(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)) or not path.is_file():
        raise ValueError(f"missing or symlinked input: {relative}")
    return path


def hashed(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"refusing symlinked output: {path.name}")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".completion-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def business_errors(root, data, progress, result):
    """Check coverage, saved marks and actual inputs/review provenance.

    This verifies completion/consistency, not strategy-content accuracy or that
    a recorded push is visible in Jira. Trusted host validation reads Jira back.
    """
    errors = []
    from strat_freshness import verify_progress
    try:
        verify_progress(root, data, progress)
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))

    def check(condition, message):
        if not condition:
            errors.append(message)

    try:
        acquired = data["acquired_keys"]
        if not isinstance(acquired, list) or not all(
            isinstance(k, str) and re.fullmatch(r"RHAIRFE-\d+", k) for k in acquired
        ):
            raise ValueError("invalid acquired_keys")
        check(len(acquired) == len(set(acquired)), "duplicate acquired RFEs")
        check(set(acquired) <= set(data.get("input_keys", [])), "acquired keys exceed input inventory")
        check(progress.get("run_id") == data["run_id"], "progress belongs to another run")
        create = progress.get("create") or {}
        pairs = create.get("strategies", [])
        skips = create.get("skipped", [])
        if not isinstance(pairs, list) or not isinstance(skips, list):
            raise ValueError("invalid create inventory")
        keys = [p["strat"] for p in pairs]
        rfes = [p["rfe"] for p in pairs]
        check(len(keys) == len(set(keys)) and len(rfes) == len(set(rfes)), "create mapping is not one strategy per RFE")
        check(set(rfes) <= set(acquired), "created strategy is outside the acquired inventory")
        expected_skips = {s["key"] for s in data.get("expected_skips", [])}
        skipped = [s["key"] for s in skips]
        check(len(skipped) == len(set(skipped)), "duplicate create skips")
        check(set(skipped) <= expected_skips & set(acquired), "create skip was not expected by the host")
        check(not set(skipped) & set(rfes), "RFE has both a strategy and a skip")
        check(set(rfes) | set(skipped) == set(acquired), "create inventory does not cover all acquired RFEs")
        check(timestamp(create.get("done_at")), "create completion is not recorded")
        check(not create.get("pending"), "create still has pending RFEs")
        refined, reviews = progress["refine"], progress["review"]
        if not isinstance(refined, dict) or not isinstance(reviews, dict):
            raise ValueError("invalid refine/review progress")
        check(set(refined) <= set(keys) and set(reviews) <= set(keys), "progress contains an unassigned strategy")
        covered = (progress.get("push") or {}).get("strategies", [])
        check(isinstance(covered, list) and set(covered) <= set(keys), "push coverage contains an unassigned strategy")
        listed = result["artifacts"]
        if not isinstance(listed, list):
            raise ValueError("invalid artifact inventory")
        for relative in listed:
            check(relative.startswith("artifacts/"), "listed output is outside artifacts")
            regular(root, relative)
        decisions = []
        for pair in pairs:
            key, rfe = pair["strat"], pair["rfe"]
            if not isinstance(key, str) or not re.fullmatch(r"(?:RHAISTRAT|STRAT)-\d+", key):
                raise ValueError("invalid strategy key")
            task = regular(root, f"artifacts/strat-tasks/{key}.md")
            fm, body = read_frontmatter_validated(task, "strat-task")
            check(fm.get("strat_id") == key and fm.get("source_rfe") == rfe, f"{key}: task identity/source mismatch")
            check(not key.startswith("RHAISTRAT-") or fm.get("jira_key") == key, f"{key}: Jira key mismatch")
            check(fm.get("status") in ("Refined", "Reviewed"), f"{key}: strategy is not refined")
            section = re.search(r"^## Strategy(?:[ \t][^\n]*)?\r?\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
            check(
                bool(section and re.sub(r"<!--.*?-->", "", section[1], flags=re.S).strip())
                and "To be filled by /strategy-refine" not in body,
                f"{key}: strategy body is missing or a placeholder",
            )
            if data.get("resume_points", {}).get(rfe) != "review":
                check(timestamp(refined.get(key)), f"{key}: refinement completion missing")
                push = progress.get("push") or {}
                check(timestamp(push.get("done_at")) and key in covered, f"{key}: push completion missing")
                check(bool(data.get("dry_run")) or not push.get("skipped"), f"{key}: push skipped outside dry run")
            review = reviews.get(key)
            check(isinstance(review, dict) and timestamp(review.get("done_at")), f"{key}: review completion missing")
            manifest = verify_review(root, key)
            check(manifest.get("workflow_run_id") == data["run_id"], f"{key}: review belongs to another run")
            check(
                isinstance(review, dict) and review.get("composition_session") == manifest["session_id"],
                f"{key}: recorded review has another composition session",
            )
            decision = manifest["decision"]
            check(
                isinstance(review, dict)
                and review.get("recommendation") == decision["recommendation"]
                and type(review.get("needs_attention")) is bool
                and review["needs_attention"] == decision["needs_attention"],
                f"{key}: review progress disagrees with numeric decision",
            )
            decisions.append({"rfe": rfe, "strat": key, **decision})
            required = [
                f"artifacts/strat-tasks/{key}.md",
                f"artifacts/strat-reviews/{key}-review-provenance.json",
                manifest["review"]["path"],
                manifest["scorer"]["path"],
                *(entry["path"] for entry in manifest["reports"].values()),
            ]
            check(set(required) <= set(listed), f"{key}: required outputs omitted from artifact inventory")
        check(result["strategies"] == decisions, "result decisions disagree with verified saved reviews")
        check(
            result["skipped"] == data.get("skipped", []) + skips, "result skip inventory disagrees with saved coverage"
        )
        for field in ("run_id", "mode", "input_keys", "acquired_keys", "dry_run", "architecture_context"):
            check(result.get(field) == data.get(field), f"result {field} disagrees with run input")
        if not errors:
            check(result["completed_phases"] == ["create", "refine", "push", "review"], "result omits completed phases")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors.append(f"business validation: {error}")
    return list(dict.fromkeys(errors))


def summary(root, receipt):
    return {
        "kind": "strat-completion",
        "run_id": receipt["run_id"],
        "action": receipt["result"]["action"],
        "check_exit": receipt["check_exit"],
        "receipt_id": receipt["receipt_id"],
        "receipt_sha256": hashed(regular(root, RECEIPT)),
    }


def finalize(root, data, progress, result, reported_failures, output):
    errors = business_errors(root, data, progress, result)
    errors = list(dict.fromkeys([*result["errors"], *errors]))
    if errors:
        errors = list(
            dict.fromkeys(
                [*(f"{step}: {key or 'step'} returned no result" for step, key in reported_failures), *errors]
            )
        )
    result = {**result, "action": "failed" if errors else "completed", "errors": errors}
    if errors:
        result["summary"] = f"Business validation failed with {len(errors)} error(s); completed progress is retained."
    if not errors:
        try:
            previous = verify_completion(root)
            prior_result = {key: value for key, value in previous["result"].items() if key != "completion"}
            if prior_result == result:
                # Reporting recovery restores the same receipt/native identity.
                atomic_json(output, previous["result"])
                if hashed(output) != previous["result_sha256"]:
                    raise ValueError("restored result hash changed")
                return summary(root, previous)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    receipt_id = uuid.uuid4().hex
    result["completion"] = {"contract": CONTRACT, "receipt_id": receipt_id, "check_exit": 1 if errors else 0}
    atomic_json(output, result)
    paths = ["tmp/strat-input.json", "tmp/strat-progress.json", *result["artifacts"]]
    inputs = {}
    for relative in paths:
        try:
            inputs[relative] = hashed(regular(root, relative))
        except (OSError, ValueError):
            if not errors:
                raise
    receipt = {
        "contract": CONTRACT,
        "run_id": data["run_id"],
        "receipt_id": receipt_id,
        "check_exit": 1 if errors else 0,
        "result": result,
        "result_sha256": hashed(output),
        "inputs": inputs,
        "reported_failures": [list(item) for item in reported_failures],
    }
    atomic_json(root / RECEIPT, receipt)
    return summary(root, receipt)


def verify_completion(root, result_path=None, *, allow_failed=False):
    root = root.resolve()
    receipt = read_json(regular(root, RECEIPT))
    if receipt.get("contract") != CONTRACT:
        raise ValueError("unsupported completion contract")
    data = read_json(regular(root, "tmp/strat-input.json"))
    progress = read_json(regular(root, "tmp/strat-progress.json"))
    from strat_freshness import verify_progress
    verify_progress(root, data, progress)
    if receipt.get("run_id") != data.get("run_id"):
        raise ValueError("completion belongs to another run")
    for relative, identity in receipt["inputs"].items():
        if hashed(regular(root, relative)) != identity:
            raise ValueError(f"completion input changed: {relative}")
    result = receipt["result"]
    canonical_result = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    if hashlib.sha256(canonical_result).hexdigest() != receipt.get("result_sha256"):
        raise ValueError("saved result content differs from its completion hash")
    if result.get("completion") != {
        "contract": CONTRACT,
        "receipt_id": receipt["receipt_id"],
        "check_exit": receipt["check_exit"],
    }:
        raise ValueError("result completion identity mismatch")
    if result_path is not None and hashed(result_path) != receipt["result_sha256"]:
        raise ValueError("result file differs from the validated completion")
    if result["action"] != "completed" or receipt["check_exit"] != 0:
        if allow_failed and result["action"] == "failed" and receipt["check_exit"] == 1 and result["errors"]:
            return receipt
        raise ValueError("business completion failed: " + "; ".join(result["errors"])[:500])
    required = {"tmp/strat-input.json", "tmp/strat-progress.json", *result["artifacts"]}
    if set(receipt["inputs"]) != required:
        raise ValueError("completion input inventory is incomplete")
    errors = business_errors(root, data, progress, result)
    if errors or result["errors"]:
        raise ValueError("business completion inconsistent: " + "; ".join([*errors, *result["errors"]])[:500])
    return receipt


def observe(root, state_path, journal_path, cli_exit, result_path=None):
    errors, receipt = [], None
    try:
        receipt = verify_completion(root, result_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors.append(str(error))
    state, journal = {}, []
    try:
        state = read_json(state_path)
        journal = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
        if state.get("runId") != journal_path.parent.name:
            errors.append("native state and journal workflow IDs differ")
        if state.get("status") != "completed":
            errors.append("native Workflow is not completed: " + str(state.get("status")))
        expected = summary(root, receipt) if receipt else None
        if expected is None or state.get("result") != expected:
            errors.append("native Workflow result does not match validated completion")
        if not journal or journal[0].get("type") != "launched":
            errors.append("native launch journal missing")
        started, completion_agents = set(), set()
        for row in journal:
            identity = (row.get("key"), row.get("agentId"))
            if row.get("type") == "started":
                started.add(identity)
            if row.get("type") == "result" and row.get("result") == expected and all(identity) and identity in started:
                completion_agents.add(row["agentId"])
        completed_agents = {
            row.get("agentId") for row in state.get("workflowProgress", []) if row.get("state") == "done"
        }
        if expected is None or not completion_agents & completed_agents:
            errors.append("validated completion is absent from native result journal")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors.append("native evidence unavailable/invalid: " + str(error))
    accepted = not errors
    return {
        "kind": "strat-runtime-outcome",
        "business_completed": receipt is not None,
        "native_status": state.get("status", "unobserved"),
        "runtime_verified": accepted,
        "cli_exit": cli_exit,
        "reporting": "succeeded" if cli_exit == 0 else "failed_or_unconfirmed",
        "outcome": ("completed" if cli_exit == 0 else "completed_with_reporting_error")
        if accepted
        else "incomplete_or_unverified",
        "retry_work": receipt is None,
        "errors": errors,
        "completion": summary(root, receipt) if receipt else None,
        "journal_failures": sum(row.get("type") == "failed" for row in journal if isinstance(row, dict)),
    }


def guard(event):
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "StructuredOutput":
        return None
    value = event.get("tool_input", {})
    if value.get("kind") != "strat-completion":
        return None
    root = Path(event["cwd"]).resolve()
    receipt = verify_completion(root, allow_failed=True)
    expected = summary(root, receipt)
    if value != expected:
        raise ValueError("final completion disagrees with saved validation; return helper JSON exactly")
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--repo", type=Path, required=True)
    inspect.add_argument("--state", type=Path, required=True)
    inspect.add_argument("--journal", type=Path, required=True)
    inspect.add_argument("--cli-exit", type=int, required=True)
    inspect.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = observe(args.repo, args.state, args.journal, args.cli_exit, args.result)
        print(json.dumps(result))
        return 0 if result["runtime_verified"] else 1
    event, verified, error = {}, None, None
    try:
        event = json.load(sys.stdin)
        verified = guard(event)
    except (Exception, SystemExit) as exception:
        error = str(exception)
    if verified or error:
        path = Path(event.get("cwd", ".")) / "tmp/completion-checkpoints/events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "agent_id": event.get("agent_id"),
                        "tool_use_id": event.get("tool_use_id"),
                        "input": event.get("tool_input"),
                        "allowed": error is None,
                        "error": error,
                        "verified": verified,
                    }
                )
                + "\n"
            )
    if error:
        print("Completion checkpoint: " + error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
