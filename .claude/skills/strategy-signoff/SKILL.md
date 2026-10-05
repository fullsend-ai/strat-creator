---
name: strategy-signoff
description: Sign off on a CI-approved strategy — pushes content and adds strat-creator-human-sign-off label. For rubric-pass strategies only.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

You are signing off on a strategy that has passed CI review, marking it as feature-ready after human confirmation.

## Input

`$ARGUMENTS` must contain a RHAISTRAT key (e.g., `RHAISTRAT-1520`). If no key is provided, ask the user for one.

## Step 1: Validate Pre-Conditions

Read the strategy file from `local/strat-tasks/RHAISTRAT-NNNN.md`. Verify it exists.

Then fetch the current labels and parent from Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAISTRAT-NNNN --fields labels,parent --markdown
```

**Guard checks** (the labels are the type's `conventions.labels.needs_attention` / `rubric_pass` in `types/<type>/type.yaml`, `<type>` being the `--type` value in `$ARGUMENTS`, default `rfe-strategy`):

- If the issue has `strat-creator-needs-attention` (not `rubric-pass`): tell the user this strategy needs CI approval first. Suggest using `/strategy-push` to resubmit, then waiting for CI to approve before signing off. **Stop here.**
- If the issue does NOT have `strat-creator-rubric-pass`: tell the user this strategy hasn't been CI-approved yet and cannot be signed off. **Stop here.**
- If the local file does not exist: tell the user to run `/strategy-pull RHAISTRAT-NNNN` first. **Stop here.**

**Parent check (non-blocking):**

- If the issue has no `parent` field set: print `[WARNING] RHAISTRAT-NNNN has no parent Outcome set. Consider setting one in Jira for proper hierarchy navigation.` Continue with sign-off.

## Step 2: Confirm with User

Before proceeding, show the user a summary:

1. Read the local strategy file and display the title and key sections
2. If a local review exists in `local/strat-reviews/`, show the score summary

Ask the user to confirm: "Ready to sign off on RHAISTRAT-NNNN? This will push the strategy content to Jira and add the `strat-creator-human-sign-off` label."

## Step 3: Push Strategy Content

Push the updated strategy section to Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/push_strategy.py RHAISTRAT-NNNN local/strat-tasks/RHAISTRAT-NNNN.md
```

## Step 4: Post Review Summary Comment

If a review summary exists at `local/strat-reviews/RHAISTRAT-NNNN-review-summary.md`, post it as a comment to the Jira issue:

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import add_comment, markdown_to_adf, require_env
s, u, t = require_env()
comment_md = open(sys.argv[1]).read()
add_comment(s, u, t, sys.argv[2], markdown_to_adf(comment_md))
" local/strat-reviews/RHAISTRAT-NNNN-review-summary.md RHAISTRAT-NNNN
```

Print `[COMMENT] Review summary posted to RHAISTRAT-NNNN`.

If the summary file does not exist, skip and print `[SKIP] No review summary found — skipping comment`.

## Step 5: Attach Review File

If a full review file exists at `local/strat-reviews/RHAISTRAT-NNNN-review.md`, attach it to the Jira issue:

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import add_attachment, require_env
s, u, t = require_env()
add_attachment(s, u, t, sys.argv[1], sys.argv[2])
" RHAISTRAT-NNNN local/strat-reviews/RHAISTRAT-NNNN-review.md
```

Print `[ATTACHMENT] Review file attached to RHAISTRAT-NNNN`.

If the review file does not exist, skip and print `[SKIP] No review file found — skipping attachment`.

## Step 6: Add human-sign-off Label

The label is the type's `conventions.labels.human_sign_off`; sign-off is the `signoff` stage of `pipeline.stages` (human sign-off stays).

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import add_labels, require_env
s, u, t = require_env()
add_labels(s, u, t, 'RHAISTRAT-NNNN', ['strat-creator-human-sign-off'])
"
```

Print `[LABEL] strat-creator-human-sign-off added to RHAISTRAT-NNNN`.

## Step 7: Remove [DRAFT] Prefix

Strip the `[DRAFT]` prefix (and its trailing space; `conventions.summary_prefix`, removed at this stage) from the Jira summary if present. If the type has no `conventions.summary_prefix` (a same-ticket type, whose summary is its owner's), skip this step and print `[SKIP] No summary prefix for <type> — summary unchanged`:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/remove_draft_prefix.py RHAISTRAT-NNNN
```

## Step 8: Confirm Completion

Tell the user: "RHAISTRAT-NNNN signed off and marked feature-ready."

$ARGUMENTS
