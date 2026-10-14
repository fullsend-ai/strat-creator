# Fullsend strategy entrypoint POC

This branch preserves the `single-rfe` and discovery-batch paths from the strat-pipeline source
checkout at commit `fd36b15c5095c9f20a270b1d69933c578c04d9da`. The target repo
is the checked-out `feat/fullsend-strategy-entrypoint` branch; CI must not
clone public `main` over it. The sequence is issue lock, strategy create,
strategy refine, deterministic refined-strategy push to Jira, strategy review,
report/data publication, and lock release on every exit path. A blocked lock is
a no-work success. A missing or duplicate strategy artifact is a failure.

## Local invocation

The GitLab job uses rfe-autofixer's CI image directly. A separate Go build job
produces the Fullsend feature binary artifact; the strategy job downloads that
artifact, then runs the pinned job-owned gateway wrapper from the repository
root:

```bash
export CI_PROJECT_DIR="$PWD"
export CI_JOB_ID=local-001
export RFE_KEY=RHAIRFE-1234
export JIRA_SERVER=https://jira.local
export JIRA_USER='set-me'
export JIRA_TOKEN='set-me'
export RESULTS_REPO_URL='https://gitlab.local/group/project.git'
export RESULTS_PUSH_TOKEN='set-me'
export RESULTS_GIT_USER=oauth2
export ANTHROPIC_VERTEX_PROJECT_ID='set-me'
export ORG_PULSE_URL=
export ORG_PULSE_API_TOKEN=
# The `fullsend-build/` artifact comes from build-fullsend.sh in the Go job.
unset FULLSEND_MODEL FULLSEND_FALLBACK_MODELS
bash .fullsend/scripts/ci/with-openshell.sh -- fullsend run strategy \
  --fullsend-dir "$PWD/.fullsend" \
  --target-repo "$PWD" \
  --output-dir "$PWD/fullsend-run-output"
```

Build the binary artifact in a separate job using a Go image with Go 1.26.5 or
newer:

```yaml
build-fullsend-feature:
  image: golang:1.26.5-bookworm
  script:
    - bash .fullsend/scripts/ci/build-fullsend.sh
  artifacts:
    paths:
      - fullsend-build/
```

The strategy job uses
`quay.io/aipcc/agentic-ci/openshell:0.3.46` directly. It requires the M1/M2
Podman-capable job environment, the build artifact, and one unique `CI_JOB_ID`.
Each job starts its own Podman API and OpenShell gateway with mTLS, a unique
network, generated certificates, and job-local state. It saves gateway/Podman
logs, removes the job's containers/network, unregisters the gateway, and
deletes job state on exit. It does not use a host engine socket or shared
gateway. The Kubernetes executor must provide isolated, ephemeral job
storage and the permissions validated by M1/M2.

## Required GitLab variables

Configure these in local GitLab project settings, never in this repository:

