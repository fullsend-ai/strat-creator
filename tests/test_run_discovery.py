"""Publication-to-reader contracts, including mixed legacy/Workflow history."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from artifact_utils import write_frontmatter  # noqa: E402


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


extract = load("scripts/extract-pipeline-data.py", "discovery_extract")
dashboard = load("scripts/generate-dashboard.py", "discovery_dashboard")
publisher = load(".fullsend/shared/publish/push-results.py", "discovery_publisher")
publication = load(".fullsend/shared/prepare_publication.py", "discovery_bundle")


def artifacts(root, timestamp, *, dry_run=False):
    task = root / "strat-tasks/RHAISTRAT-7001.md"
    task.parent.mkdir(parents=True)
    task.write_text("## Strategy\nThe router cancels requests at the deadline.\n")
    write_frontmatter(task, {"strat_id": "RHAISTRAT-7001", "source_rfe": "RHAIRFE-7001",
                            "title": "Bounded request lifetime", "status": "Refined",
                            "priority": "Major"}, "strat-task")
    review = root / "strat-reviews/RHAISTRAT-7001-review.md"
    review.parent.mkdir()
    review.write_text("## Review\nAuthored review.\n")
    scores = dict.fromkeys(("feasibility", "testability", "scope", "architecture"), 2)
    write_frontmatter(review, {"strat_id": "RHAISTRAT-7001", "recommendation": "approve",
                              "needs_attention": False, "scores": {**scores, "total": 8},
                              "reviewers": dict.fromkeys(scores, "approve")}, "strat-review")
    data = extract.extract_run(str(root))
    assert data is not None
    data.update(timestamp=timestamp, dry_run=dry_run)
    (root / "pipeline-data.json").write_text(json.dumps(data))


def test_actual_publisher_output_appears_in_dashboard_and_summary(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    artifacts(tmp_path, "2026-10-20T15:00:00")
    run = publisher.organize_identified_run(tmp_path, "review-probe")
    assert (run / ".publication.json").is_file()
    for runs in (dashboard.scan_all_runs(str(run.parent), {}), extract.scan_runs(str(run.parent))):
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-review-probe" and runs[0]["is_current"]
        assert runs[0]["strategies"][0]["strat_id"] == "RHAISTRAT-7001"


def test_mixed_layouts_sort_by_generation_time_and_respect_current_and_dry_run(tmp_path):
    for name, timestamp, dry in [("20261020-140000", "2026-10-20T14:00:00", False),
                                 ("run-z-middle", "2026-10-20T11:00:00-04:00", False),
                                 ("run-a-latest", "2026-10-20T16:00:00+00:00", False),
                                 ("run-dry", "2026-10-20T17:00:00", True)]:
        run = tmp_path / name
        artifacts(run, timestamp, dry_run=dry)
        if name.startswith("run-"):
            (run / ".publication.json").write_text(json.dumps({"contract": "strat-publication-v1",
                                                              "run_id": name[4:]}))
    (tmp_path / "current").symlink_to("run-a-latest")
    runs = dashboard.scan_all_runs(str(tmp_path), {})
    assert [r["run_id"] for r in runs] == ["20261020-140000", "run-z-middle", "run-a-latest"]
    assert runs[1]["timestamp"] == "2026-10-20T15:00:00"
    assert [r["run_id"] for r in runs if r["is_current"]] == ["run-a-latest"]
    assert dashboard.scan_all_runs(str(tmp_path), {}, max_runs=1) == runs[-1:]
    exported = extract.scan_runs(str(tmp_path))
    assert [r["run_id"] for r in exported] == [r["run_id"] for r in runs] + ["run-dry"]
    assert exported[-1]["dry_run"] is True


@pytest.mark.parametrize("fault", ["wrong_identity", "wrong_contract", "symlink"])
def test_both_readers_reject_invalid_identified_run_metadata(tmp_path, fault):
    run = tmp_path / "run-bad"
    artifacts(run, "2026-10-20T15:00:00")
    value = {"contract": "strat-publication-v1", "run_id": "bad"}
    if fault == "wrong_identity":
        value["run_id"] = "another-run"
    elif fault == "wrong_contract":
        value["contract"] = "unknown"
    manifest = run / ".publication.json"
    if fault == "symlink":
        (tmp_path / "outside.json").write_text(json.dumps(value))
        manifest.symlink_to(tmp_path / "outside.json")
    else:
        manifest.write_text(json.dumps(value))
    for reader in (lambda: dashboard.scan_all_runs(str(tmp_path), {}), lambda: extract.scan_runs(str(tmp_path))):
        with pytest.raises(ValueError, match="published run"):
            reader()


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("header,row,labels", [
    ("RFE | Summary | Reason", "RHAIRFE-7001 | A gated RFE | Existing STRAT is Closed", ""),
    ("RFE | Summary | Reason | Run", "RHAIRFE-7001 | A gated RFE | Existing STRAT is Closed | fixture", ""),
    ("RFE | Summary | Labels | Missing",
     "RHAIRFE-7001 | A gated RFE | legacy-input-label | Existing STRAT is Closed", "legacy-input-label"),
])
def test_sealed_skip_only_publication_survives_summary_extraction(tmp_path, dry_run, header, row, labels):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    separator = " | ".join("---" for _ in header.split("|"))
    (handoff / "strat-skipped.md").write_text(f"| {header} |\n| {separator} |\n| {row} |\n")
    result = tmp_path / "agent-result.json"
    result.write_text(json.dumps({"dry_run": dry_run}))
    bundle = tmp_path / "bundle"
    publication.prepare(handoff, bundle, result, "batch")
    results = tmp_path / "results"
    shutil.copytree(bundle, results)
    subprocess.run(["git", "init", "-q", str(results)], check=True)
    run = publisher.organize_identified_run(results, "skip-only")
    runs = extract.scan_runs(str(run.parent))
    assert len(runs) == 1 and runs[0]["stats"]["total"] == 0
    assert runs[0]["dry_run"] is dry_run
    assert runs[0]["skipped"] == [{"rfe_key": "RHAIRFE-7001", "title": "A gated RFE",
                                  "labels": labels, "missing": "Existing STRAT is Closed"}]
    output = run.parent / "summary.json"
    completed = subprocess.run([sys.executable, str(ROOT / "scripts/extract-pipeline-data.py"),
                                "--data-dir", str(run.parent), "--output", str(output)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(output.read_text())
    assert len(summary["runs"]) == 1 and summary["summary"]["skipped"][0]["rfe_key"] == "RHAIRFE-7001"
    production = json.loads(output.with_name("summary-production.json").read_text())
    assert len(production["runs"]) == (0 if dry_run else 1)


def test_empty_legacy_or_unsealed_runs_are_not_adopted(tmp_path):
    (tmp_path / "20261021-160000").mkdir()
    run = tmp_path / "run-incomplete"
    run.mkdir()
    (run / ".publication.json").write_text(json.dumps({"contract": "strat-publication-v1", "run_id": "incomplete"}))
    (run / "pipeline-data.json").write_text(json.dumps({"timestamp": "2026-10-21T17:00:00", "strategies": []}))
    assert extract.scan_runs(str(tmp_path)) == []


def test_empty_publication_metadata_must_match_its_seal(tmp_path):
    run = tmp_path / "run-invalid"
    run.mkdir()
    data = json.dumps({"timestamp": "2026-10-21T17:00:00", "strategies": [], "stats": {"total": 0}})
    (run / "pipeline-data.json").write_text(data + "\n")
    (run / ".publication.json").write_text(json.dumps({"contract": "strat-publication-v1", "run_id": "invalid",
        "files": {"pipeline-data.json": hashlib.sha256(data.encode()).hexdigest()}}))
    with pytest.raises(ValueError, match="published run"):
        extract.scan_runs(str(tmp_path))
