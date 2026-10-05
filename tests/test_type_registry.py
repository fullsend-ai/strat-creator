"""Unit tests for scripts/type_registry.py and scripts/validate_types.py."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import apply_scores  # noqa: E402
import artifact_utils  # noqa: E402
import clone_issue  # noqa: E402
import find_strat_for_rfe  # noqa: E402
import jira_utils  # noqa: E402
import lock_issues  # noqa: E402
import pull_strategy  # noqa: E402
import push_strategy  # noqa: E402
import remove_draft_prefix  # noqa: E402
import type_registry  # noqa: E402
import validate_types  # noqa: E402

# Hermetic: the shipped root only, no drop-in roots from the environment.
REG = type_registry.load(extra_roots=[], env={})
SHIPPED = "rfe-strategy"
TWIN = "initiative-strategy"
SHIPPED_ALL = [TWIN, SHIPPED]  # directory order


def _dropin(root, name, mutate=None):
    """Copy the shipped descriptor under <root>/<name>/type.yaml as `type: <name>`."""
    data = yaml.safe_load((REPO / "types" / SHIPPED / "type.yaml").read_text(encoding="utf-8"))
    data["type"] = name
    if mutate:
        mutate(data)
    (root / name).mkdir(parents=True)
    (root / name / "type.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return root


# ── registry ──────────────────────────────────────────────────────────────────────────────


def test_shipped_types():
    assert REG.names() == SHIPPED_ALL
    assert SHIPPED in REG and TWIN in REG
    assert len(REG) == 2
    assert [d.name for d in REG] == SHIPPED_ALL


def test_get_dotted_path():
    d = REG.get(SHIPPED)
    assert d.get("identity.jira.project") == "RHAISTRAT"
    assert d.get("inputs.0.relation.link_type") == "Cloners"
    assert d.get("pipeline.dimensions.3.name") == "architecture"
    assert d.get("no.such.field", default=None) is None
    assert d.get("pipeline.dimensions.9.name", "fallback") == "fallback"
    with pytest.raises(KeyError, match="no such descriptor field"):
        d.get("no.such.field")


def test_unknown_type_names_the_registered_ones():
    with pytest.raises(KeyError, match=f"registered: {', '.join(SHIPPED_ALL)}"):
        REG.get("nope")


def test_owns_and_detect():
    d = REG.get(SHIPPED)
    assert d.owns("STRAT-12")
    assert d.owns("RHAISTRAT-400")
    assert not d.owns("RHAIRFE-1")
    assert not d.owns("")
    assert not d.owns(None)
    assert REG.detect("RHAISTRAT-400") is d
    assert REG.detect("STRAT-7") is d
    assert REG.detect("RHAIRFE-1") is None
    assert REG.detect("RHOAIENG-5").name == TWIN
    assert REG.detect("ISTRAT-2").name == TWIN
    assert REG.detect(None) is None


def test_detect_refuses_an_ambiguous_id(tmp_path):
    root = _dropin(tmp_path, "twin-strategy")  # same id grammar as the shipped type
    reg = type_registry.load(extra_roots=[root], env={})
    with pytest.raises(type_registry.RegistryError, match="claimed by"):
        reg.detect("RHAISTRAT-1")


def test_dirs_forms():
    d = REG.get(SHIPPED)
    assert d.dirs()["tasks"] == "artifacts/strat-tasks"
    assert d.dirs("bare") == {"tasks": "strat-tasks", "originals": "strat-originals", "reviews": "strat-reviews"}
    with pytest.raises(ValueError):
        d.dirs("elsewhere")


def test_projections():
    d = REG.get(SHIPPED)
    assert d.tracker == "jira"
    assert d.key_prefixes == ["RHAISTRAT-"]
    assert d.write_prefix == "RHAISTRAT-"
    assert d.local_prefix == "STRAT-"
    assert d.local_id_pattern == r"^STRAT-\d+$"
    assert d.id_field == "strat_id"
    assert d.tracker_key_field == "jira_key"
    assert d.score_fields == ["feasibility", "testability", "scope", "architecture"]
    assert d.stages == ["create", "refine", "review", "pull", "push", "signoff"]
    assert d.labels["processing"] == "strat-creator-processing"
    assert repr(d).startswith("Descriptor('rfe-strategy'")


def test_extra_roots_by_argument_and_by_env(tmp_path):
    root = _dropin(tmp_path, "third-strategy")
    assert type_registry.load(extra_roots=[root], env={}).names() == SHIPPED_ALL + ["third-strategy"]
    env = {type_registry.EXTRA_ROOTS_ENV: str(root)}
    assert type_registry.load(env=env).names() == SHIPPED_ALL + ["third-strategy"]
    assert type_registry.load(env={type_registry.EXTRA_ROOTS_ENV: ""}).names() == SHIPPED_ALL


def test_duplicate_type_across_roots(tmp_path):
    root = _dropin(tmp_path, SHIPPED)
    with pytest.raises(type_registry.RegistryError, match="duplicate type"):
        type_registry.load(extra_roots=[root], env={})


def test_type_must_match_directory_name(tmp_path):
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "type.yaml").write_text("type: bar\n", encoding="utf-8")
    with pytest.raises(type_registry.RegistryError, match="does not match its directory name"):
        type_registry.load(root=tmp_path, extra_roots=[], env={})


def test_missing_descriptor_and_root(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(type_registry.RegistryError, match="missing type.yaml"):
        type_registry.load(root=tmp_path, extra_roots=[], env={})
    with pytest.raises(type_registry.RegistryError, match="not a directory"):
        type_registry.load(root=tmp_path / "absent", extra_roots=[], env={})
    # an absent EXTRA root is skipped, not an error
    assert type_registry.load(extra_roots=[tmp_path / "absent"], env={}).names() == SHIPPED_ALL


# ── CLI ───────────────────────────────────────────────────────────────────────────────────


def test_cli(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv(type_registry.EXTRA_ROOTS_ENV, raising=False)
    assert type_registry.main(["list"]) == 0
    assert capsys.readouterr().out.split() == SHIPPED_ALL

    assert type_registry.main(["get", SHIPPED, "identity.jira.project"]) == 0
    assert capsys.readouterr().out.strip() == "RHAISTRAT"

    assert type_registry.main(["get", SHIPPED, "identity.jira.key_prefixes", "--json"]) == 0
    assert capsys.readouterr().out.strip() == '[\n  "RHAISTRAT-"\n]'

    assert type_registry.main(["show", SHIPPED]) == 0
    assert "type: rfe-strategy" in capsys.readouterr().out

    assert type_registry.main(["get", SHIPPED, "no.such"]) == 1
    assert "no such descriptor field" in capsys.readouterr().err
    assert type_registry.main(["show", "nope"]) == 1
    assert "unknown type" in capsys.readouterr().err

    assert type_registry.main(["--root", str(tmp_path / "absent"), "list"]) == 1
    assert "not a directory" in capsys.readouterr().err


# ── the --type ladder ─────────────────────────────────────────────────────────────────────


def _two_types(tmp_path):
    """A hermetic root: rfe-strategy plus "other-strategy" with its own ids and input."""
    def other(data):
        data["identity"]["jira"]["key_prefixes"] = ["OTHER-"]
        data["identity"]["local_prefix"] = "OSTRAT-"
        data["identity"]["local_id_pattern"] = r"^OSTRAT-\d+$"
        data["inputs"][0]["jira"]["key_prefixes"] = ["OTHERIN-"]
    root = _dropin(tmp_path / "types", SHIPPED)
    _dropin(root, "other-strategy", other)
    return type_registry.load(root=root, extra_roots=[], env={})


def test_candidates_rungs(tmp_path):
    reg = _two_types(tmp_path)
    ids = ["STRAT-1", "RHAISTRAT-1", "RHAIRFE-1", "OTHERIN-1", "FOO-1", ""]
    assert [(r, [d.name for d in m]) for r, m in map(reg.candidates, ids)] == [
        ("local_id_pattern", [SHIPPED]),
        ("key_prefix", [SHIPPED]),
        ("input_key_prefix", [SHIPPED]),  # strat-creator's own rung: the input's id names the type
        ("input_key_prefix", ["other-strategy"]),
        (None, []),
        (None, []),
    ]


def test_resolve_ladder(tmp_path):
    reg = _two_types(tmp_path)
    resolve = type_registry.resolve
    assert resolve(reg, env={}).line() == f"TYPE RESOLVED: {SHIPPED} (legacy default)"
    assert resolve(reg, explicit_type="other-strategy", env={}).line() == "TYPE RESOLVED: other-strategy (--type)"
    assert resolve(reg, ids=["RHAIRFE-1"], env={}).line() == f"TYPE RESOLVED: {SHIPPED} (id grammar)"
    assert resolve(reg, ids=["OTHER-7"], env={}).line() == "TYPE RESOLVED: other-strategy (id grammar)"
    tasks = tmp_path / "artifacts" / "strat-tasks"
    tasks.mkdir(parents=True)
    (tasks / "notes.md").write_text("---\ntype: other-strategy\n---\n# x\n")
    fm = resolve(reg, artifact=tasks / "notes.md", env={})
    assert fm.line() == "TYPE RESOLVED: other-strategy (frontmatter type)"
    (tasks / "OTHER-3.md").write_text("# no frontmatter\n")
    assert resolve(reg, artifact=tasks / "OTHER-3.md", env={}).line() == "TYPE RESOLVED: other-strategy (artifact dir)"
    (tasks / "loose.md").write_text("# no frontmatter, a stem no type owns\n")
    ambiguous = resolve(reg, artifact=tasks / "loose.md", env={})  # both types share strat-tasks/
    assert ambiguous.ambiguous and ambiguous.line() == "TYPE AMBIGUOUS: other-strategy, rfe-strategy - pass --type"
    with pytest.raises(type_registry.ResolveError, match="ambiguous type") as exc:
        resolve(reg, artifact=tasks / "loose.md", env={"CI": "true"})
    assert exc.value.exit_code == type_registry.EXIT_AMBIGUOUS


def test_resolve_errors(tmp_path):
    reg = _two_types(tmp_path)
    resolve, err = type_registry.resolve, type_registry.ResolveError
    with pytest.raises(err, match="unknown type 'nope' \\(--type\\); registered types: other-strategy, rfe-strategy"):
        resolve(reg, explicit_type="nope", env={})
    with pytest.raises(err, match="conflicting type signals: --type other-strategy vs RHAIRFE-1"):
        resolve(reg, explicit_type="other-strategy", ids=["RHAIRFE-1"], env={})
    with pytest.raises(err, match="conflicting type signals"):
        resolve(reg, ids=["RHAIRFE-1", "OTHER-1"], env={})
    # A headless run never guesses: an id no type owns is an error there, no signal interactively.
    assert resolve(reg, ids=["FOO-1"], env={}).rung == type_registry.LEGACY_DEFAULT_RUNG
    for env in ({"CI": "true"}, {"GITHUB_ACTIONS": "1"}, {"STRAT_CREATOR_HEADLESS": "yes"}):
        with pytest.raises(err, match="no registered type owns id 'FOO-1'"):
            resolve(reg, ids=["FOO-1"], env=env)
    assert resolve(reg, ids=["FOO-1"], env={"CI": "false"}).rung == type_registry.LEGACY_DEFAULT_RUNG
    assert resolve(reg, explicit_type=SHIPPED, ids=["FOO-1"], env={"CI": "true"}).rung == "--type"
    with pytest.raises(err, match="no registered type owns id"):
        resolve(reg, ids=["FOO-1"], env={}, headless=True)


def test_resolve_cli(capsys, monkeypatch):
    monkeypatch.delenv(type_registry.EXTRA_ROOTS_ENV, raising=False)
    for var in type_registry.HEADLESS_MARKER_VARS:
        monkeypatch.delenv(var, raising=False)
    assert type_registry.main(["resolve", "RHAIRFE-1"]) == 0
    assert capsys.readouterr().out == f"TYPE RESOLVED: {SHIPPED} (id grammar)\n"
    assert type_registry.main(["resolve"]) == 0
    assert capsys.readouterr().out == f"TYPE RESOLVED: {SHIPPED} (legacy default)\n"
    assert type_registry.main(["resolve", "--type", "nope"]) == 1
    registered = ", ".join(REG.names())
    assert capsys.readouterr().err == f"ERROR: unknown type 'nope' (--type); registered types: {registered}\n"
    assert type_registry.main(["resolve", "--headless", "FOO-1"]) == 1
    assert "a headless run never guesses" in capsys.readouterr().err
    assert type_registry.main(["resolve", "--json", "--type", SHIPPED]) == 0
    assert '"rung": "--type"' in capsys.readouterr().out


# ── gate 1 ────────────────────────────────────────────────────────────────────────────────


def test_gate1_passes_on_the_shipped_types(capsys):
    assert validate_types.main([]) == 0
    assert f"OK: 2 descriptor(s) valid: {', '.join(SHIPPED_ALL)}" in capsys.readouterr().out


def test_gate1_findings(tmp_path, capsys):
    def drop_project(data):
        del data["identity"]["jira"]["project"]

    root = _dropin(tmp_path, "broken-strategy", drop_project)
    (root / "broken-strategy" / "helper.py").write_text("print()\n", encoding="utf-8")
    (root / "renamed").mkdir()
    (root / "renamed" / "type.yaml").write_text("type: other\n", encoding="utf-8")
    assert validate_types.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "'project' is a required property" in out
    assert "code file under a descriptor root: helper.py" in out
    assert "does not match the directory name 'renamed'" in out


def test_gate1_requires_what_the_importers_read(tmp_path, capsys):
    """artifact_utils reads schema.task and inputs[0].source_ref_field at import, so gate 1 must
    refuse a descriptor without them rather than let every importer crash."""
    def drop_task_and_ref(data):
        del data["schema"]["task"]
        del data["inputs"][0]["source_ref_field"]

    def drop_inputs(data):
        data["inputs"] = []

    root = _dropin(tmp_path, "no-task-strategy", drop_task_and_ref)
    assert validate_types.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "'task' is a required property" in out
    assert "'source_ref_field' is a required property" in out

    root = _dropin(tmp_path / "b", "no-input-strategy", drop_inputs)
    assert validate_types.main(["--root", str(root)]) == 1
    assert "[] should be non-empty" in capsys.readouterr().out


def test_gate1_binding_uniqueness(tmp_path):
    _dropin(tmp_path, "a-strategy")
    _dropin(tmp_path, "b-strategy")  # same (RHAISTRAT, Feature) pair
    findings, names = validate_types.validate_all(tmp_path)
    assert names == ["a-strategy", "b-strategy"]
    assert findings == ["types/b-strategy and types/a-strategy both claim the jira binding (RHAISTRAT, Feature)"]


def test_gate1_no_types_is_a_finding(tmp_path):
    (tmp_path / "_schema").mkdir()
    schema = (REPO / "types" / "_schema" / "type.schema.json").read_text(encoding="utf-8")
    (tmp_path / "_schema" / "type.schema.json").write_text(schema, encoding="utf-8")
    findings, names = validate_types.validate_all(tmp_path)
    assert names == [] and findings == [f"{tmp_path}: no type directories"]


def test_default_root_is_file_relative():
    assert type_registry.DEFAULT_ROOT == REPO / "types"
    assert os.path.isdir(type_registry.DEFAULT_ROOT)


# ── the first consumer ────────────────────────────────────────────────────────────────────


def test_jira_utils_derives_the_4c6ae1c_literals(monkeypatch):
    """The first consumer composes what used to be literals; equality with the text on main at
    4c6ae1c is the byte-identical check (their pins were deleted with that change)."""
    assert jira_utils._TYPE.name == SHIPPED
    assert jira_utils._TITLE_KEY_RE.pattern == r"^#\s+(RFE-\d+|RHAIRFE-\d+|STRAT-\d+|RHAISTRAT-\d+):"
    seen = []  # find_processed_rfe_ids queries the default type's project (the 4c6ae1c default)
    monkeypatch.setattr(jira_utils, "search_issues", lambda s, u, t, jql, **kw: seen.append(jql) or [])
    jira_utils.find_processed_rfe_ids("s", "u", "t", ["x"])
    assert seen == ['project = RHAISTRAT AND (labels = "x")']
    link = {"type": {"name": "Cloners"}, "outwardIssue": {"key": "RHAIRFE-1"}, "inwardIssue": {"key": "RHAISTRAT-2"}}
    assert jira_utils._linked_input_key(link) == "RHAIRFE-1"
    assert jira_utils._linked_input_key({**link, "type": {"name": "Blocks"}}) is None
    inward_only = {"type": {"name": "Cloners"}, "inwardIssue": {"key": "RHAIRFE-3"}}
    assert jira_utils._linked_input_key(inward_only) == "RHAIRFE-3"
    assert jira_utils._linked_input_key({"type": {"name": "Cloners"}, "inwardIssue": None}) is None


# ── the intake gate ───────────────────────────────────────────────────────────────────────

# What build_jql_from_config produced from config/pipeline-settings.yaml on main at b396172.
FROZEN_INTAKE_JQL = (
    'project = RHAIRFE AND (labels = "strat-creator-3.5" OR labels = "strat-creator-3.6" OR labels = "str'
    'at-creator-3.7" OR labels = "strat-creator-3.8" OR cf[10855] in ("rhoai-3.5", "rhoai-3.5.EA1", "rhoa'
    'i-3.5.EA2", "rhoai-3.6", "rhoai-3.6.EA1", "rhoai-3.6.EA2", "rhai-3.6", "rhai-3.7", "rhai-3.8", "3.5 '
    'EA1 RHAII RELEASE", "3.5 EA1 RHOAI RELEASE", "3.5 EA1 RHELAI RELEASE", "3.5 EA2 RHAII RELEASE", "3.5'
    ' EA2 RHOAI RELEASE", "3.5 EA2 RHELAI RELEASE", "3.5 GA RHAII RELEASE", "3.5 GA RHOAI RELEASE", "3.5 '
    'GA RHELAI RELEASE", "3.6 EA1 RHAII RELEASE", "3.6 EA1 RHOAI RELEASE", "3.6 EA1 RHELAI RELEASE", "3.6'
    ' EA2 RHAII RELEASE", "3.6 EA2 RHOAI RELEASE", "3.6 EA2 RHELAI RELEASE", "3.6 GA RHAII RELEASE", "3.6'
    ' GA RHOAI RELEASE", "3.6 GA RHELAI RELEASE", "3.7 EA RHAII RELEASE", "3.7 EA RHOAI RELEASE", "3.7 EA'
    ' RHELAI RELEASE", "3.7 GA  RHAII RELEASE", "3.7 GA RHOAI RELEASE", "3.7 GA RHELAI RELEASE", "3.8 EA1'
    ' RHAII RELEASE", "3.8 EA1 RHOAI RELEASE", "3.8 EA1 RHELAI RELEASE", "3.8 EA2 RHAII RELEASE", "3.8 EA'
    '2 RHOAI RELEASE", "3.8 EA2 RHELAI RELEASE", "3.8 GA RHAII RELEASE", "3.8 GA RHOAI RELEASE", "3.8 GA '
    'RHELAI RELEASE")) AND (labels = "rfe-creator-autofix-rubric-pass" OR labels = "tech-reviewed") AND ('
    'labels NOT IN ("strat-creator-processing") OR labels IS EMPTY) AND status NOT IN ("Closed", "Resolve'
    'd", "Draft") ORDER BY key ASC'
)


def test_intake_jql_is_the_b396172_text():
    assert jira_utils.build_jql_from_type(REG.get(SHIPPED)) == FROZEN_INTAKE_JQL
    assert jira_utils.build_jql_from_type() == FROZEN_INTAKE_JQL


def test_each_type_names_its_settings_file():
    for name in REG.choices():
        path = REG.get(name).get("discovery.settings")
        assert (REPO / path).is_file(), f"{name}: discovery.settings {path}"


def test_processed_check_refuses_a_non_clones_relation(tmp_path, monkeypatch):
    def parent(data):
        data["inputs"][0]["relation"] = {"kind": "parent"}
    desc = type_registry.load(extra_roots=[_dropin(tmp_path, "parent-strategy", parent)], env={}).get("parent-strategy")
    monkeypatch.setattr(jira_utils, "search_issues", lambda *a, **kw: pytest.fail("queried Jira"))
    with pytest.raises(ValueError, match="follows a clones relation, not 'parent'"):
        jira_utils.find_processed_rfe_ids("s", "u", "t", ["x"], desc=desc)


# ── reviewers + sign-off ──────────────────────────────────────────────────────────────────


def test_review_consumers_equal_the_4c6ae1c_values():
    """What artifact_utils, lock_issues, apply_scores and remove_draft_prefix carried as literals on
    main at 4c6ae1c, now derived from the descriptor: same values, same field order. The one field
    added since is strat-task's self-describing `type`, appended last with no default (test_type_stamp);
    #85 widened the reviewer enums."""
    frozen = json.loads((REPO / "tests" / "fixtures" / "strat-schemas-4c6ae1c.json").read_text(encoding="utf-8"))
    frozen["strat-task"]["type"] = {"type": "string", "required": False, "enum": [SHIPPED]}
    frozen["order"]["strat-task"].append("type")
    # #85 added the provisional reviewer values: pending for each specialist, skipped for architecture.
    for name, spec in frozen["strat-review"]["reviewers"]["fields"].items():
        spec["enum"] += ["skipped", "pending"] if name == "architecture" else ["pending"]
    for name in ("strat-task", "strat-review"):
        assert artifact_utils.SCHEMAS[name] == frozen[name], name
        assert list(artifact_utils.SCHEMAS[name]) == frozen["order"][name], f"{name} field order"
    assert list(artifact_utils.SCHEMAS) == [
        "rfe-task", "rfe-review", "strat-task", "strat-review", f"{TWIN}-task", f"{TWIN}-review"]
    assert artifact_utils.LABEL_CATEGORIES == {
        "strat-creator-auto-created": "provenance",
        "strat-creator-auto-refined": "provenance",
        "strat-creator-auto-revised": "provenance",
        "strat-creator-rubric-pass": "gate",
        "strat-creator-needs-attention": "escalation",
        "strat-creator-ignore": "exclusion",
        "strat-creator-human-sign-off": "gate",
    }
    assert artifact_utils.label_category("strat-creator-processing") == "unknown"
    assert lock_issues.PROCESSING_LABEL == "strat-creator-processing"
    assert lock_issues.BLOCKING_LABELS == {
        "strat-creator-processing", "strat-creator-needs-attention", "strat-creator-human-sign-off"}
    assert lock_issues.STRAT_REQUIRED_LABEL == "strat-creator-auto-created"
    assert lock_issues.STRAT_BLOCKING_LABELS == {"strat-creator-needs-attention", "strat-creator-human-sign-off"}
    assert remove_draft_prefix.DRAFT_PREFIX == "[DRAFT] "
    assert apply_scores.DIMENSIONS == ["feasibility", "testability", "scope", "architecture"]
    columns = [apply_scores.COLUMNS[d] for d in apply_scores.DIMENSIONS]
    assert columns == ["Feasibility", "Testability", "Scope", "Architecture"]
    assert apply_scores.TOTAL_FIELD == "total" and apply_scores.MAX_TOTAL == 8
    assert os.path.normpath(apply_scores.REVIEW_DIR_DEFAULT) == str(REPO / "artifacts" / "strat-reviews")


