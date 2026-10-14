"""Tests for the script-led Fullsend single-RFE flow."""
import fnmatch
import os
import subprocess
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO / ".fullsend/scripts/strategy-entrypoint.sh"
CI_DIR = REPO / ".fullsend/scripts/ci"


def _run_flow(tmp_path, *, locked=True, fail_stage="", batch=False, candidates="RHAIRFE-1234 RHAIRFE-1235", acquired="", size="", offset="", stale=False, context_rc="0"):
    root = tmp_path / "repo"
    ci = root / ".fullsend/scripts/ci"
    (root / "scripts").mkdir(parents=True)
    ci.mkdir(parents=True)
    (root / "scripts/fetch-architecture-context.sh").write_text(
        '#!/bin/sh\necho "context fetch stdout"\necho "context fetch stderr" >&2\nexit "${MOCK_CONTEXT_RC:-0}"\n')
    log = tmp_path / "events.log"

    (ci / "clone-data-repo.sh").write_text(
        "#!/bin/sh\nmkdir -p \"$2/.git/info\"\n"
    )
    (ci / "clone-data-repo.sh").chmod(0o755)
    (ci / "ca-bundle.sh").write_text((CI_DIR / "ca-bundle.sh").read_text())
    (ci / "prepare-claude-settings.py").write_text(
        (CI_DIR / "prepare-claude-settings.py").read_text())
    if stale:
        old = root / "artifacts/strat-tasks"
        old.mkdir(parents=True)
        (old / "RHAISTRAT-OLD.md").write_text("stale")
        (root / "artifacts/.git/info").mkdir(parents=True)
    (ci / "run-claude.sh").write_text(
        "#!/bin/sh\n"
        "printf 'claude %s\\n' \"$1\" >> \"$EVENT_LOG\"\n"
        "case \"$1\" in /strategy-create*)\n"
        "  mkdir -p \"$STRAT_CREATOR_ROOT/artifacts/strat-tasks\"\n"
        "  n=55; for key in $FULLSEND_OWNED_RFE_KEYS; do\n"
        "    printf '%s\\n' strategy > \"$STRAT_CREATOR_ROOT/artifacts/strat-tasks/RHAISTRAT-$n.md\"; n=$((n+1)); done ;;\n"
        "esac\n"
        "if [ \"${FAIL_STAGE:-}\" = \"$1\" ]; then exit 7; fi\n"
    )
    (ci / "pipeline-post.sh").write_text(
        "#!/bin/sh\n"
        "printf 'post %s\\n' \"$1\" >> \"$EVENT_LOG\"\n"
        "mkdir -p \"$STRAT_CREATOR_ROOT/artifacts/RHAISTRAT/20261005\"\n"
        "printf '%s\\n' published > \"$STRAT_CREATOR_ROOT/artifacts/RHAISTRAT/20261005/result.txt\"\n"
        "ln -sfn 20261005 \"$STRAT_CREATOR_ROOT/artifacts/RHAISTRAT/current\"\n"
    )
    for helper in (ci / "run-claude.sh", ci / "pipeline-post.sh"):
        helper.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        f"  */prepare-claude-settings.py) exec {sys.executable} \"$@\" ;;\n"
        "  */otel-collector.py) exec sleep 600 ;;\n"
        "  -c) exit 0 ;;\n"
        "  */lock_issues.py)\n"
        "    if [ \"$2\" = lock ]; then\n"
        "      printf 'lock %s\\n' \"$*\" >> \"$EVENT_LOG\"\n"
        "      keys=\"${ACQUIRED:-${MOCK_CANDIDATES}}\"\n"
        "      if [ \"${LOCKED:-1}\" = 1 ]; then\n"
        "        for arg in \"$@\"; do case \"$arg\" in */locked-rfe-ids.txt) printf '%s\\n' $keys > \"$arg\" ;; esac; done\n"
        "        printf '%s\\n' \"$keys\"\n"
        "      fi\n"
        "    else printf 'unlock %s\\n' \"$*\" >> \"$EVENT_LOG\"; fi\n"
        "    exit 0 ;;\n"
        "  */list-rfe-ids.py) printf 'discover %s\\n' \"$*\" >> \"$EVENT_LOG\"; printf '%s\\n' $MOCK_CANDIDATES; exit 0 ;;\n"
        "  */push_refined_strategies.py) printf 'push\\n' >> \"$EVENT_LOG\"; exit 0 ;;\n"
        "  */otel-summary.py) exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    python.chmod(0o755)
    git = bin_dir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = clone ]; then dest=\"${@: -1}\"; mkdir -p \"$dest/.git/info\"; fi\n"
        "exit 0\n"
    )
    git.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STRAT_CREATOR_ROOT": str(root),
        "FULLSEND_OUTPUT_DIR": str(tmp_path / "output"),
        "RESULTS_REPO_URL": "https://gitlab.local/group/results.git",
        "RESULTS_PUSH_TOKEN": "test-token",
        "RESULTS_GIT_USER": "oauth2",
        "CI_JOB_ID": "unit-test",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "EVENT_LOG": str(log),
        "MOCK_CONTEXT_RC": context_rc,
        "LOCKED": "1" if locked else "0",
        "FAIL_STAGE": fail_stage,
        "MOCK_CANDIDATES": candidates if batch else "RHAIRFE-1234", "ACQUIRED": acquired,
        "BATCH_SIZE": size, "BATCH_OFFSET": offset,
    })
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "batch-discover" if batch else "RHAIRFE-1234"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    events = log.read_text().splitlines() if log.exists() else []
    return result, events, tmp_path / "output"


