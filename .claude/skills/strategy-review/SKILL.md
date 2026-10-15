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
  `/tmp/strat-assess/review`.

Then continue with Step 5. Without the flag, follow the rest of this step.

### Launch the scorer

Launch a strat-scorer agent to produce numeric scores for the strategy. The
in-repository assess-strat implementation provides the rubric and agent
definition.

Create a clean run directory (removes stale result files from prior reviews in the same CI job):

```bash
rm -rf /tmp/strat-assess/review
mkdir -p /tmp/strat-assess/review
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
- `{RUN_DIR}` → `/tmp/strat-assess/review`

Wait for the scorer agent to complete.

## Step 5: Parse Scores and Apply Verdicts (AUTOMATED — no LLM judgment)

After the scorer agent has completed, run the scoring scripts to deterministically compute the verdict and apply it to the review file. With `--scores-from <dir>`, replace `/tmp/strat-assess/review` with `<dir>` in all three commands:

```bash
# Parse .result.md files → scores.csv with deterministic verdicts
python3 scripts/assess-strat/parse_results.py /tmp/strat-assess/review/

# Apply scores and verdicts to review file frontmatter
python3 ${CLAUDE_SKILL_DIR}/scripts/apply_scores.py /tmp/strat-assess/review/scores.csv \
    --review-dir artifacts/strat-reviews \
    --result-dir /tmp/strat-assess/review

# Print summary statistics
python3 scripts/assess-strat/summarize_run.py /tmp/strat-assess/review/
```

**Do NOT manually extract scores, compute verdicts, or set frontmatter.** The scripts handle this deterministically. The verdict rules are:
```
APPROVE:  total >= 6  AND  no zeros       → needs_attention=false
REVISE:   total >= 3  AND  ≤1 zero        → needs_attention=true
REJECT:   total < 3   OR   2+ zeros       → needs_attention=true
```

## Step 6: Run Prose Reviews

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

Each reviewer auto-detects local mode (`local/strat-tasks/` vs `artifacts/strat-tasks/`) and reads the appropriate directories.

## Step 7: Write Prose to Review Files

Update the review file body in `artifacts/strat-reviews/{id}-review.md` with the prose from all four reviewers. The scores table was already written by `apply_scores.py` in Step 5 — add the prose sections after it.

The review file body should contain:

```markdown
## Scores
[already written by apply_scores.py — do not overwrite]

## Feasibility Review: {STRAT_ID} — {title}
<assessment from feasibility reviewer>

## Testability Review: {STRAT_ID} — {title}
<assessment from testability reviewer>

## Scope Review: {STRAT_ID} — {title}
<assessment from scope reviewer>

## Architecture Review: {STRAT_ID} — {title}
<assessment from architecture reviewer, or "skipped — no context">

## Agreements
<where reviewers aligned>

## Disagreements
<where reviewers diverged — preserve both views>
```

After writing prose, update the `reviewers.*` frontmatter fields with each prose reviewer's individual verdict:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/frontmatter.py set artifacts/strat-reviews/<id>-review.md \
    reviewers.feasibility=<prose_verdict> \
    reviewers.testability=<prose_verdict> \
    reviewers.scope=<prose_verdict> \
    reviewers.architecture=<prose_verdict>
```

**Important:** The `recommendation` field is NEVER changed by prose reviewers. It comes from the numeric scores only. Prose reviewers set their own `reviewers.*` verdicts for informational purposes — these do NOT affect the gate decision.

**Preserve disagreements.** If the feasibility reviewer says "this is fine" but the scope reviewer says "this is too big," report both views. Do not average or harmonize.

## Step 7a: Post Review Summary to Jira

Compose a review summary comment and post it to the RHAISTRAT issue (or save to file in dry-run mode).

Read the review file frontmatter to get scores and recommendation:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/frontmatter.py read artifacts/strat-reviews/{id}-review.md
```

Compose the comment in markdown using this format:

```markdown
*[Strat Creator]* Strategy Review — {VERDICT} (Score: {total}/8)

| Criterion | Score | Status |
|-----------|-------|--------|
| Feasibility | {F}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Testability | {T}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Scope | {S}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Architecture | {A}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |

{For each dimension scored < 2, one sentence summarizing the issue from the prose review.}

**Action:** {verdict-specific guidance}
```

Action text by verdict:
- **APPROVE**: "No action needed — strategy passed quality review."
- **REVISE**: "Edit the strategy to address flagged issues, then remove the `needs-attention` label. The pipeline will re-evaluate automatically."
- **REJECT**: "This strategy has fundamental problems. Consider revisiting the source RFE or re-running `/strategy-refine` with different constraints."

**Posting:**

Save the composed markdown to a temp file, then post:

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import add_comment, markdown_to_adf
import os
comment_md = open(sys.argv[1]).read()
add_comment(os.environ['JIRA_SERVER'], os.environ['JIRA_USER'],
            os.environ['JIRA_TOKEN'], sys.argv[2], markdown_to_adf(comment_md))
" /tmp/strat-review-comment-{KEY}.md RHAISTRAT-NNNN
```

- **Dry-run mode**: Write the comment markdown to `artifacts/strat-reviews/{id}-review-comment.md` instead. Print `[DRY RUN] Review comment saved to artifacts/strat-reviews/{id}-review-comment.md`.
- **Jira credentials unavailable**: Save to file (same as dry-run) and notify the user.

## Step 7b: Attach Full Review File to Jira

If NOT in dry-run mode and `jira_key` is not null, attach the full review file to the RHAISTRAT issue as a Jira attachment. This gives reviewers access to the complete prose reviews alongside the summary comment.

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from jira_utils import add_attachment, require_env
s, u, t = require_env()
add_attachment(s, u, t, sys.argv[1], sys.argv[2])
" RHAISTRAT-NNNN artifacts/strat-reviews/{id}-review.md
```

Print `[ATTACHMENT] Review file attached to RHAISTRAT-NNNN`.

In dry-run mode, skip and print `[DRY RUN] Skipping attachment for RHAISTRAT-NNNN`.

## Step 7c: Apply Verdict Labels

If NOT in dry-run mode and `jira_key` is not null, add the appropriate label based on the verdict:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from jira_utils import add_labels, require_env
s, u, t = require_env()
add_labels(s, u, t, sys.argv[1], sys.argv[2:])
" RHAISTRAT-NNNN <labels>
```

Labels by verdict:
- **APPROVE**: add `strat-creator-rubric-pass`
- **REVISE**: add `strat-creator-needs-attention`
- **REJECT**: add `strat-creator-needs-attention`

Print `[LABEL] <label> added to RHAISTRAT-NNNN`.

In dry-run mode, skip and print `[DRY RUN] Skipping labels for RHAISTRAT-NNNN`.

## Step 8: Advise the User

Based on the result:
- **Approved** (`needs_attention=false`): Tell the user the strategy is ready for sign-off.
- **Needs revision** (`needs_attention=true`, verdict=REVISE): List specific issues by dimension. Tell the user to edit the strategy, remove `needs-attention`, and re-run `/strategy-review`.
- **Fundamental problems** (`needs_attention=true`, verdict=REJECT): Recommend revisiting the RFE or re-running `/strategy-refine` with different constraints.

$ARGUMENTS
