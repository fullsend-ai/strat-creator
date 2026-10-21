---
name: strategy-reject
description: Use when an SME rejects a pipeline-created strategy. Closes it in Jira and returns its source RFE for human rework.
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion
---

Reject a RHAISTRAT only when an SME has determined that its source RFE needs
rework. This is a Jira-writing workflow. Do not modify strategy content,
close child epics, or remove the RFE rework label; the RFE owner performs the
rework and explicitly removes that label when ready.

## Input

$ARGUMENTS must contain exactly one RHAISTRAT key, for example RHAISTRAT-2283.
If it is missing or does not match RHAISTRAT-NNNN, stop with:

~~~
Usage: /strategy-reject RHAISTRAT-NNNN
~~~

## State and credentials

This workflow persists progress so it can resume safely after context
compression. Set these paths after validating the key:

~~~bash
STATE_FILE="tmp/strategy-reject-RHAISTRAT-NNNN.state"
REASON_FILE="tmp/strategy-reject-RHAISTRAT-NNNN-reason.md"
~~~

All Jira operations require JIRA_SERVER, JIRA_USER, and JIRA_TOKEN. Use
require_env() from jira_utils.py; if any are absent, report the missing
credentials and stop before making changes.

If STATE_FILE already exists, read it with state.py and resume at its recorded
phase. Always refetch the STRAT before resuming. Do not repeat a completed
comment, transition, or label operation merely because the skill was
re-entered.

## Step 1: Validate the STRAT

Fetch the STRAT with its project, status, labels, issue links, and subtasks:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAISTRAT-NNNN \
  --fields project,status,labels,issuelinks,subtasks
~~~

Stop and report an error if any of these checks fail:

1. The issue exists and fields.project.key is RHAISTRAT.
2. The labels include strat-creator-auto-created.

Set strat_terminal=true when fields.status.statusCategory.key is done. An
already Closed or Resolved STRAT is not a validation failure: it is a
reconciliation case, so its rejection comment and RFE updates still run but
its Jira transition is skipped.

## Step 2: Resolve the source RFE and warn about child epics

From fields.issuelinks, inspect only links whose type name is Cloners. For
each such link, inspect both outwardIssue and inwardIssue; the source RFE is
the first linked key beginning RHAIRFE-. If there is no such key, report
RHAISTRAT-NNNN has no Cloners link to an RHAIRFE and stop.

Find child Epics through the parent hierarchy:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/find_child_epics.py RHAISTRAT-NNNN
~~~

If any child epics exist, warn before proceeding:

~~~
WARNING: RHAISTRAT-NNNN has N child epic(s): RHAISTRAT-NNNN-1, ...
These epics will NOT be automatically closed. You may need to close them
manually or use /epic-rewind (future skill) to unwind them.
~~~

Initialize the state only after validation and RFE resolution succeed:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py init "$STATE_FILE" \
  "strat_key=RHAISTRAT-NNNN" "rfe_key=RHAIRFE-MMMM" \
  "strat_terminal=true-or-false" "phase=validated"
~~~

## Step 3: Collect the rejection reason

Ask the SME for a rejection comment before making Jira changes. It must say
why the STRAT is rejected and what the RFE must change before a fresh STRAT
can be created. Do not accept an empty reason.

Save the exact response in REASON_FILE with the Write tool, then persist the
phase:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" \
  "phase=reason-collected"
~~~

## Step 4: Comment on and close the STRAT

Format the rejection comment and post it:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/format_reject_comment.py strat \
  --reason-file "$REASON_FILE" \
  --strat-key RHAISTRAT-NNNN \
  --rfe-key RHAIRFE-MMMM > tmp/strat-comment.md

python3 ${CLAUDE_SKILL_DIR}/scripts/post_comment.py RHAISTRAT-NNNN \
  --body-file tmp/strat-comment.md
~~~

Record phase=strat-commented only after the comment succeeds:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" \
  "phase=strat-commented"
~~~

If strat_terminal=true, skip only the transition and record
phase=strat-closed:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" \
  "phase=strat-closed"
~~~

Otherwise, fetch and display every available transition:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/get_transitions_json.py RHAISTRAT-NNNN
~~~

Find a transition whose destination status (to.name) is Closed. Select Won't
Do as its resolution when that resolution is available; otherwise use Rejected
when available. Inspect the selected transition's resolution allowedValues
when Jira supplies them. If Jira does not supply allowedValues, try Won't Do;
only if Jira rejects that resolution may you retry the same Closed transition
with Rejected. Execute only that transition:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/do_transition.py RHAISTRAT-NNNN \
  TRANSITION_ID --resolution "Won't Do"
~~~

If there is no transition to Closed, a usable resolution cannot be selected,
or the transition fails, report the available transitions and stop. Do not
choose another workflow transition or update the RFE. Record
phase=strat-closed only after the transition succeeds:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" \
  "phase=strat-closed"
~~~

## Step 5: Update the source RFE

Fetch the RFE status. If fields.status.statusCategory.key is done, warn the
SME that the RFE is already closed, but continue. The human must reopen it or
create a new RFE; this skill does neither.

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAIRFE-MMMM \
  --fields status
~~~

Format the rejection comment and post it to the RFE:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/format_reject_comment.py rfe \
  --reason-file "$REASON_FILE" \
  --strat-key RHAISTRAT-NNNN \
  --rfe-key RHAIRFE-MMMM > tmp/rfe-comment.md

python3 ${CLAUDE_SKILL_DIR}/scripts/post_comment.py RHAIRFE-MMMM \
  --body-file tmp/rfe-comment.md
~~~

Record phase=rfe-commented after the comment succeeds:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" \
  "phase=rfe-commented"
~~~

Then perform the label updates. Both operations are intentionally idempotent:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/update_labels.py RHAIRFE-MMMM \
  --remove strat-creator-consumed \
  --add strat-creator-rework-needed
~~~

Record phase=complete only after the label update succeeds. If
strat-creator-consumed is absent, its removal is a no-op and is not an error:

~~~bash
python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set "$STATE_FILE" "phase=complete"
~~~

## Step 6: Report

Print this summary, using the STRAT final existing status if it was already
terminal:

~~~
Strategy rejected:
  STRAT:  RHAISTRAT-NNNN -> Closed
  RFE:    RHAIRFE-MMMM   -> strat-creator-rework-needed

The RFE is now on hold. To resume:
  1. Update the RFE or leave a comment with new context
  2. Remove the strat-creator-rework-needed label
  3. The strat-pipeline will create a fresh strategy on its next run
~~~

$ARGUMENTS
