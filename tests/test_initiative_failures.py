"""Failure cases for a second work type, initiative-strategy (AISDLC-60's success criteria): an invalid
definition, a retry, a duplicate run, a partial failure and the recovery from a job that died holding its lock.
The Jira cases run the production scripts against the jira-emulator, the way strat-pipeline's batch job calls
them."""
import os
import subprocess
import sys
import urllib.error

import pytest
import yaml

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import jira_utils  # noqa: E402
import validate_types  # noqa: E402

GATE = "initiative-autofix-rubric-pass"
LOCK = "strat-creator-processing"
TWIN = os.path.join(REPO, "types", "initiative-strategy", "type.yaml")


def _run(script, *args, jira=None, env=None, cwd=REPO):
    if env is None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JIRA_", "STRAT_CREATOR_"))}
    if jira is not None:
        env = dict(env, JIRA_SERVER=jira.url, JIRA_USER="admin", JIRA_TOKEN="admin")
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts", script), *args],
                          capture_output=True, text=True, env=env, cwd=cwd)


def _discover(jira):
    result = _run("list-rfe-ids.py", "--jql-default", "--type", "initiative-strategy", jira=jira)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def _initiative(jira, key, *labels):
    jira.create(key, f"Initiative {key}", "## Objective\n\nGrow the platform.",
                labels=[GATE, *labels], issue_type="Initiative")


