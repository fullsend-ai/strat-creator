#!/usr/bin/env python3
"""Capture delivered review Skills and assemble an attributed numeric-gated review."""
import argparse
import fcntl
import hashlib
import json
import re
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from review_scope import TARGET, check_report, inputs, safe_path

ROLES = ("feasibility", "testability", "scope", "architecture")
POLICY = "numeric-only-v1"
AGENT = re.compile(r"[a-zA-Z0-9_-]+")
# A delivered assessment carries its target heading or a Recommendation field.
REPORT = re.compile(r"^(?:###\s+(?:RHAISTRAT|STRAT)-[0-9]+:|\*\*Recommendation\*\*:)", re.M)
HEADING = re.compile(r"^###\s+((?:RHAISTRAT|STRAT)-[0-9]+):", re.M)


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


def regular(root, path):
    path = safe_path(root, str(path))
    if not path.is_file():
        raise ValueError(f"Missing regular input: {path}")
    return path


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def locked(root, key):
    if not TARGET.fullmatch(key):
        raise ValueError("One exact strategy key is required")
    directory = safe_path(root, str(root / "tmp/review-composition" / key))
    directory.mkdir(parents=True, exist_ok=True)
    with safe_path(root, str(directory / "session.lock")).open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield directory


def numeric_decision(scores):
    values = [scores.get(role) for role in ROLES]
    if any(type(value) is not int or value not in (0, 1, 2) for value in values):
        raise ValueError("All four numeric scores must be integers from 0 to 2")
    total = sum(values)
    if type(scores.get("total")) is not int or scores["total"] != total:
        raise ValueError("Numeric score total does not add up")
    zeros = values.count(0)
    verdict = "approve" if total >= 6 and not zeros else "reject" if total < 3 or zeros >= 2 else "revise"
    return {"recommendation": verdict, "needs_attention": verdict != "approve"}


class ReportError(ValueError):
    """A delivered report whose verdict fields the reviewer can correct itself."""


VERDICT_MESSAGE = ("Report must contain one consistent Recommendation: approve, revise, reject "
                   "(architecture may be skipped)")


def report_verdicts(report):
    """Return the one consistent verdict field value; role-specific rules apply later."""
    # Keep every source byte; repeated agreeing fields express one decision.
    # Read every field so an invalid value cannot hide beside a valid one.
    values = re.findall(r"^\*\*Recommendation\*\*:[ \t]*([^\r\n]*)", report, re.M)
    verdicts = {value.strip() for value in values}
    if len(verdicts) != 1 or not verdicts <= {"approve", "revise", "reject", "skipped"}:
        raise ReportError(VERDICT_MESSAGE)
    return next(iter(verdicts))


def report_verdict(scope, role, report):
    check_report(scope, report)
    if report.startswith("REVIEW_SCOPE_ERROR:"):
        raise ValueError("A scope error is not a delivered assessment")
    verdict = report_verdicts(report)
    if verdict == "skipped" and role != "architecture":
        raise ReportError(VERDICT_MESSAGE)
    return verdict


def snapshot(root, key):
    scope = inputs(root, key)
    source = {"strategy": {"path": str(scope["strategy"].relative_to(root)),
                           "sha256": digest(scope["strategy"].read_bytes().decode())},
              "rfe": {"path": str(scope["rfe"].relative_to(root)),
                      "sha256": digest(scope["rfe"].read_bytes().decode())}}
    original = scope["strategy"].parent.parent / "strat-originals" / f"{key}.md"
    if original.exists() or original.is_symlink():
        original = regular(root, original)
        source["strategy_original"] = {"path": str(original.relative_to(root)),
                                       "sha256": digest(original.read_bytes().decode())}
    return scope, source


