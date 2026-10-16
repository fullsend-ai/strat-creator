#!/usr/bin/env python3
"""Freeze a validated handoff and generated reports once for post-script retries."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from strat_side_effects import atomic, locked  # noqa: E402


def inventory(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("publication bundle must not contain symlinks")
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare(handoff, bundle, result_file, label):
    state = bundle.with_suffix(".json")
    source = {"handoff": inventory(handoff), "result": hashlib.sha256(result_file.read_bytes()).hexdigest(),
              "label": label}
    with locked(bundle.with_suffix(".lock")):
        saved = json.loads(state.read_text()) if state.exists() else None
        if saved and saved["source"] != source:
            raise ValueError("validated publication input changed; use a new run after reconciliation")
        if saved and saved.get("ready"):
            if inventory(bundle) != saved["files"]:
                raise ValueError("sealed publication bundle changed")
            return
        atomic(state, {"source": source, "ready": False})
        shutil.copytree(handoff, bundle, dirs_exist_ok=True)
        for name in ("agent-result.json", "strat-progress.json"):
            (bundle / name).unlink(missing_ok=True)
        if any((bundle / "strat-tasks").glob("*.md")):
            subprocess.run([sys.executable, str(ROOT / "scripts/generate-report.py"), "--artifacts", str(bundle),
                            "--output", str(bundle / "reports/report.html")], check=True)
        else:
            import html
            (bundle / "reports").mkdir(exist_ok=True)
            summary = json.loads(result_file.read_text()).get("summary", "All acquired work was skipped.")
            (bundle / "reports/report.html").write_text(
                "<!doctype html><meta charset='utf-8'><h1>No strategies required processing</h1><p>"
                + html.escape(summary) + "</p>\n")
        result = json.loads(result_file.read_text())
        command = [sys.executable, str(ROOT / "scripts/extract-pipeline-data.py"), "--run-dir", str(bundle),
                   "--output", str(bundle / "pipeline-data.json"), "--config", label, "--allow-empty"]
        if result.get("dry_run") is True:
            command.append("--dry-run")
        subprocess.run(command, check=True)
        atomic(state, {"source": source, "ready": True, "files": inventory(bundle)})


if __name__ == "__main__":
    prepare(*(Path(x) for x in sys.argv[1:4]), sys.argv[4])