def test_type_stamp(tmp_path):
    """rfe-creator PR 184's self-describing `type`, on strategy task files: written only when a writer
    passes it (no default, no back-fill), appended after the fields given, refused when it names
    another type (that type's schema refuses the file), read by resolve()'s frontmatter rung, and
    stripped with the frontmatter before a push, so nothing new reaches Jira."""
    new_task = ["strat_id=RHAISTRAT-500", "title=New strat", "source_rfe=RHAIRFE-101",
                "jira_key=RHAISTRAT-500", "priority=Major", "status=Draft"]  # strategy-create's set

    def fm_set(where, *fields):
        path = tmp_path / where / "strat-tasks" / "RHAISTRAT-500.md"
        argv = [sys.executable, str(REPO / "scripts" / "frontmatter.py"), "set", str(path), *fields]
        return path, subprocess.run(argv, capture_output=True, text=True)

    plain, run = fm_set("plain", *new_task)
    assert run.returncode == 0, run.stderr
    typed, run = fm_set("typed", *new_task, f"type={SHIPPED}")
    assert run.returncode == 0, run.stderr
    before = plain.read_text(encoding="utf-8")
    stamped = typed.read_text(encoding="utf-8")
    assert "type:" not in before
    assert stamped == before.replace("status: Draft\n", f"status: Draft\ntype: {SHIPPED}\n", 1)
    for other, reason in ((TWIN, "Unknown field: source_rfe"), ("bogus", f"type: 'bogus' not in ['{SHIPPED}']")):
        _, run = fm_set("typed", f"type={other}")
        assert run.returncode == 1 and reason in run.stderr, other
    assert typed.read_text(encoding="utf-8") == stamped
    assert type_registry.resolve(REG, artifact=typed, env={"CI": "true"}).rung == "frontmatter type"
    assert jira_utils.strip_metadata(stamped) == jira_utils.strip_metadata(before)


