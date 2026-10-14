# strat-creator

Takes approved RFEs, which describe the WHAT and WHY, and produces the HOW: actionable implementation strategies grounded in real platform architecture. The pipeline checks technical feasibility against architecture context and scores every strategy so the team knows what's ready and what needs attention.

## What This Does

Given an approved RFE (from the `rfe-creator` pipeline), this pipeline:

1. **Creates** a strategy stub from the RFE data (`strategy-create`)
2. **Refines** the stub into a structured strategy using architecture context (`strategy-refine`)
3. **Reviews** the strategy across 4 dimensions — feasibility, testability, scope, architecture (`strategy-review`)
4. **Human sign-off** — a staff engineer or architect reviews the approved strategy and marks it feature-ready (`strategy-signoff`)

Steps 1–3 run in CI. Step 4 is a human workflow using a separate `local/` workspace.

## Workflows

### CI Pipeline (automated)

The CI pipeline runs `strategy-create` → `strategy-refine` → `strategy-review` in sequence. Each step runs in its own Claude session with artifacts on disk as the handoff. Output lands in `artifacts/`.

Strategies that score **6+ total (no zeros)** get `strat-creator-rubric-pass`. Everything else gets `strat-creator-needs-attention` and waits for a human.

### Human Review (local)

After CI finishes, humans use a separate `local/` workspace to review and iterate without interfering with CI:

```
/strategy-pull RHAISTRAT-1520     # Pull post-CI strategy into local/
/strategy-refine                  # Iterate locally (reads from local/, skips Jira writes)
/strategy-review                  # Re-score locally
/strategy-push RHAISTRAT-1520    # Resubmit needs-attention strategies to CI
/strategy-signoff RHAISTRAT-1520  # Sign off rubric-pass strategies as feature-ready
```

**Two paths depending on CI verdict:**

| CI Verdict | Label | Human Workflow |
|------------|-------|----------------|
| Approved | `strat-creator-rubric-pass` | pull → review locally → `/strategy-signoff` |
| Needs attention | `strat-creator-needs-attention` | pull → fix inputs → refine/review locally → `/strategy-push` → wait for CI → `/strategy-signoff` |

The `strategy-refine` and `strategy-review` skills auto-detect local mode when files are in `local/` — they skip Jira writes and the pipeline label gate.

See [Human Review Guide](docs/human-review-guide.md) for the full walkthrough.

## RFE Discovery and Filtering

The pipeline supports two modes for selecting which RFEs to process:

1. **JQL mode** (`--jql-default` or `--jql`): Queries Jira directly using labels and statuses defined in `config/pipeline-settings.yaml`. Handles batching and pre-filtering automatically.
2. **Config file mode** (`--config`): Reads RFE IDs from a manually curated YAML batch file.

### JQL Pre-Filtering

In JQL mode, RFEs go through a two-stage filter before reaching the skills:

**Stage 1 — Jira-side (JQL query):**
- Must be in `RHAIRFE` project
- Must have at least one label in `jql.required_labels` or a Target Version in `jql.target_versions`
- Must have at least one label in `jql.quality_labels` when that list is configured
- Must NOT have a status in `jql.excluded_statuses`
- Ordered by `key ASC` for deterministic batching

**Stage 2 — Pre-filter (before batching):**
Queries RHAISTRAT to find RFEs that already have processed or active strategies, then removes them so batch slots aren't wasted. An RFE is excluded if any of its STRATs (via Cloners links):
- Have a skip label: `strat-creator-rubric-pass` or `strat-creator-needs-attention`
- Are in an active/completed status: `In Progress`, `Review`, `Refinement`, `Release Pending`, `Closed`, `Resolved`

After both stages, the first `batch_size` (default 10) remaining RFEs are selected.

### Known Pre-Filter Gaps

The pre-filter is conservative but not perfectly aligned with skill-level gates:

