---
name: strategy-review
description: Adversarial review of a single refined strategy. Scores against rubric, then runs independent forked reviewers for detailed prose. Requires a strategy key argument.
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Skill, Agent
---

You are a strategy review orchestrator. Your job is to score and review a single strategy in `artifacts/strat-tasks/`, producing a review file with numeric scores and detailed prose.

## Dry Run Mode

If `--dry-run` is in `$ARGUMENTS`, skip ALL external writes:
- Do NOT write or update any Jira issues
- Do NOT post review comments to Jira — save them to `artifacts/strat-reviews/{id}-review-comment.md` instead
- DO still read from Jira and local artifacts (reads are safe)
- DO still create local review files in `artifacts/strat-reviews/`

## Local Mode

Check if strategy files exist in `local/strat-tasks/`. If they do, this is a local human review session pulled via `/strategy-pull`.

In local mode:
- Read strategy files from `local/strat-tasks/` instead of `artifacts/strat-tasks/`
- Read review files from `local/strat-reviews/` instead of `artifacts/strat-reviews/`
- Write review files to `local/strat-reviews/`
- **Skip ALL Jira writes** — no labels, no comments, no attachments posted to Jira
- **Skip the Pipeline Label Gate** (Step 1a) — the strategy was already processed by CI
- DO still run full scoring and prose reviews locally

Local mode is also active if any strategy file's frontmatter contains `workflow: local`.

If both `local/strat-tasks/` and `artifacts/strat-tasks/` have files, prefer `local/strat-tasks/`.

## Recovery Before Generation

For a pipeline invocation with `tmp/strat-input.json`, first run:

```bash
python3 scripts/publish_review.py RHAISTRAT-NNNN --check-only
```

Exit 0 means a current-run composed review and all its source hashes verify.
Run the same command without `--check-only` to finish/reconcile publication,
then return its numeric verdict. Skip scoring, specialist calls and the label
gate in this recovery path. Exit 3 means no review exists; continue below.
Any other failure stops recovery; do not overwrite invalid or changed evidence.
This preserves a review completed before a crash even if its verdict label is
already present. Local mode remains separate and does not publish to Jira.

## Step 1: Verify Artifacts Exist

This skill processes exactly **one strategy per invocation**. `$ARGUMENTS` must contain a strategy key (e.g., `RHAISTRAT-1531` or `STRAT-001`). If no key is provided, **stop with an error**: "No strategy key provided. Usage: /strategy-review RHAISTRAT-NNNN"

Read the strategy file in `artifacts/strat-tasks/`. If it doesn't exist or hasn't been refined yet (no "Strategy" section), tell the user to run `/strategy-refine` first and stop.

Check if a prior review exists in `artifacts/strat-reviews/`. If one exists for this strategy, read it — this is a re-review after revisions.

## Step 1a: Pipeline Label Gate

Read the strategy's frontmatter to get the `jira_key`. If `jira_key` is not null, fetch the STRAT's labels from Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAISTRAT-NNNN --fields labels --markdown
```

If the STRAT has either `strat-creator-rubric-pass` or `strat-creator-needs-attention` in its labels, **stop** — it has already been processed by the pipeline:
- Do NOT review it
- Print `[SKIP] RHAISTRAT-NNNN — already has <label>, skipping review`

## Step 2: Fetch Architecture Context

If `--architecture-context <path>` is in `$ARGUMENTS`, use the local path:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/fetch-architecture-context.sh <path>
```

