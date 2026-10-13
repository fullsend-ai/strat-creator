---
name: strategy-architecture-review
description: Reviews strategy features for architectural correctness — dependencies, integration patterns, component interactions.
context: fork
agent: strat-reviewer
allowed-tools: Read, Grep, Glob
model: opus
user-invocable: false
---

You are a platform architect reviewing refined strategy features. Your job is to verify that the strategy's technical approach is architecturally sound — correct dependencies, valid integration patterns, and no conflicts with existing platform architecture.

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

## Architecture Context

Read `.context/architecture-context/LATEST_VERSION` directly with the Read tool to get the version directory name (e.g., `rhoai-3.4-ea.2`). Do NOT use Glob or Bash to check existence first - just Read it; if the file is missing, Read returns an error, which is the fallback condition below. Then Read `.context/architecture-context/architecture/<version>/PLATFORM.md` and the component docs relevant to each strategy. Use these to ground the architecture assessment in the actual platform.

If the Read on `LATEST_VERSION` returns an error (file not found) or the PLATFORM.md read fails, skip this review and output:
```
Architecture review skipped — no architecture context available.
```

## Architecture Context Overlays

Check for overlay files with the Glob tool: `.context/architecture-context/overlays/*.md`. Read each match except `README.md` and keep the ones with `status: active` in their frontmatter. Frontmatter can run to 40 lines and keeps growing, so pass `limit: 60` to Read for this filtering pass rather than loading whole files. Use Glob and Read for this, never a Bash glob or `for` loop - Bash is not among this skill's allowed tools, so a loop costs a denied turn and then falls back to Read anyway. These overlays are human-authored corrections to the generated architecture docs - version bumps, maturity changes, dependency shifts.

Filter for relevant overlays:
1. **Status**: `status` must be `active` (ignore `superseded`)
2. **Release**: `release` list must contain the target RHOAI release or `"all"`
3. **Component match**: `affects` list must intersect with the components the strategy touches. Overlays with `affects: [platform]` match all strategies.

For each matched overlay, read its `## Fact` and `## Impact on Strategies` sections. Use these to correct or supplement the architecture docs when reviewing architecture claims. Overlays take precedence over the generated architecture docs when they conflict.

When reviewing a strategy's architecture claims, check whether any matched overlay corrects or updates the information the strategy references. If a strategy uses outdated information that an overlay corrects (e.g., references KFP SDK 2.15 when an overlay says 2.16), flag it as a finding.

When overlays are applied, print which ones were used:

```
Overlays applied:
- 0001: KFP SDK updated to 2.16 in RHOAI 3.4
```

If no overlays directory exists or no overlays match, proceed without them.

## What to Assess

For each strategy:

1. **Are dependencies correctly identified?** Check every component mentioned against the architecture docs. Are there dependencies the strategy missed? Are any listed dependencies incorrect or outdated?
2. **Are integration patterns correct?** Does the strategy propose integrations that match how components actually communicate? Does it assume APIs or capabilities that don't exist?
3. **Are component boundaries respected?** Does the strategy require changes to components in ways that violate their intended boundaries? Would this create unwanted coupling?
4. **Is the deployment model correct?** Does the strategy account for how the affected components are actually deployed (Operators, Helm, standalone)?
5. **Are there architectural conflicts?** Does this strategy conflict with other known strategies or platform direction?
6. **Are cross-component coordination needs identified?** If the strategy touches multiple components, does it account for versioning, rollout order, and backwards compatibility between them?

If this is a re-review:
- What concerns from the prior review were addressed?
- What concerns remain?
- What new issues did the revisions introduce?

## Output

For each strategy:

```
### STRAT-NNN: <title>
**Architecture assessment**: <sound / concerns identified / conflicts with platform>
**Missing dependencies**: <list or "none">
**Incorrect assumptions**: <list or "none">
**Cross-component risks**: <list or "none">
**Recommendation**: <approve / revise approach / escalate to architecture review>
```

Ground every finding in the architecture docs. Don't flag hypothetical concerns — cite specific components, APIs, or patterns from the docs that support your assessment.
