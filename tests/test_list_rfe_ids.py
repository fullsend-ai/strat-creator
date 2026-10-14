"""Tests for list-rfe-ids.py config mode (no Jira connection)."""
import os
import subprocess
import sys

import pytest
import yaml

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "list-rfe-ids.py")


def _run(args, cwd=None, env=None):
    result = subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, cwd=cwd, env=env,
    )
    return result


def _write_config(tmp_path, rfes):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"test_rfes": rfes}))
    return str(path)


class TestIdsFromConfig:
    def test_all_ids(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": "RHAIRFE-100"},
            {"id": "RHAIRFE-200"},
            {"id": "RHAIRFE-300"},
        ])
        result = _run(["--config", config])
        assert result.returncode == 0
        ids = result.stdout.strip().split("\n")
        assert ids == ["RHAIRFE-100", "RHAIRFE-200", "RHAIRFE-300"]

    def test_baseline_only(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": "RHAIRFE-100", "baseline": True},
            {"id": "RHAIRFE-200", "baseline": False},
            {"id": "RHAIRFE-300"},
        ])
        result = _run(["--config", config, "--baseline"])
        assert result.returncode == 0
        ids = result.stdout.strip().split("\n")
        assert ids == ["RHAIRFE-100"]

    def test_no_baseline(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": "RHAIRFE-100", "baseline": True},
            {"id": "RHAIRFE-200", "baseline": False},
            {"id": "RHAIRFE-300"},
        ])
        result = _run(["--config", config, "--no-baseline"])
        assert result.returncode == 0
        ids = result.stdout.strip().split("\n")
        assert ids == ["RHAIRFE-200", "RHAIRFE-300"]

    def test_empty_config(self, tmp_path):
        config = _write_config(tmp_path, [])
        result = _run(["--config", config])
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestBatching:
    def test_batch_size(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": f"RHAIRFE-{i}"} for i in range(1, 11)
        ])
        result = _run(["--config", config, "--batch-size", "3"])
        ids = result.stdout.strip().split("\n")
        assert len(ids) == 3
        assert ids == ["RHAIRFE-1", "RHAIRFE-2", "RHAIRFE-3"]

    def test_batch_offset(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": f"RHAIRFE-{i}"} for i in range(1, 11)
        ])
        result = _run(["--config", config, "--batch-offset", "7"])
        ids = result.stdout.strip().split("\n")
        assert ids == ["RHAIRFE-8", "RHAIRFE-9", "RHAIRFE-10"]

    def test_batch_size_and_offset(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": f"RHAIRFE-{i}"} for i in range(1, 11)
        ])
        result = _run(["--config", config, "--batch-size", "2",
                        "--batch-offset", "3"])
        ids = result.stdout.strip().split("\n")
        assert ids == ["RHAIRFE-4", "RHAIRFE-5"]

    def test_batch_offset_beyond_end(self, tmp_path):
        config = _write_config(tmp_path, [
            {"id": "RHAIRFE-1"},
        ])
        result = _run(["--config", config, "--batch-offset", "5"])
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestMissingConfig:
    def test_missing_file(self, tmp_path):
        result = _run(["--config", str(tmp_path / "nonexistent.yaml")])
        assert result.returncode == 1

    def test_reads_real_test_rfes(self):
        """Verify the real config/test-rfes.yaml parses correctly."""
        real_config = os.path.join(
            os.path.dirname(__file__), "..", "config", "test-rfes.yaml")
        if not os.path.exists(real_config):
            pytest.skip("config/test-rfes.yaml not available")
        result = _run(["--config", real_config])
        assert result.returncode == 0
        ids = result.stdout.strip().split("\n")
        assert len(ids) >= 5
        assert all(i.startswith("RHAIRFE-") for i in ids)


class TestTypeMode:
    """--jql-default renders the work type's settings file before it needs Jira."""

    def test_jql_default_renders_the_type_settings(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        result = _run(["--jql-default"], env=env)
        assert result.returncode != 0  # no Jira credentials: stops after printing the JQL
        assert 'JQL: project = RHAIRFE AND (labels = "strat-creator-3.5" OR' in result.stderr
        assert result.stderr.count("ORDER BY key ASC") == 1
        assert "TYPE RESOLVED" not in result.stderr  # no --type: the run prints what it did before

    def test_explicit_type_says_so_and_renders_the_same_gate(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        default = _run(["--jql-default"], env=env)
        explicit = _run(["--jql-default", "--type", "rfe-strategy"], env=env)
        assert explicit.stderr == "TYPE RESOLVED: rfe-strategy (--type)\n" + default.stderr

    def test_unknown_type_is_a_usage_error(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        result = _run(["--jql-default", "--type", "nope"], env=env)
        assert result.returncode == 2
        assert "invalid choice: 'nope'" in result.stderr

    def test_type_needs_the_type_settings(self):
        settings = os.path.join(os.path.dirname(__file__), "..", "config", "pipeline-settings.yaml")
        for source in (["--config", settings], ["--jql-default", settings], []):
            result = _run(source + ["--type", "rfe-strategy"])
            assert result.returncode == 2, source
            assert "--type works with --jql-default (no settings path) or --jql" in result.stderr

    def test_type_selects_the_twin(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        result = _run(["--jql-default", "--type", "initiative-strategy"], env=env)
        assert result.returncode != 0
        assert result.stderr.startswith("TYPE RESOLVED: initiative-strategy (--type)\n")
        assert 'JQL: project = RHOAIENG AND (labels = "initiative-autofix-rubric-pass"' in result.stderr
