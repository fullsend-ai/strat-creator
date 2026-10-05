"""Integration tests for remove_draft_prefix.py against jira-emulator."""
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                      "remove_draft_prefix.py")


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


class TestRemoveDraftPrefix:

    def test_strips_draft_prefix(self, jira):
        jira.create("RHAISTRAT-700", "[DRAFT] GPU sharing for notebooks",
                     "Enable time-sliced GPU sharing.",
                     issue_type="Feature")

        result = _run(jira, ["RHAISTRAT-700"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[SUMMARY]" in result.stdout

        issue = jira.get("RHAISTRAT-700")
        assert issue["fields"]["summary"] == "GPU sharing for notebooks"

    def test_skips_when_no_prefix(self, jira):
        jira.create("RHAISTRAT-701", "Model serving autoscaler",
                     "Autoscale model serving pods.",
                     issue_type="Feature")

        result = _run(jira, ["RHAISTRAT-701"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[SKIP]" in result.stdout

        issue = jira.get("RHAISTRAT-701")
        assert issue["fields"]["summary"] == "Model serving autoscaler"

    def test_initiative_summary_is_never_touched(self, jira):
        jira.create("RHOAIENG-702", "[DRAFT] Initiative owned by rfe-creator",
                     "Initiative body.", issue_type="Initiative")

        result = _run(jira, ["RHOAIENG-702"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[SKIP] initiative-strategy has no summary prefix" in result.stdout

        issue = jira.get("RHOAIENG-702")
        assert issue["fields"]["summary"] == "[DRAFT] Initiative owned by rfe-creator"

    def test_missing_env_vars_exits_with_code_2(self, jira):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")}

        result = _run(jira, ["RHAISTRAT-700"], env_override=env)
        assert result.returncode == 2
