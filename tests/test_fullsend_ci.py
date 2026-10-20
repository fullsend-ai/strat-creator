"""Model-free checks of the opt-in Fullsend path and failure cleanup."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".fullsend/ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = load("fullsend-config")
redactor = load("fullsend-redact")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def architecture(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test Runner")
    git(repo, "config", "user.email", "runner@example.invalid")
    (repo / "architecture/rhoai-3.6-ea.2").mkdir(parents=True)
    (repo / "architecture/rhoai-3.6-ea.2/PLATFORM.md").write_text("current architecture\n")
    (repo / "architecture/rhoai-3.6-ea.2/workbenches.md").write_text("component architecture\n")
    (repo / "architecture/rhoai-3.5").mkdir()
    (repo / "architecture/rhoai-3.5/PLATFORM.md").write_text("old architecture\n")
    (repo / "architecture/rhoai-9.9").symlink_to("excluded-alias-target")
    (repo / "overlays").mkdir()
    (repo / "overlays/storage.md").write_text("current overlay\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial context")
    return repo


def test_live_context_follows_new_head_without_refreshing_previous_run(architecture, tmp_path):
    first = tmp_path / "first"
    before = config.fetch_context(first, tmp_path / "first.json", str(architecture))
    assert (first / "LATEST_VERSION").read_text() == "rhoai-3.6-ea.2\n"
    assert not (first / "architecture/rhoai-3.5").exists()
    assert not (first / "architecture/rhoai-9.9").is_symlink()
    assert (first / "overlays/storage.md").read_text() == "current overlay\n"
    (architecture / "overlays/storage.md").write_text("new SME overlay\n")
    git(architecture, "commit", "-qam", "update overlay")
    second = tmp_path / "second"
    after = config.fetch_context(second, tmp_path / "second.json", str(architecture))
    assert after["resolved_commit"] != before["resolved_commit"]
    assert (second / "overlays/storage.md").read_text() == "new SME overlay\n"
    assert (first / "overlays/storage.md").read_text() == "current overlay\n"
    with pytest.raises(ValueError, match="prepared run"):
        config.fetch_context(first, tmp_path / "never-written.json", str(architecture))


@pytest.mark.parametrize("failure", ["missing-platform", "escaping-symlink"])
def test_invalid_context_is_rejected_without_partial_snapshot(architecture, tmp_path, failure):
    if failure == "missing-platform":
        (architecture / "architecture/rhoai-3.6-ea.2/PLATFORM.md").unlink()
    else:
        (architecture / "architecture/rhoai-3.6-ea.2/unsafe").symlink_to("/etc/passwd")
    git(architecture, "add", "-A")
    git(architecture, "commit", "-qm", "invalid context")
    destination = tmp_path / "invalid"
    receipt = tmp_path / "invalid.json"
    with pytest.raises((ValueError, tarfile.FilterError)):
        config.fetch_context(destination, receipt, str(architecture))
    assert not destination.exists()
    assert not receipt.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"FULLSEND_MODE": "resume"},
        {"RFE_KEY": "RHAIRFE-1; echo unsafe"},
        {"FULLSEND_MODE": "batch", "RFE_KEY": "RHAIRFE-1"},
        {"STRAT_DRY_RUN": "yes"},
        {"FULLSEND_PUBLISH": "1"},
        {"CONFIG_FILE": "config/batch.yaml"},
        {"STRAT_KEY": "RHAISTRAT-1"},
        {"RFE_FILTER": "one"},
        {"BATCH_SIZE": "0"},
        {"BATCH_OFFSET": "-1"},
        {"CI_PIPELINE_SOURCE": "schedule"},
    ],
)
def test_invalid_inputs_stop_before_preparation(changes):
    env = {"RFE_KEY": "RHAIRFE-19", **changes}
    with pytest.raises(ValueError):
        config.validate(env)


def test_default_and_published_batch_inputs():
    assert config.validate({"RFE_KEY": "RHAIRFE-19"})["dry_run"] == "1"
    result = config.validate({"FULLSEND_MODE": "batch", "STRAT_DRY_RUN": "0", "FULLSEND_PUBLISH": "1"})
    assert result["publish"] == "1"


@pytest.fixture
def ci_consumer(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT / ".fullsend",
        source / ".fullsend",
        ignore=shutil.ignore_patterns("__pycache__", ".fullsend-cache", "config.yaml", "lock.yaml"),
    )
    git(source, "init", "-q")
    git(source, "config", "user.name", "Test Runner")
    git(source, "config", "user.email", "runner@example.invalid")
    git(source, "add", ".")
    git(source, "commit", "-qm", "test package")
    revision = git(source, "rev-parse", "HEAD")
    consumer = tmp_path / "consumer"
    (consumer / ".fullsend").mkdir(parents=True)
    entries = []
    for name in ("strat-single", "strat-batch"):
        relative = f".fullsend/{name}/{name}.yaml"
        digest = hashlib.sha256((source / relative).read_bytes()).hexdigest()
        entries.append(
            {
                "name": name,
                "source": f"https://raw.githubusercontent.com/example/strat-creator/{revision}/{relative}#sha256={digest}",
            }
        )
    (consumer / ".fullsend/config.yaml").write_text(yaml.safe_dump({"version": "1", "agents": entries}))
    env = {
        "STRAT_CREATOR_REPO": "https://github.com/example/strat-creator.git",
        "STRAT_CREATOR_REF": revision,
        "CI_PROJECT_DIR": str(consumer),
    }
    return source, consumer, env


def test_registered_package_expands_only_into_private_runtime_state(ci_consumer, tmp_path):
    source, consumer, env = ci_consumer
    destination = tmp_path / "runtime"
    config.materialize_runtime(consumer, destination, env, source)
    assert sorted(p.name for p in (consumer / ".fullsend").iterdir()) == ["config.yaml"]
    assert (destination / "config.yaml").read_bytes() == (consumer / ".fullsend/config.yaml").read_bytes()
    assert not (destination / "ci").exists()
    for name in ("strat-single", "strat-batch"):
        harness = yaml.safe_load((destination / name / f"{name}.yaml").read_text())
        paths = [harness[field] for field in ("agent", "policy", "pre_script", "post_script")]
        paths += [harness["validation_loop"][field] for field in ("script", "schema")]
        assert all((destination / path).is_file() for path in paths)
        assert (destination / harness["plugins"][0] / ".claude-plugin/plugin.json").is_file()
    config.checkout_candidate(tmp_path / "target", tmp_path / "source.json", env, source)
    assert git(tmp_path / "target", "remote", "get-url", "origin") == env["STRAT_CREATOR_REPO"]
    assert git(tmp_path / "target", "rev-parse", "HEAD") == env["STRAT_CREATOR_REF"]
    with pytest.raises(ValueError, match="already exists"):
        config.materialize_runtime(consumer, destination, env, source)


@pytest.mark.parametrize("failure", ["ref", "hash", "repository", "source-edit"])
def test_registration_mismatch_stops_before_packaging(ci_consumer, tmp_path, failure):
    source, consumer, env = ci_consumer
    if failure == "ref":
        env["STRAT_CREATOR_REF"] = "0" * 40
    elif failure == "repository":
        env["STRAT_CREATOR_REPO"] = "https://github.com/other/strat-creator.git"
    elif failure == "source-edit":
        (source / ".fullsend/shared/validate-output.sh").write_text("uncommitted replacement")
    else:
        path = consumer / ".fullsend/config.yaml"
        content = yaml.safe_load(path.read_text())
        content["agents"][0]["source"] = content["agents"][0]["source"].split("#")[0] + "#sha256=" + "0" * 64
        path.write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError):
        config.materialize_runtime(consumer, tmp_path / "rejected", env, source)
    assert not (tmp_path / "rejected").exists()


def test_secrets_and_private_key_fragments_scrubbed_without_following_links(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(
        json.dumps({"private_key": "-----BEGIN PRIVATE KEY-----\nSYNTHETIC-KEY-CONTENT\n-----END PRIVATE KEY-----\n"})
    )
    values = redactor.secrets({"JIRA_TOKEN": "synthetic-jira-secret", "GOOGLE_APPLICATION_CREDENTIALS": str(key)})
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic-jira-secret")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "escape").symlink_to(outside)
    (artifacts / "output.jsonl").write_text("synthetic-jira-secret SYNTHETIC-KEY-CONTENT")
    redactor.scrub_tree(artifacts, values)
    assert (artifacts / "output.jsonl").read_text() == "[REDACTED] [REDACTED]"
    assert outside.read_text() == "synthetic-jira-secret"
    assert not (artifacts / "escape").is_symlink()
    (artifacts / "binary").write_bytes(b"\xff\x00")
    with pytest.raises(ValueError, match="binary artifact"):
        redactor.scrub_tree(artifacts, values)


def test_adc_authorization_is_redacted_without_knowing_its_value():
    text = 'Authorization: Bearer synthetic-access-token-123456789\n{"access_token":"fresh-synthetic-token"}'
    assert redactor.redact(text, []) == 'Authorization: Bearer [REDACTED]\n{"access_token":"[REDACTED]"}'


@pytest.mark.parametrize(
    "run_code,release_code,head_matches,expected",
    [(23, 0, True, 23), (0, 17, True, 17), (0, 0, True, 0), (0, 0, False, 1)],
)
def test_actual_ci_wrapper_releases_claims_and_preserves_failure(
    tmp_path, run_code, release_code, head_matches, expected
):
    """Run the real outer shell with fake external services and no credentials."""
    scripts = tmp_path / ".fullsend/ci"
    scripts.mkdir(parents=True)
    for name in ("run-fullsend.sh", "fullsend-redact.py"):
        shutil.copyfile(ROOT / ".fullsend/ci" / name, scripts / name)
    tools = tmp_path / "tools"
    tools.mkdir()

    def executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    executable(
        tools / "python3",
        f'#!{sys.executable}\nimport os,sys\nif sys.argv[1] == "-c": sys.exit(0)\n'
        'os.execv(sys.executable,[sys.executable,*sys.argv[1:]])\n',
    )
    (scripts / "fullsend-config.py").write_text(
        'import pathlib,sys\nif sys.argv[1] != "validate": pathlib.Path(sys.argv[2]).mkdir(parents=True)\n'
    )
    archive = tmp_path / "binary.tar.gz"
    body = (b'#!/bin/sh\nif [ "$1" = --version ]; then echo "fullsend 0.47.1"; exit 0; fi\n'
            b'echo "synthetic-jira-secret"\nexit "$TEST_RUN_CODE"\n')
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("fullsend")
        member.mode, member.size = 0o755, len(body)
        stream.addfile(member, io.BytesIO(body))
    (scripts / "fullsend-versions.env").write_text(
        "STRAT_CREATOR_REF="
        + "a" * 40
        + "\nFULLSEND_VERSION=0.47.1\nFULLSEND_TARBALL_SHA256="
        + hashlib.sha256(archive.read_bytes()).hexdigest()
        + "\n"
    )
    executable(
        tools / "curl", '#!/bin/sh\nwhile [ "$1" != --output ]; do shift; done\ncp "$TEST_BINARY_ARCHIVE" "$2"\n'
    )
    executable(tools / "git", '#!/bin/sh\nprintf "%s\\n" "$TEST_HEAD_SHA"\n')
    (scripts / "with-fullsend-gateway.sh").write_text("""#!/bin/bash
