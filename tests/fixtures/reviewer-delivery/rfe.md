# RHAIRFE-9001: Serving request timeout handling

Authored test input. Add a configurable timeout to routing-to-serving requests.
Return a visible timeout failure, retain normal successful responses, and apply
configuration changes to new requests. Cancellation behavior and the exact error
format need to be settled. This test does not create or update a Jira issue.
