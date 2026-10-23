---
title: Serving request timeout handling
priority: Major
status: Ready
rfe_id: RHAIRFE-9101
size: null
parent_key: null
original_labels: null
---
# Serving request timeout handling

Authored fixture. Evidence marker: TIMEOUT_SCOPE_CANARY.

## Strategy

Add a configurable timeout to routing-to-serving HTTP requests. Retain normal
successful responses. Configuration changes apply to new requests. Return a
visible timeout error, with no automatic retry. The strategy does not specify
whether closing the request cancels work at the server. Error format is open.
Acceptance: requests below the configured timeout succeed; requests above it
fail with a timeout. Validate using a deliberately delayed serving response.
Scope: one routing component and its tests. Effort M. Retries are out of scope.
