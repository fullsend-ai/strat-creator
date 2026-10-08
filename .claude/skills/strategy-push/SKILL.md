---
name: strategy-push
description: Push a locally-refined strategy back to Jira and resubmit to CI. Works for both needs-attention and rubric-pass strategies.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

You are pushing an improved strategy back to Jira so CI can re-evaluate it. This skill works for strategies with either `strat-creator-needs-attention` or `strat-creator-rubric-pass` — in both cases, the label is removed so CI re-processes the strategy.

## Input

`$ARGUMENTS` must contain a strategy key: a RHAISTRAT key (e.g., `RHAISTRAT-1520`), or the RHOAIENG key of an
Initiative whose strategy lives on the Initiative itself (e.g., `RHOAIENG-1200`). If no key is provided, ask the
user for one. `RHAISTRAT-NNNN` below stands for whichever key you were given.

## Step 1: Validate Pre-Conditions

Read the strategy file from `local/strat-tasks/RHAISTRAT-NNNN.md`. Verify it exists and has `workflow: local` in its frontmatter.

Then fetch the current labels from Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAISTRAT-NNNN --fields labels --markdown
```

**Guard checks:**

- If the local file does not exist: tell the user to run `/strategy-pull RHAISTRAT-NNNN` first. **Stop here.**
- If the issue has NEITHER `strat-creator-needs-attention` NOR `strat-creator-rubric-pass`: tell the user this strategy hasn't been through CI review yet and cannot be pushed. **Stop here.**

Determine which label is present (`strat-creator-needs-attention` or `strat-creator-rubric-pass`) — this determines which label to remove in Step 3.

## Step 2: Push Strategy Content

Push the updated strategy section to Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/push_strategy.py RHAISTRAT-NNNN local/strat-tasks/RHAISTRAT-NNNN.md
```

## Step 3: Remove CI Label

Remove whichever CI label is present to allow CI to re-process:

- If `strat-creator-needs-attention` was found in Step 1:

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import remove_labels, require_env
s, u, t = require_env()
remove_labels(s, u, t, 'RHAISTRAT-NNNN', ['strat-creator-needs-attention'])
"
```

Print `[LABEL] strat-creator-needs-attention removed from RHAISTRAT-NNNN`.

- If `strat-creator-rubric-pass` was found in Step 1:

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_SKILL_DIR}/scripts')
from jira_utils import remove_labels, require_env
s, u, t = require_env()
remove_labels(s, u, t, 'RHAISTRAT-NNNN', ['strat-creator-rubric-pass'])
"
```

Print `[LABEL] strat-creator-rubric-pass removed from RHAISTRAT-NNNN — CI will re-evaluate.`

## Step 4: Advise the User

Tell the user:

- "Strategy pushed and resubmitted to CI. The pipeline will re-evaluate on the next run."
- "Once CI approves (adds `strat-creator-rubric-pass`), use `/strategy-pull RHAISTRAT-NNNN` again and `/strategy-signoff RHAISTRAT-NNNN` to complete the review."

If the strategy previously had `strat-creator-rubric-pass`, also note: "This strategy was previously CI-approved. Your edits have been pushed and CI will re-evaluate on the next run."

$ARGUMENTS