def test_strat_schemas_follow_a_drop_in_type(tmp_path):
    """A type with a different id grammar and one fewer dimension gets its own schemas."""
    def mutate(data):
        data["identity"]["local_prefix"] = "INIT-"
        data["identity"]["local_id_pattern"] = r"^INIT-\d+$"
        data["identity"]["jira"]["key_prefixes"] = ["RHAIINIT-"]
        data["schema"]["review"]["score_fields"] = ["feasibility", "scope"]
        data["schema"]["review"]["extra_fields"]["reviewers"]["fields"] = {
            k: v for k, v in data["schema"]["review"]["extra_fields"]["reviewers"]["fields"].items()
            if k in ("feasibility", "scope")}
    root = _dropin(tmp_path, "other-strategy", mutate)
    desc = type_registry.load(extra_roots=[root], env={}).get("other-strategy")
    schemas = artifact_utils._strat_schemas(desc)
    assert schemas["strat-task"]["strat_id"]["pattern"] == r"^(INIT-\d+|RHAIINIT-\d+)$"
    assert list(schemas["strat-review"]["scores"]["fields"]) == ["feasibility", "scope", "total"]
    assert list(schemas["strat-review"]["reviewers"]["fields"]) == ["feasibility", "scope"]
    assert list(schemas["strat-task"]) == list(artifact_utils.SCHEMAS["strat-task"])