| Variable | Purpose |
| --- | --- |
| `RFE_KEY` | One approved `RHAIRFE-NNNN` issue for the manual `single-rfe` job |
| `JIRA_SERVER` | Jira base URL; use the local emulator URL for the POC |
| `JIRA_USER` | Jira bot username |
| `JIRA_TOKEN` | Jira bot Basic password with required read/write strategy permissions |
| `RESULTS_REPO_URL` | HTTPS URL of the strat-pipeline-data project |
| `RESULTS_PUSH_TOKEN` | GitLab token allowed to push result artifacts and summaries |
| `RESULTS_GIT_USER` | Username paired with the results token (often `oauth2`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | GitLab file variable containing the GCP credential JSON; Fullsend copies it to `/tmp/.gcp-credentials.json` in the sandbox for Claude Code ADC |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project used by Fullsend's Vertex provider |
| Runner CA bundle | `/etc/gitlab-runner/certs/ca.crt`; copied into the sandbox and appended to OpenShell's trust bundle by the strategy entrypoint and Claude wrapper |
| `ORG_PULSE_URL` | Optional Org Pulse API URL; set blank to skip the non-blocking upload |
| `ORG_PULSE_API_TOKEN` | Optional Org Pulse token; set blank to skip the non-blocking upload |

The local Jira, GitLab, Org Pulse, and Vertex endpoints are runtime inputs. The
applied `.fullsend/policies/strategy.yaml` scopes sandbox traffic to the local
service names and the public GitHub hosts used by architecture-context retrieval.
Permission-only service rules belong in the base policy: importing a provider
profile without attaching that provider does not activate its permissions.
The executable patterns include versioned Python 3 interpreters, which the
reference sandbox launches through its virtual environment.

Jira uses `tls: skip` in OpenShell: this means an opaque CONNECT tunnel, not
skipping TLS verification in the client. The strategy Python client verifies
Jira’s certificate and hostname using the runner CA appended to the sandbox
trust bundle. OpenShell still enforces the endpoint, port, and executable
policy; it does not inspect Jira HTTP methods or inject credentials into this
tunnel. Jira credentials are passed to the existing client through harness
environment variables. Public GitHub endpoints retain enforced read-only
HTTP inspection. GitLab and Org Pulse use the same verified opaque local-service tunnels;
M6 verified real writes and publication.

The restricted child uses OpenShell’s HTTP CONNECT proxy. Destination DNS
resolution happens upstream of the child namespace; direct `getaddrinfo`
failure does not establish an HTTP connectivity failure. M6 must recheck
trusted HTTPS and identity after M5 resets Jira, then verify the other service
endpoints before running the strategy. The local Jira emulator currently uses
permissive authentication: `/myself` returns the supplied Basic identity but
does not validate its password. That response proves identity propagation,
not credential validity. Its `serverInfo` reports the synthetic Cloud URL
`https://jira-emulator.atlassian.net` rather than the transport base URL.

## Fullsend versions and hooks

The separate build job compiles Fullsend from
`jctanner/fullsend@8f628aec6d113181914e2a2307fce17488e2c4b4`, the M3 review-fix
commit on `feat/entrypoint-harness`, and records its source SHA and binary
checksum. The strategy harness uses
`ghcr.io/fullsend-ai/fullsend-sandbox@sha256:259605fea321353552fdefd3a6a55e8b5c260998dfc5a622ed143e41a429995a`.
The reference CI image reports OpenShell CLI and gateway `0.0.112-rhaiv.0`;
the gateway uses `quay.io/opendatahub/odh-openshell-supervisor:v0.0.112-rhaiv.0`.
The CI image already has Podman, curl, OpenSSL, Bash, Git, Python 3.12, and
PyYAML 6.0.3. The sandbox has Claude, Node, Git, Bash, Python 3.14, and PyYAML
6.0.3. Neither image has `fullsend-claude` preinstalled; Fullsend installs its
supported helper during a Claude run. The strategy scripts do not require
`jsonschema`, which is absent from the CI image. No custom image extension is
currently needed. Claude uses Fullsend's Vertex provider. Following the
rfe-autofixer harness, Fullsend `host_files` copies the GitLab file variable
at `GOOGLE_APPLICATION_CREDENTIALS` to `/tmp/.gcp-credentials.json` in the
sandbox and sets that path for Claude Code ADC. The M4.1 smoke used the
existing `authorized_user` ADC file, which contains a refresh token with
cloud-platform scope; copying it gives sandbox code the same cloud access as
that user. The user explicitly approved these existing Breadboard credentials for the
Vertex POC; the dedicated service-account bootstrap is a separate follow-up.
Keep credentials in masked variables/private files and preserve the refresh path.

Fullsend hooks remain enabled with the M3 default. This branch does not set
`security.sandbox_hooks.enabled: false`; wait for the separate hooks-switch
change before selecting an opt-out. The hook-integrity checks are checkpoint
checks and retain M3's documented bypass/restore limitations.

## Source differences

- `run-claude.sh` delegates child startup to `fullsend-claude`, retains the
  stream renderer and `FULL RUN COMPLETE` sentinel, and accepts success only
  when the parser returned 42 and the child ended from the expected SIGTERM or
  SIGPIPE. Other non-zero statuses fail the entrypoint. Fullsend owns the
  child runtime and cancellation boundary.
- The entrypoint supports single-rfe and batch-discover (native batch-jql).
  No-work discovery/locks exit without skills or publication. Created strategy
  count must match the acquired RFEs; prior working outputs are cleared while
  timestamped historical runs remain untouched.
- The workspace is the checked-out feature branch. There is no `/tmp` clone,
  root-only token directory, or `CI_PROJECT_DIR` dependency inside the sandbox.
- Results authentication uses a process-scoped Git HTTP header. Tokens are
  not embedded in `.git/config` remote URLs. Org Pulse keeps normal HTTPS
  certificate and hostname verification enabled; configure its CA trust in
  the job image.
- The UBI9 `setup-claude-ci.sh` user creation, package installation, and
  service-account key copy are replaced by the reference CI/sandbox images
  and Fullsend provider setup. Fullsend itself is a separate binary artifact.
  Dashboard triggering and GitLab artifact upload remain CI responsibilities
  for M6.

The M4.1 image and gateway compatibility checks are recorded in the Breadboard
ledger task. Re-run the gateway/data-plane checks in the privileged Kubernetes
job context before relying on this image set for a credential-bearing strategy
run.

Detailed file-by-file copy/adaptation provenance is in
`.fullsend/scripts/ci/SOURCE-PROVENANCE.md`.

## Exact strategy model pin

The POC harness uses `claude-opus-4-6` with effort `high`. Do not replace it
with the moving `opus` alias or pass a different Fullsend model override.
The CI caller clears FULLSEND_MODEL/FULLSEND_FALLBACK_MODELS and supplies
the exact model flag, matching the harness; no fallback model is configured.
All create/refine/review children use the generated Fullsend helper.
The strategy wrapper pins ANTHROPIC_DEFAULT_OPUS_MODEL and
CLAUDE_CODE_SUBAGENT_MODEL to the same ID, with
CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1, so skill-level opus requests and child
selection do not silently choose a newer version. These settings are scoped
to this POC, not unrelated Fullsend harness defaults.

The force setting is documented for Claude Code v2.1.257 and newer; older
versions before v2.1.251 give the subagent variable precedence directly. See
[Claude Code subagent model selection](https://code.claude.com/docs/en/sub-agents#run-every-subagent-on-one-model).
Runtime smoke evidence must report the actual parent model, not just the
requested argument. Historical M6 used 4.8 and is not proof of the new pin.

## Discovery batches

Run the `batch-discover` agent with BATCH_SIZE/BATCH_OFFSET (nonnegative integers;
blank size uses config/pipeline-settings.yaml). Discovery reuses list-rfe-ids.py
--jql-default, including processed-strategy exclusion before slicing. Optional
BATCH_EXPECTED_KEYS must exactly match discovered keys before locking; use it
to bound acceptance runs. Single-RFE invocation is unchanged.

Inside one sandbox: discover → lock subset → create all owned RFEs → refine
each new strategy → push refinements → review each → pipeline-settings reports
and results/Org Pulse publication. Only recorded owned locks are released on
exit. Empty discovery or no acquired locks succeeds without model calls.

CI batch timeout is five hours; Fullsend timeout is 295 minutes (five minutes reserved for cleanup) and sandbox
startup/readiness budget remains 300 seconds. These do not enable automatic
retries. Artifacts and transcripts diagnose partial failures. Before manually
retrying, inspect linked Jira strategies, per-issue gates and lock records;
never replay a partly completed batch blindly or reset processed issues.
The curated-list variant batch-selected and reprocess-strat are deferred.
