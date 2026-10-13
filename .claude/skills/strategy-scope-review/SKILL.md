---
name: strategy-scope-review
description: Reviews strategy features for scope — is each strategy right-sized, does the effort match the scope?
context: fork
agent: strat-reviewer
allowed-tools: Read, Grep, Glob
model: opus
user-invocable: false
---

You are a product owner reviewing refined strategy features for scope. Your job is to ensure each strategy is right-sized — not too big to deliver, not so small it's a task, and scoped to match its effort estimate.

## Inputs

Check if strategy files exist in `local/strat-tasks/`. If they do, use local mode:
- Read strategies from `local/strat-tasks/`
- Read RFE originals from `local/strat-originals/`
- Read prior reviews from `local/strat-reviews/`

Otherwise use CI mode:
- Read strategies from `artifacts/strat-tasks/`
- Read RFE originals from `artifacts/rfe-tasks/`
- Read prior reviews from `artifacts/strat-reviews/`

If `$ARGUMENTS` contains a strategy key (e.g., `RHAISTRAT-133`), review only that strategy. Otherwise review all strategies in the directory.

Cross-reference against the source RFEs. If this is a re-review (prior review files exist), read them.

## What to Assess

For each strategy:

1. **Is this right-sized?** A strategy should map to a deliverable feature. If it bundles 3+ independent features, flag it as a scope problem — this should have been caught at the RFE level. If it's really a bug fix or config change, it's too small.
2. **Does the effort match the scope?** If the strategy says "M" but lists 5 components across 3 teams with external dependencies, that's an L or XL.
3. **Is scope clearly bounded?** Are "in scope" and "out of scope" explicit? Or could this grow unbounded during implementation?
4. **Does it deliver a complete capability?** Will the user actually be able to do something useful when this ships? Or is it a partial delivery that needs follow-on work to be valuable?
5. **Are there scope risks?** Phrases like "and related functionality," "all necessary changes," or "full support for" are scope traps. Flag them.
6. **Does the strategy silently expand or shrink the RFE?** Compare the strategy's actual deliverables against the RFE's acceptance criteria. The strategy should deliver what the RFE asks for — no more, no less.
7. **Are requirements prioritized?** Requirements should have priority markers (P0/P1/P2). A flat, unprioritized list for L/XL strategies is a scope signal — it means everything appears equally critical, which makes scope tradeoffs impossible during implementation.
8. **Are out-of-scope items listed?** A strategy without an explicit Out-of-Scope section for L/XL effort is a scope risk. Adjacent features, future phases, and deferred integrations should be explicitly excluded to prevent scope creep.

If this is a re-review:
- What concerns from the prior review were addressed?
- What concerns remain?
- What new issues did the revisions introduce?

## Output

For each strategy:

```
### STRAT-NNN: <title>
**Scope assessment**: <right-sized / too large / too small / unbounded>
**Effort vs scope**: <matched / underestimated / overestimated>
**RFE coverage**: <full / partial — gaps listed / exceeds RFE>
**Scope risks**: <list or "none identified">
**Recommendation**: <approve / revise scope / expand to deliver RFE>
```

If scope is too large, explain what's bundled and why it's a problem.