# ── the twin ─────────────────────────────────────────────────────────────────────────────


def test_initiative_strategy_descriptor(monkeypatch):
    twin = REG.get(TWIN)
    rfe = REG.get(SHIPPED)
    assert twin.get("identity.jira.project") == "RHOAIENG" and twin.write_prefix == "RHOAIENG-"
    assert twin.get("inputs.0.relation.kind") == "self" and twin.get("inputs.0.from_type") == "initiative"
    assert twin.labels == rfe.labels, "one label vocabulary per station"
    assert twin.stages == rfe.stages
    assert twin.get("pipeline.rubric.rubric_version") == rfe.get("pipeline.rubric.rubric_version")
    assert jira_utils.build_jql_from_type(twin) == (
        'project = RHOAIENG AND (labels = "initiative-autofix-rubric-pass") '
        'AND (labels NOT IN ("strat-creator-processing") OR labels IS EMPTY) AND status NOT IN ("Closed", "Resolved") '
        'ORDER BY key ASC')
    schemas = artifact_utils._strat_schemas(twin)
    assert (artifact_utils.SCHEMAS[f"{TWIN}-task"], artifact_utils.SCHEMAS[f"{TWIN}-review"]) == (
        schemas["strat-task"], schemas["strat-review"])
    assert schemas["strat-task"]["type"]["enum"] == [TWIN] and "type" not in schemas["strat-review"]
    assert schemas["strat-task"]["strat_id"]["pattern"] == r"^(ISTRAT-\d+|RHOAIENG-\d+)$"
    assert "source_initiative" in schemas["strat-task"] and "source_rfe" not in schemas["strat-task"]
    assert list(schemas["strat-task"])[:4] == ["strat_id", "title", "source_initiative", "jira_key"]
    # A second type resolves with --type and from its own ids; the input's id still names rfe-strategy.
    assert type_registry.resolve(REG, explicit_type=TWIN, env={}).desc is twin
    assert type_registry.resolve(REG, ids=["RHOAIENG-1"], env={}).line() == f"TYPE RESOLVED: {TWIN} (id grammar)"
    assert type_registry.resolve(REG, ids=["RHAIRFE-1"], env={"CI": "true"}).type_name == SHIPPED
    # Same ticket: processed = the Initiatives that carry a skip label, from one search on RHOAIENG.
    searches = []
    monkeypatch.setattr(jira_utils, "search_issues",
                        lambda s, u, t, jql, fields=None: searches.append((jql, fields)) or [{"key": "RHOAIENG-2"}])
    skip = yaml.safe_load((REPO / twin.get("discovery.settings")).read_text(encoding="utf-8"))["skip_labels"]
    assert jira_utils.find_processed_rfe_ids("s", "u", "t", skip, desc=twin) == {"RHOAIENG-2"}
    assert searches == [('project = RHOAIENG AND (labels = "strat-creator-rubric-pass" OR labels = '
                         '"strat-creator-needs-attention" OR labels = "strat-creator-processing")', ["key"])]


