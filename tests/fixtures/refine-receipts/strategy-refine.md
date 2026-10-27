---
name: strategy-refine
description: Authored local fixture action for the refinement receipt test; no Jira.
context: fork
allowed-tools: Read, Bash
user-invocable: true
---

This fixture tests receipt handling, not strategy generation or accuracy.
The first argument is the assigned strategy key. Run this command exactly once:

```bash
python3 tests/refine-receipts/fixture.py apply $0
```

It fills this assigned file from an authored proposal, sets Refined through the
real frontmatter library and counts invocations. Return to the caller, which
records completion with strat_pipeline_state.py. Do not call Jira, regenerate
a proposal, mark progress yourself, or repeat the fixture action.
