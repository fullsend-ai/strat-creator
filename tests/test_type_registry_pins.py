"""Pin tests: types/rfe-strategy/type.yaml equals every type-keyed literal a script still carries.

rfe-creator's discipline (its tests/test_type_registry_pins.py): the descriptor becomes source of
truth BY TEST before BY IMPORT. This file pins every hardcoded literal in
scripts/, the skill bodies and the eval config to a descriptor projection. The gate lists are
not in the descriptor: discovery.settings names config/pipeline-settings.yaml, the file #79 made
their source, and only that path is pinned here. Each consumer migration turns a pinned literal into an import-time read
of scripts/type_registry.py and DELETES its pin here — a pin of a derived value is a tautology.

How to read a failure: either a script changed a pinned value (update the descriptor in the same
PR — that is the mechanic) or the descriptor drifted from the code it documents. Neither may happen
silently. Citations are file:line on main at cc1ba8c (the baseline, not the worktree's current
line numbers). Values pinned by SOURCE FORM (a regex over a script's text) are the ones a script
holds inside a function body or an argparse default; they migrate like the rest.

Grandfathered, recorded on purpose:
  * pull_strategy.py:41 spells the attachment placeholder {strat_key} where push_strategy.py:51
    spells {issue_key}; the descriptor carries push's spelling and the pin renders pull's.

Retired (the consumer reads the registry now, so the pin would be a tautology):
  * scripts/jira_utils.py: build_jql_from_config's default project (:206), the
    Cloners helpers (:251 link_type, :256 key prefix), find_processed_rfe_ids strat_project (:287)
    and the strip_metadata title regex (:1028-1052, never pinned here). Their equality with the
    4c6ae1c literals is now tests/test_type_registry.py::test_jira_utils_derives_the_4c6ae1c_literals.
  * scripts/list-rfe-ids.py: the settings path (:28 SETTINGS_PATH) is the type's discovery.settings.
    tests/test_type_registry.py::test_each_type_names_its_settings_file checks the file exists.
  * Reviewers + sign-off: artifact_utils (strat-task / strat-review schemas, LABEL_CATEGORIES,
    compute_strat_labels), lock_issues (pipeline.lock), apply_scores (dimensions, dirs.reviews) and
    remove_draft_prefix (summary_prefix) read the descriptor. Equality with the 4c6ae1c values is
    tests/test_type_registry.py::test_review_consumers_equal_the_4c6ae1c_values (+ tests/fixtures).
"""

import hashlib
import inspect
import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import jira_utils  # noqa: E402
import pull_strategy  # noqa: E402
import push_strategy  # noqa: E402
import type_registry  # noqa: E402

REG = type_registry.load(extra_roots=[], env={})
D = REG.get("rfe-strategy")


