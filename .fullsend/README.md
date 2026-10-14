# Fullsend strategy agents

Two native Fullsend agents run the strat-creator pipeline from a sandbox:

| Agent | Input | Does |
| --- | --- | --- |
| `strat-single` | `RFE_KEY` | Create, refine, push and review the strategy for one RHAIRFE |
| `strat-batch` | `BATCH_SIZE`, `BATCH_OFFSET`, optional `BATCH_EXPECTED_KEYS` | Discover RFEs with the pipeline JQL, then create all, refine all, push once, review all |

Each harness names a prompt with `agent:`; Fullsend launches Claude with its
normal hooks. The prompts drive the phase order through the existing skills
(`strategy-create`, `strategy-refine`, `strategy-review`) and helpers
(`push_refined_strategies.py`, `state.py`, `frontmatter.py`). Target runtime:
the released Fullsend **0.43.0**, unpatched. See
[SOURCE-PROVENANCE.md](SOURCE-PROVENANCE.md) for references and carried code.

## How a run works

"Host" means the process running `fullsend run`, which is the CI job
container. The sandbox is a separate container started by the job's own
OpenShell gateway.

1. **Pre-script (host)** — `strat-*/pre-strat-*.sh` → `shared/pre-strategy.sh`:
   removes working output left by earlier runs; drops the unused
   `/tmp/strat-assess` directory from project settings; fetches architecture
   context into `.context/` (failure ends the run before any lock); then
   `shared/prepare_run.py` chooses candidates (`RFE_KEY`, or
   `list-rfe-ids.py --jql-default`), locks them **once** with
   `lock_issues.py`, and writes the run record. Nothing locked → exit 78:
   Fullsend skips the sandbox and the model.
2. **Agent (sandbox)** — reads `tmp/strat-input.json` (the locked keys),
   runs the phases, keeps `strat-progress.yaml` with `state.py`, and writes
   `agent-result.json`. It never locks, unlocks or sees JQL.
3. **Validator (host)** — `shared/validate-output.sh` →
   `shared/validate_result.py` checks every claim against the host run
   record, the agent's progress, the downloaded artifacts and Jira. The
   downloaded repository is read as data only. A `failed` result never
   validates.
4. **Post-script (host, only after validation passes)** —
   `shared/post-strategy.sh` copies regular artifact files out of the
   downloaded repository (`shared/handoff.py`) and publishes them
   (`shared/publish-results.sh`: report, per-run JSON, results repository,
   summaries, Org Pulse).
5. **Lock release (host, always)** — the CI job runs
   `shared/release-locks.sh "$STRAT_STATE_DIR"` after `fullsend run`, whatever
   its exit status. Fullsend 0.43.0 skips the post-script when validation
   fails, so release cannot live there.

Up to two agent iterations (`max_iterations: 2`, `feedback_mode: append`),
as in rfe-creator. A second iteration runs in the same sandbox. Fullsend
clears `$FULLSEND_OUTPUT_DIR` first, but the repository is kept, so
progress lives in `tmp/strat-progress.yaml` (`shared/progress.sh`,
mirrored to the output directory). The retry prompt carries the
validator's findings. The agent resumes from the first phase without a
`phase_` entry and skips strategies that already have `refine_` or
`review_` entries. The skills' label gates also refuse a second review of a
STRAT that already has a verdict label. This exists because a headless agent
can end its turn after a skill's closing report mid-batch (local pipeline
2083).

## Invocation

The CI job installs the released binary, starts its Podman service and
OpenShell gateway (as rfe-autofixer's `autofix-rfe-stage-dry-fullsend` job
does), clones this repository at a pinned commit, and then:

```bash
export STRAT_RUN_ID="$CI_JOB_ID"
export STRAT_STATE_DIR="$CI_PROJECT_DIR/strat-state"   # host only; not mounted
export RFE_KEY=RHAIRFE-1234                            # strat-single
# export BATCH_SIZE=2 BATCH_OFFSET=0 BATCH_EXPECTED_KEYS="RHAIRFE-1 RHAIRFE-2"   # strat-batch

set +e
./fullsend run strat-single \
  --fullsend-dir "$CHECKOUT/.fullsend" \
  --target-repo "$CHECKOUT" \
  --env-file .env \
  --output-dir "$CI_PROJECT_DIR/fullsend-output"
rc=$?
bash "$CHECKOUT/.fullsend/shared/release-locks.sh" "$STRAT_STATE_DIR" "$CHECKOUT" || rc=1
set -e
exit $rc
```

