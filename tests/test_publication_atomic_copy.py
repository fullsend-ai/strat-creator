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
    (root / ".git").mkdir(exist_ok=True)
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


@pytest.mark.parametrize("relative", ["RHAISTRAT", "RHAISTRAT/run-copy-run",
                                    "RHAISTRAT/run-copy-run/strat-tasks",
                                    "RHAISTRAT/run-copy-run/strat-tasks/nested"])
@pytest.mark.parametrize("dangling", [False, True])
def test_cloned_directory_symlink_cannot_redirect_publication(tmp_path, relative, dangling):
    source, results, outside = (tmp_path / name for name in ("source", "results", "outside"))
    source.mkdir()
    if not dangling:
        outside.mkdir()
        (outside / "keep.txt").write_text("Unrelated outside content.\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    link = source / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    subprocess.run(["git", "-C", str(source), "add", "--", relative], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-q", "-m", "Directory symlink fixture"], check=True)
    subprocess.run(["git", "clone", "-q", str(source), str(results)], check=True)
    tracked = subprocess.check_output(["git", "-C", str(results), "ls-files", "--stage", "--", relative], text=True)
    assert tracked.startswith("120000 ") and (results / relative).is_symlink()
    task = inputs(results)
    if relative.endswith("nested"):
        nested = task.parent / "nested"
        nested.mkdir()
        task = task.rename(nested / task.name)
    content = task.read_bytes()
    publisher = module(".fullsend/shared/publish/push-results.py", "symlink_publisher")
    with pytest.raises(ValueError, match="symlink"):
        publisher.organize_identified_run(results, "copy-run")
    assert task.read_bytes() == content
    if dangling:
        assert not outside.exists()
    else:
        assert list(outside.iterdir()) == [outside / "keep.txt"]
        assert (outside / "keep.txt").read_text() == "Unrelated outside content.\n"
    assert not (results / ".git/strat-publication.json").exists()


def test_source_directory_symlink_is_rejected_before_reading_or_copying(tmp_path):
    results, outside = tmp_path / "results", tmp_path / "outside"
    results.mkdir()
    (results / ".git").mkdir()
    outside.mkdir()
    (outside / "STRAT-1.md").write_text("Outside source must stay untouched.\n")
    (results / "strat-tasks").symlink_to(outside, target_is_directory=True)
    publisher = module(".fullsend/shared/publish/push-results.py", "source_symlink_publisher")
    with pytest.raises(ValueError, match="symlink"):
        publisher.organize_identified_run(results, "copy-run")
    assert not (results / ".git/strat-publication.json").exists()
    assert not (results / "RHAISTRAT").exists()
    assert (outside / "STRAT-1.md").read_text() == "Outside source must stay untouched.\n"