| Scenario | Pre-filter | Skill | Impact |
|---|---|---|---|
| Multiple open STRATs (both New) | Passes | Skips | Wastes a batch slot (rare — 1-2 known cases) |
| Mixed-status STRATs (one Closed + one New) | Excludes | Would import New one | Under-processes (safe direction) |
| Just-created STRAT, not yet refined | Passes | Re-imports (idempotent) | Only if pipeline crashes mid-run |

Use `--include-processed` to bypass pre-filtering when needed.

### Configuration

All filter parameters are externalized in `config/pipeline-settings.yaml` — no hardcoded labels in code.

```bash
# List all matching RFEs
python3 scripts/list-rfe-ids.py --jql-default

# First batch of 10
python3 scripts/list-rfe-ids.py --jql-default --batch-size 10

# Next batch of 10
python3 scripts/list-rfe-ids.py --jql-default --batch-size 10 --batch-offset 10

# Raw JQL override
python3 scripts/list-rfe-ids.py --jql 'project = RHAIRFE AND labels = "strat-creator-3.5"'

# Config file mode (legacy)
python3 scripts/list-rfe-ids.py --config config/road-to-production/batch-07.yaml
```

## Implementation Status

| Component | Type | Description |
|-----------|------|-------------|
| `strategy-create` | Skill | Creates strategy stubs from approved RFEs, saves original RFE snapshots |
| `strategy-refine` | Skill | Adds technical approach using architecture context, size-scaled templates |
| `strategy-review` | Skill | Scores via `strat-scorer` agents, then runs 4 independent prose reviewers |
| `strategy-feasibility-review` | Skill | Technical viability and effort credibility |
| `strategy-testability-review` | Skill | Measurable criteria and edge cases |
| `strategy-scope-review` | Skill | Right-sizing and scope boundaries |
| `strategy-architecture-review` | Skill | Platform fit and dependency correctness |
| `strategy-pull` | Skill | Pull a post-CI strategy from Jira into `local/` for human review |
| `strategy-push` | Skill | Resubmit a needs-attention strategy to CI after local fixes |
| `strategy-signoff` | Skill | Sign off a rubric-pass strategy as feature-ready |
| `assess-strat` | Skill | Score a single strategy or directory against the in-repository quality rubric |
| `export-rubric` | Skill | Export scoring rubric to `artifacts/strat-rubric.md` |
| `strat-scorer` | Agent | Restricted agent (Read/Write/Glob/Grep only) for scoring strategies |
| `generate-report.py` | Script | HTML report with summary table and drill-down details |
| `generate-dashboard.py` | Script | Dashboard with aggregate stats across batch runs |

## Project Structure

```
strat-creator/
├── scripts/                # Python/shell scripts (Jira, frontmatter, state, reports)
│   ├── frontmatter.py          # YAML frontmatter read/write/schema
│   ├── state.py                # State persistence for long-running skills
│   ├── apply_scores.py         # Apply scorer results to review frontmatter
│   ├── fetch_issue.py          # Jira REST API fallback
│   ├── jira_utils.py           # Jira API, JQL search, pre-filtering
│   ├── list-rfe-ids.py         # RFE discovery (JQL, config, batching)
│   ├── find_strat_for_rfe.py   # Deterministic STRAT lookup via Cloners links
│   ├── pull_strategy.py        # Pull RHAISTRAT from Jira into local/
│   ├── fetch-architecture-context.sh
│   ├── assess-strat/                # In-repository scoring rubric and scripts
│   ├── generate-report.py      # Per-run HTML report
│   └── generate-dashboard.py   # Aggregate dashboard across runs
├── .claude/
│   ├── skills/                 # Claude Code skills (pipeline steps + reviewers)
│   └── agents/                 # Agent definitions
│       └── strat-scorer.md         # Restricted scorer agent
├── config/                 # Pipeline config and batch files
│   ├── pipeline-settings.yaml  # JQL filters, batch size, skip labels, excluded statuses
│   ├── road-to-production/     # Road-to-production batch YAML files
│   ├── engineering35-batches/  # Engineering 3.5 batch YAML files
│   ├── jen-batches/            # Jen batch files for dry runs
│   └── *.yaml                  # Individual RFE lists
├── .context/               # Fetched at runtime (gitignored)
│   └── architecture-context/   # RHOAI platform architecture docs
├── local/                  # Human review workspace (gitignored, mirrors artifacts/ structure)
│   ├── strat-tasks/            # Pulled strategy files (workflow: local)
│   ├── strat-reviews/          # Pulled/generated review files
│   └── strat-originals/        # RFE context for pulled strategies
└── artifacts/              # Pipeline output (gitignored)
    ├── strat-tasks/            # Strategy documents with YAML frontmatter
    ├── strat-reviews/          # Review files + review comments
    ├── strat-originals/        # Original RFE snapshots
    ├── strat-rubric.md         # Exported scoring rubric
    └── pipeline-report.html    # Latest HTML report
```

