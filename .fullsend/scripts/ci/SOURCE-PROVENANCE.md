# CI helper provenance

Source repository: `git@gitlab.com:redhat/rhel-ai/agentic-ci/strat-pipeline`.
Source branch: `main`. Source commit:
`fd36b15c5095c9f20a270b1d69933c578c04d9da`. Source and destination
repositories use Apache License 2.0; the repository's existing `LICENSE`
continues to cover these helpers.

| Destination | Source path | Treatment |
| --- | --- | --- |
| `stream-claude.py` | `ci-scripts/stream-claude.py` | Copied; preserves stream rendering and exit 42 completion-marker protocol. |
| `otel-collector.py` | `ci-scripts/otel-collector.py` | Copied; retains loopback collection. |
| `otel-summary.py` | `ci-scripts/otel-summary.py` | Copied; summarizes the retained Claude OTEL log. |
| `push-results.py` | `ci-scripts/push-results.py` | Copied/adapted: explicit URL plus inherited Git auth header; no token in remote URL. |
| `push-to-org-pulse.py` | `ci-scripts/push-to-org-pulse.py` | Copied/adapted: TLS certificate and hostname verification remain enabled. |
| `pipeline-post.sh` | `ci-scripts/pipeline-post.sh` | Copied/adapted: checked-out repo/artifacts paths and explicit token variables. |
| `clone-data-repo.sh` | `ci-scripts/clone-data-repo.sh` | Copied/adapted: full URL, process-scoped authentication, and failure on stale destination. |
| `run-claude.sh` | `ci-scripts/run-claude.sh` | Rewritten around `fullsend-claude`; keeps the stream parser and validates early-termination statuses. |
| `strategy-entrypoint.sh` | `.gitlab-ci.yml` `single-rfe` job | New sequential entrypoint; preserves lock/create/refine/push/review/post/unlock order. |
| `with-openshell.sh` | Breadboard M2 smoke at `var/demos/fullsend-entrypoint-poc/openshell-smoke/openshell-smoke.sh`; rfe-autofixer `.gitlab-ci.yml` job `autofix-rfe-stage-dry-fullsend` at `28bccc89fd06c118a8194d3f7464dcc4d2e39341` | Adapted to OpenShell `0.0.112-rhaiv.0`, the reference supervisor, and pinned Fullsend sandbox; uses generated mTLS certificates, readiness checks, per-job storage/network, and scoped cleanup. |
| `profiles/fullsend-vertex-ai.yaml` | `jctanner/fullsend` feature commit `8f628aec6d113181914e2a2307fce17488e2c4b4`, embedded scaffold profile; extended for OpenShell 0.0.112 provider-v2 refresh metadata | Declares the Vertex ADC refresh credential so OpenShell can keep refresh material gateway-side and inject only short-lived access tokens into the sandbox. The harness lists this reserved profile so it takes precedence over Fullsend's older embedded copy; a scoped CLI shim adds `--from-gcloud-adc` to Fullsend's provider-create call. |
| `harness/strategy.yaml` `host_files` | rfe-autofixer's imported rfe-creator harness at `fae245e778384a0fc823c1df6a71bd2b38489882` | Copies the GitLab file variable named by `GOOGLE_APPLICATION_CREDENTIALS` to `/tmp/.gcp-credentials.json` in the sandbox. Claude Code Vertex auth requires Google ADC; this follows the reference path. The M4.1 smoke credential is `authorized_user` ADC with a cloud-platform refresh token; M6 needs a dedicated Vertex-only service account instead. |
| `harness/strategy.yaml` CA `host_files` and `ca-bundle.sh` | Existing GitLab runner mount `/etc/gitlab-runner/certs/ca.crt` | Copies the runner CA into the sandbox and appends it to the existing OpenShell trust bundle for Claude, Python, curl, and Git clients. Keeping OpenShell's CA in the combined bundle is required for Vertex traffic through the gateway. |
| `policies/strategy.yaml` service network rules | Former `profiles/strat-creator-services.yaml`, corrected by Breadboard M4.2 sandbox probes | Moved permission-only rules into the applied base policy; match versioned Python executables. Jira uses an opaque TLS tunnel so the existing Python client verifies the emulator’s private CA and hostname directly. Host/port/executable controls remain active; Jira HTTP inspection is absent. Public GitHub retains read-only HTTP enforcement. |
| `build-fullsend.sh` | `jctanner/fullsend` commit `8f628aec6d113181914e2a2307fce17488e2c4b4` | Separate Go build step; verifies the source SHA and emits the binary, source revision, version, and checksum as a CI artifact. |

The source checkout was not modified. `setup-claude-ci.sh` was not copied:
its root-only UBI9 package/user setup and service-account key file are replaced
by rfe-autofixer's CI/sandbox images and Fullsend's Vertex provider. The
reference CI image lacks `jsonschema`, but the selected strategy helpers do not
import it; its existing PyYAML satisfies the scripts' YAML dependency.
Dashboard triggering remains a CI responsibility for M6.
