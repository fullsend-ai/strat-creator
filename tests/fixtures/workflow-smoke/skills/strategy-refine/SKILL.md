---
name: strategy-refine
description: Smoke-test stub of strategy-refine. Fills the Strategy section without Jira.
context: fork
user-invocable: true
allowed-tools: Read, Write, Edit, Bash
---

Smoke-test stub. `$ARGUMENTS` holds one strategy key (`STRAT-N`) and `--dry-run`.

1. In `artifacts/strat-tasks/<KEY>.md`, replace the line `<!-- To be filled by /strategy-refine -->`
   with the line `Smoke strategy: extend the existing operator with one reconciler; ship behind a flag; test with an e2e suite.`
   Use the Edit tool.
2. Run `python3 scripts/frontmatter.py set artifacts/strat-tasks/<KEY>.md status=Refined`.
3. Reply `refined <KEY>`.
