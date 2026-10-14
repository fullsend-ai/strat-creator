"""Tests for list-rfe-ids.py config mode (no Jira connection)."""
import os
import subprocess
import sys

import pytest
import yaml

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "list-rfe-ids.py")


def _run(args, cwd=None):
    result = subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, cwd=cwd,
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


def test_jql_processed_filter_precedes_size_offset(monkeypatch, capsys, tmp_path):
    """Exercise the native discovery CLI with a processed historical RFE."""
    import importlib.util
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(SCRIPT).resolve().parent))
    import jira_utils
    spec = importlib.util.spec_from_file_location('batch_discovery', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = tmp_path / 'settings.yaml'
    settings.write_text(yaml.safe_dump({'batch_size': 10, 'skip_labels': ['strat-creator-needs-attention']}))
    monkeypatch.setattr(jira_utils, 'require_env', lambda: ('server', 'user', 'token'))
    monkeypatch.setattr(jira_utils, 'build_jql_from_config', lambda path: 'native configured JQL')
    monkeypatch.setattr(module, 'ids_from_jql', lambda query: ['RHAIRFE-1', 'RHAIRFE-2', 'RHAIRFE-3', 'RHAIRFE-4'])
    monkeypatch.setattr(jira_utils, 'find_processed_rfe_ids', lambda *a, **kw: {'RHAIRFE-1'})
    monkeypatch.setattr(sys, 'argv', [SCRIPT, '--jql-default', str(settings), '--batch-size', '2', '--batch-offset', '1'])
    module.main()
    captured = capsys.readouterr()
    assert captured.out.split() == ['RHAIRFE-3', 'RHAIRFE-4']
    assert 'Excluded 1 already-processed' in captured.err
