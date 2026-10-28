"""History is external proof, not an agent-authored deferral claim."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import strat_history as history  # noqa: E402
from strat_side_effects import digest  # noqa: E402


def row(identity, *items):
    return {"id": str(identity), "created": "2026-10-28T12:00:00Z", "items": list(items)}


def change(field, old, new):
    return {"field": field, "fromString": old, "toString": new, "from": None, "to": None}


def test_cursor_handles_new_items_in_same_history_group():
    owner = change("labels", "", "strat-creator-processing strat-creator-owner-abc")
    description = change("description", "Original", "Changed")
    marker = {"id": "1", "items": [digest(owner)]}
    assert history.since([row(1, owner, description)], marker) == [description]
    assert history.since([row(1, owner)], marker) == []
    assert history.since([row(2, description)], marker) == []
    assert history.since([row(1, description)], marker) == []  # Baseline history was rewritten.


@pytest.mark.parametrize(
    "item,expected",
    [
        (change("labels", "", "strat-creator-processing strat-creator-owner-abc"), False),
        (change("labels", "strat-creator-owner-abc", "strat-creator-owner-def"), False),
        (change("labels", "release-a", "release-b"), True),
        (change("description", "Original", "Changed"), True),
        (change("description", "Original", "Original"), False),
        (change("updated", "Yesterday", "Today"), False),
        (change("Target Version", "1", "2"), True),
    ],
)
def test_only_protected_rfe_fields_count(item, expected):
    assert history.rfe_change(item) is expected


def test_paginated_history_is_fetched_without_gaps(monkeypatch):
    import jira_utils

    rows = [row(i, change("description", str(i), str(i + 1))) for i in range(5)]
    calls = []

    def api(server, path, user, token):
        calls.append(path)
        if "expand=" in path:
            return {"changelog": {"total": 5, "startAt": 0, "histories": rows[:1]}}
        from urllib.parse import parse_qs, urlsplit

        offset = int(parse_qs(urlsplit(path).query)["startAt"][0])
        return {"startAt": offset, "values": rows[offset : offset + 2]}

    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))
    monkeypatch.setattr(jira_utils, "api_call", api)
    assert history.histories("RHAIRFE-1") == rows
    assert len(calls) == 4


def test_history_unavailable_never_becomes_proof(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise OSError("History unavailable")

    monkeypatch.setattr(history, "histories", unavailable)
    assert history.cursor("RHAIRFE-1") is None
    assert not history.proves_change(tmp_path, {"freshness": {}}, {}, "RHAIRFE-1")


def test_plain_description_is_not_inferred_as_sme_content():
    with pytest.raises(ValueError, match="cannot prove"):
        history.structured_staff("Plain description without section structure.")


@pytest.mark.parametrize("text,valid", [("Use the shared queue.", True), ("Invented requirement.", False)])
def test_managed_sme_comparison_allows_only_format_round_trips(text, valid):
    from strat_freshness import verify_staff

    artifact = "## Staff Engineer / SME Input\n" + text
    live = "## Staff Engineer / SME Input\n\nUse the shared queue.\n"
    if valid:
        verify_staff(artifact, live)
    else:
        with pytest.raises(ValueError, match="cannot edit SME input"):
            verify_staff(artifact, live)


def test_structured_description_keeps_real_sme_and_ignores_empty_template():
    from jira_utils import markdown_to_adf
    from push_strategy import STAFF_INPUT_TEMPLATE

    assert history.structured_staff(json.dumps(markdown_to_adf(STAFF_INPUT_TEMPLATE))) == ""
    value = "## Staff Engineer / SME Input\nUse the shared queue."
    assert "shared queue" in history.structured_staff(json.dumps(markdown_to_adf(value)))


def test_preparation_cursor_cannot_reuse_older_real_edit(monkeypatch, tmp_path):
    item = change("description", "Original", "Earlier change")
    rows = [row(1, item)]
    monkeypatch.setattr(history, "histories", lambda key: rows)
    data = {
        "freshness": {
            "rfe_history": {"RHAIRFE-1": {"id": "1", "items": [digest(item)]}},
            "observed_at": "2026-10-28T12:00:01Z",
        }
    }
    assert not history.proves_change(tmp_path, data, {}, "RHAIRFE-1")


def test_rewritten_or_missing_history_cannot_preserve_deferral(monkeypatch, tmp_path):
    new = change("description", "Original", "New")
    monkeypatch.setattr(history, "histories", lambda key: [row(2, new)])
    data = {
        "freshness": {
            "rfe_history": {"RHAIRFE-1": {"id": "1", "items": ["unavailable"]}},
            "observed_at": "2026-10-28T12:00:01Z",
        }
    }
    assert not history.proves_change(tmp_path, data, {}, "RHAIRFE-1")


def test_cursor_reads_only_last_page_when_expansion_is_truncated(monkeypatch):
    import jira_utils

    calls = []

    def api(server, path, user, token):
        calls.append(path)
        if "expand=" in path:
            return {"changelog": {"total": 1000, "startAt": 0, "histories": []}}
        assert "startAt=999" in path
        return {"startAt": 999, "values": [row(1000)]}

    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))
    monkeypatch.setattr(jira_utils, "api_call", api)
    assert history.cursor("RHAIRFE-1")["id"] == "1000"
    assert len(calls) == 2


def test_duplicate_history_and_pagination_gap_are_unverifiable(monkeypatch):
    import jira_utils

    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))
    monkeypatch.setattr(
        jira_utils, "api_call", lambda *args: {"changelog": {"total": 2, "startAt": 0, "histories": [row(1), row(1)]}}
    )
    assert history.cursor("RHAIRFE-1") is None
    with pytest.raises(ValueError, match="duplicate"):
        history.histories("RHAIRFE-1")


@pytest.mark.parametrize("page", [{"startAt": 99, "values": [row(1)]}, {"startAt": 0, "values": []}])
def test_missing_or_out_of_order_history_page_is_not_proof(monkeypatch, page):
    import jira_utils

    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))

    def api(server, path, user, token):
        return {"changelog": {"total": 2, "startAt": 0, "histories": []}} if "expand=" in path else page

    monkeypatch.setattr(jira_utils, "api_call", api)
    with pytest.raises(ValueError, match="incomplete"):
        history.histories("RHAIRFE-1")


def test_other_strategy_cannot_supply_sme_history_proof(monkeypatch, tmp_path):
    import jira_utils

    rows = [row(1, change("labels", "", "strat-creator-human-sign-off"))]
    monkeypatch.setattr(history, "histories", lambda key: [] if key.startswith("RHAIRFE-") else rows)
    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))
    monkeypatch.setattr(
        jira_utils,
        "get_issue",
        lambda *args, **kwargs: {
            "fields": {
                "labels": [],
                "status": {"name": "Open"},
                "issuelinks": [{"type": {"name": "Cloners"}, "outwardIssue": {"key": "RHAIRFE-2"}}],
            }
        },
    )
    data = {
        "freshness": {
            "rfe_history": {},
            "strategy_history": {"RHAISTRAT-1": {"rfe": "RHAIRFE-1", "cursor": {"id": None, "items": []}}},
        }
    }
    progress = {"freshness": {"targets": {"RHAISTRAT-1": {"rfe": "RHAIRFE-1"}}},
                "create": {"strategies": [{"rfe": "RHAIRFE-1", "strat": "RHAISTRAT-1"}]}}
    assert not history.proves_change(tmp_path, data, progress, "RHAIRFE-1")
    monkeypatch.setattr(
        jira_utils,
        "get_issue",
        lambda *args, **kwargs: {
            "fields": {
                "labels": [],
                "status": {"name": "Open"},
                "issuelinks": [{"type": {"name": "Cloners"}, "outwardIssue": {"key": "RHAIRFE-1"}}],
            }
        },
    )
    assert history.proves_change(tmp_path, data, progress, "RHAIRFE-1")


def test_agent_supplied_strategy_cursor_is_not_preparation_evidence(monkeypatch, tmp_path):
    rows = [row(1, change("labels", "", "strat-creator-human-sign-off"))]
    monkeypatch.setattr(history, "histories", lambda key: [] if key.startswith("RHAIRFE-") else rows)
    data = {"freshness": {"rfe_history": {}, "strategy_history": {}}}
    progress = {"freshness": {"targets": {"RHAISTRAT-1": {"rfe": "RHAIRFE-1", "cursor": {"id": None, "items": []}}}}}
    assert not history.proves_change(tmp_path, data, progress, "RHAIRFE-1")


@pytest.mark.parametrize("inventory,expected", [(None,False),(["RHAISTRAT-1"],False),([],True)])
def test_new_gate_needs_a_successful_host_inventory(monkeypatch,tmp_path,inventory,expected):
    import jira_utils

    monkeypatch.setattr(history,"histories",lambda key:[])
    monkeypatch.setattr(jira_utils,"require_env",lambda:("http://fixture","admin","admin"))
    monkeypatch.setattr(jira_utils,"get_issue",lambda *a,**kw:{"fields":{
        "labels":["strat-creator-human-sign-off"],"status":{"name":"Open"},
        "issuelinks":[{"type":{"name":"Cloners"},"outwardIssue":{"key":"RHAIRFE-1"}}]}})
    base={"live":True,"strategy_history":{},"clone_inventory":{}}
    if inventory is not None:
        base["clone_inventory"]["RHAIRFE-1"]=inventory
    progress={"freshness":{"targets":{"RHAISTRAT-1":{"rfe":"RHAIRFE-1"}}},
              "create":{"strategies":[{"rfe":"RHAIRFE-1","strat":"RHAISTRAT-1"}]}}
    assert history.proves_change(tmp_path,{"freshness":base},progress,"RHAIRFE-1") is expected


def test_formatting_only_sme_history_is_not_independent_proof(monkeypatch,tmp_path):
    import jira_utils

    before=json.dumps(jira_utils.markdown_to_adf("## Staff Engineer / SME Input\nUse **shared queue**."))
    after=json.dumps(jira_utils.markdown_to_adf("## Staff Engineer / SME Input\nUse **shared ****queue**."))
    monkeypatch.setattr(history,"histories",lambda key:[] if key.startswith("RHAIRFE-") else
                        [row(1,change("description",before,after))])
    monkeypatch.setattr(jira_utils,"require_env",lambda:("http://fixture","admin","admin"))
    monkeypatch.setattr(jira_utils,"get_issue",lambda *a,**kw:{"fields":{
        "labels":[],"status":{"name":"Open"},
        "issuelinks":[{"type":{"name":"Cloners"},"outwardIssue":{"key":"RHAIRFE-1"}}]}})
    base={"live":True,"strategy_history":{"RHAISTRAT-1":{
        "rfe":"RHAIRFE-1","cursor":{"id":None,"items":[]},"status":"Open","gate_labels":[]}}}
    progress={"freshness":{"targets":{"RHAISTRAT-1":{"rfe":"RHAIRFE-1"}}},
              "create":{"strategies":[{"rfe":"RHAIRFE-1","strat":"RHAISTRAT-1"}]}}
    assert not history.proves_change(tmp_path,{"freshness":base},progress,"RHAIRFE-1")


@pytest.mark.parametrize("identity", [None, "", False])
def test_history_without_a_valid_identity_is_unverifiable(monkeypatch, identity):
    import jira_utils

    monkeypatch.setattr(jira_utils, "require_env", lambda: ("http://fixture", "admin", "admin"))
    malformed = row(1)
    malformed["id"] = identity
    monkeypatch.setattr(
        jira_utils, "api_call", lambda *args: {"changelog": {"total": 1, "startAt": 0, "histories": [malformed]}}
    )
    assert history.cursor("RHAIRFE-1") is None
