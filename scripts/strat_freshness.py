#!/usr/bin/env python3
"""S1 freshness: pin inputs before work, reject changes before reuse or writes.

Same-run changes stop without deleting artifacts or mutation intent. A later
run can reuse a verified push only under the same source/context and live Jira
inputs. Native journal replay is deliberately not part of this contract.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from artifact_utils import read_frontmatter
from strat_side_effects import digest

CONTRACT = "strat-freshness-v1"
SOURCE_TREES = ("scripts", "config", "workflows", ".claude/skills", ".claude/agents",
                ".fullsend/shared", ".fullsend/plugins", ".fullsend/strat-single", ".fullsend/strat-batch",
                ".fullsend/strat-resume")
SOURCE_FILES = ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md", ".claude-plugin/plugin.json", "hooks/hooks.json")
OWN_LABELS = {"strat-creator-processing", "strat-creator-auto-created", "strat-creator-auto-refined",
              "strat-creator-rubric-pass", "strat-creator-needs-attention"}
RFE_FIELDS = ("summary", "description", "priority", "components", "versions", "fixVersions",
              "customfield_10855", "parent", "status", "labels")


def hashed(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root, relative):
    """Logical file contents; internal Skill symlinks and vendored copies agree."""
    root = root.resolve()
    result = {}
    top = root / relative
    if not top.exists():
        return result
    for parent, directories, files in os.walk(top, followlinks=True):
        here = Path(parent)
        resolved = here.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("freshness tree resolves outside the repository")
        # Internal directory aliases are allowed; recursive aliases are not.
        if resolved in {p.resolve() for p in here.parents if p != root and p.is_relative_to(root)}:
            raise ValueError("recursive freshness tree alias")
        directories[:] = sorted(x for x in directories if x not in (".git", "__pycache__", ".pytest_cache"))
        for name in sorted(files):
            path = here / name
            if path.suffix == ".pyc":
                continue
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError("unsafe freshness input")
            result[str(path.relative_to(root))] = hashed(path)
    return result


def source(root):
    values = {}
    for relative in SOURCE_TREES:
        values.update(tree(root, relative))
    for relative in SOURCE_FILES:
        path = root / relative
        if path.exists():
            if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("unsafe instruction file")
            values[relative] = hashed(path)
    return values


def parameters(data):
    return digest({k: v for k, v in data.items() if k not in ("freshness", "prepared_at", "locked_at")})


def staff(markdown):
    from push_strategy import STAFF_INPUT_TEMPLATE, extract_staff_input_section
    value = extract_staff_input_section(markdown) or ""
    if value.strip() == STAFF_INPUT_TEMPLATE.strip():
        return ""
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S).strip()
    return value


def jira_rfe(key):
    from jira_utils import get_comments, get_issue, require_env
    server, user, token = require_env()
    if not all((server, user, token)):
        raise ValueError("freshness needs Jira credentials to recheck live inputs")
    fields = get_issue(server, user, token, key, fields=list(RFE_FIELDS))["fields"]
    fields["labels"] = sorted(x for x in fields.get("labels", [])
                              if x != "strat-creator-processing" and not x.startswith("strat-creator-owner-"))
    comments = [{"id": str(c["id"]), "body": c["body"]} for c in get_comments(server, user, token, key)]
    return digest({"fields": fields, "comments": comments})


def jira_human(key):
    from jira_utils import adf_to_markdown, get_issue, require_env
    fields = get_issue(*require_env(), key, fields=["description", "summary", "priority", "status"])["fields"]
    return {"staff": staff(adf_to_markdown(fields.get("description"))),
            "summary": fields.get("summary"), "priority": fields.get("priority"), "status": fields.get("status")}


def jira_published(key):
    from jira_utils import attachment_sort_key, download_attachment, get_issue, require_env
    env = require_env()
    fields = get_issue(*env, key, fields=["description", "attachment"])["fields"]
    candidates = [a for a in fields.get("attachment", []) if a.get("filename") == f"{key}-strategy.md"]
    attachment = None
    if candidates:
        latest = max(candidates, key=attachment_sort_key)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "strategy.md"
            download_attachment(*env, latest["content"], str(path))
            attachment = hashed(path)
    return {"description": digest(fields.get("description")), "attachment": attachment}


def initialize(root, data, *, live=False, trusted_source=None):
    """Called by the host before agent work; fixture callers explicitly use local inputs."""
    if "freshness" in data:
        raise ValueError("cannot replace an existing freshness baseline")
    root = root.resolve()
    baseline = {"contract": CONTRACT, "run_id": data["run_id"], "parameters": parameters(data),
                "source": source(root), "architecture": tree(root, ".context/architecture-context"),
                "live": live, "rfes": {k: jira_rfe(k) for k in data.get("acquired_keys", [])} if live else {},
                "server": None}
    if live:
        from jira_utils import require_env
        baseline["server"] = digest(require_env()[0].rstrip("/"))
    if trusted_source is not None:
        baseline["trusted_instructions"] = source(trusted_source.resolve())
    data["freshness"] = baseline
    return data


def pipeline(root, data):
    """Legacy authored unit fixtures remain usable; deployed S1 always requires the contract."""
    return "freshness" in data or (root / ".strat-creator-vendor.json").exists() \
        or (root / ".claude-plugin/plugin.json").exists()


def verify(root, data=None, *, live=True, trusted_source=None):
    root = root.resolve()
    path = root / "tmp/strat-input.json"
    if data is None:
        if not path.exists():
            return None  # Standalone/local human Skill usage, outside the S1 harness.
        data = json.loads(path.read_text())
    if not pipeline(root, data):
        return None
    base = data.get("freshness", {})
    if not isinstance(base, dict):
        raise ValueError("malformed freshness baseline")
    if base.get("contract") != CONTRACT or base.get("run_id") != data.get("run_id"):
        raise ValueError("missing/legacy freshness baseline; preserve work and start a verified new run")
    if base.get("parameters") != parameters(data):
        raise ValueError("freshness: prepared run parameters changed")
    current = source(root)
    if base["source"] != current:
        changed = sorted(k for k in base["source"].keys() | current.keys() if base["source"].get(k) != current.get(k))
        raise ValueError("freshness: pipeline source/instructions changed: " + ", ".join(changed[:6]))
    if base["architecture"] != tree(root, ".context/architecture-context"):
        raise ValueError("freshness: architecture files changed")
    if trusted_source is not None and base.get("trusted_instructions") != source(trusted_source.resolve()):
        raise ValueError("freshness: trusted host instructions changed")
    if base["live"] and live:
        from jira_utils import require_env
        server = require_env()[0]
        if not server or base["server"] != digest(server.rstrip("/")):
            raise ValueError("freshness: Jira upstream changed or unavailable")
        for key, expected in base["rfes"].items():
            if jira_rfe(key) != expected:
                raise ValueError(f"freshness: live RFE inputs changed: {key}")
    for rfe, reuse in data.get("reused_push", {}).items():
        old = reuse.get("context", {})
        if old != {"source": digest(base["source"]), "architecture": digest(base["architecture"]),
                   "rfe": base["rfes"].get(rfe), "server": base["server"],
                   "trusted_instructions": digest(base.get("trusted_instructions"))}:
            raise ValueError("freshness: reusable push and current inputs disagree")
    return base


def strategy(root, key):
    from push_strategy import extract_strategy_section
    path = root / f"artifacts/strat-tasks/{key}.md"
    if path.is_symlink() or not path.is_file():
        raise ValueError("freshness: strategy file is missing or symlinked")
    fm, body = read_frontmatter(path)
    # Our status/counter/diff metadata changes as work progresses. The strategy
    # text and its identity/priority/staff inputs must remain unchanged.
    return digest({"identity": {k: fm.get(k) for k in ("strat_id", "source_rfe", "jira_key", "title", "priority")},
                   "strategy": extract_strategy_section(body) or body.strip(), "staff": staff(body)})


def originals(root, rfe):
    return {rel: hashed(root / rel) if (root / rel).is_file() else None
            for rel in (f"artifacts/strat-originals/{rfe}.md", f"artifacts/strat-originals/{rfe}-comments.md")}


def bind(data, progress):
    if "freshness" not in data:
        return None
    saved = progress.setdefault("freshness", {"contract": CONTRACT, "baseline": data["freshness"],
                                             "targets": {}, "refined": {}, "pushed": {}, "reviewed": {}})
    if saved.get("contract") != CONTRACT or saved.get("baseline") != data["freshness"]:
        raise ValueError("freshness: progress baseline disagrees with prepared inputs")
    return saved


def record(root, data, progress, phase, key, rfe):
    saved = bind(data, progress)
    if saved is None:
        return
    value = {"strategy": strategy(root, key), "originals": originals(root, rfe)}
    if phase == "create":
        saved["targets"][key] = {"rfe": rfe, "human": jira_human(key) if data["freshness"]["live"] else None}
    else:
        if phase == "push":
            value["published"] = jira_published(key) if data["freshness"]["live"] else None
            value["human"] = jira_human(key) if data["freshness"]["live"] else None
        if phase == "review":
            value["review"] = hashed(root / f"artifacts/strat-reviews/{key}-review.md")
        saved[{"refine": "refined", "push": "pushed", "review": "reviewed"}[phase]][key] = value


def own_summary_change(root, key, summary):
    for path in (root / "tmp/strat-effects").glob("*.json"):
        value = json.loads(path.read_text())
        identity = value.get("identity", {})
        if (identity.get("kind") == "fields" and identity.get("target") == key
                and identity.get("payload_hash") == digest({"summary": summary})):
            return True
    return False


def verify_progress(root, data, progress, *, live=True):
    base = verify(root, data, live=live)
    if base is None:
        return
    if not isinstance(progress, dict):
        raise ValueError("freshness: saved progress is not a JSON object")
    saved = progress.get("freshness")
    if saved is None:
        if progress.get("create") or progress.get("refine") or progress.get("push") or progress.get("review"):
            raise ValueError("freshness: saved progress has no provenance")
        return
    if saved.get("contract") != CONTRACT or saved.get("baseline") != base:
        raise ValueError("freshness: progress belongs to different inputs")
    for phase, marks in (("refined", progress.get("refine", {})), ("pushed", (progress.get("push") or {}).get(
            "strategies", [])), ("reviewed", progress.get("review", {}))):
        for key in marks:
            stamp = saved[phase].get(key)
            target = saved["targets"].get(key)
            if not stamp or not target or stamp["strategy"] != strategy(root, key):
                raise ValueError(f"freshness: {phase} strategy changed or lacks provenance: {key}")
            if stamp["originals"] != originals(root, target["rfe"]):
                raise ValueError(f"freshness: cached RFE inputs changed: {key}")
            if phase == "reviewed" and stamp["review"] != hashed(root / f"artifacts/strat-reviews/{key}-review.md"):
                raise ValueError(f"freshness: saved review changed: {key}")
            if phase == "pushed" and base["live"] and live and stamp["published"] != jira_published(key):
                raise ValueError(f"freshness: published strategy changed: {key}")
    if base["live"] and live:
        for key, target in saved["targets"].items():
            expected, current = dict(target["human"]), jira_human(key)
            if expected.get("summary") != current.get("summary") and own_summary_change(root, key, current["summary"]):
                expected["summary"] = current["summary"]
            if expected != current:
                raise ValueError(f"freshness: live strategy/staff inputs changed: {key}")
    for rfe, reuse in data.get("reused_push", {}).items():
        pairs = (progress.get("create") or {}).get("strategies", [])
        pair = next((p for p in pairs if p["rfe"] == rfe), None)
        if pair and (pair["strat"] != reuse["key"] or strategy(root, pair["strat"]) != reuse["proof"]["strategy"]):
            raise ValueError("freshness: imported strategy differs from the reusable push")


def reusable(root, rfe, key, previous, *, trusted_source=None):
    saved = previous.get("freshness") or {}
    base = saved.get("baseline") or {}
    proof = saved.get("pushed", {}).get(key)
    if saved.get("contract") != CONTRACT or base.get("contract") != CONTRACT or not base.get("live") or not proof:
        return None
    if base["source"] != source(root) or base["architecture"] != tree(root, ".context/architecture-context"):
        return None
    if trusted_source is not None and base.get("trusted_instructions") != source(trusted_source.resolve()):
        return None
    from jira_utils import require_env
    if base["server"] != digest(require_env()[0].rstrip("/")) or base["rfes"].get(rfe) != jira_rfe(rfe):
        return None
    if proof.get("published") != jira_published(key) or proof.get("human") != jira_human(key):
        return None
    return {"key": key, "proof": proof, "previous_run_id": previous["run_id"],
            "context": {"source": digest(base["source"]), "architecture": digest(base["architecture"]),
                        "rfe": base["rfes"][rfe], "server": base["server"],
                        "trusted_instructions": digest(base.get("trusted_instructions"))}}
