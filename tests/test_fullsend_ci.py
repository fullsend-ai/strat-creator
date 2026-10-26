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


def branch_registration(source, consumer, env):
    branch = "feat/test-source"
    git(source, "checkout", "-qb", branch)
    config_path = consumer / ".fullsend/config.yaml"
    content = yaml.safe_load(config_path.read_text())
    for entry in content["agents"]:
        name = entry["name"]
        entry["source"] = f"https://raw.githubusercontent.com/example/strat-creator/{branch}/.fullsend/{name}/{name}.yaml"
    config_path.write_text(yaml.safe_dump(content))
    env["STRAT_CREATOR_BRANCH"] = branch
    return branch, config_path.read_bytes()


def test_branch_updates_only_affect_new_jobs(ci_consumer, tmp_path):
    upstream, consumer, env = ci_consumer
    branch, registered = branch_registration(upstream, consumer, env)
    first_source = tmp_path / "first-source"
    git(tmp_path, "clone", "-q", "--branch", branch, str(upstream), str(first_source))
    first_revision = git(first_source, "rev-parse", "HEAD")
    policy = upstream / ".fullsend/shared/policy.yaml"
    policy.write_text(policy.read_text() + "\n# next job's policy\n")
    git(upstream, "commit", "-qam", "advance branch")
    second_source = tmp_path / "second-source"
    git(tmp_path, "clone", "-q", "--branch", branch, str(upstream), str(second_source))
    second_revision = git(second_source, "rev-parse", "HEAD")
    assert first_revision != second_revision
    # Resolve registrations and clone targets only after the upstream branch
    # has moved. Both must still use each job's already fetched snapshot.
    jobs = (("first", first_source, first_revision), ("second", second_source, second_revision))
    for label, source, revision in jobs:
        job_env = {**env, "STRAT_CREATOR_REF": revision}
        runtime, target, receipt = (tmp_path / f"{label}-{name}" for name in ("runtime", "target", "source.json"))
        config.materialize_runtime(consumer, runtime, job_env, source)
        config.checkout_candidate(target, receipt, job_env, source)
        assert git(target, "rev-parse", "HEAD") == revision
        resolved = yaml.safe_load((runtime / "config.yaml").read_text())
        record = json.loads(receipt.read_text())
        assert record["branch"] == branch and record["commit"] == revision
        for entry in resolved["agents"]:
            name = entry["name"]
            digest = hashlib.sha256((source / f".fullsend/{name}/{name}.yaml").read_bytes()).hexdigest()
            assert entry["source"] == f"https://raw.githubusercontent.com/example/strat-creator/{revision}/.fullsend/{name}/{name}.yaml#sha256={digest}"
            assert record["harness_sha256"][name] == digest
        assert (runtime / "shared/policy.yaml").read_bytes() == (source / ".fullsend/shared/policy.yaml").read_bytes()
        assert config.verify_consumer(consumer, job_env, source)["commit"] == revision
    assert (consumer / ".fullsend/config.yaml").read_bytes() == registered
    assert sorted(p.name for p in (consumer / ".fullsend").iterdir()) == ["config.yaml"]


@pytest.mark.parametrize("branch", ["other-branch", "--bad", "bad ref", "bad..branch", "feature.lock"])
def test_branch_registration_mismatch_is_rejected(ci_consumer, tmp_path, branch):
    source, consumer, env = ci_consumer
    branch_registration(source, consumer, env)
    env["STRAT_CREATOR_BRANCH"] = branch
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


@pytest.mark.parametrize("kind", ["file", "directory", "empty-directory"])
def test_recovery_scrubber_rejects_secret_paths_without_echoing_them(tmp_path, kind):
    secret = "synthetic-path-secret-123"
    path = tmp_path / secret
    if kind == "file":
        path.write_text("Harmless body.")
    else:
        path.mkdir()
        if kind == "directory":
            (path / "report.md").write_text("Harmless body.")
    with pytest.raises(ValueError, match="credential") as error:
        redactor.scrub_tree(tmp_path, [secret])
    assert secret not in str(error.value)


def test_binary_scrub_error_does_not_echo_filename_or_traceback(tmp_path):
    filename = "private-name.bin"
    (tmp_path / filename).write_bytes(b"\xff\x00")
    process = subprocess.run([sys.executable, str(ROOT / ".fullsend/ci/fullsend-redact.py"), "tree", str(tmp_path)],
                             capture_output=True, text=True, timeout=15)
    assert process.returncode != 0 and "binary artifact" in process.stderr
    assert filename not in process.stderr and "Traceback" not in process.stderr


