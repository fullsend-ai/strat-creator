# Plan: UX Research Reviewer — Integration

## Goal

Add a review dimension that checks whether a strategy's claimed user needs are
supported by existing UXD research from the research catalog. It is
**informational**: it produces a specialist report that is composed into the
review and attributed like the others, but it carries no score and never
changes the numeric approval gate.

## What is built

In this repository (untracked until committed):

- `.claude/skills/strategy-uxresearch-review/SKILL.md` — the reviewer.
  Claims come from one per `## User Needs` bullet; personas from
  `## Target Personas`. It gathers evidence in a single call, screens every
  returned slide for tension, and derives its verdict mechanically from the
  evidence tags: `approve` when nothing is tagged `tension`/`contradicts`,
  `revise` otherwise.
- `.claude/agents/strat-uxresearch-reviewer.md` — agent definition; its tool
  list matches the skill's `allowed-tools`.

The reviewer requires the research MCP's `gather_research_evidence` tool, which
runs its searches server-side and returns a reproducible evidence set. That
work lives in the [research-mcp](https://gitlab.cee.redhat.com/uxd/research-mcp)
repository.

## What is drafted, not merged

The informational wiring exists in the working tree and passes the affected
tests; it is not committed and has not been reviewed by the pipeline
maintainers.

| File | Change |
|---|---|
| `types/_schema/type.schema.json` | optional `scored` on `dimension` (absent means true) |
| `types/rfe-strategy/type.yaml` | `uxresearch` dimension with `scored: false`; optional, skippable reviewer; `UX Research` label |
| `scripts/review_composition.py` | `INFORMATIONAL` / `ALL_ROLES`: capture, attribute, compose and export the report; keep it out of `numeric_decision`, the score table, the /8 total and the Dissent list |
| `.claude/skills/strategy-review/SKILL.md` | launches the fifth reviewer with the other four |
| `scripts/extract-pipeline-data.py`, `generate-dashboard.py`, `generate-report.py` | carry the verdict through as a `UXR` column beside the scored four, on all three dashboard tables and the HTML report; labelled informational and never given a score |
| `scripts/publish_review.py` | the Jira summary's dissent list is derived from score fields, so an informational verdict can never read as dissent |
| `.fullsend/plugins/strat-pipeline/scripts/review_composition.py` | byte-identical copy (a test enforces parity) |
| tests | the new key is pinned; four composition tests prove the report cannot move the gate, may be absent, may be `skipped`, and that scored roles still cannot |

### Why `scored: false` on the dimension

`pipeline.dimensions[].name` was pinned equal to `schema.review.score_fields`,
which held while every dimension was scored. The dimension gains an optional
`scored` boolean (absent means true) and the pin now compares only the scored
entries, so the list stays one ordered set of reviewers — which is how the
orchestrator launches them — while the unscored member is marked explicitly
rather than inferred from its position or its `blocking` flag.

Reusing `blocking` was rejected: the schema defines it as "whether the ASSESS
wave waits on the dimension", which is a different question from whether the
verdict gates. `uxresearch` is `blocking: true` and `scored: false`.

The invariant is deliberately relaxed, so the tests assert the new contract:
scored dimensions equal `score_fields`; every dimension has a skill and a
launch line; unscored dimensions are composed into `reviewers`, never appear in
`score_fields`, and match `review_composition.INFORMATIONAL`, which no scored
role may join.

## What still needs a decision

1. **Scored or informational.** This plan keeps it informational: a 0/1/2
   criterion would move the total from /8 to /10 and require recalibrating
   every threshold below. The reviewer's own output also mixes two signals —
   coverage (how much research exists) and alignment (whether it agrees) — and
   collapsing both into one 0–2 score loses the distinction that makes the
   report useful.
2. **Rubric.** Only if (1) changes. `types/rfe-strategy/type.yaml` pins a
   sha256 of `scripts/assess-strat/agent_prompt.md`, so editing the rubric
   requires updating that pin, and a test enforces it.
3. **Publication.** `scripts/publish_review.py` renders `Score: {total}/8` from
   `2 * len(score_fields)`, which stays correct while the dimension is
   unscored, and its dissent line ignores the informational verdict. Adding the
   verdict to the Jira summary as a visible line of its own (rather than only in
   the attached review) is a separate, smaller change.
4. **Verify MCP tool inheritance before relying on the pipeline run.** In
   local headless runs, a `context: fork` skill invoked as a slash command
   inherited the plugin's MCP tools; a model-initiated `Skill` call and a direct
   subagent invocation did not, both returning `skipped` with "research-mcp not
   available". That held with `user-invocable` set both ways, so the flag is not
   the cause: the fork itself does not receive plugin MCP tools in this mode.
   The pipeline launches reviewers through the Skill tool, so confirm the fork
   there sees the tools — otherwise the reviewer silently assesses nothing. The
   failure is quiet, not an error.
5. **Data leakage.** The reviewer cites research findings and attaches its
   report to a RHAISTRAT ticket, which is VPN-internal. If pipeline artifacts
   ever reach a public repo, a scrub step must strip research citations before
   publication.

## Rollout (from Eder)

1. **Build** — done (skill, agent, server tool).
2. **Test manually** — done on 15 RHAISTRAT strategies; see below.
3. **Deploy non-blocking** — the drafted wiring; needs maintainer review.
4. **Observe** — compare verdicts against human judgment.
5. **Promote** — only relevant if (1) flips to scored.

## Test record

15 real RHAISTRAT strategies, each run repeatedly through the reviewer skill
against the research MCP. What the runs established, in order:

- **Free-form search was not reproducible.** Verdicts flipped on 4 of 7
  strategies between identical runs because each run retrieved a different
  slice of the catalog.
- **Fixing the procedure in the prompt was not enough.** With fixed queries the
  model still chose different slides from the same results.
- **Moving retrieval into the server fixed it.** One call, one evidence set:
  verdicts then agreed on 6 of 7, and the remaining flip was a tagging
  judgment rather than a retrieval difference.
- **A tension screen raised recall.** Screening every returned slide for
  tension, and requiring each tension to name both personas, lifted `revise`
  from 2/7 to 5/7 on the tension-bearing set and made every finding auditable
  in a sentence.
- **Base rate:** on 8 strategies never selected for having tensions, 2 were
  flagged (25%). On the 7 selected for known tensions, 5 were (71%).

Cost: about $0.35 per review on Opus 5.5, roughly $5 per 15-strategy suite.
Cache write (not output) dominates spend.

Known limitation: slide selection is lexical, so a finding worded differently
from every query can still be missed. The reviewer is told to fetch a full
report before concluding it lacks a topic.