set -e
mkdir -p "$STRAT_STATE_DIR/strat-creator/.fullsend/shared"
cat > "$STRAT_STATE_DIR/strat-creator/.fullsend/shared/release-locks.sh" <<'EOF'
#!/bin/sh
touch "$CI_PROJECT_DIR/released"
exit "$TEST_RELEASE_CODE"
EOF
shift
"$@"
""")
    credential = tmp_path / "file-variable.json"
    credential.write_text('{"type":"synthetic"}')
    env = {
        **os.environ,
        "PATH": str(tools) + ":" + os.environ["PATH"],
        "CI_PROJECT_DIR": str(tmp_path),
        "CI_JOB_ID": "123",
        "RFE_KEY": "RHAIRFE-19",
        "FULLSEND_MODE": "single",
        "STRAT_DRY_RUN": "1",
        "FULLSEND_PUBLISH": "0",
        "JIRA_SERVER": "https://jira.example.invalid",
        "JIRA_USER": "test@example.invalid",
        "JIRA_TOKEN": "synthetic-jira-secret",
        "AIPCC_CICD_GCP_PROJECT_ID": "synthetic-project",
        "TEST_HEAD_SHA": ("a" if head_matches else "b") * 40,
        "AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY": str(credential),
        "TEST_BINARY_ARCHIVE": str(archive),
        "TEST_RUN_CODE": str(run_code),
        "TEST_RELEASE_CODE": str(release_code),
    }
    for name in ("CONFIG_FILE", "RFE_FILTER", "STRAT_KEY"):
        env.pop(name, None)
    result = subprocess.run(
        ["bash", str(scripts / "run-fullsend.sh"), "strat-single"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == expected, result.stderr
    assert (tmp_path / "released").exists() == head_matches
    assert "synthetic-jira-secret" not in result.stdout
    status = json.loads((tmp_path / "fullsend-output/exit-status.json").read_text())
    assert status == {"run_exit": run_code, "release_exit": release_code if head_matches else 1}
