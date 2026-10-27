---
title: Dataset retention and deletion
priority: Major
status: Ready
rfe_id: RHAIRFE-9102
size: null
parent_key: null
original_labels: null
---
# Dataset retention and deletion

Authored fixture. Evidence marker: RETENTION_SCOPE_CANARY.

## Strategy

Add an optional retention period for dataset objects. The dataset controller
records an expiration timestamp and deletes expired objects through the storage
API. A preview lists objects due for deletion; legal holds prevent deletion.
Acceptance: expired unheld objects are deleted; held objects remain; preview
matches deletion candidates. The strategy does not specify partial failure
recovery or audit retention. Scope: dataset controller, storage API, preview UI.
Effort M. Serving requests and pipeline credentials are outside this feature.
