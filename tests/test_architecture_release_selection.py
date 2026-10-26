"""The host and Skill bootstrap must agree on numeric architecture versions."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_ci_config", ROOT / ".fullsend/ci/fullsend-config.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


@pytest.mark.parametrize("releases,expected", [
    (["rhoai-3.9", "rhoai-3.10"], "rhoai-3.10"),
    (["rhoai-3.6-ea.2", "rhoai-3.6-ea.10"], "rhoai-3.6-ea.10"),
    # Preserve the existing suffix convention; change numeric ordering only.
    (["rhoai-3.6", "rhoai-3.6-ea.2"], "rhoai-3.6-ea.2"),
])
@pytest.mark.parametrize("entrypoint", ["ci", "local", "remote"])
def test_all_fetch_paths_select_the_same_numeric_release(tmp_path, releases, expected, entrypoint):
    repo = tmp_path / "context-source"
    repo.mkdir()
    for release in releases:
        path = repo / "architecture" / release
        path.mkdir(parents=True)
        (path / "PLATFORM.md").write_text(release + "\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "synthetic release directories"], check=True)
    target = tmp_path / "target"
    target.mkdir()
    if entrypoint == "ci":
        output = target / "context"
        receipt = config.fetch_context(output, tmp_path / "receipt.json", str(repo))
        assert receipt["release"] == expected
    else:
        env = dict(os.environ)
        for name in ("RFE_SKIP_BOOTSTRAP", "STRAT_ARCHITECTURE_CONTEXT_PATH"):
            env.pop(name, None)
        args = ["bash", str(ROOT / "scripts/fetch-architecture-context.sh")]
        if entrypoint == "local":
            args.append(str(repo))
        else:
            tools = tmp_path / "tools"
            tools.mkdir()
            listing = tmp_path / "listing.json"
            listing.write_text(json.dumps([{"name": release, "type": "dir"} for release in releases]
                                          + [{"name": "rhoai-99.99", "type": "symlink"}]))
            curl = tools / "curl"
            curl.write_text('#!/bin/sh\ncat "$TEST_RELEASE_LISTING"\n')
            curl.chmod(0o755)
            env.update({"PATH": str(tools) + ":" + env["PATH"], "TEST_RELEASE_LISTING": str(listing),
                        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{repo}.insteadOf",
                        "GIT_CONFIG_VALUE_0": "https://github.com/opendatahub-io/architecture-context"})
        result = subprocess.run(args, cwd=target, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        output = target / ".context/architecture-context"
    assert (output / "LATEST_VERSION").read_text().strip() == expected
    assert (output / "architecture" / expected / "PLATFORM.md").read_text() == expected + "\n"
