"""Fault cases for derived summaries, review inventory and publication metadata."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from review_composition_support import prepare

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from publish_review import publish, review_comment  # noqa: E402


def task(root, key, rfe):
    path = root / f"artifacts/strat-tasks/{key}.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nstrat_id: {key}\nsource_rfe: {rfe}\njira_key: null\n"
                    "title: Authored test strategy\npriority: Major\nstatus: Refined\n---\n"
                    "## Strategy\nUse bounded request queues.\n")


@pytest.mark.parametrize("existing", [False, True])
def test_interrupted_summary_write_preserves_destination_and_allows_retry(tmp_path, existing):
    key, rfe = "STRAT-9701", "RHAIRFE-9701"
    task(tmp_path, key, rfe)
    prepare(tmp_path, key, rfe, (2, 2, 2, 2))
    review = tmp_path / f"artifacts/strat-reviews/{key}-review.md"
    summary = review.with_name(f"{key}-review-comment.md")
    verdict = {"key": key, "recommendation": "approve", "needs_attention": False}
    expected = review_comment(review, verdict)
    assert len(expected.encode()) > 64
    if existing:
        summary.write_text(expected)
    code = (
        "import resource,signal,sys; from pathlib import Path; "
        f"sys.path.insert(0,{str(ROOT / 'scripts')!r}); from publish_review import publish; "
        "signal.signal(signal.SIGXFSZ,signal.SIG_DFL); "
        "resource.setrlimit(resource.RLIMIT_FSIZE,(64,64)); "
        f"publish(Path({str(tmp_path)!r}),{key!r},dry_run=True)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=20)
    if not existing:
        assert result.returncode != 0
    assert summary.exists() == existing
    if existing:
        assert summary.read_text() == expected
    assert publish(tmp_path, key, dry_run=True) == verdict
    assert summary.read_text() == expected
    summary.write_text("A different complete summary.\n")
    with pytest.raises(ValueError, match="reconcile"):
        publish(tmp_path, key, dry_run=True)


def test_unplanned_review_is_a_controlled_error_without_changing_saved_progress(tmp_path):
    key, rfe = "STRAT-9702", "RHAIRFE-9702"
    task(tmp_path, key, rfe)
    directory = tmp_path / "tmp"
    directory.mkdir()
    (directory / "strat-input.json").write_text(json.dumps({"run_id": "review-edge", "mode": "batch",
        "dry_run": True, "acquired_keys": [rfe], "input_keys": [rfe], "skipped": []}))
    prepare(tmp_path, key, rfe, (2, 2, 2, 2))
    progress = directory / "strat-progress.json"
    progress.write_text(json.dumps({"run_id": "review-edge", "create": {"strategies": []},
                                    "refine": {}, "push": None, "review": {}}))
    before = progress.read_bytes()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/strat_pipeline_state.py"), "record-review", key],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "not in this run's create inventory" in result.stderr
    assert "Traceback" not in result.stderr
    assert progress.read_bytes() == before


@pytest.mark.parametrize("live,ambient", [(True, "0"), (True, "1"), (False, "0")])
def test_validated_result_alone_controls_published_dry_run(tmp_path, monkeypatch, live, ambient):
    spec = importlib.util.spec_from_file_location(
        "publication_environment", ROOT / ".fullsend/shared/prepare_publication.py")
    publication = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publication)
    monkeypatch.setenv("DRY_RUN", ambient)
    handoff, bundle, result = tmp_path / "handoff", tmp_path / "bundle", tmp_path / "result.json"
    handoff.mkdir()
    (handoff / "strat-skipped.md").write_text("All selected work was already complete.\n")
    result.write_text(json.dumps({"summary": "Skip-only fixture", "dry_run": not live}))
    publication.prepare(handoff, bundle, result, "test")
    assert json.loads((bundle / "pipeline-data.json").read_text())["dry_run"] is (not live)