# ── the Jira scripts ──────────────────────────────────────────────────────────────────────


def test_jira_scripts_equal_the_4c6ae1c_literals():
    """What find_strat_for_rfe, pull_strategy, clone_issue, push_strategy and lock_issues' lock-strat
    walk carried as literals on main at 4c6ae1c, now read from the default type (their pins are
    retired)."""
    for module in (find_strat_for_rfe, pull_strategy, clone_issue, push_strategy, lock_issues):
        assert module._TYPE.name == SHIPPED, module.__name__
    assert (find_strat_for_rfe.LINK_TYPE, find_strat_for_rfe.STRAT_PREFIX) == ("Cloners", "RHAISTRAT-")
    assert (pull_strategy.LINK_TYPE, pull_strategy.INPUT_PREFIX) == ("Cloners", "RHAIRFE-")
    assert (lock_issues.LINK_TYPE, lock_issues.INPUT_PREFIX) == ("Cloners", "RHAIRFE-")
    assert pull_strategy.POST_CI_LABELS == {"strat-creator-rubric-pass", "strat-creator-needs-attention"}
    assert pull_strategy.STRAT_CREATOR_COMMENT_MARKER == "[Strat Creator]"
    assert pull_strategy.STRATEGY_ATTACHMENT_TEMPLATE.format(issue_key="K") == "K-strategy.md"
    assert pull_strategy.WORKSPACE == "local" and pull_strategy.pull_strategy.__defaults__ == ("local",)
    assert pull_strategy._DIRS == {"tasks": "strat-tasks", "originals": "strat-originals", "reviews": "strat-reviews"}
    t = pull_strategy._TYPE  # the frontmatter keys pull writes, and its key check
    keys = (t.id_field, t.get("inputs.0.source_ref_field"), t.tracker_key_field)
    assert keys == ("strat_id", "source_rfe", "jira_key") and t.write_prefix == "RHAISTRAT-"
    assert pull_strategy.NO_SOURCE_REF == push_strategy.NO_SOURCE_REF == "RHAIRFE-0"
    assert clone_issue._PARENT == {"key_prefixes": ["RHAISTRAT-"], "issue_type": "Outcome", "statuses_not": ["Closed"]}
    assert clone_issue.COPY_FIELDS == [
        "summary", "description", "priority", "labels", "components", "versions", "customfield_10855", "parent"]
    assert (clone_issue.ISSUE_TYPE, clone_issue.DRAFT_PREFIX, clone_issue.LOCK_LABEL, clone_issue.LINK_TYPE) == (
        "Feature", "[DRAFT] ", "strat-creator-processing", "Cloners")
    assert push_strategy._SOURCE_REF_RE.pattern == r"source_rfe:\s*(RHAIRFE-\d+)"
    assert push_strategy.STRATEGY_ATTACHMENT_TEMPLATE == "{issue_key}-strategy.md"
    assert push_strategy._TYPE.dirs("bare")["originals"] == "strat-originals"


