"""Missing managed-run input stops helpers; standalone human use still works."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from strat_freshness import verify  # noqa: E402
from strat_side_effects import check_owner, perform  # noqa: E402


@pytest.mark.parametrize("owner", [None, "", 42, ["run"], "bad/owner"])
def test_prepared_input_requires_owner_before_reads_writes_or_intent(tmp_path, monkeypatch, owner):
    monkeypatch.chdir(tmp_path)
    # Explicit standalone settings must not let a prepared run opt out.
    monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "standalone"))
    monkeypatch.setenv("STRAT_RUN_ID", "run")
    (tmp_path / "tmp").mkdir()
    data = {"run_id": "run", "acquired_keys": ["RHAIRFE-999"]}
    if owner is not None:
        data["lock_owner"] = owner
    (tmp_path / "tmp/strat-input.json").write_text(json.dumps(data))
    events = []
    with pytest.raises(ValueError, match="lock owner"):
        perform("local", "comment", "RHAIRFE-999", "body",
                lambda: events.append("read"), lambda: events.append("write"))
    assert events == []
    assert not (tmp_path / "tmp/strat-effects").exists()
    assert not (tmp_path / "standalone").exists()
    with pytest.raises(ValueError, match="lock owner"):
        check_owner("local", None, None, "RHAIRFE-999", data)


def test_explicit_standalone_journal_keeps_human_helper_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRAT_MANAGED_RUN", raising=False)
    monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("STRAT_RUN_ID", "human-run")
    events = []
    for _ in range(2):
        assert perform("local", "comment", "RHAIRFE-999", "body",
                       lambda: events[-1] if events else None,
                       lambda: events.append("published"),
                       guard=lambda data: check_owner("local", None, None, "RHAIRFE-999", data)) == "published"
    assert events == ["published"]
    assert len(list((tmp_path / "effects").glob("*.json"))) == 1


@pytest.mark.parametrize("managed", [True, False])
def test_missing_managed_input_stops_before_any_read_or_write(tmp_path, monkeypatch, managed):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRAT_EFFECTS_DIR", raising=False)
    if managed:
        monkeypatch.setenv("STRAT_MANAGED_RUN", "1")
    else:
        monkeypatch.delenv("STRAT_MANAGED_RUN", raising=False)
    events = []

    def operation():
        return perform("https://jira.invalid", "comment", "RHAIRFE-999", {"body": "fake"},
                       read=lambda: events[-1] if events else None,
                       write=lambda: events.append({"id": "fake-only"}),
                       guard=lambda data: check_owner("https://jira.invalid", None, None, "RHAIRFE-999", data))

    if managed:
        with pytest.raises(ValueError, match="managed run.*input"):
            operation()
        assert events == [] and not (tmp_path / "tmp").exists()
        with pytest.raises(ValueError, match="managed run.*input"):
            verify(tmp_path)
    else:
        assert operation() == {"id": "fake-only"}
        assert verify(tmp_path) is None


@pytest.mark.parametrize("relative", ["scripts/strat_freshness_guard.py",
                                     ".fullsend/plugins/strat-pipeline/scripts/strat_freshness_guard.py"])
def test_missing_managed_input_refuses_hook_launch(tmp_path, monkeypatch, relative):
    monkeypatch.setenv("STRAT_MANAGED_RUN", "1")
    spec = importlib.util.spec_from_file_location("managed_guard", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event = {"cwd": str(tmp_path), "tool_name": "Workflow", "tool_input": {}}
    with pytest.raises(ValueError, match="managed run.*input"):
        module.guard(event)
    monkeypatch.delenv("STRAT_MANAGED_RUN")
    assert module.guard(event) is None