def begin(root, key, scorer):
    from strat_freshness import source_rfe, verify
    verify(root, rfe=source_rfe(root, key))
    from artifact_utils import read_frontmatter_validated
    scope, source = snapshot(root, key)
    fm, _ = read_frontmatter_validated(scope["prior"], "strat-review")
    decision = numeric_decision(fm["scores"])
    if fm.get("strat_id") != key or any(fm.get(field) != value for field, value in decision.items()):
        raise ValueError("Prepared review does not match the numeric decision")
    scorer = regular(root, scorer)
    scorer_text = scorer.read_bytes().decode()
    # Import trusted implementation beside the helpers, never code from a downloaded repository.
    from parse_results import extract_scores
    parsed = extract_scores(scorer_text)
    if not parsed or {role: parsed[role.title()] for role in ROLES} != {role: fm["scores"][role] for role in ROLES}:
        raise ValueError("Prepared scores disagree with this scorer result")
    session = {"key": key, "source_rfe": scope["source_rfe"], "session_id": uuid.uuid4().hex,
               "started_at": datetime.now(timezone.utc).isoformat(), "inputs": source,
               "scores": fm["scores"], "decision": decision, "owner": None, "complete": False,
               "scorer": {"original_path": str(scorer), "text": scorer_text, "sha256": digest(scorer_text)}}
    workflow_input = root / "tmp/strat-input.json"
    session["workflow_run_id"] = (json.loads(regular(root, workflow_input).read_text()).get("run_id")
                                  if workflow_input.exists() else None)
    with locked(root, key) as directory:
        save(directory / session["session_id"] / "session.json", session)
        save(directory / "active.json", {"session_id": session["session_id"]})
    return {"kind": "strat-review-session", "key": key, "session_id": session["session_id"]}


