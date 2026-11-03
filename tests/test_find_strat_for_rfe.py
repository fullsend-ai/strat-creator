"""Integration tests for find_strat_for_rfe.py against jira-emulator."""
import json
import os
import subprocess
import sys

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "find_strat_for_rfe.py")


def _env(jira):
    return {
        **os.environ,
        "JIRA_SERVER": jira.url,
        "JIRA_USER": "admin",
        "JIRA_TOKEN": "admin",
    }


def _run(jira, args, env_override=None):
    env = env_override if env_override is not None else _env(jira)
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, env=env,
    )


def _create_cloners_link(jira, strat_key, rfe_key):
    jira.request("POST", "/rest/api/3/issueLink", {
        "type": {"name": "Cloners"},
        "inwardIssue": {"key": strat_key},
        "outwardIssue": {"key": rfe_key},
    })


class TestFindStratForRfe:

    def test_json_mode_returns_strat_clone(self, jira):
        jira.create("RHAIRFE-3000", "GPU sharing RFE", "RFE description.")
        jira.create("RHAISTRAT-3000", "GPU sharing strategy",
                     "Strategy description.",
                     labels=["strat-creator-rubric-pass"])
        _create_cloners_link(jira, "RHAISTRAT-3000", "RHAIRFE-3000")

        result = _run(jira, ["RHAIRFE-3000", "--json"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1
        keys = [item["key"] for item in data]
        assert "RHAISTRAT-3000" in keys
        match = next(item for item in data if item["key"] == "RHAISTRAT-3000")
        assert "status" in match
        assert isinstance(match["labels"], list)
        assert match["gate_label"] == "strat-creator-rubric-pass"
        assert match["gate_labels_present"] == ["strat-creator-rubric-pass"]

    def test_json_mode_ignores_rfe_creator_lookalike_labels(self, jira):
        jira.create("RHAIRFE-3010", "Inherited labels RFE", "Description.")
        jira.create("RHAISTRAT-3010", "Inherited labels strategy",
                     "Description.",
                     labels=["rfe-creator-needs-attention",
                             "rfe-creator-autofix-rubric-pass"])
        _create_cloners_link(jira, "RHAISTRAT-3010", "RHAIRFE-3010")

        result = _run(jira, ["RHAIRFE-3010", "--json"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        match = next(item for item in json.loads(result.stdout)
                     if item["key"] == "RHAISTRAT-3010")
        assert match["gate_label"] is None
        assert match["gate_labels_present"] == []

    def test_text_mode_output_format(self, jira):
        jira.create("RHAIRFE-3001", "Text mode RFE", "Description.")
        jira.create("RHAISTRAT-3001", "Text mode strategy", "Description.",
                     labels=["some-label"])
        _create_cloners_link(jira, "RHAISTRAT-3001", "RHAIRFE-3001")

        result = _run(jira, ["RHAIRFE-3001"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        lines = result.stdout.strip().split("\n")
        assert len(lines) >= 1
        assert "RHAISTRAT-3001" in lines[0]
        assert "status=" in lines[0]
        assert "labels=" in lines[0]

    def test_no_cloners_links_exits_1(self, jira):
        jira.create("RHAIRFE-3002", "Orphan RFE", "No strategy exists.")

        result = _run(jira, ["RHAIRFE-3002", "--json"])
        assert result.returncode == 1

        data = json.loads(result.stdout)
        assert data == []

    def test_no_cloners_links_text_mode(self, jira):
        jira.create("RHAIRFE-3003", "Another orphan", "No strategy.")

        result = _run(jira, ["RHAIRFE-3003"])
        assert result.returncode == 1
        assert result.stdout.strip() == "none"

    def test_multiple_strat_clones(self, jira):
        jira.create("RHAIRFE-3004", "Multi-strat RFE", "Has two strategies.")
        jira.create("RHAISTRAT-3004", "Strategy A", "First strategy.")
        jira.create("RHAISTRAT-3005", "Strategy B", "Second strategy.")
        _create_cloners_link(jira, "RHAISTRAT-3004", "RHAIRFE-3004")
        _create_cloners_link(jira, "RHAISTRAT-3005", "RHAIRFE-3004")

        result = _run(jira, ["RHAIRFE-3004", "--json"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(result.stdout)
        keys = {item["key"] for item in data}
        assert "RHAISTRAT-3004" in keys
        assert "RHAISTRAT-3005" in keys

    def test_non_rhaistrat_links_excluded(self, jira):
        jira.create("RHAIRFE-3005", "RFE with mixed links", "Description.")
        jira.create("RHAISTRAT-3006", "Real strategy", "Strategy desc.")
        jira.create("RHAIRFE-3006", "Another RFE", "Not a strategy.")
        _create_cloners_link(jira, "RHAISTRAT-3006", "RHAIRFE-3005")
        _create_cloners_link(jira, "RHAIRFE-3006", "RHAIRFE-3005")

        result = _run(jira, ["RHAIRFE-3005", "--json"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(result.stdout)
        keys = [item["key"] for item in data]
        assert "RHAISTRAT-3006" in keys
        assert "RHAIRFE-3006" not in keys

    def test_missing_env_vars_exits_2(self, jira):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")}

        result = _run(jira, ["RHAIRFE-3000"], env_override=env)
        assert result.returncode == 2