@pytest.mark.parametrize(
    "run_code,release_code,head_matches,recovery_case,expected",
    [(23, 0, True, "none", 23), (0, 17, True, "none", 17), (0, 0, True, "none", 0),
     (0, 0, False, "none", 1), (23, 0, True, "file", 23),
     (23, 0, True, "directory", 23), (0, 0, True, "file", 1)],
)
@pytest.mark.parametrize("mode,agent,selectors", [
    ("single", "strat-single", {"RFE_KEY": "RHAIRFE-19"}),
    ("batch", "strat-batch", {}),
    ("config", "strat-batch", {"CONFIG_FILE": "config/synthetic.yaml", "RFE_FILTER": "--baseline"}),
    ("reprocess", "strat-resume", {"STRAT_KEY": "RHAISTRAT-19"}),
])
def test_actual_ci_wrapper_releases_claims_and_preserves_failure(
    tmp_path, run_code, release_code, head_matches, recovery_case, expected, mode, agent, selectors
):
    """Run the real outer shell with fake external services and no credentials."""
    scripts = tmp_path / ".fullsend/ci"
    scripts.mkdir(parents=True)
    for name in ("run-fullsend.sh", "fullsend-redact.py"):
        shutil.copyfile(ROOT / ".fullsend/ci" / name, scripts / name)
    shutil.copyfile(ROOT / ".fullsend/shared/capture_failure.py", scripts / "capture_failure.py")
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
{
  printf '%s\\n' "$FULLSEND_MODE" "$CONFIG_FILE" "$RFE_FILTER"
  printf '%s\\n' "$STRAT_REPROCESS_KEY" "$FULLSEND_WORK_ITEM_KEY"
} > "$CI_PROJECT_DIR/operation.txt"
mkdir -p "$STRAT_STATE_DIR/strat-creator/.fullsend/shared"
printf 'RHAIRFE-19\\n' > "$STRAT_STATE_DIR/owned-rfe-ids.txt"
printf '{"owner":"synthetic-run"}\\n' > "$STRAT_STATE_DIR/owned-rfe-ids.txt.owner.json"
cat > "$STRAT_STATE_DIR/strat-creator/.fullsend/shared/release-locks.sh" <<'EOF'
#!/bin/sh
touch "$CI_PROJECT_DIR/released"
exit "$TEST_RELEASE_CODE"
EOF
if [[ "$TEST_RECOVERY_CASE" != none ]]; then
  mkdir -p "$TARGET_REPO_DIR/tmp"
  if [[ "$TEST_RECOVERY_CASE" == directory ]]; then
    mkdir -p "$TARGET_REPO_DIR/tmp/$JIRA_TOKEN"
    printf 'Harmless body.\\n' > "$TARGET_REPO_DIR/tmp/$JIRA_TOKEN/report.md"
  else
    printf 'Harmless body.\\n' > "$TARGET_REPO_DIR/tmp/$JIRA_TOKEN.txt"
  fi
  python3 "$CI_PROJECT_DIR/.fullsend/ci/capture_failure.py" "$TARGET_REPO_DIR" "$STRAT_STATE_DIR" missing.json
fi
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
        "FULLSEND_MODE": mode,
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
        "TEST_RECOVERY_CASE": recovery_case,
    }
    for name in ("CONFIG_FILE", "RFE_FILTER", "STRAT_KEY", "RFE_KEY"):
        env.pop(name, None)
    env.update(selectors)
    result = subprocess.run(
        ["bash", str(scripts / "run-fullsend.sh"), agent], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == expected, result.stderr
    assert (tmp_path / "operation.txt").read_text().splitlines() == [
        mode, selectors.get("CONFIG_FILE", ""), selectors.get("RFE_FILTER", ""),
        selectors.get("STRAT_KEY", ""), selectors.get("STRAT_KEY", ""),
    ]
    assert (tmp_path / "released").exists() == head_matches
    assert "synthetic-jira-secret" not in result.stdout
    assert "synthetic-jira-secret" not in result.stderr
    if recovery_case != "none":
        assert "credential" in result.stderr
        assert not (tmp_path / "fullsend-output").exists()
        assert (tmp_path / ".fullsend-state/123/strat-state/recovery").is_dir()
        return  # Original private recovery is kept; unsafe CI export is removed.
    status = json.loads((tmp_path / "fullsend-output/exit-status.json").read_text())
    assert status == {"run_exit": run_code, "release_exit": release_code if head_matches else 1}
    exported = tmp_path / "fullsend-output/host-state"
    assert (exported / "owned-rfe-ids.txt").read_text() == "RHAIRFE-19\n"
    assert json.loads((exported / "owned-rfe-ids.txt.owner.json").read_text()) == {"owner": "synthetic-run"}
