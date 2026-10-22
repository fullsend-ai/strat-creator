"""Missing managed-run input stops helpers; standalone human use still works."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from strat_freshness import verify  # noqa: E402
from strat_side_effects import check_owner, perform  # noqa: E402


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
