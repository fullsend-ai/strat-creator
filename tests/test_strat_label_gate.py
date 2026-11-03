"""Tests for the exact-match pipeline label gate (strat_label_gate.py)."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import strat_label_gate  # noqa: E402
from strat_label_gate import pipeline_label_gate  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "strat_label_gate.py")

RUBRIC_PASS = "strat-creator-rubric-pass"
NEEDS_ATTENTION = "strat-creator-needs-attention"


def _run(args, env):
    return subprocess.run([sys.executable, SCRIPT] + args,
                          capture_output=True, text=True, env=env)


def _env(jira):
    return {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}


def _no_jira_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")}


class TestPipelineLabelGate:

    @pytest.mark.parametrize("labels", [
        [],
        ["rfe-creator-needs-attention"],
        ["rfe-creator-autofix-rubric-pass", "rfe-creator-needs-attention",
         "strat-creator-auto-created", "strat-creator-human-sign-off"],
        ["strat-creator-needs-attention-x", "x-strat-creator-rubric-pass",
         "Strat-Creator-Rubric-Pass"],
    ])
    def test_lookalikes_are_not_gate_labels(self, labels):
        assert pipeline_label_gate(labels) == {
            "eligible": True, "gate_label": None, "gate_labels_present": []}

    def test_needs_attention_is_reported(self):
        gate = pipeline_label_gate(["rfe-creator-needs-attention", NEEDS_ATTENTION])
        assert gate == {"eligible": False, "gate_label": NEEDS_ATTENTION,
                        "gate_labels_present": [NEEDS_ATTENTION]}

    def test_rubric_pass_is_reported(self):
        gate = pipeline_label_gate([RUBRIC_PASS])
        assert gate == {"eligible": False, "gate_label": RUBRIC_PASS,
                        "gate_labels_present": [RUBRIC_PASS]}

    def test_both_gate_labels_are_reported(self):
        gate = pipeline_label_gate([NEEDS_ATTENTION, "foo", RUBRIC_PASS])
        assert gate["eligible"] is False
        assert gate["gate_labels_present"] == [RUBRIC_PASS, NEEDS_ATTENTION]

    @pytest.mark.parametrize("labels", [None, "strat-creator-rubric-pass", [RUBRIC_PASS, 3]])
    def test_malformed_labels_are_errors(self, labels):
        with pytest.raises(ValueError):
            pipeline_label_gate(labels)


class TestCli:

    def test_labels_argument(self):
        result = _run(["RHAISTRAT-3115", "--labels", "rfe-creator-needs-attention,foo"],
                      _no_jira_env())
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "key": "RHAISTRAT-3115", "eligible": True, "gate_label": None,
            "gate_labels_present": []}

    def test_labels_argument_reports_gate(self):
        result = _run(["RHAISTRAT-3115", "--labels", f" {NEEDS_ATTENTION} , foo"],
                      _no_jira_env())
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["eligible"] is False
        assert out["gate_label"] == NEEDS_ATTENTION

    def test_rejects_non_strat_key(self):
        result = _run(["RHAIRFE-1", "--labels", ""], _no_jira_env())
        assert result.returncode == 1
        assert result.stdout == ""

    def test_missing_credentials_exits_2(self):
        result = _run(["RHAISTRAT-3115"], _no_jira_env())
        assert result.returncode == 2
        assert result.stdout == ""

    def test_fetches_labels_from_jira(self, jira):
        jira.create("RHAISTRAT-3120", "Inherited", "Description.",
                     labels=["rfe-creator-needs-attention"])
        jira.create("RHAISTRAT-3121", "Processed", "Description.",
                     labels=["rfe-creator-needs-attention", NEEDS_ATTENTION])

        result = _run(["RHAISTRAT-3120"], _env(jira))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["eligible"] is True

        result = _run(["RHAISTRAT-3121"], _env(jira))
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["eligible"] is False
        assert out["gate_labels_present"] == [NEEDS_ATTENTION]

    def test_jira_error_exits_1(self, jira):
        result = _run(["RHAISTRAT-999999"], _env(jira))
        assert result.returncode == 1
        assert result.stdout == ""


class TestMainFetch:
    """Jira fetch path without the emulator: get_issue is stubbed in-process."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.invalid")
        monkeypatch.setenv("JIRA_USER", "user")
        monkeypatch.setenv("JIRA_TOKEN", "token")

    def _stub(self, monkeypatch, response):
        calls = []

        def get_issue(server, user, token, key, fields=None, **_):
            calls.append((key, fields))
            return {"key": key, "fields": response}

        monkeypatch.setattr(strat_label_gate, "get_issue", get_issue)
        return calls

    def test_clone_inherited_label_is_eligible(self, monkeypatch, capsys):
        calls = self._stub(monkeypatch, {"labels": ["rfe-creator-needs-attention"]})
        assert strat_label_gate.main(["RHAISTRAT-3115"]) == 0
        assert calls == [("RHAISTRAT-3115", ["labels"])]
        assert json.loads(capsys.readouterr().out) == {
            "key": "RHAISTRAT-3115", "eligible": True, "gate_label": None,
            "gate_labels_present": []}

    def test_gate_label_is_reported(self, monkeypatch, capsys):
        self._stub(monkeypatch, {"labels": [NEEDS_ATTENTION, RUBRIC_PASS]})
        assert strat_label_gate.main(["RHAISTRAT-3106"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["eligible"] is False
        assert out["gate_label"] == RUBRIC_PASS
        assert out["gate_labels_present"] == [RUBRIC_PASS, NEEDS_ATTENTION]

    def test_missing_labels_field_fails_closed(self, monkeypatch, capsys):
        self._stub(monkeypatch, {})
        assert strat_label_gate.main(["RHAISTRAT-3115"]) == 1
        assert capsys.readouterr().out == ""
