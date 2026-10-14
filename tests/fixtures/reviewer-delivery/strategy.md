---
strat_id: RHAISTRAT-9001
source_rfe: RHAIRFE-9001
title: Serving request timeout handling
priority: Major
status: Refined
jira_key: RHAISTRAT-9001
workflow: null
latest_diff: null
---
# Strategy: Serving request timeout handling

## Strategy

Add a bounded timeout to requests forwarded from the routing component to the
serving component. This authored fixture has no customer or production data.

## RFE

RHAIRFE-9001 requests a configurable timeout and a visible failure when a serving
request does not finish. Successful responses should retain their existing behavior.

## Implementation

The routing component applies the configured timeout before returning the response.
On timeout it records an error and closes the outstanding request. A configuration
change takes effect for new requests. No automatic retry is added.

## Acceptance criteria

- A request that completes before its timeout returns the normal response.
- A request that exceeds its timeout returns a timeout error.
- A client can configure the timeout for subsequent requests.

## Scope and effort

One routing component change and tests. Effort: M. The serving component is
unchanged. Automatic retries and provider-specific quota policy are out of scope.

## Risks and open questions

The strategy has not specified the timeout error format or how cancellation
propagates to a request that is still executing at the serving component.