# ── the reporters ─────────────────────────────────────────────────────────────────────────


def test_reporters_read_every_types_files(tmp_path):
    """generate-report and extract-pipeline-data list every registered type's tasks, reviews and review
    comments: rfe-strategy's STRAT- and RHAISTRAT- (the 4c6ae1c globs) plus the twin's RHOAIENG-."""
    ids = ["RHAISTRAT-2", "RHOAIENG-3", "STRAT-1"]
    (tmp_path / "strat-tasks").mkdir()
    (tmp_path / "strat-reviews").mkdir()
    for strat_id in ids:
        for path in (tmp_path / "strat-tasks" / f"{strat_id}.md", tmp_path / "strat-reviews" / f"{strat_id}-review.md"):
            path.write_text(f"---\nstrat_id: {strat_id}\n---\nbody\n", encoding="utf-8")
        (tmp_path / "strat-reviews" / f"{strat_id}-review-comment.md").write_text("comment\n", encoding="utf-8")
    (tmp_path / "strat-tasks" / "RHAIRFE-9-comments.md").write_text("not a strategy\n", encoding="utf-8")
    reporters = (("generate-report.py", "load_artifacts"), ("extract-pipeline-data.py", "load_run_artifacts"))
    for filename, loader in reporters:
        spec = importlib.util.spec_from_file_location(filename[:-3].replace("-", "_"), REPO / "scripts" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tasks, reviews, comments = getattr(module, loader)(str(tmp_path))[:3]
        assert sorted(tasks) == sorted(reviews) == sorted(comments) == ids, filename