def src(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def pin(descriptor_value, live_value, where):
    assert descriptor_value == live_value, (
        f"{where}: descriptor {descriptor_value!r} != live {live_value!r}"
    )


# ── identity + artifact schemas: derived (artifact_utils._strat_schemas); descriptor-internal checks only ──


def test_source_ref_grammar_is_the_input_grammar():
    pattern = D.get("schema.task.extra_fields.source_rfe.pattern")
    pin(D.get("inputs.0.source_ref_field"), "source_rfe", "inputs.0.source_ref_field — artifact_utils.py:149")
    assert D.get("inputs.0.jira.key_prefixes.0") + r"\d+" in pattern, "inputs.0.jira.key_prefixes — artifact_utils.py:152"


# ── labels (artifact_utils.py:250-282; lock_issues.py:43-56) ──────────────────────────────


def test_every_label_carries_the_prefix():
    prefix = D.get("conventions.label_prefix") + "-"
    for key, value in D.labels.items():
        assert value.startswith(prefix), f"conventions.labels.{key}={value!r} lacks {prefix!r}"


def test_lock_labels():
    lock = D.get("pipeline.lock")
    pin(lock["label"], D.labels["processing"], "pipeline.lock.label == conventions.labels.processing")


# ── inputs[0] + discovery vs jira_utils ──────────────────────────────────────────────────


def test_processed_lookup_override_rule():
    doc = jira_utils.find_processed_rfe_ids.__doc__
    assert D.get("inputs.0.skip_if.single_open_unlabeled_override") is True and "exactly one open" in doc, "jira_utils.py:294-298"


def test_pull_strategy_source_side():
    source = inspect.getsource(pull_strategy)
    assert f'"{D.get("inputs.0.relation.link_type")}"' in source, "pull_strategy.py:59"
    assert f'startswith("{D.get("inputs.0.jira.key_prefixes.0")}")' in source, "pull_strategy.py:64"
    pin(D.get("pipeline.body_overflow.attachment").replace("{issue_key}", "{strat_key}"), pull_strategy.STRATEGY_ATTACHMENT_TEMPLATE, "pipeline.body_overflow.attachment — pull_strategy.py:41 (grandfathered placeholder name)")
    pin(D.get("workspace.root"), inspect.signature(pull_strategy.pull_strategy).parameters["local_dir"].default, "workspace.root — pull_strategy.py:86")
    assert f'"workflow": None if pipeline_rfe is not None else "{D.get("workspace.root")}"' in source, \
        "pull_strategy.py workflow=local (a pipeline import has none)"
    assert D.get("companions.comments") is True and "-comments.md" in source, "companions.comments — pull_strategy.py:222-229"


def test_find_strat_for_rfe_source_form():
    source = src("scripts/find_strat_for_rfe.py")
    assert f'"{D.get("inputs.0.relation.link_type")}"' in source, "find_strat_for_rfe.py:42"
    assert f'startswith("{D.write_prefix}")' in source, "find_strat_for_rfe.py:47"
    assert '("outwardIssue", "inwardIssue")' in source, "both directions searched — find_strat_for_rfe.py:44"


def test_clone_issue_source_form():
    source = src("scripts/clone_issue.py")
    parent = D.get("inputs.0.parent_gate")
    assert f'startswith("{parent["key_prefixes"][0]}")' in source, "inputs.0.parent_gate.key_prefixes — clone_issue.py:43"
    assert f'!= "{parent["issue_type"]}"' in source, "inputs.0.parent_gate.issue_type — clone_issue.py:58"
    assert f'== "{parent["statuses_not"][0]}"' in source, "inputs.0.parent_gate.statuses_not — clone_issue.py:64"
    assert f'default="{D.get("identity.jira.issue_type")}"' in source, "identity.jira.issue_type — clone_issue.py:79"
    for field in D.get("inputs.0.copy_fields"):
        assert f'"{field}"' in source, f"inputs.0.copy_fields {field!r} — clone_issue.py:89-92"
    prefix = D.get("conventions.summary_prefix.value")
    assert f'startswith("{prefix}")' in source and f"{prefix}{{summary}}" in source, "conventions.summary_prefix — clone_issue.py:96-97"


def test_removed_context_marker():
    marker = D.get("inputs.0.removed_context_marker")
    line = src(".claude/skills/strategy-refine/SKILL.md")
    assert f"`{marker['comment_prefix']}`" in line and marker["phrase"] in line, "strategy-refine/SKILL.md:123"


# ── dirs / workspace / files ──────────────────────────────────────────────────────────────


def test_dirs():
    assert f'default="{D.dirs()["tasks"]}"' in src("scripts/push_refined_strategies.py"), "dirs.tasks — push_refined_strategies.py:60"
    claude = src("CLAUDE.md")
    for key, value in D.dirs("bare").items():
        assert f"{value}/" in claude, f"dirs.{key} — CLAUDE.md:10-24"
    assert f"{D.get('workspace.root')}/" in claude, "workspace.root — CLAUDE.md:21"


def test_files():
    skipped = os.path.basename(D.get("files.skipped"))
    assert skipped in src("scripts/generate-report.py"), "files.skipped — generate-report.py:99"
    assert D.get("files.skipped") in src(".claude/skills/strategy-create/SKILL.md"), "files.skipped — strategy-create/SKILL.md:66"
    assert os.path.basename(D.get("files.tickets")) in src("CLAUDE.md"), "files.tickets — CLAUDE.md:19"


# ── push: the body contract ───────────────────────────────────────────────────────────────


def test_section_ownership():
    sections = D.get("pipeline.section_ownership")
    pin(sections[0]["heading"], jira_utils.BUSINESS_NEED_HEADING, "pipeline.section_ownership.0 — jira_utils.py:1127")
    pin(sections[1]["heading"], push_strategy.STRATEGY_HEADING, "pipeline.section_ownership.1 — push_strategy.py:41")
    pin(sections[2]["heading"], push_strategy.STAFF_INPUT_HEADING, "pipeline.section_ownership.2 — push_strategy.py:42")
    pin([s["owner"] for s in sections], ["source", "pipeline", "human"], "section owners")


def test_body_overflow():
    overflow = D.get("pipeline.body_overflow")
    pin(overflow["attachment"], push_strategy.STRATEGY_ATTACHMENT_TEMPLATE, "pipeline.body_overflow.attachment — push_strategy.py:51")
    assert f'"{overflow["on_error"]}"' in inspect.getsource(push_strategy), "pipeline.body_overflow.on_error — push_strategy.py:407"


def test_draft_prefix_lifecycle():
    prefix = D.get("conventions.summary_prefix")
    assert prefix["added_at"] in D.stages and prefix["removed_at"] in D.stages
    signoff = src(".claude/skills/strategy-signoff/SKILL.md")
    assert f"Remove {prefix['value'].strip()} Prefix" in signoff, "strategy-signoff/SKILL.md:99"


# ── pipeline: stages, dimensions, rubric, scorer ──────────────────────────────────────────


def test_stages_are_skills():
    for stage in D.stages:
        assert (REPO / ".claude" / "skills" / f"strategy-{stage}" / "SKILL.md").is_file(), stage


def test_dimensions_are_the_review_skills():
    review = src(".claude/skills/strategy-review/SKILL.md")
    dims = D.get("pipeline.dimensions")
    pin([d["name"] for d in dims], D.score_fields, "pipeline.dimensions[].name == schema.review.score_fields")
    for dim in dims:
        assert (REPO / dim["prompt"]).is_file(), dim["prompt"]
        assert f'Skill(skill="strategy-{dim["name"]}-review"' in review, f"strategy-review/SKILL.md:137-140 {dim['name']}"
    architecture = dims[3]
    assert architecture["condition"] == {"context_exists": ".context/architecture-context"}
    assert architecture["condition"]["context_exists"] + "/" in review, "strategy-review/SKILL.md:94"


def test_verdict_rules_and_review_labels():
    review = src(".claude/skills/strategy-review/SKILL.md")
    rules = D.get("schema.review.verdict_rules")
    assert f"total >= {rules['approve']['total_min']}" in review and "no zeros" in review, "strategy-review/SKILL.md:127"
    assert f"total >= {rules['revise']['total_min']}" in review, "strategy-review/SKILL.md:128"
    publish = src("scripts/publish_review.py")
    assert f"/{2 * len(D.score_fields)})" in publish, "publish_review.py review_comment Score: {total}/8"
    assert f"*{D.get('conventions.comment_prefix')}*" in publish, "conventions.comment_prefix — publish_review.py review_comment"
    assert f'"{D.labels["rubric_pass"]}"' in publish and f'"{D.labels["needs_attention"]}"' in publish, "publish_review.py verdict labels"


def test_signoff_label():
    signoff = src(".claude/skills/strategy-signoff/SKILL.md")
    assert f"['{D.labels['human_sign_off']}']" in signoff, "conventions.labels.human_sign_off — strategy-signoff/SKILL.md:93"
    assert D.labels["rubric_pass"] in signoff, "strategy-signoff/SKILL.md:27"


def test_rubric():
    rubric = D.get("pipeline.rubric")
    digest = hashlib.sha256((REPO / rubric["path"]).read_bytes()).hexdigest()
    pin(rubric["rubric_version"], digest, "pipeline.rubric.rubric_version — sha256 of pipeline.rubric.path")
    export = src("scripts/assess-strat/export_rubric.py")
    assert f'"{rubric["export"]}"' in export, "pipeline.rubric.export — export_rubric.py:15"
    assert os.path.basename(rubric["path"]) in export, "pipeline.rubric.path — export_rubric.py:9,:21"


def test_scorer_agent_and_template():
    agent = src(".claude/agents/strat-scorer.md")
    pin(D.get("pipeline.scorer_agent"), re.search(r"^name:\s*(\S+)", agent, re.M).group(1), "pipeline.scorer_agent — .claude/agents/strat-scorer.md:2")
    assert D.get("pipeline.scorer_agent") in src(".claude/skills/assess-strat/SKILL.md"), "assess-strat/SKILL.md:55"
    assert (REPO / D.get("pipeline.prompts.template")).is_file()
    bootstrap = D.get("pipeline.context_sources.0.bootstrap")
    assert (REPO / bootstrap).is_file() and bootstrap in src("CLAUDE.md"), "pipeline.context_sources — CLAUDE.md:128"


# ── eval ──────────────────────────────────────────────────────────────────────────────────


def test_eval():
    config = yaml.safe_load((REPO / D.get("eval.config")).read_text(encoding="utf-8"))
    pin(D.get("eval.thresholds"), config["thresholds"], "eval.thresholds — eval/strat-refine.yaml:425-437")
    pin(D.get("eval.timeout"), config["execution"]["timeout"], "eval.timeout — eval/strat-refine.yaml:18")
    pin(D.get("eval.dataset"), "eval/" + config["dataset"]["path"], "eval.dataset — eval/strat-refine.yaml:70")


# ── binding prose (CLAUDE.md:83-91; strategy-create/SKILL.md) ────────────────────────────


def test_claude_md_binding_prose():
    claude = src("CLAUDE.md")
    assert f"**Project**: `{D.get('identity.jira.project')}`" in claude, "CLAUDE.md:84"
    assert f"**Issue Type**: `{D.get('identity.jira.issue_type')}`" in claude, "CLAUDE.md:85"
    assert f"`{D.get('inputs.0.relation.link_type')}`" in claude, "CLAUDE.md:86"
    assert f"**Project**: `{D.get('inputs.0.jira.project')}`" in claude, "CLAUDE.md:90"
    assert f"**Issue Type**: `{D.get('inputs.0.jira.issue_type')}`" in claude, "CLAUDE.md:91"


def test_create_skill_binding_prose():
    create = src(".claude/skills/strategy-create/SKILL.md")
    assert f"--target-project {D.get('identity.jira.project')} --issue-type {D.get('identity.jira.issue_type')}" in create, "strategy-create/SKILL.md:104"
    assert f"{D.get('inputs.0.jira.project')}-1146 → `{D.local_prefix}1146`" in create, "identity.local_prefix — strategy-create/SKILL.md:17"
    # #79 (2026-10-04): Step 2a reads the gate lists from config/pipeline-settings.yaml and carries no copy.
    jql = yaml.safe_load((REPO / D.get("discovery.settings")).read_text(encoding="utf-8"))["jql"]
    gate_values = jql["required_labels"] + jql["quality_labels"]
    assert D.get("discovery.settings") in create and not [v for v in gate_values if v in create], "strategy-create/SKILL.md Step 2a"
