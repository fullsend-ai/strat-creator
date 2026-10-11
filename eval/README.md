# strat-creator eval

An [agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness) eval
that covers the **end-to-end strat-creator pipeline** and **differentiates strategy
quality across models**.

For each approved RFE, it reproduces the production `strat-pipeline` flow —
`strategy-create` → `strategy-refine` → `strategy-review` — then scores the refined
strategy with an independent, held-constant judge using the same **assess-strat**
rubric the pipeline itself uses (Feasibility / Testability / Scope / Architecture,
0–2 each = **/8**).

## How it works

- **Runner + steps:** the native `claude-code` runner with a two-step
  `execution.steps` pipeline. `strategy-create` is mechanical (a verbatim RFE copy),
  so its output stub is staged by a `before_each` hook; the two LLM-driven,
  quality-determining stages run as **native steps** — `refine` then `review` —
  sharing one per-case workspace and handing off through `artifacts/` on disk,
  exactly like prod. Each step is a fresh Claude session (real topology), so
  cross-session handoff behaviour is preserved.
- **Instrumentation:** the harness captures each step's stream-json → `events.json`
  (`traces.events: true`) — giving judges the reasoning/tool trace — plus **subagent
  transcripts** (`cases/<case>/subagents/*.jsonl`, written by the harness SubagentStop
  hook; this is where a `context: fork` skill's tool calls live, and what
  `architecture_context_used` reads) and **per-step cost/tokens
  in `run_result.json`** (summed per case, including the scorer + all 4 forked
  reviewers). No hand-rolled `metrics.json` and no cli driver: the harness owns
  staging, execution, collection, and metrics natively.
- **Hermetic:** every step runs `--dry-run` (no Jira writes) and the stub has
  `jira_key=null` (Jira label gates are bypassed), so no live Jira. `assess-strat`
  and `architecture-context` are staged from `eval/.assets/` (by the `before_all`
  hook) into each case workspace by the `before_each` hook — which rebuilds a real
  (not symlinked) `.claude/skills` + `.context` so the strat skills' bootstrap writes
  stay inside the throwaway workspace, never back into the repo.
- **Environment parity:** the case workspace is made to look like prod CI, not like a
  developer laptop. The `before_each` hook pre-stages the `strat-scorer` agent into
  `.claude/agents/`, because Claude Code registers agent types at *startup*: the
  review skill's mid-session bootstrap is too late, and the scorer would silently
  fall back to a generic agent.
- **Permissions:** isolated workspaces are untrusted, so recent Claude Code drops the
  workspace `settings.json` allow-list. The runner uses `permission_mode: dontAsk`
  with a trust-independent allow-list (`permissions.allow` → `--allowed-tools`). Keep
  that list COMPLETE — in `dontAsk` mode every sub-command of a compound Bash call
  must prefix-match a rule, or the call is silently denied.
- **One case = one RFE** (`execution.mode: case`) — the natural repeated-measures
  unit for `/eval-anova`.
- **Model under test** is `models.skill` (default `claude-opus-4-6`, the prod model;
  override with `--model`). The **judge** (`models.judge`, default `claude-opus-4-8`)
  is held constant across candidates to avoid self-preference bias.

## Judges