Runner environment. Fullsend 0.43.0 refuses to run when a variable the
harness references is unset on the host, so define every optional one, even
as an empty string:

| Variable | Required | Use |
| --- | --- | --- |
| `STRAT_RUN_ID` | yes | Run identifier; must match `[A-Za-z0-9._-]+` |
| `STRAT_STATE_DIR` | yes | Host run record: `run.json`, `owned-rfe-ids.txt`, `preflight-result.json` |
| `JIRA_SERVER`, `JIRA_USER`, `JIRA_TOKEN` | yes | Skills, helpers, validator readback |
| `GOOGLE_APPLICATION_CREDENTIALS`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, `GOOGLE_CLOUD_PROJECT` | yes | Vertex (`shared/gcp-vertex.env`) |
| `RFE_KEY` / `BATCH_*` | per agent | Input selection |
| `STRAT_LOCAL_CA_FILE` | local stack | Private CA for `jira.local`; appended to the sandbox trust bundle |
| `RESULTS_REPO_URL`, `RESULTS_PUSH_TOKEN`, `RESULTS_GIT_USER` | to publish | Results repository; without a URL the run is assembled locally only |
| `ORG_PULSE_URL`, `ORG_PULSE_API_TOKEN` | optional | Org Pulse push (non-blocking) |
| `STRAT_PUBLISH_DIR` | optional | Publication workspace (default: the Fullsend run directory) |
| `STRAT_ARCHITECTURE_CONTEXT_PATH` | optional | Local architecture-context copy instead of GitHub |

Outcomes:

| `fullsend run` | Meaning | Look at |
| --- | --- | --- |
| exit 0, "skipped" | Nothing discovered or nothing lockable; no model ran | `$STRAT_STATE_DIR/preflight-result.json` |
| exit 0 | Validated and published | `fullsend-output/*/iteration-1/output/agent-result.json`, publish directory |
| non-zero | Pre-script, agent, validation or publication failed | Job trace, `iteration-1/validation-feedback.txt`, `strat-progress.yaml`, `$STRAT_STATE_DIR/run.json` |

A `revise` or `reject` review is a successful run that needs human
follow-up, not a failure. The recommendation comes from the review's numeric
scores (`scripts/assess-strat/parse_results.py`); the four prose reviewers'
verdicts are informational, so `approve` with dissenting prose reviewers is
valid. The validator checks the recommendation against the scores.

## Sandbox policy

`shared/policy.yaml` allows only `jira.local:443`, as an opaque TLS tunnel
(the Python client verifies the private CA and hostname). Unlike
rfe-creator's read-only Jira profile, strategy creation clones, links,
labels and pushes, so Jira is read-write. Architecture context is fetched on
the host and publication runs on the host, so the sandbox has no GitHub,
GitLab or Org Pulse egress. Vertex comes from the pinned upstream profile
and provider.

## Recovery

- **Agent, validation or post-script failure.** `release-locks.sh` releases
  the recorded locks and renames the record `owned-rfe-ids.txt.released`.
  Jira may hold partial work (a cloned STRAT, refined content, labels). Do
  not replay it by hand: rerun. Discovery finds the RFE again (a STRAT
  without a verdict label is not "processed"), and `strategy-create` imports
  the existing Cloners-linked STRAT instead of cloning again. The validator
  rejects a second open clone.
- **Cleanup step failed or the job pod was lost.** Locks may still be held.
  If `$STRAT_STATE_DIR/owned-rfe-ids.txt` survives, run
  `release-locks.sh <state-dir>`. Otherwise reconcile in Jira: find RFEs
  labelled `strat-creator-processing` with no running strategy job, and
  release them with `python3 scripts/lock_issues.py unlock <keys>`.
- **"already records locks".** `prepare_run.py` refuses a state directory
  whose `owned-rfe-ids.txt` still lists keys, because `lock_issues.py` would
  overwrite the record. Release those keys first, or use a fresh directory.
- **Missing architecture context.** The pre-script fails before locking.
  Reviews never run without context, and the validator rejects a result
  that claims otherwise.
- **Blocked by `strat-creator-needs-attention` or
  `strat-creator-human-sign-off`.** Expected human gates; the RFE is skipped
  with that reason. Clear them in Jira when the human work is done.

## Tests

`tests/test_fullsend_agents.py` covers the host boundaries against the Jira
emulator: discovery, locking, the pre-script, validation, handoff and lock
release. The prompts themselves are exercised only in CI with a model.