def test_single_rfe_order_and_unlock_on_success(tmp_path):
    result, events, output = _run_flow(tmp_path)

    assert result.returncode == 0, result.stderr
    assert [event.split()[0] for event in events] == [
        "lock", "claude", "claude", "push", "claude", "post", "unlock",
    ]
    assert events[1] == "claude /strategy-create RHAIRFE-1234"
    assert events[2] == "claude /strategy-refine RHAISTRAT-55"
    assert events[4] == "claude /strategy-review RHAISTRAT-55"
    assert (output / "strategy-run/result.txt").read_text().strip() == "published"


def test_blocked_rfe_is_no_work_without_skill_or_publish(tmp_path):
    result, events, _ = _run_flow(tmp_path, locked=False)

    assert result.returncode == 0, result.stderr
    assert "No work:" in result.stdout
    assert [event.split()[0] for event in events] == ["lock"]


def test_refine_failure_unlocks_without_review_or_publish(tmp_path):
    result, events, _ = _run_flow(tmp_path, fail_stage="/strategy-refine RHAISTRAT-55")

    assert result.returncode == 7
    assert [event.split()[0] for event in events] == [
        "lock", "claude", "claude", "unlock",
    ]


def test_child_wrapper_uses_fullsend_and_preserves_completion_guard():
    wrapper = (CI_DIR / "run-claude.sh").read_text()

    assert 'fullsend-claude "$prompt"' in wrapper
    assert "this entrypoint owns the strat-creator-processing lock" in wrapper
    assert '== "$owned_keys"' in wrapper
    assert "\nclaude \"$1\"" not in wrapper
    assert 'stream_rc" -eq 42' in wrapper
    assert 'claude_rc" -eq 143' in wrapper
    assert 'claude_rc" -eq 141' in wrapper


