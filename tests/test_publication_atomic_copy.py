"""Faults during a real copy must never leave a truncated published artifact."""

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_pr85_regressions import module

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('publisher', sys.argv[1])
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
def interrupted(src, dest):
    data = Path(src).read_bytes()
    Path(dest).write_bytes(data[:128])
    Path('copy-target.txt').write_text(str(dest))
    if sys.argv[2] == 'kill':
        os.kill(os.getpid(), signal.SIGKILL)
    raise OSError('authored disk/copy failure')
publisher.shutil.copyfile = interrupted
publisher.organize_identified_run(Path.cwd(), 'copy-run')
'''


def inputs(root):
    (root / ".git").mkdir()
    tasks = root / "strat-tasks"
    tasks.mkdir()
    task = tasks / "STRAT-1.md"
    task.write_bytes(b"Authored immutable strategy.\n" * 4096)
    return task


@pytest.mark.parametrize("fault", ["kill", "exception"])
def test_mid_copy_failure_keeps_final_absent_and_retries_cleanly(tmp_path, fault):
    task = inputs(tmp_path)
    expected = task.read_bytes()
    child = subprocess.run([sys.executable, "-c", CHILD,
                            str(ROOT / ".fullsend/shared/publish/push-results.py"), fault],
                           cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert child.returncode == -signal.SIGKILL if fault == "kill" else child.returncode != 0
    copied_to = Path((tmp_path / "copy-target.txt").read_text())
    assert task.read_bytes() == expected
    final = tmp_path / "RHAISTRAT/run-copy-run/strat-tasks/STRAT-1.md"
    assert not final.exists(), "an interrupted copy must not publish partial final bytes"
    if fault == "kill":
        assert copied_to.read_bytes() == expected[:128]  # A real orphan, outside the published inventory.
    publisher = module(".fullsend/shared/publish/push-results.py", "atomic_publisher")
    run = publisher.organize_identified_run(tmp_path, "copy-run")
    assert final.read_bytes() == expected
    assert not task.exists()
    manifest = json.loads((run / ".publication.json").read_text())
    assert set(manifest["files"]) == {"strat-tasks/STRAT-1.md"}
    assert {str(p.relative_to(run)) for p in run.rglob("*") if p.is_file()} == {
        ".publication.json", "strat-tasks/STRAT-1.md"}
    assert publisher.organize_identified_run(tmp_path, "copy-run") == run


def test_genuine_destination_conflict_preserves_sources_and_refuses_overwrite(tmp_path):
    task = inputs(tmp_path)
    final = tmp_path / "RHAISTRAT/run-copy-run/strat-tasks/STRAT-1.md"
    final.parent.mkdir(parents=True)
    final.write_text("Different existing publication.\n")
    before = task.read_bytes()
    publisher = module(".fullsend/shared/publish/push-results.py", "conflict_publisher")
    with pytest.raises(ValueError, match="conflicting"):
        publisher.organize_identified_run(tmp_path, "copy-run")
    assert task.read_bytes() == before
    assert final.read_text() == "Different existing publication.\n"


def test_changed_copy_bytes_never_become_the_final_publication(tmp_path, monkeypatch):
    task = inputs(tmp_path)
    publisher = module(".fullsend/shared/publish/push-results.py", "changed_publisher")

    def wrong_bytes(src, dest):
        Path(dest).write_bytes(b"Unexpected bytes from an unstable source.")

    monkeypatch.setattr(publisher.shutil, "copyfile", wrong_bytes)
    with pytest.raises(ValueError, match="content|changed|hash"):
        publisher.organize_identified_run(tmp_path, "copy-run")
    assert task.exists()
    assert not (tmp_path / "RHAISTRAT/run-copy-run/strat-tasks/STRAT-1.md").exists()