## Documentation

- [Human Review Guide](docs/human-review-guide.md) — How staff engineers and architects handle strategies flagged by the pipeline
- [Dashboard](https://strat-dashboard-0f1209.gitlab.io/) — Live dashboard with aggregate stats, per-run trends, and pipeline diagram
- [JSON API](https://strat-dashboard-0f1209.gitlab.io/summary.json) — Aggregated pipeline data for external consumers
  - Per-run data: `https://strat-dashboard-0f1209.gitlab.io/runs/<timestamp>.json` (e.g. `runs/20260419-093253.json`)

## Development

### Setup

```bash
uv sync
```

### Running Tests

```bash
# All tests
make test

# By category
make test-unit          # Unit tests (schemas, frontmatter, scores, JQL, state)
make test-integration   # Integration tests (jira-emulator)
make test-e2e           # E2E pipeline replay (two scenarios)
make test-mermaid       # Mermaid workflow diagram validation

# Or directly via pytest
uv run pytest tests/ -v --tb=short
```

Integration and E2E tests use [jira-emulator](https://github.com/jctanner/jira-emulator) — a real HTTP server started in-process — so no external Jira instance is needed.

### CI

Tests run automatically on PRs and pushes to `main` via GitHub Actions (Python 3.11 + 3.12 matrix). See `.github/workflows/tests.yml`.

## Related Projects

- **rfe-creator** — Phase 1: RFE assessment pipeline (upstream). Has `strat.*` skill stubs that these skills were forked from.
- **strat-pipeline** (GitLab) — CI runner for this pipeline
- **strat-pipeline-data** (GitLab) — Data repo with timestamped run artifacts and JSON outputs
- **strat-dashboard** (GitLab) — GitLab Pages site serving the dashboard and JSON API

### Fullsend CI progress

The script entrypoint forwards its readable stdout to CI and retains it in the
iteration output artifact. The Claude renderer supports completed assistant
messages and partial event streams without printing a response twice. Tool
summaries remain compact; transcripts retain complete tool inputs and results.
The integration caller masks credentials before forwarding live logs.

The Fullsend strategy entrypoint removes the unused `/tmp/strat-assess`
additional-directory permission from its disposable checkout before starting
Claude. That directory belongs to local assessment tooling. Other project
settings and Fullsend security hooks are preserved. The entrypoint merges
this project's trust acceptance into
`$CLAUDE_CONFIG_DIR/.claude.json` with an atomic, owner-only write, preserving
existing config. Missing config is created; malformed config fails startup.
Each Claude invocation retains a separate stderr artifact and prints it once.
Historical working stderr logs are cleared after cloning results.

The entrypoint fetches architecture context before starting Claude, with a
180-second setup timeout. stdout/stderr and exit status are visible in the job
trace and retained as `architecture-context-fetch.log` and
`architecture-context-fetch.exit-code`. Fetch failure remains optional under
the existing skill contract; it is reported explicitly. GitHub API failures
include curl's HTTP error instead of being hidden by JSON parsing.
