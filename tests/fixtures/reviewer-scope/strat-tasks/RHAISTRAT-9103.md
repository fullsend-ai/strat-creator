---
title: Pipeline credential rotation
priority: Major
status: Refined
strat_id: RHAISTRAT-9103
source_rfe: RHAIRFE-9103
jira_key: RHAISTRAT-9103
workflow: null
latest_diff: null
---
# Pipeline credential rotation

Authored fixture. Evidence marker: ROTATION_SCOPE_CANARY.

## Strategy

Allow pipeline administrators to rotate credentials used by scheduled pipelines.
The scheduler reads credentials through a secret reference when starting a run.
New runs use the new credential after rotation; running jobs retain the previous
one until completion. Acceptance: new jobs use the replacement secret; active
jobs continue; invalid credentials yield an actionable failure. The strategy
leaves revocation order and rollback permissions unspecified. Scope: scheduler
secret reference and tests. Effort M. Dataset deletion is outside this feature.