| Judge | Type | What it measures |
|---|---|---|
| `feasibility_score` / `testability_score` / `scope_score` | LLM (0–2, `samples: 3`) | Independent per-dimension rubric score of the refined strategy — **the model-differentiation signal**. Feasibility carries most of it (real strategies cluster at 1 there). |
| `architecture_score` | **agent** (0–2, `samples: 3`) | Grounded Architecture score — a native `agent:` judge (held-constant judge model run as a *tool-using agent* with `[Read, Grep, Glob]`) that Greps/Reads `.context/architecture-context` to validate the strategy's components/CRDs/APIs. A plain LLM judge can't open the docs and inflates this to ~2.0; the agent grounds it. The harness stages an isolated workspace (strategy under `./strat-tasks/`, docs read-only under `./.context/architecture-context`) and reads `output/score.json`. |
| `pipeline_total` | check (0–8) | The pipeline's **own** assess-strat total (from the review frontmatter) — reported for pipeline-vs-independent-judge agreement. |
| `files_exist`, `strategy_nonempty` | check (gate) | The pipeline produced a filled strategy + a scored review. |
| `frontmatter_valid` | check (gate) | Review frontmatter matches the `strat-review` schema (4 int scores, total = sum, valid enums). |
| `recommendation_consistency` | check (gate) | The pipeline's recommendation matches the deterministic assess-strat verdict of its own scores. |
| `architecture_context_used` | check (subagent transcripts) | **`strategy-refine` itself** consulted the architecture-context docs. Attributed to the refine step by session id, so the reviewers reading the docs while grading can't be mistaken for grounding. |
| `cost_budget` | builtin | Per-case pipeline cost (refine + review, summed across steps in `run_result.json`); Pareto input. |

The independent per-dimension total = sum of the four judge means. The four judge
prompts (`prompts/*-judge.md`) are the verbatim assess-strat rubric with its
calibration anchors, so the eval scores on the same axis production CI uses.

## Prerequisites

- The `claude` CLI on `PATH`, authenticated the same way prod runs it (Vertex:
  `CLAUDE_CODE_USE_VERTEX=1` + `ANTHROPIC_VERTEX_PROJECT_ID`; or an Anthropic API key).
- Read access to the internal prod data repo **`strat-pipeline-data`** (for building
  the dataset) and to **`assess-strat`** + **`architecture-context`** (staged assets).
