#!/usr/bin/env python3
"""Enforce a forked strategy review's ticket scope through plugin hooks.

The assignment comes from the first user message in this agent's transcript,
never from a process-wide 'current ticket'. No report text is recovered here.
"""
import json
import re
import sys
import time
from pathlib import Path

import yaml

TARGET = re.compile(r"(?:RHAISTRAT|STRAT)-[0-9]+")
SOURCE = re.compile(r"(?:RHAIRFE|RFE)-[0-9]+")
MARKER = "REVIEW_SCOPE_TARGET: "


class ScopeError(ValueError):
    pass


class AssignmentPending(ScopeError):
    pass


def text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
    return ""


def assignment(prompt):
    lines = [line[len(MARKER):].strip() for line in prompt.splitlines()
             if line.startswith(MARKER)]
    if len(lines) != 1 or not TARGET.fullmatch(lines[0]):
        raise ScopeError("Exactly one strategy key is required; batch review is not allowed.")
    return lines[0]


def agent_transcript(event):
    explicit = event.get("agent_transcript_path")
    if explicit:
        return Path(explicit)
    agent = event.get("agent_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent):
        raise ScopeError("Reviewer agent identity is unavailable.")
    path = Path(event["transcript_path"])
    if path.name == f"agent-{agent}.jsonl":
        return path
    matches = list((path.parent / path.stem / "subagents").rglob(f"agent-{agent}.jsonl"))
    if not matches:
        raise AssignmentPending("Reviewer assignment transcript has not been flushed yet.")
    if len(matches) != 1:
        raise ScopeError("Reviewer assignment transcript is ambiguous.")
    return matches[0]


def assigned_key(event):
    # Claude writes transcripts asynchronously. Wait for the initial assignment,
    # without granting access or consulting another agent's conversation.
    deadline = time.monotonic() + 2
    while True:
        try:
            with agent_transcript(event).open() as stream:
                for line in stream:
                    item = json.loads(line)
                    if item.get("type") == "user":
                        return assignment(text(item.get("message", {}).get("content", "")))
            raise AssignmentPending("Reviewer assignment has not been flushed yet.")
        except (AssignmentPending, FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise ScopeError("Reviewer assignment unavailable after 2 seconds; input access blocked.") from None
            time.sleep(0.02)


def safe_path(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if ".." in path.parts or not path.is_relative_to(root):
        raise ScopeError("Path is outside the assigned input scope.")
    if path.resolve() != path:
        raise ScopeError("Symlinks and indirect paths are not review inputs.")
    return path


def inputs(root, key):
    root = root.resolve()
    local = root / "local/strat-tasks" / f"{key}.md"
    mode = "local" if local.is_file() else "artifacts"
    strategy = safe_path(root, f"{mode}/strat-tasks/{key}.md")
    content = strategy.read_text()
    if not content.startswith("---\n") or "\n---" not in content[4:]:
        raise ScopeError("Strategy frontmatter is missing.")
    metadata = yaml.safe_load(content.split("---", 2)[1])
    if not isinstance(metadata, dict) or metadata.get("strat_id") != key:
        raise ScopeError("Strategy frontmatter does not match the assigned key.")
    source = metadata.get("source_rfe", "")
    if not isinstance(source, str) or not SOURCE.fullmatch(source):
        raise ScopeError("Strategy source_rfe is missing or invalid.")
    rfe_dir = "strat-originals" if mode == "local" else "rfe-tasks"
    rfe = safe_path(root, f"{mode}/{rfe_dir}/{source}.md")
    if not rfe.is_file():
        raise ScopeError("Assigned strategy's source RFE is missing.")
    prior = safe_path(root, f"{mode}/strat-reviews/{key}-review.md")
    architecture = safe_path(root, ".context/architecture-context")
    return {"key": key, "source_rfe": source, "strategy": strategy, "rfe": rfe,
            "prior": prior, "architecture": architecture}


def check_tool(root, scope, name, value):
    if name not in {"Read", "Grep", "Glob"}:
        raise ScopeError("Reviewer tools are limited to Read, Grep and Glob.")
    field = "file_path" if name == "Read" else "path"
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ScopeError("Specify an exact input file or the architecture directory as path.")
    path = safe_path(root, raw)
    architecture = scope["architecture"]
    shared = path.is_relative_to(architecture)
    if name == "Glob":
        pattern = value.get("pattern", "")
        if (not shared or not isinstance(pattern, str) or not pattern
                or Path(pattern).is_absolute() or ".." in Path(pattern).parts):
            raise ScopeError("Glob is limited to relative patterns inside architecture context.")
    elif not shared and path not in {scope["strategy"], scope["rfe"], scope["prior"]}:
        raise ScopeError("Access to other tickets or batch directories is blocked.")
    # Recursive searches must not follow architecture symlinks into ticket data.
    if shared and name in {"Grep", "Glob"} and path.is_dir():
        if any(child.is_symlink() for child in path.rglob("*")):
            raise ScopeError("Architecture search contains symlinks; use exact regular files.")


def check_report(scope, report):
    if report.startswith("REVIEW_SCOPE_ERROR:"):
        return  # Return an explicit failure to the caller, never an assessment.
    key = scope["key"]
    headings = re.findall(r"^###\s+((?:RHAISTRAT|STRAT)-[0-9]+):", report, re.M)
    if headings != [key]:
        raise ScopeError(f"Return one assessment headed '### {key}: <title>'.")
    if set(TARGET.findall(report)) != {key}:
        raise ScopeError("Assessment contains another strategy key.")
    if set(SOURCE.findall(report)) - {scope["source_rfe"]}:
        raise ScopeError("Assessment contains an unrelated source RFE.")


def guard(event):
    if event.get("agent_type", "").split(":")[-1] != "strat-reviewer":
        return  # Leave the receiving agent and scorer unchanged.
    root = Path(event["cwd"]).resolve()
    if (event["hook_event_name"] == "SubagentStop"
            and event.get("last_assistant_message", "").startswith("REVIEW_SCOPE_ERROR:")):
        return {"key": None}
    scope = inputs(root, assigned_key(event))
    if event["hook_event_name"] == "PreToolUse":
        check_tool(root, scope, event["tool_name"], event.get("tool_input", {}))
    elif event["hook_event_name"] == "SubagentStop":
        check_report(scope, event.get("last_assistant_message", ""))
    else:
        raise ScopeError("Unsupported reviewer scope event.")
    return scope


def audit(event, scope, error=None):
    if event.get("agent_type", "").split(":")[-1] != "strat-reviewer":
        return
    agent = event.get("agent_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent):
        raise ScopeError("Cannot record the reviewer identity.")
    directory = Path(event["cwd"]) / "tmp/review-scope"
    directory.mkdir(parents=True, exist_ok=True)
    record = {"agent_id": agent, "event": event["hook_event_name"],
              "key": scope.get("key") if scope else None,
              "tool_use_id": event.get("tool_use_id"), "tool": event.get("tool_name"),
              "input": event.get("tool_input"), "allowed": error is None,
              "error": str(error) if error else None}
    with (directory / f"{agent}.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def main():
    event = {}
    try:
        event = json.load(sys.stdin)
        scope = guard(event)
        audit(event, scope)
    except Exception as error:
        try:
            audit(event, None, error)
        except Exception:
            pass
        # Exit 2 blocks the action. Parser/path failures must not grant access.
        if event.get("hook_event_name") == "SubagentStop":
            print(json.dumps({"decision": "block", "reason": f"Reviewer scope: {error}"}))
            return 0
        print(f"Reviewer scope: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
