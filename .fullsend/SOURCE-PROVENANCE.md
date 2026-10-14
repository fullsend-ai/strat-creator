# Source provenance

Native Fullsend agents for strat-creator, targeting the released Fullsend
**0.43.0** binary without patches. No custom entrypoint and no nested
`fullsend-claude` phase launcher: each harness names a prompt with `agent:`,
and Fullsend launches Claude with its normal hooks.

## References followed

| Reference | Revision | Used for |
| --- | --- | --- |
| `opendatahub-io/rfe-creator` `.fullsend/` | `15b924c` (local checkout); rfe-autofixer imports the harness at `fae245e778384a0fc823c1df6a71bd2b38489882`, which differs only in `prompt.md` wording and a README | Harness layout (`agent:` prompt, `pre_script`, `post_script`, `validation_loop` with schema), sandbox image digest, pinned `fullsend-ai/agents` policy/profile/provider URLs, `gcp-vertex.env`, `RFE_SKIP_BOOTSTRAP` host bootstrap, prompt structure and `fullsend-check-output` step |
| `redhat/rhel-ai/agentic-ci/rfe-autofixer` `.gitlab-ci.yml` job `autofix-rfe-stage-dry-fullsend` | `28bccc89fd06c118a8194d3f7464dcc4d2e39341` | Released Fullsend 0.43.0, `openshell:0.3.46` CI image, `odh-openshell-supervisor:v0.0.112-rhaiv.0`, CI-owned gateway. Wired in A3. |
| Fullsend source | tag `v0.43.0` | `post_script` runs only after validation passes (`internal/cli/run.go`); iteration output is extracted after every agent run; pre-script skip protocol (`internal/prescript`, exit 78); validation env (`TARGET_REPO_DIR` is the downloaded repo) |

`fullsend lock strat-single` and `fullsend lock strat-batch` with the
released 0.43.0 binary (sha256 `7728f2f3…dd14`) resolve both harnesses and
their two pinned remote dependencies.

Discovery and locking follow strat-pipeline's `.gitlab-ci.yml` at
`fd36b15c5095c9f20a270b1d69933c578c04d9da` (`strat-batch` and `single-rfe`
jobs, `.rfe-unlock-cleanup`): `list-rfe-ids.py --jql-default`, one
`lock_issues.py lock --locked-keys-file` call before Claude, and unlock from
that record in cleanup. `shared/prepare_run.py` runs that sequence in the
host pre-script; `shared/release-locks.sh` is the cleanup. Agents receive the
locked keys in `tmp/strat-input.json` and never lock or see JQL.

## Carried from the script-led POC

Source: `github.com/jctanner-opendatahub-io/strat-creator` branch
`feat/fullsend-strategy-entrypoint` at
`78ab3f1e97b09d02229bd03dd045a7eed97b0c33`. Its own upstream provenance is
`strat-pipeline` `main` at `fd36b15c5095c9f20a270b1d69933c578c04d9da`.

| Destination | Source | Treatment |
| --- | --- | --- |
| `shared/publish/push-results.py` | `.fullsend/scripts/ci/push-results.py` | Copied unchanged (explicit URL, process-scoped Git auth header, no token in remote URL). |
| `shared/publish/push-to-org-pulse.py` | `.fullsend/scripts/ci/push-to-org-pulse.py` | Copied unchanged (TLS verification kept). |
| `shared/publish/clone-data-repo.sh` | `.fullsend/scripts/ci/clone-data-repo.sh` | Copied unchanged. |
| `shared/publish-results.sh` | `.fullsend/scripts/ci/pipeline-post.sh` | Adapted: runs in the host post-script against a validated handoff directory instead of the sandbox's `artifacts/`; local-only assembly when `RESULTS_REPO_URL` is unset. |
| `shared/prepare-claude-settings.py` | `.fullsend/scripts/ci/prepare-claude-settings.py` | Settings edit only (drops unused `/tmp/strat-assess`); the workspace-trust step was for `fullsend-claude` and is not carried. |
| `shared/local-ca.env` | `.fullsend/scripts/ci/ca-bundle.sh` | Same bundle logic, as a sandbox `.env.d` file so it applies before Claude starts. |
| `shared/policy.yaml` | `.fullsend/policies/strategy.yaml` (Breadboard ADR-0010/0011) | Jira only, opaque TLS tunnel; GitHub, GitLab and Org Pulse rules dropped because context fetch and publication now run on the host. |
| `scripts/fetch-architecture-context.sh` | commit `43a6457` | Same change: fail on listing errors instead of continuing with an empty version. |

Not carried: `strategy-entrypoint.sh` and the script-led orchestration,
`run-claude.sh`, `stream-claude.py`, `with-openshell.sh`, OTEL collector and
summary, `build-fullsend.sh`, the custom `fullsend-vertex-ai.yaml` profile
(replaced by the pinned upstream profile and provider), and the feature
binary's redaction. Redaction now relies on Fullsend 0.43.0's built-in
security hooks (`secret_redactor` and post-tool redaction default on); A3
verifies them in the job trace. Model pins follow the reference harness
(`model`, `CLAUDE_CODE_SUBAGENT_MODEL`) plus `ANTHROPIC_DEFAULT_OPUS_MODEL`
from `strategy-model.sh`.