Otherwise, fetch from remote:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/fetch-architecture-context.sh
```

## Step 3: Use the in-repository assess-strat implementation

The scorer agent, rubric, skills, scripts, and tests are versioned in this
repository. The scorer agent is registered from `.claude/agents/strat-scorer.md`
and the scoring scripts live under `scripts/assess-strat/`. No bootstrap or
runtime clone is required.

## Step 4: Score Strategy

### Scores from a caller (`--scores-from <dir>`)

If `--scores-from <dir>` is in `$ARGUMENTS`, the caller has already run the
strat-scorer for this strategy and wrote `<dir>/<KEY>.result.md`. A Workflow
script does this: its agents have no Agent tool, so it launches the scorer as a
workflow agent of its own and passes the result directory here.

In this mode:
- Do NOT launch a scorer agent and do NOT delete or recreate `<dir>`.
- Check that `<dir>/<KEY>.result.md` exists. If it does not, **stop with an
  error**: "No scorer result at <dir>/<KEY>.result.md".
- Use `<dir>` as the run directory everywhere Step 5 says
  `tmp/strat-assess/review`.

Then continue with Step 5. Without the flag, follow the rest of this step.

### Launch the scorer

Launch a strat-scorer agent to produce numeric scores for the strategy. The
in-repository assess-strat implementation provides the rubric and agent
definition.

Create a clean run directory (removes stale result files from prior reviews in the same CI job):

```bash
rm -rf tmp/strat-assess/review
mkdir -p tmp/strat-assess/review
```

Spawn one agent (model: opus, run_in_background: true, subagent_type: strat-scorer) with this prompt:

```
You are a strategy quality assessor. Your task:
1. Read `{PROMPT_PATH}` for the full scoring rubric.
2. Follow its instructions exactly, substituting {KEY} for the strategy key and {RUN_DIR} for the run directory. Read the strategy from {DATA_FILE} (not the path in the rubric's step 1).
3. If architecture context is available at `.context/architecture-context/`, use Glob and Grep to validate architecture claims against real component docs.
Strategy key: {KEY}
Data file: {DATA_FILE}
Run directory: {RUN_DIR}
```

Substitute all placeholders:
- `{PROMPT_PATH}` → absolute path of `scripts/assess-strat/agent_prompt.md`
- `{DATA_FILE}` → the strategy file path (e.g., `artifacts/strat-tasks/RHAISTRAT-1469.md`)
- `{KEY}` → the strategy key (e.g., `RHAISTRAT-1469`)
- `{RUN_DIR}` → `tmp/strat-assess/review`

Wait for the scorer agent to complete.

## Step 5: Parse Scores and Apply Verdicts (AUTOMATED — no LLM judgment)

After the scorer agent has completed, run the scoring scripts to deterministically compute the verdict and apply it to the review file. With `--scores-from <dir>`, replace `tmp/strat-assess/review` with `<dir>` in all three commands:

```bash
# Parse .result.md files → scores.csv with deterministic verdicts
python3 scripts/assess-strat/parse_results.py tmp/strat-assess/review/

# Apply scores and verdicts to review file frontmatter
python3 ${CLAUDE_SKILL_DIR}/scripts/apply_scores.py tmp/strat-assess/review/scores.csv \
    --review-dir artifacts/strat-reviews \
    --result-dir tmp/strat-assess/review

# Print summary statistics
python3 scripts/assess-strat/summarize_run.py tmp/strat-assess/review/
```

**Do NOT manually extract scores, compute verdicts, or set frontmatter.** The scripts handle this deterministically. The verdict rules are:
```
APPROVE:  total >= 6  AND  no zeros       → needs_attention=false
REVISE:   total >= 3  AND  ≤1 zero        → needs_attention=true
REJECT:   total < 3   OR   2+ zeros       → needs_attention=true
```

## Step 6: Run Prose Reviews

Before invoking any reviewer, begin a fresh, target-specific capture session:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/review_composition.py begin {KEY} \
    --scorer-result tmp/strat-assess/review/{KEY}.result.md
```

With --scores-from, substitute the supplied directory here too. This command
checks the score source and prepared review. Loaded plugin PostToolUse hooks bind
this session to the actual caller and save each delivered Skill result. Hook
capture is required; standalone Skill text without the plugin is insufficient.
Run begin in its own Bash call; do not launch two begin sessions in one call.
Do not substitute transcripts, earlier scorer feedback or invented reports.


Use the **Skill tool** to invoke all four reviewer skills in parallel. Each runs in its own isolated `context: fork` — no reviewer sees another's output. Pass the strategy key to each:

```
Skill(skill="strategy-feasibility-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-testability-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-scope-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-architecture-review", args="RHAISTRAT-NNNN")
```

Do NOT use the Agent tool for reviews. Use the Skill tool — the reviewer skills are defined in `.claude/skills/` and contain specific review instructions.

- **`strategy-feasibility-review`**: Can we build this with the proposed approach? Are effort estimates credible?
- **`strategy-testability-review`**: Are acceptance criteria testable? What edge cases are missing?
- **`strategy-scope-review`**: Is the strategy right-sized? Does the effort match the scope?
- **`strategy-architecture-review`** (if architecture context available): Are dependencies correctly identified? Are integration patterns correct?

Each reviewer resolves only the exact assigned strategy file and its
`source_rfe`. A local file for a different ticket does not change its mode.
The loaded strat-creator/strat-pipeline plugin blocks other ticket reads and
checks each completed assessment's target. If any return begins with
`REVIEW_SCOPE_ERROR:`, lacks the assigned heading, or includes another strategy
key, stop with an error before composition or Jira publication. Do not treat
that result as a review, rewrite it into a valid report, or rerun the reviewer.

## Step 7: Compose the captured reviews (AUTOMATED)

Run the deterministic composer once:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/review_composition.py compose {KEY}
python3 ${CLAUDE_SKILL_DIR}/scripts/review_composition.py verify {KEY}
```

It writes the final review from the four actual delivered Skill results, preserving
all report text unchanged with role, caller/tool and source-hash attribution. It
exports each role report, the separate scorer assessment and a provenance manifest
beside the final file. Do not rewrite, summarize, harmonize or replace these
sections. Scorer feedback is labeled separately and cannot stand in for a missing
specialist. Missing or invalid required reports stop composition and publication.

The **numeric scores remain the sole approval gate**. The composer derives both
recommendation and needs_attention from those scores. Reviewer recommendations
populate reviewers.* from their own reports, remain informational and do not
change the numeric gate. Disagreements with that gate are explicitly listed and
all opposing findings remain in the full reports. Do not change either gate field
or reviewer frontmatter yourself. An architecture skip is recorded as skipped,
never as an invented approval.

A passing numeric gate can coexist with specialist dissent. State both facts to
the human reader; do not describe the review as unanimous or concerns as resolved.

## Step 7: Publish the Verified Review

Use the deterministic publisher. It builds a stable numeric summary with
specialist dissent, saves `{id}-review-comment.md`, reconciles an existing
comment and matching attachment, and applies the numeric verdict label.
Do not post through MCP, curl, inline Python, or another write path.

```bash
python3 scripts/publish_review.py RHAISTRAT-NNNN
```

In dry-run mode use `--dry-run`; it saves the summary and skips external writes.
Missing credentials also saves locally. For local human review, skip this step.

A pending or conflicting write stops publication. Preserve `tmp/strat-effects/`
and the verified review. Do not regenerate reports, change the summary, delete
write intent or repeat an uncertain POST to make recovery appear successful.

## Step 8: Advise the User

Based on the result:
- **Approved** (`needs_attention=false`): Tell the user the numeric gate passed and the strategy is ready for sign-off. Also identify any specialist dissent or skipped architecture assessment, which remains for the human reader.
- **Needs revision** (`needs_attention=true`, verdict=REVISE): List specific issues by dimension. Tell the user to edit the strategy, remove `needs-attention`, and re-run `/strategy-review`.
- **Fundamental problems** (`needs_attention=true`, verdict=REJECT): Recommend revisiting the RFE or re-running `/strategy-refine` with different constraints.

$ARGUMENTS