- The **agent-eval-harness plugin** providing `/eval-run`, `/eval-anova`,
  `/eval-compare` — recent enough to include the native **`agent:` judge** type
  (PR #170) and **`execution.steps`** multi-step support (PR #172). Both are merged
  to `main`, so a current plugin has them; update an older install.
  **Pin it for comparison runs.** Harness changes can alter step semantics, judge
  behaviour or tool permissions, so a moving `main` silently makes two runs
  incomparable. Check out a fixed harness commit/release for a comparison series and
  record that revision (plus the `claude` CLI version) alongside the run.

## Setup (one-time)

The dataset and staged assets are **git-ignored** — they contain internal RFE and
strategy content and are rebuilt locally.

```bash
# 1) Clone the internal prod data repo (source of real RFE snapshots + reference
#    scores). Ask the strat-pipeline maintainers for its location; the build script
#    takes any checkout via --data-repo.
# The checkout holds internal RFE/strategy content, so keep it private: a
# predictable /tmp path with the usual umask 022 is readable by other local users.
DATA_DIR="$(umask 077 && mktemp -d)"
git clone --depth 1 <strat-pipeline-data repo> "$DATA_DIR"

# 2) Build the ~24-case stratified dataset from real prod triples
python3 eval/scripts/build_dataset.py --data-repo "$DATA_DIR/RHAISTRAT"   # --force to rebuild

# 2b) Remove the internal checkout once the dataset is built
rm -rf "$DATA_DIR"

# 3) Stage read-only assets (assess-strat rubric + architecture-context)
bash eval/scripts/stage-assets.sh                # also runs from the before_all hook if missing
```

The dataset spans the full quality range (rubric totals 1–8, all sizes S–XL,
including the scarce reject / zero-dimension cases) so models actually separate. The
curated RFE key list lives in `eval/scripts/build_dataset.py` (`CURATED`).

## Run

```bash
# Cheap smoke test first — one small (S) case; --cases takes EXACT case IDs (space-separated)
/eval-run --config eval/strat-refine.yaml --model claude-opus-4-6 \
  --cases RHAIRFE-912-configuration-persistence-for-gen-ai-studio

# Full run against the prod model (all 24 cases)
/eval-run --config eval/strat-refine.yaml --model claude-opus-4-6
```

### Initiative strategies

`eval/strat-refine-initiative.yaml` is the same eval for `initiative-strategy` (the strategy
is written on the Initiative itself): the steps add `--type initiative-strategy` and the
cases live in `dataset/initiative-cases/`. Everything else (judges, thresholds, hooks,
permissions) is the RFE eval's, and `tests/test_eval_checks.py` keeps it that way. Build
its cases from the data repo's `initiative-strategy/` folder, which strat-pipeline's
Initiative jobs write; every Initiative found becomes a case (there is no curated list yet):

```bash
python3 eval/scripts/build_dataset.py --type initiative-strategy --data-repo "$DATA_DIR/initiative-strategy"
/eval-run --config eval/strat-refine-initiative.yaml --model claude-opus-4-6
```

The judge prompts still name the RFE's `## Business Need (from RFE)` heading; an Initiative
stub has `## Business Need (from Initiative)`.

Each run writes `eval/runs/<run-id>/` with `summary.yaml`, `report.html`,
`run_result.json`, and per-case artifacts (incl. per-step `steps/<id>/` traces).
Reports show per-dimension judge means, the pipeline's own totals, the deterministic
gates, and cost.

### Cost controls (two layers)

1. **Per-step hard cap** — `execution.max_budget_usd` (default 8.0) is passed to each
   step's `claude --max-budget-usd`, which **aborts a step** that exceeds it. It's
   per-step, and a case runs two steps (refine + review), so worst case is ~2×. Set
   it as a **runaway guard**, not a tight budget (a review that hits the cap
   mid-scoring truncates and fails that step). Per-step budget/timeout are enforced
   natively by the harness.
2. **Per-case total gate** — the `cost_budget` judge reads the summed per-case cost
   (refine + review, including subagents) from `run_result.json` and flags cases over
   `max_cost_usd` (default 12.0). Post-hoc detection + a Pareto input. The
   `architecture_score` agent judge's cost is attributed to that judge, not the case.

Calibrate both after the smoke test reveals real per-case cost. Ballpark: each case
is two opus steps (review forks a scorer + 4 prose reviewers) **plus** the grounded
architecture agent judge (`samples: 3`), so the full 24-case run is substantial
(order of an hour, tens of dollars). **Start small.**

## Model comparison (Phase 2)

Uncomment the `matrix:` block in `eval/strat-refine.yaml` and run:

```bash
/eval-anova --config eval/strat-refine.yaml       # sweeps the model factor with replications
/eval-compare eval/runs                   # side-by-side model comparison report
```

`/eval-anova` reports per-judge **F / p / effect-size** (accounting for case
difficulty) and a cost/quality **Pareto** view — i.e. *which model produces better
strategies, and is the difference significant.* The judge model stays fixed across
all cells. Add `--baseline <run-id>` to activate the blind `pairwise` judge
(`prompts/pairwise-judge.md`).

## Files

```text
eval/
  strat-refine.yaml                 # harness config (claude-code runner, execution.steps, hooks, judges, matrix[commented])
  strat-refine-initiative.yaml      # the same for initiative-strategy (--type, dataset/initiative-cases)
  prompts/*.md              # rubric judges + pairwise. Architecture has TWO prompts:
                            #   architecture-agent-judge.md  ACTIVE  (grounded agent judge, wired in strat-refine.yaml)
                            #   architecture-judge.md        legacy  (text-only, inflates to ~2.0; kept for the cheap variant)
  scripts/build_dataset.py  # materializes dataset/cases from strat-pipeline-data
  scripts/stage-assets.sh   # before_all hook: stages assess-strat + architecture-context into .assets/
  scripts/stage-case.sh     # before_each hook: rebuilds the per-case project tree + stages the create stub
  dataset/cases/<id>/       # (git-ignored) input.yaml, stub.md, rfe-original.md, reference/, annotations.yaml
  dataset/initiative-cases/ # (git-ignored) the same for initiative-strategy
  .assets/                  # (git-ignored) assess-strat + architecture-context
  runs/                     # (git-ignored) eval run outputs
```

## Fidelity notes & limitations

- **Create is staged, not LLM-run.** `strategy-create` copies the RFE verbatim into a
  stub and sets frontmatter — mechanical work that doesn't generate strategy content,
  so a `before_each` hook stages its output deterministically, preserving the real
  create→refine handoff while focusing the eval on the stages that determine quality.
  A full create-via-emulator mode (seeding the repo's jira-emulator) is a possible
  follow-up for end-to-end-including-create fidelity.
- **The Architecture judge is a native agent judge.** It Greps/Reads the real arch
  docs to validate claims, so it doesn't inflate to 2.0 like a plain LLM judge would.
  The old text-only prompt is kept at `prompts/architecture-judge.md` if you want the
  cheap variant. The first full run (2026-07-31) used the text-only judge — its
  A-scores (mean 2.0) are inflated; re-score with the agent judge for a trustworthy
  Architecture number.
- **`expected_*` in `annotations.yaml` are single-sample prod references** — use them
  as calibration anchors (with `score_tolerance`), not hard gates.
- **`architecture_context_used` depends on subagent capture.** `strategy-refine` is a
  `context: fork` skill, so its tool calls land in a subagent transcript rather than
  `steps/refine/stdout.log`, and with `execution.steps` the case-level `stdout.log`
  is the *last* step's (review) — so an events-based check sees only the review
  step's own top-level calls (Bash touching the docs directory, Agent invocations
  naming it), never refine, and never even the reviewers' actual reads, which live in
  the subagent transcripts. The gate instead reads
  `cases/<case>/subagents/*.jsonl` and keeps the transcripts whose `sessionId` matches
  the refine step's own `session_id`, which pins them to that step (the review step
  always has a different one) and to that run — the collected `subagents/` directory
  is only ever added to, so **re-running an existing run-id leaves earlier transcripts
  in place** (this is part of what preflight's DIRTY warning is protecting you from).
  It goes red if the SubagentStop hook is ever disabled: no transcripts, no evidence.
  That is
  deliberate — the run genuinely lost its instrumentation, and that should surface
  rather than be papered over by counting the reviewers' reads.
- **Never set `disableAllHooks` in a case workspace.** The harness installs its
  SubagentStop capture hook into the workspace `.claude/settings.json`, and
  `settings.local.json` outranks it, so a blanket disable silently deletes the
  subagent traces (it did: 153 transcripts in one run, zero in the five after).
  Disable unwanted plugins via `enabledPlugins` instead — and put it in
  `settings.json` (e.g. through `runner.settings`), because Claude Code drops
  `enabledPlugins` from local scope unless the key already exists in `settings.json`
  ([#27247](https://github.com/anthropics/claude-code/issues/27247)). Note there is
  no wildcard for "all plugins"
  ([#20873](https://github.com/anthropics/claude-code/issues/20873)) and an absent
  entry means *enabled* — it falls back to the plugin's `defaultEnabled` — so every
  id has to be listed. Disabling all of them is safe for this eval: skills, agents
  and assess-strat are staged from the repo, so no plugin is needed inside a case.
- **Assets are fetched from moving branches.** `stage-assets.sh` clones `assess-strat`
  and fetches `architecture-context` at HEAD, so a rubric or docs change between runs
  can move scores independently of the model under test. Pin both (and the harness,
  above) for a comparison series.
- Pin the `claude` CLI version if you need run-to-run reproducibility (prod doesn't).

## Robustness coverage

- **[argument-parsing-coverage.md](argument-parsing-coverage.md)** — proposed judges
  (`refine_recognized_arg`, `strat_id_honored`) + matrix wiring to cover the flaky
  `/strategy-refine` argument-parsing refusal (RHAIFIRST-399), and the multi-step
  gotcha (a judge sees the *review* step, not refine's refusal — read
  `steps/refine/stdout.log`).
