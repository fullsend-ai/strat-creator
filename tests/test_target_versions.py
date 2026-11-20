"""Tests for target_versions.py: the resolved intake Target Versions strategy-create's gate compares."""
import os
import subprocess
import sys

import yaml

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "target_versions.py")


def _run(args, env=None):
    if env is None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)


def test_prints_a_static_list_one_per_line(tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"jql": {"target_versions": ["3.7 GA  RHAII RELEASE", "rhai-3.7"]}}))
    result = _run([str(settings)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "3.7 GA  RHAII RELEASE\nrhai-3.7\n"


def test_prints_nothing_without_version_settings():
    result = _run(["--type", "initiative-strategy"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_release_trains_without_jira_fail_loudly():
    result = _run([])
    assert result.returncode == 2
    assert "Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN are required" in result.stderr
    assert result.stdout == ""


def test_settings_and_type_are_exclusive(tmp_path):
    result = _run([str(tmp_path / "x.yaml"), "--type", "rfe-strategy"])
    assert result.returncode == 2
    assert "pass a settings file or --type, not both" in result.stderr


def test_resolves_the_shipped_trains_against_jira(jira):
    jira.create("RHAIRFE-1", "Seeds the project", "d")
    jira.create_version("RHAIRFE", "3.7 EA1 RHOAI RELEASE")  # conftest seeds each train's GA RHOAI
    env = dict(os.environ, JIRA_SERVER=jira.url, JIRA_USER="admin", JIRA_TOKEN="admin")
    result = _run([], env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "3.7 EA1 RHOAI RELEASE", "3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE",
        "rhai-3.7", "rhai-3.8", "3.7 EA RHELAI RELEASE"]
