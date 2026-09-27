"""Unit tests for scripts/type_registry.py and scripts/validate_types.py."""

import inspect
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import jira_utils  # noqa: E402
import type_registry  # noqa: E402
import validate_types  # noqa: E402

# Hermetic: the shipped root only, no drop-in roots from the environment.
REG = type_registry.load(extra_roots=[], env={})
SHIPPED = "rfe-strategy"


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
    assert REG.names() == [SHIPPED]
    assert SHIPPED in REG
    assert len(REG) == 1
    assert [d.name for d in REG] == [SHIPPED]


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
    with pytest.raises(KeyError, match=f"registered: {SHIPPED}"):
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
    root = _dropin(tmp_path, "initiative-strategy")
    assert type_registry.load(extra_roots=[root], env={}).names() == [SHIPPED, "initiative-strategy"]
    env = {type_registry.EXTRA_ROOTS_ENV: str(root)}
    assert type_registry.load(env=env).names() == [SHIPPED, "initiative-strategy"]
    assert type_registry.load(env={type_registry.EXTRA_ROOTS_ENV: ""}).names() == [SHIPPED]


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
    assert type_registry.load(extra_roots=[tmp_path / "absent"], env={}).names() == [SHIPPED]


# ── CLI ───────────────────────────────────────────────────────────────────────────────────


def test_cli(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv(type_registry.EXTRA_ROOTS_ENV, raising=False)
    assert type_registry.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == SHIPPED

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
    assert f"OK: 1 descriptor(s) valid: {SHIPPED}" in capsys.readouterr().out


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


def test_jira_utils_derives_the_4c6ae1c_literals():
    """The first consumer composes what used to be literals; equality with the text on main at
    4c6ae1c is the byte-identical check (their pins were deleted with that change)."""
    assert jira_utils._TYPE.name == SHIPPED
    assert jira_utils._TITLE_KEY_RE.pattern == r"^#\s+(RFE-\d+|RHAIRFE-\d+|STRAT-\d+|RHAISTRAT-\d+):"
    assert inspect.signature(jira_utils.find_processed_rfe_ids).parameters["strat_project"].default == "RHAISTRAT"
    link = {"type": {"name": "Cloners"}, "outwardIssue": {"key": "RHAIRFE-1"}, "inwardIssue": {"key": "RHAISTRAT-2"}}
    assert jira_utils._linked_input_key(link) == "RHAIRFE-1"
    assert jira_utils._linked_input_key({**link, "type": {"name": "Blocks"}}) is None
    inward_only = {"type": {"name": "Cloners"}, "inwardIssue": {"key": "RHAIRFE-3"}}
    assert jira_utils._linked_input_key(inward_only) == "RHAIRFE-3"
    assert jira_utils._linked_input_key({"type": {"name": "Cloners"}, "inwardIssue": None}) is None