class TestInvalidDefinition:
    """A broken descriptor fails closed: before any Jira request, never by running with a guessed value."""

    def _twin_without_settings(self, root):
        with open(TWIN, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        data["type"] = "nosettings-strategy"
        del data["discovery"]["settings"]
        (root / "nosettings-strategy").mkdir()
        (root / "nosettings-strategy" / "type.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        return root

    def test_gate1_refuses_a_twin_without_its_settings_file(self, tmp_path, capsys):
        assert validate_types.main(["--root", str(self._twin_without_settings(tmp_path))]) == 1
        assert "$.discovery: 'settings' is a required property" in capsys.readouterr().out

    def test_a_type_without_its_settings_file_stops_before_jira(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        env["STRAT_CREATOR_EXTRA_TYPES"] = str(self._twin_without_settings(tmp_path))
        result = _run("list-rfe-ids.py", "--jql-default", "--type", "nosettings-strategy", env=env)
        assert result.returncode == 1
        assert "no such descriptor field 'discovery.settings'" in result.stderr
        # The JQL is printed before the search; without JIRA_* a search would exit 2 asking for them.
        assert "JQL:" not in result.stderr and "JIRA_SERVER" not in result.stderr

    def test_a_malformed_descriptor_stops_every_script(self, tmp_path, capsys):
        """Every script that imports jira_utils, artifact_utils or type_registry loads the registry when it starts,
        so one malformed descriptor stops them all, the RFE job included. A drop-in root shows it here; a malformed
        shipped descriptor fails the same way (the same TypeRegistry._scan), which is why `make lint` (gate 1, which
        reports it) must gate main: strat-pipeline runs main as it is."""
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "type.yaml").write_text("type: [broken\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        env["STRAT_CREATOR_EXTRA_TYPES"] = str(tmp_path)
        for args in (["list-rfe-ids.py", "--jql-default"], ["lock_issues.py", "lock", "RHAIRFE-1"]):
            result = _run(*args, env=env)
            assert result.returncode == 1, args
            assert "type_registry.RegistryError" in result.stderr and "invalid YAML" in result.stderr, args
            assert "JQL:" not in result.stderr and "JIRA_SERVER" not in result.stderr, args
        assert validate_types.main(["--root", str(tmp_path)]) == 1
        assert "broken/type.yaml: invalid YAML" in capsys.readouterr().out


class TestRetry:
    """The JSON REST calls go through api_call_with_retry (add_attachment has its own loop, download_attachment none): a
    502-504 waits 1, 4 and 16 seconds, and the third failure is raised. A retried push appends a second
    {key}-strategy.md and leaves the Initiative as it was (tests/test_push_strategy_integration.py::TestPushSameTicket);
    pull then reads the newest attachment (tests/test_pull_strategy.py::TestPullSameTicket)."""

    @staticmethod
    def _jira(monkeypatch, *codes):
        calls, sleeps = [], []

        def api_call(*args):
            calls.append(args)
            if len(calls) <= len(codes):
                raise urllib.error.HTTPError("url", codes[len(calls) - 1], "Service Unavailable", {}, None)
            return {"ok": True}

        monkeypatch.setattr(jira_utils, "api_call", api_call)
        monkeypatch.setattr(jira_utils.time, "sleep", sleeps.append)
        return calls, sleeps

    def test_a_503_then_200_returns(self, monkeypatch):
        calls, sleeps = self._jira(monkeypatch, 503)
        assert jira_utils.api_call_with_retry("s", "/issue/RHOAIENG-1", "u", "t") == {"ok": True}
        assert len(calls) == 2 and sleeps == [1]

    def test_three_503s_raise(self, monkeypatch):
        calls, sleeps = self._jira(monkeypatch, 503, 503, 503)
        with pytest.raises(urllib.error.HTTPError) as error:
            jira_utils.api_call_with_retry("s", "/issue/RHOAIENG-1", "u", "t")
        assert error.value.code == 503
        assert len(calls) == 3 and sleeps == [1, 4, 16]


class TestDuplicate:

    def test_a_second_lock_takes_nothing(self, jira):
        _initiative(jira, "RHOAIENG-3000")
        first = _run("lock_issues.py", "lock", "RHOAIENG-3000", jira=jira)
        second = _run("lock_issues.py", "lock", "RHOAIENG-3000", jira=jira)
        assert (first.returncode, first.stdout.split()) == (0, ["RHOAIENG-3000"])
        # batch-jql reads stdout as the keys it owns: empty means "already locked", nothing to do.
        assert (second.returncode, second.stdout.strip()) == (0, "")
        assert f"BLOCKED RHOAIENG-3000 — has label(s): {LOCK}" in second.stderr


class TestPartialFailure:

    def test_one_missing_initiative_fails_the_run_not_the_others(self, jira, art_dir):
        _initiative(jira, "RHOAIENG-3101")  # RHOAIENG-3100 does not exist and sorts first
        before = jira.get("RHOAIENG-3101")["fields"]
        tasks = art_dir / "artifacts" / "strat-tasks"
        for key in ("RHOAIENG-3100", "RHOAIENG-3101"):
            (tasks / f"{key}.md").write_text(
                f"---\nstrat_id: {key}\ntitle: t\nsource_initiative: {key}\njira_key: {key}\npriority: Major\n"
                "status: Refined\ntype: initiative-strategy\n---\n\n## Business Need (from Initiative)\n\nGrow.\n\n"
                "## Strategy (AI Generated by Agentic SDLC Pipeline)\n\nThe how.\n")

        result = _run("push_refined_strategies.py", "--artifacts-dir", str(tasks), jira=jira)
        assert result.returncode == 1
        assert "ERROR: push failed for RHOAIENG-3100" in result.stderr
        assert "Pushed 1/2 strategies to Jira (0 skipped, 1 failed)" in result.stdout
        after = jira.get("RHOAIENG-3101")["fields"]
        assert "strat-creator-auto-refined" in after["labels"]
        assert (after["summary"], after["description"]) == (before["summary"], before["description"])
        assert [a["filename"] for a in after["attachment"]] == ["RHOAIENG-3101-strategy.md"]


class TestRecovery:

    def test_a_stale_lock_hides_the_initiative_until_unlock(self, jira):
        """The job died after push_refined_strategies, before its review and after_script: the lock and
        auto-refined stay on the Initiative. Discovery skips it until unlock, then takes it again, because
        auto-refined is not a skip label."""
        _initiative(jira, "RHOAIENG-3200", LOCK, "strat-creator-auto-refined")
        _initiative(jira, "RHOAIENG-3201")
        assert _discover(jira) == ["RHOAIENG-3201"]

        unlock = _run("lock_issues.py", "unlock", "RHOAIENG-3200", jira=jira)
        assert unlock.returncode == 0, unlock.stderr
        assert LOCK not in jira.get("RHOAIENG-3200")["fields"]["labels"]
        assert _discover(jira) == ["RHOAIENG-3200", "RHOAIENG-3201"]