def active(root, directory):
    pointer = json.loads(regular(root, directory / "active.json").read_text())
    if (not isinstance(pointer, dict) or not isinstance(pointer.get("session_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", pointer["session_id"])):
        raise ValueError("Invalid review session identity")
    path = directory / pointer["session_id"] / "session.json"
    session = json.loads(regular(root, path).read_text())
    if (not isinstance(session, dict) or session.get("session_id") != pointer["session_id"]
            or not isinstance(session.get("inputs"), dict) or not isinstance(session.get("complete"), bool)
            or (session.get("owner") is not None and (not isinstance(session["owner"], dict)
                                                      or not isinstance(session["owner"].get("session_id"), str)))):
        raise ValueError("Invalid review session record")
    return path, session


def owner(event):
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        raise ValueError("Review caller session is unavailable")
    return {"session_id": session, "agent_id": event.get("agent_id")}


def store(root, directory, session, key, role, agent_id, tool_use_id, report, redelivery=False):
    """Validate one delivered report and save it for this role/session.

    A redelivery is a stop-hook continuation: the same reviewer returning after
    another SubagentStop hook blocked its earlier stop."""
    scope, source = snapshot(root, key)
    if session["inputs"] != source:
        raise ValueError("Review inputs changed during assessment")
    verdict = report_verdict(scope, role, report)
    saved = {"key": key, "role": role, "session_id": session["session_id"], "caller": session["owner"],
             "agent_id": agent_id, "tool_use_id": tool_use_id,
             "sha256": digest(report), "recommendation": verdict, "text": report}
    path = directory / session["session_id"] / f"{role}.json"
    if path.exists():
        existing = json.loads(regular(root, path).read_text())
        # The scope hook runs in parallel and may reject a stop this capture
        # already stored. Only the same reviewer's stop-hook continuation
        # replaces its own earlier delivery; anything else is a second assessment.
        if existing != saved and not (redelivery and existing.get("agent_id") == agent_id):
            raise ValueError("Multiple different assessments for one role/session")
    save(path, saved)


def adopt(root, directory, session, key, launch, agent, caller_session, report, redelivery=False):
    """Check a delivered report against its pending launch and store it for the role."""
    if (launch.get("key") != key or launch.get("session_id") != session["session_id"]
            or launch.get("agent_id") != agent or launch.get("role") not in ROLES
            or not launch.get("tool_use_id") or launch.get("caller") != session["owner"]):
        raise ValueError("Pending reviewer launch identity disagrees")
    if not session["owner"] or caller_session != session["owner"]["session_id"]:
        raise ValueError("Delivered review belongs to another caller/session")
    role = launch["role"]
    try:
        if not isinstance(report, str) or not report:
            raise ValueError("Missing delivered report text")
        store(root, directory, session, key, role, agent, launch["tool_use_id"], report, redelivery)
    except ValueError:
        # An invalid latest delivery must not leave this reviewer's earlier report standing.
        path = directory / session["session_id"] / f"{role}.json"
        if path.is_file() and json.loads(regular(root, path).read_text()).get("agent_id") == agent:
            path.unlink()
        raise


def deliver(event):
    """Capture a forked reviewer's report when it completes after its Skill call returned.

    Interactive sessions return a launch receipt from the Skill call and deliver
    the report later. Only a reviewer agent recorded at launch for the bound
    caller can fill its role; other subagents are ignored. A delivery that
    precedes its launch record is buffered and adopted when the launch arrives.
    """
    agent = event.get("agent_id")
    agent_type = event.get("agent_type")
    if (not isinstance(agent, str) or not AGENT.fullmatch(agent) or not isinstance(agent_type, str)
            or agent_type.split(":")[-1] != "strat-reviewer"):
        return
    root = Path(event["cwd"]).resolve()
    base = root / "tmp/review-composition"
    if not base.is_dir():
        return
    report = event.get("last_assistant_message")
    redelivery = bool(event.get("stop_hook_active"))
    usable = isinstance(report, str) and bool(report)
    # A buffered report is not otherwise validated until its launch arrives, which
    # is too late for the reviewer to correct a verdict field. Check it now.
    defect = None
    if usable and not report.startswith("REVIEW_SCOPE_ERROR:"):
        try:
            report_verdicts(report)
        except ReportError as error:
            defect = error
    claimed = set(HEADING.findall(report)) if usable else set()
    buffering = False
    for pointer in sorted(base.glob("*/active.json")):
        key = pointer.parent.name
        if not TARGET.fullmatch(key):
            continue
        with locked(root, key) as directory:
            try:
                _, session = active(root, directory)
            except (ValueError, OSError):
                continue  # One damaged key must not stop delivery for the others.
            if session["complete"]:
                continue
            launch_path = directory / session["session_id"] / "launches" / f"{agent}.json"
            if not launch_path.exists() and not launch_path.is_symlink():
                # Buffer under the same lock the launch handler takes, so a delivery
                # cannot slip between its launch check and the reconcile there.
                # Nothing is trusted yet: every other check runs when the launch arrives.
                if session["owner"] and event.get("session_id") == session["owner"]["session_id"]:
                    buffered = directory / session["session_id"] / "pending-deliveries" / f"{agent}.json"
                    buffering = True
                    if usable and not defect and claimed and key not in claimed:
                        # An authenticated later delivery claims another target: it can never be
                        # adopted here, and it must not leave the earlier buffered report standing.
                        buffered.unlink(missing_ok=True)
                    elif usable and not defect:
                        if buffered.is_file() and not redelivery:
                            earlier = json.loads(regular(root, buffered).read_text())
                            if earlier.get("last_assistant_message") != report:
                                # Same rule as store(): only a stop-hook continuation replaces
                                # a reviewer's earlier delivery; fail closed on a second assessment.
                                buffered.unlink()
                                raise ValueError("Multiple different assessments for one role/session")
                        save(buffered, {"agent_id": agent, "session_id": event["session_id"],
                                        "stop_hook_active": redelivery,
                                        "last_assistant_message": report})
                    elif buffered.is_file():
                        # An authenticated empty or invalid later delivery invalidates the earlier buffered one.
                        buffered.unlink()
                continue
            launch = json.loads(regular(root, launch_path).read_text())
            adopt(root, directory, session, key, launch, agent, event.get("session_id"), report, redelivery)
            return
    if defect and buffering:
        raise defect


def capture(event):
    if event.get("hook_event_name") == "SubagentStop":
        return deliver(event)
    if event.get("hook_event_name") != "PostToolUse":
        return
    root = Path(event["cwd"]).resolve()
    response = event.get("tool_response", {})
    if event.get("tool_name") == "Bash":
        if not isinstance(response, dict) or not isinstance(response.get("stdout"), str):
            return
        sessions = []
        for line in response["stdout"].splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("kind") == "strat-review-session":
                sessions.append(value)
        if not sessions:
            return
        if len(sessions) != 1:
            raise ValueError("One begin session per Bash call is required")
        value = sessions[0]
        with locked(root, value["key"]) as directory:
            path, session = active(root, directory)
            if value.get("session_id") != session["session_id"] or session["owner"] is not None:
                raise ValueError("Review session cannot be rebound to another caller")
            session["owner"] = owner(event)
            save(path, session)
        return
    if event.get("tool_name") != "Skill":
        return
    tool = event.get("tool_input", {})
    skill = tool.get("skill", "").split(":")[-1]
    matching = [role for role in ROLES if skill == f"strategy-{role}-review"]
    if not matching:
        return
    key = tool.get("args", "").strip()
    if not TARGET.fullmatch(key):
        raise ValueError("Delivered review must belong to one exact target")
    # Standalone reviewer invocations do not participate in a composition session.
    if not (root / "tmp/review-composition" / key / "active.json").exists():
        return
    with locked(root, key) as directory:
        _, session = active(root, directory)
        if session["complete"]:
            return
        if session["owner"] != owner(event):
            raise ValueError("Delivered review belongs to another caller/session")
        scope, source = snapshot(root, key)
        if session["inputs"] != source:
            raise ValueError("Review inputs changed during assessment")
        if (not isinstance(response, dict) or response.get("success") is not True
                or response.get("status") != "forked" or not response.get("agentId")
                or response.get("commandName", "").split(":")[-1] != skill):
            raise ValueError("No successful forked Skill receipt")
        report = response.get("result")
        if not isinstance(report, str):
            raise ValueError("Missing delivered report text")
        role = matching[0]
        if not report.startswith("REVIEW_SCOPE_ERROR:") and not REPORT.search(report):
            # Launch receipt only: the fork delivers its report at SubagentStop.
            agent = response["agentId"]
            if not isinstance(agent, str) or not AGENT.fullmatch(agent) or not event.get("tool_use_id"):
                raise ValueError("Forked reviewer identity is unavailable")
            launch = {"key": key, "role": role, "session_id": session["session_id"],
                      "caller": session["owner"], "agent_id": agent, "tool_use_id": event["tool_use_id"]}
            path = directory / session["session_id"] / "launches" / f"{agent}.json"
            if path.exists() and json.loads(regular(root, path).read_text()) != launch:
                raise ValueError("Forked reviewer was launched for another role/call")
            save(path, launch)
            buffered = path.parent.parent / "pending-deliveries" / f"{agent}.json"
            if buffered.is_file():
                # The reviewer finished before this launch was recorded; reconcile now.
                delivery = json.loads(regular(root, buffered).read_text())
                buffered.unlink()
                if delivery.get("agent_id") == agent:
                    adopt(root, directory, session, key, launch, agent, delivery.get("session_id"),
                          delivery.get("last_assistant_message"), bool(delivery.get("stop_hook_active")))
                    return f"Review composition: the {role} report for {key} was delivered and captured."
            return (f"Review composition: the {role} report for {key} is pending. It is captured when "
                    "the forked reviewer delivers it. Wait for all four reviewer reports before compose. "
                    f"A delivered report starts with '### {key}: <title>' and has one "
                    "'**Recommendation**: approve|revise|reject' line.")
        store(root, directory, session, key, role, response["agentId"], event["tool_use_id"], report)
        # A direct delivery supersedes any report this agent buffered before the call returned.
        agent = response["agentId"]
        if isinstance(agent, str) and AGENT.fullmatch(agent):
            (directory / session["session_id"] / "pending-deliveries" / f"{agent}.json").unlink(missing_ok=True)


def rendered(key, scores, decision, reports, scorer):
    body = "## Scores\n\n| Criterion | Score |\n| --- | ---: |\n"
    body += "".join(f"| {role.title()} | {scores[role]}/2 |\n" for role in ROLES)
    body += f"| Total | {scores['total']}/8 |\n\n"
    body += (f"## Decision\n\nPolicy: {POLICY}. Numeric scores are the sole approval gate.\n\n"
             f"Numeric recommendation: **{decision['recommendation']}**. "
             f"Needs attention: **{str(decision['needs_attention']).lower()}**.\n"
             "Specialist recommendations below are retained independently and do not change this gate.\n\n")
    body += ("## Reviewer sources\n\n| Role | Recommendation | Agent | Delivered report SHA-256 |\n"
             "| --- | --- | --- | --- |\n")
    body += "".join(f"| {role} | {reports[role]['recommendation']} | {reports[role]['agent_id']} | "
                    f"{reports[role]['sha256']} |\n" for role in ROLES)
    for role in ROLES:
        body += f"\n## {role.title()} Review: {key}\n\n" + reports[role]["text"] + "\n"
    dissent = [role for role in ROLES if reports[role]["recommendation"] != decision["recommendation"]]
    body += "\n## Disagreements\n\n"
    if dissent:
        body += ("These role recommendations differ from the numeric recommendation; "
                 "their full findings remain above:\n\n")
        body += "".join(f"- {role}: {reports[role]['recommendation']} (numeric: {decision['recommendation']}).\n"
                        for role in dissent)
    else:
        body += ("All role recommendations match the numeric recommendation. "
                 "Full assessments above retain any differences in reasoning.\n")
    body += ("\n## Scorer assessment\n\nThis is the separate numeric assessor's output, "
             "not a specialist report.\n\n" + scorer + "\n")
    return body


def compose(root, key):
    from strat_freshness import source_rfe, verify
    verify(root, rfe=source_rfe(root, key))
    from artifact_utils import write_frontmatter
    with locked(root, key) as directory:
        path, session = active(root, directory)
        if session["complete"]:
            return {"key": key, **verify_review(root, key)["decision"]}
        if not session["owner"]:
            raise ValueError("Review session has no verified caller")
        scope, source = snapshot(root, key)
        if source != session["inputs"]:
            raise ValueError("Review inputs changed before composition")
        reports = {}
        for role in ROLES:
            item = json.loads(regular(root, path.parent / f"{role}.json").read_text())
            if (item.get("session_id") != session["session_id"] or item.get("role") != role
                    or item.get("key") != key or item.get("caller") != session["owner"]
                    or item.get("sha256") != digest(item["text"])
                    or item.get("recommendation") != report_verdict(scope, role, item["text"])):
                raise ValueError("Captured report identity or content disagrees")
            reports[role] = item
        decision = numeric_decision(session["scores"])
        body = rendered(key, session["scores"], decision, reports, session["scorer"]["text"])
        review = scope["prior"]
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(body)
        write_frontmatter(review, {"strat_id": key, **decision, "scores": session["scores"],
                                  "reviewers": {role: reports[role]["recommendation"] for role in ROLES}},
                          "strat-review")
        # Frontmatter helpers use universal newline reads. Restore the exact
        # rendered body after writing metadata so CRLF report bytes survive.
        front = review.read_bytes().decode().split("---", 2)[1]
        review.write_bytes(("---" + front + "---\n\n" + body).encode())
        for role in ROLES:
            exported = safe_path(root, str(review.with_name(f"{key}-review-{role}.md")))
            exported.write_text(reports[role]["text"])
            reports[role] = {field: value for field, value in reports[role].items() if field != "text"}
            reports[role]["path"] = str(exported.relative_to(root))
        exported_scorer = safe_path(root, str(review.with_name(f"{key}-review-scorer.md")))
        exported_scorer.write_text(session["scorer"]["text"])
        manifest = {"schema_version": 1, "policy": POLICY, "key": key, "source_rfe": scope["source_rfe"],
                    "workflow_run_id": session["workflow_run_id"], "caller": session["owner"],
                    "session_id": session["session_id"], "inputs": source, "scores": session["scores"],
                    "decision": decision, "reports": reports,
                    "scorer": {"path": str(exported_scorer.relative_to(root)), "sha256": session["scorer"]["sha256"]},
                    "review": {"path": str(review.relative_to(root)), "sha256": digest(review.read_bytes().decode())}}
        save(review.with_name(f"{key}-review-provenance.json"), manifest)
        session["complete"] = True
        save(path, session)
        # Leftover buffers can never be adopted once the review is composed.
        shutil.rmtree(path.parent / "pending-deliveries", ignore_errors=True)
    return {"key": key, **decision}


def recover(root, key):
    """Complete an interrupted composition only from verified captured reports."""
    pointer = root / "tmp/review-composition" / key / "active.json"
    if not pointer.exists():
        return None
    with locked(root, key) as directory:
        path, session = active(root, directory)
        ready = all((path.parent / f"{role}.json").is_file() for role in ROLES)
    if ready:
        return compose(root, key)
    if session.get("complete"):
        raise ValueError("completed composition is missing role reports")
    return None


def verify_review(root, key, *, live=True):
    """Read the repository only as data; safe for use by the trusted host validator."""
    from strat_freshness import source_rfe, verify
    verify(root, live=live, rfe=source_rfe(root, key))
    return _verify_review(root, key)


def _verify_review(root, key):
    """Check every report/provenance byte after this caller verified freshness.

    Completion checks own the batch's input/progress verification. Keeping the
    data-only checks here avoids rescanning the same source/context per ticket;
    public standalone callers always use verify_review instead.
    """
    from artifact_utils import read_frontmatter_validated

    scope, source = snapshot(root, key)
    review = scope["prior"]
    provenance = regular(root, review.with_name(f"{key}-review-provenance.json"))
    manifest = json.loads(provenance.read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("policy") != POLICY or manifest.get("key") != key
            or manifest.get("source_rfe") != scope["source_rfe"] or manifest.get("inputs") != source):
        raise ValueError("Review provenance/input identity disagrees")
    fm, _ = read_frontmatter_validated(regular(root, review), "strat-review")
    body = review.read_bytes().decode().split("---", 2)[2].lstrip("\r\n")
    decision = numeric_decision(fm["scores"])
    if (fm.get("strat_id") != key or any(fm.get(field) != value for field, value in decision.items())
            or manifest.get("decision") != decision or manifest.get("scores") != fm["scores"]
            or manifest.get("review") != {"path": str(review.relative_to(root)),
                                         "sha256": digest(review.read_bytes().decode())}):
        raise ValueError("Composed review disagrees with numeric policy or its recorded content")
    if set(manifest.get("reports", {})) != set(ROLES):
        raise ValueError("Review provenance lacks a required role")
    reports = {}
    for role in ROLES:
        item = manifest["reports"][role]
        expected = review.with_name(f"{key}-review-{role}.md")
        if item.get("path") != str(expected.relative_to(root)):
            raise ValueError("Report source path does not match role/target")
        content = regular(root, expected).read_bytes().decode()
        if (item.get("sha256") != digest(content) or item.get("key") != key or item.get("role") != role
                or item.get("session_id") != manifest.get("session_id") or not item.get("agent_id")
                or item.get("caller") != manifest.get("caller")
                or not item.get("tool_use_id") or item.get("recommendation") != report_verdict(scope, role, content)
                or (fm.get("reviewers") or {}).get(role) != item["recommendation"]):
            raise ValueError("Exported report attribution/content disagrees")
        reports[role] = {**item, "text": content}
    scorer = regular(root, review.with_name(f"{key}-review-scorer.md"))
    if manifest.get("scorer") != {"path": str(scorer.relative_to(root)),
                                  "sha256": digest(scorer.read_bytes().decode())}:
        raise ValueError("Scorer provenance disagrees")
    if body != rendered(key, fm["scores"], decision, reports, scorer.read_bytes().decode()):
        raise ValueError("Final review omitted, rewrote or substituted a source report")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    initial = sub.add_parser("begin")
    initial.add_argument("key")
    initial.add_argument("--scorer-result", required=True, type=Path)
    for command in ("compose", "verify"):
        sub.add_parser(command).add_argument("key")
    sub.add_parser("hook")
    args = parser.parse_args()
    event = {}
    try:
        root = Path.cwd().resolve()
        if args.command == "hook":
            event = json.load(sys.stdin)
            sys.path.insert(0, str(Path(event["cwd"]) / "scripts"))
            message = capture(event)
            if message:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": event["hook_event_name"],
                                                         "additionalContext": message}}))
            return 0
        sys.path.insert(0, str(Path(__file__).parent / "assess-strat"))
        if args.command == "begin":
            result = begin(root, args.key, args.scorer_result)
        elif args.command == "compose":
            result = compose(root, args.key)
        else:
            manifest = verify_review(root, args.key)
            result = {"key": args.key, **manifest["decision"]}
        print(json.dumps(result))
        return 0
    except Exception as error:
        if isinstance(event, dict) and event.get("hook_event_name") == "SubagentStop":
            # Only a verdict-field error is the reviewer's to correct, once; the scope
            # hook owns heading/target errors. Other faults never hold the reviewer open.
            # Nothing was captured, so composition still stops on the missing role.
            if isinstance(error, ReportError) and not event.get("stop_hook_active"):
                print(json.dumps({"decision": "block", "reason": f"Review composition: {error}"}))
            else:
                print(json.dumps({"systemMessage": f"Review composition: {error}"}))
            return 0
        print(f"Review composition: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # CLI plugin hooks run inside the vendored workspace. Trusted host imports
    # never execute this branch or import code from the downloaded repository.
    sys.path.insert(0, str(Path.cwd().resolve() / "scripts"))
    raise SystemExit(main())
