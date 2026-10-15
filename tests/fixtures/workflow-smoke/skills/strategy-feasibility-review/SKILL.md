---
name: strategy-feasibility-review
description: Smoke-test stub of the feasibility reviewer. Returns a fixed one-paragraph review.
context: fork
user-invocable: false
allowed-tools: Read
---

Smoke-test stub. Do not use any tool. Reply with exactly these two lines, with the
strategy key from `$ARGUMENTS` in place of KEY:

Verdict: approve
feasibility review of KEY: smoke review, no findings.
