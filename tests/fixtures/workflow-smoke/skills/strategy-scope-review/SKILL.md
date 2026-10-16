---
name: strategy-scope-review
description: Isolated smoke fixture for the scope reviewer.
context: fork
agent: strat-reviewer
user-invocable: false
---

REVIEW_SCOPE_TARGET: $0

Do not use tools. The exact strategy key is supplied in `$ARGUMENTS` after
REVIEW_SCOPE_TARGET. Return only the following completed report with KEY replaced:

### KEY: Smoke strategy
**Scope assessment**: right-sized
The authored smoke strategy supplies enough concrete detail for this fixture.
**Recommendation**: approve
