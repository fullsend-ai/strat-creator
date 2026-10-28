"""External evidence for protected inputs that changed and then reverted.

The host records the RFE cursor before agent work. Progress alone cannot
authorize a sticky deferral: a later host must verify real Jira history.
Missing, truncated or unrecognized history provides no proof.
"""

import json
import re

from strat_side_effects import digest


def histories(key, *, latest=False):
    from jira_utils import api_call, require_env

    server, user, token = require_env()
    log = api_call(server, f"/issue/{key}?fields=summary&expand=changelog", user, token)["changelog"]
    rows, total = log["histories"], log["total"]
    if not isinstance(rows, list) or type(total) is not int or total < 0:
        raise ValueError("malformed Jira history")
    if log.get("startAt", 0) != 0 or len(rows) != total:
        rows = []
        offset = max(total - 1, 0) if latest else 0
        while offset < total:
            page = api_call(server, f"/issue/{key}/changelog?startAt={offset}&maxResults=100", user, token)
            values = page["values"]
            if page["startAt"] != offset or not values:
                raise ValueError("incomplete Jira history")
            rows.extend(values)
            offset += len(values)
    if any(type(v.get("id")) not in (str, int) or str(v["id"]) == "" for v in rows):
        raise ValueError("malformed Jira history identity")
    if len({str(v["id"]) for v in rows}) != len(rows):
        raise ValueError("duplicate Jira history identity")
    if any(not isinstance(v.get("items"), list) or any(not isinstance(i, dict) for i in v["items"]) for v in rows):
        raise ValueError("malformed Jira history items")
    return rows[-1:] if latest else rows


def cursor(key):
    """Optional recovery evidence; normal current-input checks do not need it."""
    try:
        rows = histories(key, latest=True)
        last = rows[-1] if rows else None
        return {"id": str(last["id"]) if last else None, "items": [digest(v) for v in last["items"]] if last else []}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def initial_evidence(rfes):
    """Host-only history anchors, including existing linked strategies."""
    from jira_utils import get_issue, require_env

    evidence = {"rfe_history": {}, "strategy_history": {}}
    for rfe in rfes:
        evidence["rfe_history"][rfe] = cursor(rfe)
        try:
            fields = get_issue(*require_env(), rfe, fields=["issuelinks"])["fields"]
            for link in fields["issuelinks"]:
                if link["type"]["name"] != "Cloners":
                    continue
                for side in ("inwardIssue", "outwardIssue"):
                    key = link.get(side, {}).get("key", "")
                    if isinstance(key, str) and key.startswith("RHAISTRAT-"):
                        evidence["strategy_history"][key] = {"rfe": rfe, "cursor": cursor(key)}
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Optional evidence cannot substitute for current-input checks.
    return evidence


def since(rows, marker):
    if not isinstance(marker, dict):
        return []
    if marker["id"] is None:
        return [item for row in rows for item in row["items"]]
    matches = [i for i, row in enumerate(rows) if str(row["id"]) == marker["id"]]
    if len(matches) != 1:
        return []
    index = matches[0]
    saved = set(marker["items"])
    current = rows[index]["items"]
    if not saved <= {digest(v) for v in current}:
        return []
    # The emulator can append items to an existing per-second history group.
    return [v for v in current if digest(v) not in saved] + [item for row in rows[index + 1 :] for item in row["items"]]


def labels(value):
    if value is None:
        return set()
    if not isinstance(value, str):
        raise ValueError("malformed history labels")
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list) or any(not isinstance(v, str) for v in parsed):
            raise ValueError("malformed history labels")
        return set(parsed)
    return set(filter(None, re.split(r"[,\s]+", value)))


def rfe_change(item):
    from strat_freshness import RFE_FIELDS

    field = item.get("fieldId") or item["field"]
    if not isinstance(field, str):
        raise ValueError("malformed history field")
    name = field.lower().replace(" ", "")
    if name == "labels":

        def protected(value):
            return {
                v for v in labels(value) if v != "strat-creator-processing" and not v.startswith("strat-creator-owner-")
            }

        return protected(item.get("fromString")) != protected(item.get("toString"))
    allowed = {v.lower() for v in RFE_FIELDS} | {"targetversion", "fixversion", "version"}
    return name in allowed and (item.get("from"), item.get("fromString")) != (item.get("to"), item.get("toString"))


def structured_staff(value):
    from jira_utils import adf_to_markdown
    from strat_freshness import staff

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("malformed historical description")
    if value.startswith("{"):
        doc = json.loads(value)
        if not isinstance(doc, dict) or doc.get("type") != "doc":
            raise ValueError("unrecognized historical description")
        return staff(adf_to_markdown(doc))
    if re.search(r"^## Staff Engineer / SME Input\b", value, re.M):
        return staff(value)
    # Plain text loses heading semantics. Do not infer a protected SME edit.
    raise ValueError("historical description cannot prove SME input")


def proves_change(root, data, progress, rfe):
    """Only externally verified history can explain a reverted live mismatch."""
    from jira_utils import get_issue, require_env

    try:
        base = data["freshness"]
        if any(rfe_change(v) for v in since(histories(rfe), base.get("rfe_history", {}).get(rfe))):
            return True
        for key, target in (progress.get("freshness") or {}).get("targets", {}).items():
            if target["rfe"] != rfe or not key.startswith("RHAISTRAT-"):
                continue
            prepared = base.get("strategy_history", {}).get(key)
            if prepared is None or prepared["rfe"] != rfe:
                continue
            fields = get_issue(*require_env(), key, fields=["issuelinks"])["fields"]
            linked = {
                link[side]["key"]
                for link in fields["issuelinks"]
                if link["type"]["name"] == "Cloners"
                for side in ("inwardIssue", "outwardIssue")
                if side in link and link[side]["key"].startswith("RHAIRFE-")
            }
            if linked != {rfe}:
                continue
            items = since(histories(key), prepared["cursor"])
            for item in items:
                field = item.get("fieldId") or item["field"]
                if field == "description":
                    try:
                        if structured_staff(item.get("fromString")) != structured_staff(item.get("toString")):
                            return True
                    except (ValueError, TypeError):
                        continue
                if field == "labels":
                    # Our publisher never writes sign-off. Numeric attention
                    # labels alone cannot prove an external human-gate edit.
                    if "strat-creator-human-sign-off" in (
                        labels(item.get("fromString")) ^ labels(item.get("toString"))
                    ):
                        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False
