# strat-creator on fullsend

Three [fullsend](https://github.com/fullsend-ai/fullsend) agents run the
strategy pipeline (create, refine, push, review) in a sandbox. A Claude Code
Workflow script holds the sequence. The skills and Python helpers in this
repository do the work.

| Agent | Starts | Work |
|---|---|---|
| `strat-single` | `fullsend run strat-single`, with `RFE_KEY` | one approved RHAIRFE, 90 min |
| `strat-batch` | `fullsend run strat-batch` | the RHAIRFEs `scripts/list-rfe-ids.py --jql-default` finds, 295 min |
| `strat-resume` | a `/fs-strat resume` comment on a RHAISTRAT issue (CEL trigger), or `fullsend run strat-resume` with `FULLSEND_WORK_ITEM_KEY` | one STRAT, from the step the host computes, 90 min |

Requirements: the harnesses pin the Fullsend **0.47.1** sandbox by its
multi-architecture image digest. Its published Containerfile and OCI build history
pin Claude Code **2.1.296**, the runtime qualified for these hooks. Preflight and
the launch guard refuse other versions. Upgrade only after repeating the native
hook controls; newer versions are not assumed compatible. See the pinned [Containerfile](https://github.com/fullsend-ai/fullsend/blob/v0.47.1/images/sandbox/Containerfile)
and the runtime's [hook failure policy](https://code.claude.com/docs/en/hooks).

The benchmark jobs and native loopback controls used Claude Code 2.1.296 outside
Fullsend/OpenShell. They do not establish that the complete deployment works in
that sandbox. Image metadata verification and a runtime version check close the
known version mismatch, but a deployment smoke test remains necessary.

## Agent registration

This repository provides harnesses, prompts, the plugin and shared helpers.
`.fullsend/config.yaml` is installer-managed and is deliberately absent and
ignored here. Register the three sources shown in `fleet-entry.example.yaml`
through the deployment's external fleet configuration. The example is an agent
selection fragment, not a complete fleet manifest. Run the normal installer to
generate repository configuration, then lock the dependencies. Tests create an
isolated generated configuration to exercise dispatch.

## How a run works

```mermaid
sequenceDiagram
    autonumber
    participant CI as CI job (host)
    participant Pre as pre-strategy.sh (host)
    participant SB as sandbox: claude --agent strat-*
    participant WF as Workflow strat-pipeline
    participant Val as validate-output.sh (host)
    participant Post as post-strategy.sh (host)
    CI->>Pre: fullsend run strat-*
    Pre->>Pre: clone strat-creator at STRAT_CREATOR_REF, vendor into target
    Pre->>Pre: fetch architecture context (fail before any lock)
    Pre->>Pre: resume points, human gates, expected create skips, lock once, run.json
    Pre-->>CI: exit 78 when nothing to do (no sandbox, no model)
    CI->>SB: prompt "Run the agent task"
    SB->>WF: Workflow {name: "strat-pipeline:strat-pipeline"}
    WF->>WF: read, create, refine x N, push, score x N, review x N, result
    WF-->>SB: agent-result.json written
    SB-->>Val: output + downloaded repository
    Val->>Val: schema, run record, create skips vs host gates, progress, artifacts, scores, Jira
    Val->>Val: on failure, retain artifacts and progress; end this invocation
    Val->>Post: passed
    Post->>Post: handoff of listed files, report, optional publication
    CI->>CI: release-locks.sh from the host clone, whatever the exit status
```

| Piece | Where it runs | What it trusts |
|---|---|---|
| `shared/pre-strategy.sh`, `prepare_run.py`, `vendor.py` | host, before the sandbox | a strat-creator clone at a verified sha |
| `plugins/strat-pipeline/workflows/strat-pipeline.js` | sandbox, as a fullsend-scanned plugin | the order of the steps |
| skills, `strat-scorer`, `scripts/strat_pipeline_state.py` | sandbox | Jira, with `JIRA_TOKEN` |
| `shared/validate-output.sh`, `validate_result.py` | host, after each attempt | reads the downloaded repository as data |
| `shared/post-strategy.sh`, `handoff.py`, `publish/` | host, after validation | copies only the regular files the validated result lists |
| `shared/release-locks.sh` | CI, after `fullsend run` | the host lock record |

The agent never locks or unlocks RFEs and never sees JQL. The sandbox can reach
Jira (opaque TLS tunnel, `shared/policy.yaml`) and the model provider, and
nothing else. It cannot reach GitHub or the results services.

The sandbox does hold `JIRA_TOKEN`, and this is a known exposure. Strategy
creation has to write to Jira (clone, link, label, push, comment), and the
repository's Python Jira client signs those requests itself, so the token
cannot stay on the runner the way rfe-creator's read-only Jira provider keeps
it. The policy limits that token to `redhat.atlassian.net:443`, called from
`python3`. The `strat-resume` trigger accepts only a human with `write`,
`maintain` or `admin` role, because the run it starts writes to Jira. The
role of a Jira commenter comes from fullsend's Jira adapter. The trigger was
evaluated with `fullsend dispatch` on the event fixtures in
`tests/fixtures/fullsend-events/`, not on a live Jira event.

## The Workflow

The final step validates expected RFE coverage, saved progress, required artifacts
and complete numeric review provenance before writing `agent-result.json` and
`tmp/strat-completion.json`. Its schema-bearing result must match that receipt;
the loaded plugin checks the match against current files. A failed/null final
result fails the Workflow. The trusted host validator independently checks the
same receipt and still performs Jira readback. Numeric scores remain the sole
approval gate; a valid revise outcome is completed work.

`python3 scripts/strat_pipeline_state.py completion-status` checks saved work
without running skills. On a reporting retry, `result` restores the same output
and receipt when input/progress/artifact hashes still match. The primary agents
use this recovery path for completed work. A failed business receipt is terminal
within this prepared run; it never authorizes an automatic Workflow relaunch.
Completion receipts remain in the downloaded repository; they are also
preserved inline in the validated result's completion identity.

When native state/journal exports are available, a read-only observer distinguishes
business completion, native Workflow completion and the CLI/reporting exit:

```bash
python3 scripts/strat_pipeline_outcome.py inspect --repo <repository> \
  --state <workflows/wf_ID.json> \
  --journal <subagents/workflows/wf_ID/journal.jsonl> --cli-exit <code> \
  --result <agent-result.json>
```

It requires this receipt in both the completed native state and a matched
journaled agent result. Completed work plus a nonzero reporting exit is recorded
as `completed_with_reporting_error`, with `retry_work: false`. A killed/failed/
missing runtime or null result remains unverified even if the CLI exits zero.
Fullsend's existing entrypoint is unchanged. Its host validation checks business
completion; native verification additionally needs exported native evidence and
is exercised by the focused diagnostic job. No native export contract is assumed
for Fullsend, and this check does not grade content accuracy or provide general
side-effect idempotency or stale-journal invalidation.

`plugins/strat-pipeline/workflows/strat-pipeline.js` has named steps:

1. **read**: one agent returns `tmp/strat-input.json` and `tmp/strat-progress.json` as JSON.
2. **create**: `/strategy-create` over the locked keys. It returns the RFE to STRAT map.
3. **refine**: `parallel()` runs one `/strategy-refine` per strategy, then a fresh persisted-state check determines readiness.
4. **push**: one agent runs `scripts/push_refined_strategies.py`.
5. **prepare reviews**: one shell-capable agent runs the mechanical recovery helper. It finishes fully captured compositions, verifies and publishes completed reviews, then records them without rescoring.
6. **score**: one restricted `strat-scorer` per remaining strategy, using repository-local `tmp/strat-assess/<KEY>`. It needs no Bash tool.
7. **review**: `/strategy-review <KEY> --scores-from tmp/strat-assess/<KEY>`.
8. **result**: `scripts/strat_pipeline_state.py result` writes `agent-result.json`, then `fullsend-check-output` checks it.

### Reviewer ticket scope

The four forked reviewer Skills require exactly one strategy key. They select
`strat-reviewer`, whose tools are Read, Grep and Glob. The loaded strat-pipeline
plugin registers command hooks that restrict those tools to the assigned
strategy, its frontmatter-linked source RFE (the frozen strat-originals file,
with rfe-tasks as a CI compatibility fallback), its prior review and shared
architecture context. Root and batch-directory searches are blocked. A local
file for another ticket does not switch this review into local mode.

The completion hook requires one assessment heading for the assigned key and
rejects other strategy or unrelated RFE keys. An explicit `REVIEW_SCOPE_ERROR:`
may return to the caller, which must stop before composition or publication.
The receiving agent and scorer keep their existing tools and status schema.

The plugin bundles `scripts/review_scope.py`; its copy matches the project
helper in a repository test. The interactive strat-creator plugin bundles the
same hooks. Keep the plugin loaded and hooks enabled: standalone Skill files
alone supply instructions, without this enforced tool boundary.

This adapter targets Claude Code 2.1.296. It binds scope using each agent's
initial assignment transcript and the hook's agent ID, not a shared current-key
file. It waits up to two seconds for the asynchronous transcript flush and
blocks access if the assignment remains unavailable. Per-agent decisions are
recorded in `tmp/review-scope/`. It depends on this transcript layout; qualify
Claude upgrades against the scope control. These are application scope checks,
not a replacement for the OpenShell sandbox.

### Refinement receipts

Each refinement agent can return only its assigned strategy key or `none`.
The completion helper refuses to record a strategy unless it belongs to this
run and its saved file has valid identity/source metadata, Refined or Reviewed
status, and a nonempty Strategy section. These are structural checks, not a
content quality assessment.

After refinement, one agent runs `strat_pipeline_state.py refine-status`.
The StructuredOutput hook compares its returned JSON with the actual saved
progress and files. A missing or malformed receipt can recover when that
evidence verifies completion; a plausible receipt alone cannot advance a
strategy. The initial read also verifies saved refinement readiness. Completed
work is not repeated just to repair its receipt.

### Review composition

Numeric scores remain the sole approval gate. The four specialist verdicts
remain separate, including any disagreement with that gate. Prepared reviews
mark those verdicts pending rather than borrowing the scorer's recommendation.

The review Skill opens one session per target. PostToolUse hooks bind its
begin receipt to the actual caller and capture the four delivered forked Skill
results with role, agent/tool identity and content hashes. The Python composer
copies all report text unchanged, lists dissent and labels the scorer output
separately. It exports the role reports and a provenance manifest beside the
review. Missing reports prevent composition; recording review and host
validation check the final file, sources, hashes and numeric decision. Handoff
includes those provenance/source files. This does not judge report quality;
the delivery-content gate and independent accuracy evaluation remain separate.

This adapter is tested on Claude Code 2.1.296 and depends on the actual Bash
and forked Skill PostToolUse payload fields. Requalify it when upgrading Claude.

A workflow has no shell of its own, so every command runs in a step agent.
Each step records its outcome in `tmp/strat-progress.json` through
`scripts/strat_pipeline_state.py`. For steps other than refinement, when
`agent()` returns `null` (the agent failed or was stopped), the script records
the step and key with `--failed`, and the result is `failed`. Refinement uses
the verified saved evidence described above before deciding whether to record
a failure.

Parallel steps take a file lock around each progress update, so no step's
entry is lost. A create that leaves some RFEs with neither a strategy nor a
skip stays incomplete. A later verified run can select those RFEs again. A push covers the refined strategies that
`push_refined_strategies.py` pushes: status `Refined`, with a matching
`jira_key`. A strategy
refined later, by a retried create, gets a push of its own before its review,
and a refined strategy no push covers is never reviewed.

The agent cannot skip work by claiming it was skipped. Before the run,
`prepare_run.py` evaluates `strategy-create`'s gates for every locked RFE:
excluded status, release scope, quality label, and STRAT state, from
`config/pipeline-settings.yaml`. It records the outcome as `expected_skips` in
the host run record. If the host cannot evaluate the gates, nothing is locked
and the run fails. A claimed skip the host did not expect stays pending, so
a later verified run processes `strategy-create` for that RFE again. The validator rejects a claimed skip the host did not
expect, and a strategy created for an RFE the host expected to be skipped.
`strategy-create`'s "reconstruction failed" skip cannot be predicted from Jira
fields, so it always fails validation, with a line telling a human to restore
the RFE original or fix the STRAT description.

Workflow agents have no Agent tool, on every Claude Code version tested. So the
scorer that `strategy-review` would normally launch runs as a workflow agent of
its own. `--scores-from` tells the skill to use that agent's result instead of
launching another.

Concurrency is the Workflow default: min(16, CPUs - 2) agents at a time.

## Publication and restart recovery

Jira mutations use a separate durable intent ledger in `tmp/strat-effects/`.
Before sending a write, the helper fsyncs intent; before repeating it, the helper
reads Jira. Identical comments and byte-identical named attachments are reused,
clone creation carries a searchable marker in the same request as the ticket,
and existing links, fields and labels are reconciled. Reads may retry transient
HTTP errors; append writes never receive blind HTTP retries. If a pending write
is absent or differs, preserve the ledger and reconcile it instead of reposting.
Search may lag a committed create, so absence alone cannot authorize recreation.

`python3 scripts/publish_review.py KEY --check-only` verifies an existing
current-run composed review. Without the flag it finishes publication using a
stable summary, the original full review and the numeric verdict labels. A
later verified recovery can reuse this review without rerunning scoring or specialists.
The numeric approval policy and dissent in the attached report are unchanged.
MCP is for reads on this path; write helpers must not be bypassed after errors.

The host records lock intent before acquisition, adds a run owner label, and
checks that owner at each pipeline write. Cleanup needs the matching owner;
foreign, mixed or orphaned claims are never cleared automatically. A repeated
pre-script preserves the same prepared run and recovery data. Each new run
requires a fresh `STRAT_STATE_DIR`; an existing owner sidecar may be reused
only by its original prepared run, never a different run ID. Jira label
operations are not compare-and-swap and do not prove distributed exclusivity;
concurrent-job qualification needs an atomic lease/fencing backend.

The post-script seals the validated handoff and generated reports once. Results
use `RHAISTRAT/run-<run_id>` with an immutable hash manifest. Death during copy,
after local commit or after accepted push reuses that identity. Different content
under the same run ID fails. A clean local tree still pushes an unpushed commit;
reconciling an existing remote run does not move `current` over a newer run.
Org Pulse remains a keyed bulk upsert; its deployment behavior is separate
qualification, not an exactly-once event-delivery claim.

Validate these mechanics without inference using
`uv run pytest tests/test_side_effect_recovery.py`; the isolated Jira/Git
fixtures inject SIGKILL after committed writes and authored lost responses.
They do not exercise Fullsend/OpenShell or prove power-loss recovery. Preserve
the sandbox workspace/ledger and host state for restart; a lost workspace must
not be treated as a verified new attempt.

## Resume points and human gates

A human stop is a run boundary, not a pause inside a run. The pre-script gives
each RFE a resume point from durable state: the STRAT's Jira labels, plus the
previous run's progress record when `STRAT_PREVIOUS_PROGRESS` names one. The
first rule that matches wins:

| STRAT state | Previous progress | Result |
|---|---|---|
| no open STRAT | | `create` |
| `strat-creator-rubric-pass`, `-needs-attention` or `-human-sign-off` | | skipped, not locked (done, or waiting for a human) |
| no `strat-creator-auto-refined` | | `refine` |
| auto-refined, no verdict label | pushed, no review recorded | `review` |
| auto-refined, no verdict label | anything else (a human cleared needs-attention) | `refine` |

`create` always runs, because it is also how an existing STRAT gets into the
sandbox: `strategy-create` imports it (Path A) instead of cloning it. With the
`review` resume point, the run skips refine and push for that RFE.

A run that ends at a human gate is a success: the strategy is reported with
`needs_attention: true`, and the validator accepts it. To continue, a human
edits the STRAT, removes `strat-creator-needs-attention`, and comments
`/fs-strat resume` on the RHAISTRAT issue.

`max_iterations: 1` ends the invocation after failed business validation. The
host retains regular `artifacts/` and `tmp/` files plus a hash manifest under
`STRAT_STATE_DIR/recovery/failed-*/`, even when validation prevents the normal
post-script. Preserve that directory and the lock-owner record. A later
host-prepared run may use `STRAT_PREVIOUS_PROGRESS` after checking current inputs.
It launches a fresh Workflow, never a native journal replay. Jira mutations
reconcile durable intent; missing or changed evidence requires reconciliation.

Review-only Path A uses `scripts/import_strategy.py`, including full overflow
attachments. Proofs compare the publisher's semantic ADF content rather than
Markdown whitespace or HTML comments that publication removes. Links, marks,
heading levels and table spans still matter. Live published-strategy readback
runs in command helpers and host validation before reuse or writes.

Short completion/refinement hooks check local immutable evidence without Jira
network calls. Pre/PostToolUse hooks explicitly set `onFailure: block`, supported
by the qualified runtime, so a timeout cannot silently permit the tool call.
SubagentStop allows at most one correction; if parsing still fails during active
stop handling, it permits termination and report capture rejects the malformed
assessment. This does not claim that Stop hooks can enforce timeout blocking.

## Verify the harnesses locally

```bash
make lint
make test-unit
env -u STRAT_WORKFLOW_SMOKE make test
claude plugin validate .fullsend/plugins/strat-pipeline
claude plugin validate .
```

The default suite makes no model calls. It covers real local Jira imports and
mutations, full attachment import, readback normalization, lost-response and
publication recovery, CRLF preservation, scoped reviewer access, terminal failure,
and the JavaScript control flow under Node. All bundled script/hook/workflow
counterparts are checked for byte equality.

Native controls run the real 2.1.296 binary against an authored loopback API with
fresh Claude state and no inherited credentials. Sanitized observed payloads
under `tests/fixtures/native-hooks/` document the runtime-specific fields. These
controls verify orchestration and enforcement, not model reasoning or content
quality. Diagnostic run scripts also create a fresh `CLAUDE_CONFIG_DIR`; they
never copy the developer's global Claude history.

The optional full Workflow smoke uses stub skills and the real scorer,
composition and checkpoint helpers. Its stubs follow the enforced target and
Recommendation format. It calls the configured model and needs separate
paid-run authorization; it was not rerun for these review fixes:

```bash
STRAT_WORKFLOW_SMOKE=1 CLAUDE_BIN=<qualified-2.1.296-binary> \
  STRAT_SMOKE_MODEL=rits/zai-org/glm-5-3 make test-workflow-smoke
```

## Run it in CI

Not run in this change: the gateway available for development could not create
sandboxes. These are the steps a CI job takes. Every variable referenced in
`env.runner` must be set; an empty value is fine.

```bash
export STRAT_CREATOR_REF=<the strat-creator commit this harness is pinned to>
export STRAT_CREATOR_REPO=                       # empty: https://github.com/fullsend-ai/strat-creator.git
export STRAT_RUN_ID="${CI_JOB_ID}" STRAT_STATE_DIR="$(mktemp -d)"   # host only
export STRAT_DRY_RUN= STRAT_PREVIOUS_PROGRESS= STRAT_ARCHITECTURE_CONTEXT_PATH= STRAT_PUBLISH_DIR=
export RESULTS_REPO_URL= RESULTS_PUSH_TOKEN= RESULTS_GIT_USER= ORG_PULSE_URL= ORG_PULSE_API_TOKEN=
export RFE_KEY=RHAIRFE-1234                      # strat-single
export BATCH_SIZE=10 BATCH_OFFSET=0 BATCH_EXPECTED_KEYS=   # strat-batch
export FULLSEND_WORK_ITEM_KEY=RHAISTRAT-5678     # strat-resume (dispatch sets it)

fullsend run strat-single --fullsend-dir .fullsend --target-repo .; rc=$?
# Always, whatever $rc is: release the locks this run recorded.
release="${STRAT_STATE_DIR}/strat-creator/.fullsend/shared/release-locks.sh"
[ -f "${release}" ] && bash "${release}" "${STRAT_STATE_DIR}"
exit "${rc}"
```

`JIRA_SERVER`, `JIRA_USER`, `JIRA_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`,
`ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION` and `GOOGLE_CLOUD_PROJECT` come
from the CI secret store. `STRAT_DRY_RUN=1` writes nothing to Jira: candidates
are checked for blocking labels instead of being locked, and the skills run
with `--dry-run`. Resume points still come from Jira in a dry run. A dry-run
strategy is a local `STRAT-NNNN` with no Jira clone, so a dry run never
resumes at `review`.

If the CI job itself is lost, the locks stay. Find them with the JQL
`labels = strat-creator-processing`, and release them with
the original `release-locks.sh <STRAT_STATE_DIR>` with its owner record. If that
record is lost, reconcile the owner before manual cleanup; a bare `unlock`
refuses owned claims.

## Install in another repository

A harness added by URL (`fullsend agent add <url>`) resolves its relative paths
against the consumer's `.fullsend/`, not against this repository. Instead,
write a thin harness that names this one as `base:`. fullsend then fetches
every relative reference, scripts included, from the same pinned tree.

```yaml
# .fullsend/strat-single.yaml in the consumer repository
base: https://raw.githubusercontent.com/fullsend-ai/strat-creator/<sha>/.fullsend/strat-single/strat-single.yaml#sha256=<sha256>
```

Set `STRAT_CREATOR_REF=<sha>`, the same commit. The pre-script clones
strat-creator at that sha on the host, checks it, and vendors these paths into
the consumer checkout before the sandbox starts:

- `.claude/skills/` (the `scripts` symlinks are dereferenced, so only regular files are copied)
- `.claude/agents/strat-scorer.md`
- `.claude/agents/strat-reviewer.md` (explicit Read/Grep/Glob tools for forked reviewer reports)
- `scripts/`, `config/` and `workflows/`

`artifacts/` and the progress record (`artifacts/strat-progress.json`) end up
in the consumer repository. The vendor step refuses to overwrite a file the
consumer owns, and records what it wrote in `.strat-creator-vendor.json`.

How `base:` resolution works was checked with rfe-creator's published harness:
`fullsend lock` from a consumer resolved all 12 of its dependencies from the
base URL. The strat-creator URLs exist only once this change is merged.

The repository-root plugin (`.claude-plugin/plugin.json`) is for Claude Code
and marketplace users. It ships the skills, the strat-scorer agent and the
same workflow. Because the plugin's skills and agent are namespaced, the
workflow accepts `args.namespace` (`"strat-creator:"`). That interactive path
was validated with `claude plugin validate .` only; it has not been run.

## Platform dependency

fullsend cannot yet pin a definition repository as a whole. `plugins:` refuses
a tree URL at a repository root ("URL must point to a directory inside the
repo, not the repo root") and refuses trees that contain symlinks (the
tree-contents rule in fullsend's harness reference). The vendoring step in the
pre-script stands in for that pin, and the orchestration script ships as the
small scanned plugin in `plugins/strat-pipeline`. Once fullsend can pin a
definition repository the way it pins a fleet agent, the vendoring step
collapses into one `plugins:` entry.

## Provenance

The three publication helpers in `shared/publish/` (`push-results.py`,
`push-to-org-pulse.py`, `clone-data-repo.sh`) are carried from
github.com/jctanner-opendatahub-io/strat-creator at `78ab3f1e`
(`.fullsend/scripts/ci/`, Apache-2.0). They have no other public source. Subsequent changes add sealed run identity and recovery for interrupted
publication, including partial source cleanup. Everything else under
`.fullsend/` was introduced by this integration.
