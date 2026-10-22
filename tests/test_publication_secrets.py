"""Host publication rejects credential material without rewriting accepted bytes."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redactor = load(".fullsend/ci/fullsend-redact.py", "publication_redactor")
publication = load(".fullsend/shared/prepare_publication.py", "safe_publication")


@pytest.mark.parametrize("content", ["synthetic-private-token-123", "Authorization: Bearer unknown-token-12345678",
                                     '{"access_token":"unknown-token"}'])
def test_publication_check_rejects_credentials_without_changing_input(tmp_path, content):
    artifact = tmp_path / "strategy.md"
    artifact.write_text(content)
    original = artifact.read_bytes()
    with pytest.raises(ValueError, match="credential") as rejected:
        redactor.require_clean(tmp_path, ["synthetic-private-token-123"])
    assert content not in str(rejected.value)
    assert artifact.read_bytes() == original


@pytest.mark.parametrize("kind", ["binary", "symlink", "filename"])
def test_publication_check_refuses_unsafe_files(tmp_path, kind):
    path = tmp_path / ("synthetic-private-token-123" if kind == "filename" else "artifact.md")
    if kind == "symlink":
        path.symlink_to("/etc/hosts")
    else:
        path.write_bytes(b"\xff\x00" if kind == "binary" else b"safe body")
    with pytest.raises(ValueError):
        redactor.require_clean(tmp_path, ["synthetic-private-token-123"])


@pytest.mark.parametrize("location", ["handoff", "result"])
def test_unsafe_publication_stops_before_sealing(tmp_path, monkeypatch, capfd, location):
    secret = "synthetic-private-token-123"
    monkeypatch.setenv("RESULTS_PUSH_TOKEN", secret)
    handoff, bundle, result = tmp_path / "handoff", tmp_path / "bundle", tmp_path / "result.json"
    handoff.mkdir()
    artifact = handoff / "strat-skipped.md"
    artifact.write_text(secret if location == "handoff" else "No acquired work.\n")
    result.write_text(json.dumps({"summary": secret if location == "result" else "No work", "dry_run": True}))
    original = artifact.read_bytes(), result.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        publication.prepare(handoff, bundle, result, "single-rfe")
    output = capfd.readouterr()
    assert "credential" in output.err and secret not in output.err + output.out
    assert (artifact.read_bytes(), result.read_bytes()) == original
    assert not bundle.exists() and not bundle.with_suffix(".json").exists()