def test_all_strategy_children_invoke_exact_model_and_force_subagent_pin(tmp_path):
    """Exercise the real FIFO wrapper/helper/Claude argv chain for every phase."""
    harness = yaml.safe_load((REPO / ".fullsend/harness/strategy.yaml").read_text())
    assert harness["model"] == "claude-opus-4-6"
    root = tmp_path / "repo"
    ci = root / ".fullsend/scripts/ci"
    ci.mkdir(parents=True)
    for name in ("run-claude.sh", "ca-bundle.sh", "stream-claude.py", "strategy-model.sh"):
        (ci / name).write_text((CI_DIR / name).read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "invocations.jsonl"
    # Same argv layout as runtime.ClaudeEntrypointHelper. The live Vertex
    # smoke independently exercises the actual generated Fullsend helper.
    (bin_dir / "fullsend-claude").write_text(
        "#!/bin/sh\nexec claude '--print' '--verbose' '--output-format' 'stream-json' "
        "'--dangerously-skip-permissions' '--model' "
        f"'{harness['model']}' '--effort' '{harness['effort']}' "
        "'--settings' '/sandbox/hooks.json' \"$@\"\n"
    )
    (bin_dir / "claude").write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "keys=('ANTHROPIC_DEFAULT_OPUS_MODEL','CLAUDE_CODE_SUBAGENT_MODEL',"
        "'CLAUDE_CODE_SUBAGENT_MODEL_FORCE','ANTHROPIC_MODEL')\n"
        "with open(os.environ['MODEL_CAPTURE'],'a') as f: "
        "f.write(json.dumps({'argv':sys.argv[1:],'env':{k:os.getenv(k) for k in keys}})+'\\n')\n"
        "print('stderr: '+sys.argv[-1], file=sys.stderr)\n"
        "print(json.dumps({'type':'system','subtype':'init','model':'claude-opus-4-6'}))\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'smoke'}))\n"
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "STRAT_CREATOR_ROOT": str(root),
                "MODEL_CAPTURE": str(capture), "ANTHROPIC_MODEL": "claude-opus-4-8",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "0", "FULLSEND_CA_BUNDLE_READY": "1"})
    (root / "artifacts").mkdir()
    (root / "artifacts/claude-stderr.log").write_text("historical stderr\n")
    for phase in ("create", "refine", "review"):
        prompt = f"/strategy-{phase} TEST-1"
        result = subprocess.run(["bash", str(ci / "run-claude.sh"), prompt],
                                cwd=root, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stderr.count("stderr: ") == 1
        assert f"stderr: {prompt}" in result.stderr
        assert "historical stderr" not in result.stderr
    logs = list((root / "artifacts/claude-stderr").glob("*.log"))
    assert len(logs) == 3
    assert {path.read_text().strip() for path in logs} == {
        f"stderr: /strategy-{phase} TEST-1" for phase in ("create", "refine", "review")}
    invocations = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(invocations) == 3
    for invocation, phase in zip(invocations, ("create", "refine", "review")):
        argv = invocation["argv"]
        assert argv[argv.index("--model") + 1] == "claude-opus-4-6"
        assert argv[argv.index("--effort") + 1] == "high"
        assert "--settings" in argv
        assert argv[-1] == f"/strategy-{phase} TEST-1"
        assert invocation["env"] == {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-6",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-4-6",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "ANTHROPIC_MODEL": None,
        }


def test_reference_image_and_secure_gateway_configuration():
    harness = (REPO / ".fullsend/harness/strategy.yaml").read_text()
    launcher = (CI_DIR / "with-openshell.sh").read_text()
    claude_wrapper = (CI_DIR / "run-claude.sh").read_text()

    assert "ghcr.io/fullsend-ai/fullsend-sandbox@sha256:259605fea321353552fdefd3a6a55e8b5c260998dfc5a622ed143e41a429995a" in harness
    assert 'SUPERVISOR_TAG="quay.io/opendatahub/odh-openshell-supervisor:v$VERSION"' in launcher
    assert "VERSION=0.0.112-rhaiv.0" in launcher
    assert 'openshell gateway add "https://127.0.0.1:17670" --local' in launcher
    assert "providers_v2_enabled" in launcher
    assert "--server-san host.containers.internal" in launcher
    assert "--enable-mtls-auth true" in launcher
    assert "--tls-client-ca" in launcher
    assert "--from-gcloud-adc" in launcher
    assert '"$STATE/bin/openshell"' in launcher
    assert '"$OPENSHELL_REAL_BIN" "$@" --from-gcloud-adc' in launcher
    assert 'ROOT="${STRAT_CREATOR_ROOT:-${CI_PROJECT_DIR:-$PWD}}"' in launcher
    assert 'grpc_endpoint = "https://host.containers.internal:17670"' in launcher
    assert "guest_tls_cert" in launcher
    assert "application_default_credentials.json" in launcher
    assert (REPO / ".fullsend/profiles/fullsend-vertex-ai.yaml").is_file()
    assert "profiles/fullsend-vertex-ai.yaml" in harness
    assert "src: ${GOOGLE_APPLICATION_CREDENTIALS}" in harness
    assert "dest: /tmp/.gcp-credentials.json" in harness
    assert "GOOGLE_APPLICATION_CREDENTIALS: /tmp/.gcp-credentials.json" in harness
    assert "src: /etc/gitlab-runner/certs/ca.crt" in harness
    assert "dest: /tmp/gitlab-ca.crt" in harness
    ca_helper = (CI_DIR / "ca-bundle.sh").read_text()
    assert 'base_bundle="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"' in ca_helper
    assert 'cat "$base_bundle" "$local_ca" >"$destination"' in ca_helper
    assert 'source "$CI_SCRIPTS/ca-bundle.sh"' in claude_wrapper
    assert 'fullsend_prepare_ca_bundle "$TMP_DIR/ca-bundle.pem"' in claude_wrapper
    assert "disable_tls" not in launcher
    assert "allow_unauthenticated_users" not in launcher
    assert "openshell-community/sandboxes/base:latest" not in launcher
    assert "podman --url \"$PODMAN_URL\" build" not in launcher
    assert not (REPO / ".fullsend/images/ci.Containerfile").exists()
    assert not (REPO / ".fullsend/images/sandbox.Containerfile").exists()


def test_fullsend_build_artifact_is_pinned_and_verified():
    builder = (CI_DIR / "build-fullsend.sh").read_text()
    launcher = (CI_DIR / "with-openshell.sh").read_text()

    assert "FEATURE_SHA=bdedebeaf96cbfabf298472397321a946af621c9" in builder
    assert "git -C \"$SOURCE_DIR\" rev-parse HEAD" in builder
    assert "fullsend-source-sha" in builder
    assert "fullsend.sha256" in builder
    assert "fullsend-version.txt" in builder
    assert "fullsend-source-sha" in launcher


def test_shell_helpers_parse():
    for script in [
        ENTRYPOINT,
        CI_DIR / "build-fullsend.sh",
        CI_DIR / "run-claude.sh",
        CI_DIR / "with-openshell.sh",
        CI_DIR / "pipeline-post.sh",
    ]:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_service_permissions_are_in_applied_policy():
    harness = yaml.safe_load((REPO / ".fullsend/harness/strategy.yaml").read_text())
    policy = yaml.safe_load((REPO / ".fullsend" / harness["policy"]).read_text())
    services = policy["network_policies"]["local_services"]
    endpoints = {entry["host"]: entry for entry in services["endpoints"]}
    assert set(endpoints) == {
        "jira.local", "gitlab.local", "orgpulse.local", "github.com",
        "api.github.com", "raw.githubusercontent.com", "codeload.github.com",
    }
    assert all(entry["port"] == 443 for entry in endpoints.values())
    local_hosts = {"jira.local", "gitlab.local", "orgpulse.local"}
    for host in local_hosts:
        assert endpoints[host]["tls"] == "skip"
        assert "protocol" not in endpoints[host]
    assert all(entry["enforcement"] == "enforce" and "tls" not in entry
               for host, entry in endpoints.items() if host not in local_hosts)
    assert endpoints["api.github.com"]["access"] == "read-only"
    # Architecture context clone needs POST git-upload-pack; the exact rule set
    # keeps push (git-receive-pack) and any other POST denied.
    git_transport = endpoints["github.com"]
    assert "access" not in git_transport
    assert sorted((rule["allow"]["method"], rule["allow"]["path"])
                  for rule in git_transport["rules"]) == [
        ("GET", "**"), ("HEAD", "**"), ("OPTIONS", "**"),
        ("POST", "/opendatahub-io/architecture-context/git-upload-pack"),
    ]
    fetch = (REPO / "scripts/fetch-architecture-context.sh").read_text()
    assert "https://github.com/opendatahub-io/architecture-context " in fetch
    binaries = [entry["path"] for entry in services["binaries"]]
    assert any(fnmatch.fnmatch(
        "/sandbox/.uv/python/cpython-3.14.3-linux-x86_64-gnu/bin/python3.14", pattern,
    ) for pattern in binaries)
    assert "profiles/strat-creator-services.yaml" not in harness["openshell"]["profiles"]


def test_batch_native_order_limits_and_excludes_prior_artifacts(tmp_path):
    result, events, _ = _run_flow(tmp_path, batch=True, size="2", offset="3", stale=True)
    assert result.returncode == 0, result.stderr
    assert "--batch-size 2 --batch-offset 3" in events[0]
    assert [e for e in events if e.startswith("claude")] == [
        "claude /strategy-create RHAIRFE-1234 RHAIRFE-1235",
        "claude /strategy-refine RHAISTRAT-55", "claude /strategy-refine RHAISTRAT-56",
        "claude /strategy-review RHAISTRAT-55", "claude /strategy-review RHAISTRAT-56"]
    assert events[-2] == "post pipeline-settings"
    assert events[-1].endswith("RHAIRFE-1234 RHAIRFE-1235")
    assert not any("OLD" in e for e in events)


def test_batch_partial_locks_process_and_unlock_only_owned_subset(tmp_path):
    result, events, _ = _run_flow(tmp_path, batch=True, acquired="RHAIRFE-1235")
    assert result.returncode == 0, result.stderr
    assert "claude /strategy-create RHAIRFE-1235" in events
    assert events[-1].endswith("unlock RHAIRFE-1235")


def test_batch_empty_discovery_calls_no_models_or_locks(tmp_path):
    result, events, _ = _run_flow(tmp_path, batch=True, candidates="")
    assert result.returncode == 0, result.stderr
    assert len(events) == 1 and events[0].startswith("discover")


def test_batch_all_blocked_is_no_work(tmp_path):
    result, events, _ = _run_flow(tmp_path, batch=True, locked=False)
    assert result.returncode == 0, result.stderr
    assert [e.split()[0] for e in events] == ["discover", "lock"]


def test_batch_failure_keeps_nonzero_and_unlocks_all_owned_keys(tmp_path):
    result, events, output = _run_flow(tmp_path, batch=True, fail_stage="/strategy-refine RHAISTRAT-56")
    assert result.returncode == 7
    assert events[-1].endswith("unlock RHAIRFE-1234 RHAIRFE-1235")
    assert (output/"owned-rfe-ids.txt").read_text().split() == ["RHAIRFE-1234", "RHAIRFE-1235"]
    assert (output/"partial-work/strat-tasks/RHAISTRAT-56.md").exists()
    assert not any(e.startswith("push") or e.startswith("post") or "/strategy-review" in e for e in events)


def test_batch_harness_keeps_model_and_deadline_contract():
    single=yaml.safe_load((REPO/".fullsend/harness/strategy.yaml").read_text())
    batch=yaml.safe_load((REPO/".fullsend/harness/batch-discover.yaml").read_text())
    for key in ("model", "effort", "image", "policy", "host_files", "providers", "openshell"):
        assert batch[key] == single[key]
    assert batch["entrypoint"]["command"][-1] == "batch-discover"
    assert batch["timeout_minutes"] == 295
    assert batch["sandbox_timeout_seconds"] == single["sandbox_timeout_seconds"] == 300


def test_prepare_ci_settings_preserves_other_permissions_and_hooks(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prepare_ci_settings", CI_DIR / "prepare-claude-settings.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.prepare(tmp_path)  # Missing project settings are valid.
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    settings = {"permissions": {"allow": ["Edit(artifacts/**)"],
                 "additionalDirectories": ["/tmp/strat-assess", "/other"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash"}]}}
    path.write_text(json.dumps(settings))
    helper.prepare(tmp_path)
    settings["permissions"]["additionalDirectories"] = ["/other"]
    assert json.loads(path.read_text()) == settings
    before = path.read_bytes()
    helper.prepare(tmp_path)
    assert path.read_bytes() == before
    settings["permissions"]["additionalDirectories"] = ["/tmp/strat-assess"]
    path.write_text(json.dumps(settings))
    helper.prepare(tmp_path)
    del settings["permissions"]["additionalDirectories"]
    assert json.loads(path.read_text()) == settings


def test_prepare_ci_settings_rejects_malformed_json(tmp_path):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text("invalid json")
    result = subprocess.run([sys.executable, str(CI_DIR / "prepare-claude-settings.py"),
                             str(tmp_path)], capture_output=True)
    assert result.returncode != 0
    assert path.read_text() == "invalid json"


def test_entrypoint_banner_is_first_output_before_argument_validation():
    result = subprocess.run(["bash", str(ENTRYPOINT)], text=True, capture_output=True)
    assert result.returncode == 2
    assert result.stdout.splitlines() == [
        "#" * 67,
        "# STRAT CREATOR ENTRYPOINT SCRIPT",
        "#" * 67,
    ]
    assert "usage:" in result.stderr


def test_workspace_trust_preserves_config_and_is_repeatable(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("prepare_trust", CI_DIR / "prepare-claude-settings.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    root = tmp_path / "repo"
    config = tmp_path / "config"
    config.mkdir()
    path = config / ".claude.json"
    settings = {"other": {"keep": True}, "projects": {
        str(root): {"custom": "preserved", "hasTrustDialogAccepted": False},
        "/other/project": {"hasTrustDialogAccepted": False}}}
    path.write_text(json.dumps(settings))
    helper.prepare_trust(root, config)
    settings["projects"][str(root)]["hasTrustDialogAccepted"] = True
    assert json.loads(path.read_text()) == settings
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    helper.prepare_trust(root, config)
    assert path.read_bytes() == before
    assert list(config.iterdir()) == [path]
    fresh = tmp_path / "fresh"
    helper.prepare_trust(root, fresh)
    assert json.loads((fresh / ".claude.json").read_text()) == {
        "projects": {str(root): {"hasTrustDialogAccepted": True}}}


def test_workspace_trust_rejects_malformed_config_without_replacing_it(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    path = config / ".claude.json"
    path.write_text("invalid json")
    result = subprocess.run([sys.executable, str(CI_DIR / "prepare-claude-settings.py"),
                             str(tmp_path / "repo")], capture_output=True,
                            env=dict(os.environ, CLAUDE_CONFIG_DIR=str(config)))
    assert result.returncode != 0
    assert path.read_text() == "invalid json"
    assert list(config.iterdir()) == [path]


def test_initial_context_fetch_logs_stdout_stderr_and_status(tmp_path):
    result, events, output = _run_flow(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "before starting Claude" in result.stdout
    log = (output / "architecture-context-fetch.log").read_text()
    assert "context fetch stdout" in log and "context fetch stderr" in log
    assert "Architecture context fetch exit status: 0" in log
    assert (output / "architecture-context-fetch.exit-code").read_text().strip() == "0"
    assert any(event.startswith("claude ") for event in events)


def test_initial_context_failure_is_visible_and_preserves_optional_behavior(tmp_path):
    result, events, output = _run_flow(tmp_path, context_rc="22")
    assert result.returncode == 0, result.stderr
    assert "architecture context setup failed" in result.stdout
    assert "Architecture context fetch exit status: 22" in result.stdout
    assert (output / "architecture-context-fetch.exit-code").read_text().strip() == "22"
    assert any(event.startswith("claude ") for event in events)


def test_native_context_fetch_reports_http_error_before_json_parsing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text('#!/bin/sh\necho "curl: (22) HTTP 403" >&2\nexit 22\n')
    curl.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    env.pop("RFE_SKIP_BOOTSTRAP", None)
    result = subprocess.run(["bash", str(REPO / "scripts/fetch-architecture-context.sh")],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 22
    assert "HTTP 403" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / ".context/architecture-context/LATEST_VERSION").exists()
