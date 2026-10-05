"""The handoff to the Epic Decomposer: what each strat-creator type gives the next station.

tests/fixtures/epic-decomposer-contract.yaml records what the decomposer reads. rfe-strategy meets
all of it. initiative-strategy's gaps are pinned exactly, so a change on either side (Initiative
strategies cloned into RHAISTRAT, or the decomposer taking other work types, AISDLC-61) fails here
until the list is updated.
"""

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import type_registry  # noqa: E402

CONTRACT = yaml.safe_load((REPO / "tests" / "fixtures" / "epic-decomposer-contract.yaml").read_text(encoding="utf-8"))


def handoff_gaps(desc):
    gaps = set()
    if desc.get("identity.jira.project") != CONTRACT["project"]:
        gaps.add("project")
    if desc.get("identity.jira.issue_type") != CONTRACT["issue_type"]:
        gaps.add("issue_type")
    labels = desc.get("conventions.labels")
    if {labels["rubric_pass"], labels["human_sign_off"]} != set(CONTRACT["labels_all"]):
        gaps.add("labels")
    # A settings file's target_versions render as the Target Version field (jira_utils.build_jql_from_config).
    settings = yaml.safe_load((REPO / desc.get("discovery.settings")).read_text(encoding="utf-8"))
    if CONTRACT["target_version_field"] != "customfield_10855" or not settings["jql"].get("target_versions"):
        gaps.add("target_version")
    key = desc.get("identity.jira.key_prefixes")[0] + "1"
    if not re.fullmatch(CONTRACT["id_pattern"], key):
        gaps.add("id_pattern")
    attachment = desc.get("pipeline.body_overflow.attachment").format(issue_key=key)
    if not re.fullmatch(CONTRACT["attachment_pattern"], attachment):
        gaps.add("attachment")
    template = (REPO / desc.get("pipeline.prompts.template")).read_text(encoding="utf-8")
    if any(text not in template for text in CONTRACT["headings"] + CONTRACT["hlr_priorities"]):
        gaps.add("template")
    return gaps


def test_rfe_strategy_meets_the_decomposer_contract():
    assert handoff_gaps(type_registry.load().get("rfe-strategy")) == set()


def test_initiative_strategy_gaps_are_known():
    # Same ticket: an RHOAIENG Initiative is never selected by the decomposer's JQL, its key fails
    # every RHAISTRAT id check, and its overflow attachment name is not the one fetch_strategy reads.
    # Labels and the template already fit.
    assert handoff_gaps(type_registry.load().get("initiative-strategy")) == {
        "project",
        "issue_type",
        "target_version",
        "id_pattern",
        "attachment",
    }
