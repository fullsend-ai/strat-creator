"""Integration tests for lock_issues.py against jira-emulator."""
import os
import subprocess
import sys

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "lock_issues.py")


def _env(jira):
    return {
        **os.environ,
        "JIRA_SERVER": jira.url,
        "JIRA_USER": "admin",
        "JIRA_TOKEN": "admin",
    }


def _run(jira, args):
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, env=_env(jira),
    )


def _get_labels(jira, key):
    data = jira.get(key)
    return set(data.get("fields", {}).get("labels", []))


def _create_cloners_link(jira, strat_key, rfe_key):
    jira.request("POST", "/rest/api/3/issueLink", {
        "type": {"name": "Cloners"},
        "inwardIssue": {"key": strat_key},
        "outwardIssue": {"key": rfe_key},
    })


class TestLock:

    def test_lock_single_rfe(self, jira):
        jira.create("RHAIRFE-5000", "Test RFE", "Description.")

        result = _run(jira, ["lock", "RHAIRFE-5000"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "LOCKED RHAIRFE-5000" in result.stderr

        labels = _get_labels(jira, "RHAIRFE-5000")
        assert "strat-creator-processing" in labels

    def test_lock_already_locked_single_skips(self, jira):
        jira.create("RHAIRFE-5001", "Already locked",
                     "Description.",
                     labels=["strat-creator-processing"])

        result = _run(jira, ["lock", "RHAIRFE-5001"])
        assert result.returncode == 0
        assert "BLOCKED" in result.stderr
        assert "strat-creator-processing" in result.stderr
        assert result.stdout.strip() == ""

    def test_lock_needs_attention_single_skips(self, jira):
        jira.create("RHAIRFE-5003", "RFE with blocking label",
                     "Description.",
                     labels=["strat-creator-needs-attention"])

        result = _run(jira, ["lock", "RHAIRFE-5003"])
        assert result.returncode == 0
        assert "BLOCKED" in result.stderr
        assert result.stdout.strip() == ""

    def test_lock_human_signoff_single_skips(self, jira):
        jira.create("RHAIRFE-5004", "Signed off RFE",
                     "Description.",
                     labels=["strat-creator-human-sign-off"])

        result = _run(jira, ["lock", "RHAIRFE-5004"])
        assert result.returncode == 0
        assert "BLOCKED" in result.stderr
        assert result.stdout.strip() == ""

    def test_lock_rework_needed_single_skips(self, jira):
        jira.create("RHAIRFE-5005", "RFE awaiting rework",
                     "Description.",
                     labels=["strat-creator-rework-needed"])

        result = _run(jira, ["lock", "RHAIRFE-5005"])
        assert result.returncode == 0
        assert "BLOCKED" in result.stderr
        assert "strat-creator-rework-needed" in result.stderr
        assert result.stdout.strip() == ""

    def test_lock_batch_skips_blocked(self, jira):
        jira.create("RHAIRFE-5010", "Good RFE A", "Description.")
        jira.create("RHAIRFE-5011", "Locked RFE B",
                     "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAIRFE-5012", "Good RFE C", "Description.")

        result = _run(jira, ["lock",
                              "RHAIRFE-5010", "RHAIRFE-5011", "RHAIRFE-5012"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        # stdout contains the locked subset
        locked = result.stdout.strip().split()
        assert "RHAIRFE-5010" in locked
        assert "RHAIRFE-5011" not in locked
        assert "RHAIRFE-5012" in locked

        # stderr has the BLOCKED message
        assert "BLOCKED RHAIRFE-5011" in result.stderr

    def test_lock_batch_all_blocked(self, jira):
        jira.create("RHAIRFE-5020", "Blocked A",
                     "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAIRFE-5021", "Blocked B",
                     "Description.",
                     labels=["strat-creator-needs-attention"])

        result = _run(jira, ["lock", "RHAIRFE-5020", "RHAIRFE-5021"])
        assert result.returncode == 0
        assert "No keys locked" in result.stderr


class TestUnlock:

    def test_unlock_removes_processing_label(self, jira):
        jira.create("RHAIRFE-5100", "Locked RFE",
                     "Description.",
                     labels=["strat-creator-processing", "strat-creator-3.5"])

        result = _run(jira, ["unlock", "RHAIRFE-5100"])
        assert result.returncode == 0
        assert "UNLOCKED RHAIRFE-5100" in result.stderr

        labels = _get_labels(jira, "RHAIRFE-5100")
        assert "strat-creator-processing" not in labels
        assert "strat-creator-3.5" in labels

    def test_unlock_multiple(self, jira):
        jira.create("RHAIRFE-5101", "Locked A",
                     "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAIRFE-5102", "Locked B",
                     "Description.",
                     labels=["strat-creator-processing"])

        result = _run(jira, ["unlock", "RHAIRFE-5101", "RHAIRFE-5102"])
        assert result.returncode == 0

        assert "strat-creator-processing" not in _get_labels(
            jira, "RHAIRFE-5101")
        assert "strat-creator-processing" not in _get_labels(
            jira, "RHAIRFE-5102")


class TestLockStrat:

    def test_lock_strat_success(self, jira):
        jira.create("RHAIRFE-5200", "Source RFE", "Description.")
        jira.create("RHAISTRAT-5200", "Strategy",
                     "Description.",
                     labels=["strat-creator-auto-created",
                             "strat-creator-rubric-pass"])
        _create_cloners_link(jira, "RHAISTRAT-5200", "RHAIRFE-5200")

        result = _run(jira, ["lock-strat", "RHAISTRAT-5200"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        labels = _get_labels(jira, "RHAIRFE-5200")
        assert "strat-creator-processing" in labels

    def test_lock_strat_missing_auto_created_fails(self, jira):
        jira.create("RHAIRFE-5201", "Source RFE", "Description.")
        jira.create("RHAISTRAT-5201", "Not our strategy",
                     "Description.",
                     labels=["strat-creator-rubric-pass"])
        _create_cloners_link(jira, "RHAISTRAT-5201", "RHAIRFE-5201")

        result = _run(jira, ["lock-strat", "RHAISTRAT-5201"])
        assert result.returncode == 2
        assert "strat-creator-auto-created" in result.stderr

    def test_lock_strat_needs_attention_fails(self, jira):
        jira.create("RHAIRFE-5202", "Source RFE", "Description.")
        jira.create("RHAISTRAT-5202", "Needs attention",
                     "Description.",
                     labels=["strat-creator-auto-created",
                             "strat-creator-needs-attention"])
        _create_cloners_link(jira, "RHAISTRAT-5202", "RHAIRFE-5202")

        result = _run(jira, ["lock-strat", "RHAISTRAT-5202"])
        assert result.returncode == 1
        assert "BLOCKED" in result.stderr
        assert "strat-creator-needs-attention" in result.stderr

    def test_lock_strat_human_signoff_fails(self, jira):
        jira.create("RHAIRFE-5203", "Source RFE", "Description.")
        jira.create("RHAISTRAT-5203", "Signed off",
                     "Description.",
                     labels=["strat-creator-auto-created",
                             "strat-creator-human-sign-off"])
        _create_cloners_link(jira, "RHAISTRAT-5203", "RHAIRFE-5203")

        result = _run(jira, ["lock-strat", "RHAISTRAT-5203"])
        assert result.returncode == 1
        assert "BLOCKED" in result.stderr

    def test_lock_strat_no_cloners_link_fails(self, jira):
        jira.create("RHAISTRAT-5204", "Orphan strategy",
                     "Description.",
                     labels=["strat-creator-auto-created"])

        result = _run(jira, ["lock-strat", "RHAISTRAT-5204"])
        assert result.returncode == 2
        assert "no Cloners link" in result.stderr

    def test_lock_strat_rfe_already_locked_fails(self, jira):
        jira.create("RHAIRFE-5205", "Locked RFE",
                     "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAISTRAT-5205", "Strategy",
                     "Description.",
                     labels=["strat-creator-auto-created",
                             "strat-creator-rubric-pass"])
        _create_cloners_link(jira, "RHAISTRAT-5205", "RHAIRFE-5205")

        result = _run(jira, ["lock-strat", "RHAISTRAT-5205"])
        assert result.returncode == 1
        assert "BLOCKED" in result.stderr


class TestUnlockStrat:

    def test_unlock_strat_success(self, jira):
        jira.create("RHAIRFE-5300", "Locked RFE",
                     "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAISTRAT-5300", "Strategy",
                     "Description.",
                     labels=["strat-creator-auto-created"])
        _create_cloners_link(jira, "RHAISTRAT-5300", "RHAIRFE-5300")

        result = _run(jira, ["unlock-strat", "RHAISTRAT-5300"])
        assert result.returncode == 0
        assert "UNLOCKED" in result.stderr

        labels = _get_labels(jira, "RHAIRFE-5300")
        assert "strat-creator-processing" not in labels

    def test_unlock_strat_no_cloners_link_fails(self, jira):
        jira.create("RHAISTRAT-5301", "Orphan",
                     "Description.",
                     labels=["strat-creator-auto-created"])

        result = _run(jira, ["unlock-strat", "RHAISTRAT-5301"])
        assert result.returncode == 2
        assert "no Cloners link" in result.stderr


class TestLockUnlockRoundtrip:

    def test_lock_then_unlock(self, jira):
        jira.create("RHAIRFE-5400", "Roundtrip RFE", "Description.")

        lock_result = _run(jira, ["lock", "RHAIRFE-5400"])
        assert lock_result.returncode == 0
        assert "strat-creator-processing" in _get_labels(
            jira, "RHAIRFE-5400")

        unlock_result = _run(jira, ["unlock", "RHAIRFE-5400"])
        assert unlock_result.returncode == 0
        assert "strat-creator-processing" not in _get_labels(
            jira, "RHAIRFE-5400")

        # Can lock again after unlock
        relock_result = _run(jira, ["lock", "RHAIRFE-5400"])
        assert relock_result.returncode == 0


class TestEdgeCases:

    def test_missing_env_vars(self, jira):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")}
        result = subprocess.run(
            [sys.executable, SCRIPT, "lock", "RHAIRFE-9999"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 2

    def test_no_args(self, jira):
        result = _run(jira, [])
        assert result.returncode == 2

    def test_unknown_command(self, jira):
        result = _run(jira, ["frobnicate", "RHAIRFE-9999"])
        assert result.returncode == 2

    def test_lock_strat_multiple_keys_rejected(self, jira):
        result = _run(jira, ["lock-strat", "RHAISTRAT-1", "RHAISTRAT-2"])
        assert result.returncode == 2
        assert "exactly one" in result.stderr


class TestLockedKeysFile:

    def test_batch_records_only_acquired_keys(self, jira, tmp_path):
        """Mixed batch records only actually-locked keys, not blocked ones."""
        jira.create("RHAIRFE-6010", "Lockable A", "Description.")
        jira.create("RHAIRFE-6011", "Blocked B", "Description.",
                     labels=["strat-creator-processing"])
        jira.create("RHAIRFE-6012", "Lockable C", "Description.")
        keys_file = str(tmp_path / "locked.txt")

        result = _run(jira, ["lock",
                              "--locked-keys-file", keys_file,
                              "RHAIRFE-6010", "RHAIRFE-6011", "RHAIRFE-6012"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        recorded = open(keys_file).read().strip().split('\n')
        assert "RHAIRFE-6010" in recorded
        assert "RHAIRFE-6011" not in recorded
        assert "RHAIRFE-6012" in recorded

    def test_multiple_keys_persisted(self, jira, tmp_path):
        """All successfully acquired keys are persisted; parent dirs created."""
        jira.create("RHAIRFE-6020", "RFE A", "Description.")
        jira.create("RHAIRFE-6021", "RFE B", "Description.")
        jira.create("RHAIRFE-6022", "RFE C", "Description.")
        keys_file = str(tmp_path / "sub" / "locked.txt")

        result = _run(jira, ["lock",
                              "--locked-keys-file", keys_file,
                              "RHAIRFE-6020", "RHAIRFE-6021", "RHAIRFE-6022"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        recorded = open(keys_file).read().strip().split('\n')
        assert set(recorded) == {
            "RHAIRFE-6020", "RHAIRFE-6021", "RHAIRFE-6022"}

        stdout_keys = result.stdout.strip().split()
        assert set(stdout_keys) == {
            "RHAIRFE-6020", "RHAIRFE-6021", "RHAIRFE-6022"}

    def test_lock_strat_records_rfe_key(self, jira, tmp_path):
        """lock-strat records the resolved RHAIRFE key, not the RHAISTRAT key."""
        jira.create("RHAIRFE-6030", "Source RFE", "Description.")
        jira.create("RHAISTRAT-6030", "Strategy", "Description.",
                     labels=["strat-creator-auto-created"])
        _create_cloners_link(jira, "RHAISTRAT-6030", "RHAIRFE-6030")
        keys_file = str(tmp_path / "locked.txt")

        result = _run(jira, ["lock-strat",
                              "--locked-keys-file", keys_file,
                              "RHAISTRAT-6030"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        content = open(keys_file).read()
        assert "RHAIRFE-6030" in content
        assert "RHAISTRAT-6030" not in content

    def test_omitted_preserves_behavior(self, jira):
        """Omitting --locked-keys-file preserves existing behavior."""
        jira.create("RHAIRFE-6040", "RFE", "Description.")

        result = _run(jira, ["lock", "RHAIRFE-6040"])
        assert result.returncode == 0
        assert "RHAIRFE-6040" in result.stdout.strip()
        assert "LOCKED RHAIRFE-6040" in result.stderr
        assert "strat-creator-processing" in _get_labels(jira, "RHAIRFE-6040")

    def test_stale_file_replaced(self, jira, tmp_path):
        """Stale contents from a prior invocation are replaced."""
        jira.create("RHAIRFE-6050", "New RFE", "Description.")
        keys_file = str(tmp_path / "locked.txt")

        with open(keys_file, 'w') as f:
            f.write("RHAIRFE-9999\nRHAIRFE-8888\n")

        result = _run(jira, ["lock",
                              "--locked-keys-file", keys_file,
                              "RHAIRFE-6050"])
        assert result.returncode == 0

        content = open(keys_file).read()
        assert "RHAIRFE-9999" not in content
        assert "RHAIRFE-8888" not in content
        assert "RHAIRFE-6050" in content

    def test_partial_failure_retains_earlier_records(self, jira, tmp_path):
        """API failure mid-batch retains records for earlier acquired locks."""
        jira.create("RHAIRFE-6060", "Good RFE", "Description.")
        # RHAIRFE-6061 intentionally not created — causes API error
        keys_file = str(tmp_path / "locked.txt")

        result = _run(jira, ["lock",
                              "--locked-keys-file", keys_file,
                              "RHAIRFE-6060", "RHAIRFE-6061"])
        assert result.returncode != 0

        content = open(keys_file).read()
        assert "RHAIRFE-6060" in content
        assert "RHAIRFE-6061" not in content

        labels = _get_labels(jira, "RHAIRFE-6060")
        assert "strat-creator-processing" in labels

    def test_recording_failure_triggers_rollback(self, tmp_path, monkeypatch,
                                                  capsys):
        """Unit test: write failure after add_labels triggers remove_labels."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import lock_issues

        remove_calls = []
        monkeypatch.setattr(lock_issues, "_get_labels",
                            lambda *a: set())
        monkeypatch.setattr(lock_issues, "add_labels",
                            lambda *a: None)
        monkeypatch.setattr(lock_issues, "remove_labels",
                            lambda s, u, t, k, labels: remove_calls.append(k))

        keys_file = str(tmp_path / "locked.txt")

        real_open = open

        def _failing_open(path, mode='r', *a, **kw):
            if path == keys_file and 'a' in mode:
                raise OSError("simulated write failure")
            return real_open(path, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", _failing_open)

        exit_code, locked = lock_issues.lock(
            "http://x", "u", "t", ["RHAIRFE-7001"],
            locked_keys_file=keys_file)

        assert exit_code == 2
        assert "RHAIRFE-7001" not in locked
        assert "RHAIRFE-7001" in remove_calls

        captured = capsys.readouterr()
        assert "RHAIRFE-7001" not in captured.out

    def test_lock_strat_missing_label_truncates_stale_file(self, jira,
                                                            tmp_path):
        """Stale file is truncated even when lock-strat fails on missing label."""
        jira.create("RHAISTRAT-6100", "Not ours", "Description.",
                     labels=["strat-creator-rubric-pass"])
        jira.create("RHAIRFE-6100", "Source RFE", "Description.")
        _create_cloners_link(jira, "RHAISTRAT-6100", "RHAIRFE-6100")
        keys_file = str(tmp_path / "locked.txt")

        with open(keys_file, 'w') as f:
            f.write("RHAIRFE-9999\n")

        result = _run(jira, ["lock-strat",
                              "--locked-keys-file", keys_file,
                              "RHAISTRAT-6100"])
        assert result.returncode == 2

        content = open(keys_file).read()
        assert "RHAIRFE-9999" not in content
        assert content.strip() == ""

    def test_lock_strat_blocking_label_truncates_stale_file(self, jira,
                                                             tmp_path):
        """Stale file is truncated even when lock-strat is blocked."""
        jira.create("RHAISTRAT-6110", "Blocked", "Description.",
                     labels=["strat-creator-auto-created",
                             "strat-creator-needs-attention"])
        jira.create("RHAIRFE-6110", "Source RFE", "Description.")
        _create_cloners_link(jira, "RHAISTRAT-6110", "RHAIRFE-6110")
        keys_file = str(tmp_path / "locked.txt")

        with open(keys_file, 'w') as f:
            f.write("RHAIRFE-9999\n")

        result = _run(jira, ["lock-strat",
                              "--locked-keys-file", keys_file,
                              "RHAISTRAT-6110"])
        assert result.returncode == 1

        content = open(keys_file).read()
        assert "RHAIRFE-9999" not in content
        assert content.strip() == ""

    def test_lock_strat_no_cloners_truncates_stale_file(self, jira, tmp_path):
        """Stale file is truncated even when lock-strat finds no Cloners link."""
        jira.create("RHAISTRAT-6120", "Orphan", "Description.",
                     labels=["strat-creator-auto-created"])
        keys_file = str(tmp_path / "locked.txt")

        with open(keys_file, 'w') as f:
            f.write("RHAIRFE-9999\n")

        result = _run(jira, ["lock-strat",
                              "--locked-keys-file", keys_file,
                              "RHAISTRAT-6120"])
        assert result.returncode == 2

        content = open(keys_file).read()
        assert "RHAIRFE-9999" not in content
        assert content.strip() == ""

    def test_lock_strat_api_failure_truncates_stale_file(self, jira,
                                                          tmp_path):
        """Stale file is truncated even when lock-strat hits API failure."""
        # RHAISTRAT-6130 intentionally not created — causes 404
        keys_file = str(tmp_path / "locked.txt")

        with open(keys_file, 'w') as f:
            f.write("RHAIRFE-9999\n")

        result = _run(jira, ["lock-strat",
                              "--locked-keys-file", keys_file,
                              "RHAISTRAT-6130"])
        assert result.returncode != 0

        content = open(keys_file).read()
        assert "RHAIRFE-9999" not in content
        assert content.strip() == ""
