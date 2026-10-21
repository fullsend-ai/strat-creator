# Strat Creator

Skills for creating, refining, and reviewing strategy documents from approved RFEs.

## Artifact Conventions

All skills read from and write to the `artifacts/` directory.

```
artifacts/                      # CI pipeline output (gitignored)
  strat-tasks/                    # Strategy files with YAML frontmatter
    STRAT-001.md
    RHAISTRAT-400.md
  strat-reviews/                  # Per-strategy review files with YAML frontmatter
    STRAT-001-review.md
    RHAISTRAT-400-review.md
  strat-originals/                # Original RFE snapshots at time of strategy creation
    RHAIRFE-1595.md
  strat-tickets.md                # RHAISTRAT ticket mapping after cloning

local/                          # Human review workspace (gitignored, mirrors artifacts/ structure)
  strat-tasks/                    # Pulled strategy files (workflow: local)
  strat-reviews/                  # Pulled/generated review files
  strat-originals/                # RFE context for pulled strategies
```

### Frontmatter

All task and review files use YAML frontmatter for structured metadata. Skills must use `scripts/frontmatter.py` to read schemas, set fields, and read validated data — never write YAML by hand.

```bash
# Get schema for a file type
python3 scripts/frontmatter.py schema strat-task
python3 scripts/frontmatter.py schema strat-review

# Set/update frontmatter on a file
python3 scripts/frontmatter.py set <path> field=value field=value ...

# Read validated frontmatter as JSON
python3 scripts/frontmatter.py read <path>
```

### State Persistence

Long-running skills use `scripts/state.py` to persist state to `tmp/` files so it survives context compression.

```bash
python3 scripts/state.py init <file> key=value ...
python3 scripts/state.py set <file> key=value ...
python3 scripts/state.py set-default <file> key=value ...
python3 scripts/state.py read <file>
python3 scripts/state.py write-ids <file> ID ...
python3 scripts/state.py read-ids <file>
python3 scripts/state.py timestamp
python3 scripts/state.py clean
```

### File Naming

- **Cloned from Jira**: Use Jira key as filename (e.g., `RHAISTRAT-400.md`)
- **Local pre-submission**: Use `STRAT-NNN.md` naming
- **On submit**: `STRAT-NNN.md` files are renamed to `RHAISTRAT-NNNN.md`

## Pipeline Gates

Gate logic (label checks, skip conditions) is duplicated across all three skills: `strategy-create`, `strategy-refine`, and `strategy-review`. When changing a gate, update all three skills to keep them consistent.

## Jira Integration

### Read Operations

Read operations support two modes:

1. **Atlassian MCP server** (preferred when available)
2. **REST API fallback** via `python3 scripts/fetch_issue.py` using `JIRA_SERVER`/`JIRA_USER`/`JIRA_TOKEN` env vars

### Write Operations

Skills that change Jira issues use `scripts/jira_utils.py` with the
`JIRA_SERVER`, `JIRA_USER`, and `JIRA_TOKEN` environment variables. Use the
provided helpers for comments, labels, and transitions rather than making raw
REST calls. The `/strategy-reject` skill closes a pipeline-created strategy
and returns its source RFE for human rework.

## Jira Field Mappings

### RHAISTRAT Project
- **Project**: `RHAISTRAT`
- **Issue Type**: `Feature`
- **Clone link type**: `Cloners` (outward: "clones", inward: "is cloned by")
- **Related link type**: `Related`

### RHAIRFE Project (source — read only)
- **Project**: `RHAIRFE`
- **Issue Type**: `Feature Request`

## Testing

### Implementation and test changes -- run repository tests

Run the repository test suite when **executable implementation, test, configuration, or build files** change. These include files in `scripts/`, `tests/`, `Makefile`, and `pyproject.toml`. Use `make test-unit` for changes to scripts or library code. Use `make test` to run all tests when integration/E2E tests are also relevant. Never skip this step -- a change is not done until tests pass.

**Always run `make test` (full suite including integration tests) before pushing repository code to remote.** Unit tests alone are not sufficient -- the jira-emulator integration tests catch issues that unit tests miss.

### Agent instruction changes -- run integrity checks

Changes to `CLAUDE.md` or `.claude/skills/*/SKILL.md` modify agent behavior, not executable code. They do not require `make test-unit` or `make test` on their own. Instead, run the skill-integrity tests that validate instruction structure: `make test-unit` includes `test_skill_integrity.py`, which checks symlinks, script references, and script quality. Run that subset when only instruction files changed.

### Documentation-only changes -- no repository tests required

Changes limited to files under `docs/` (bug reports, ledger entries, plans, ADRs, notes) are passive documentation and do not require any repository test command unless they accompany implementation changes in the same commit.

### Workflow output -- no repository tests required

Changes limited to the following are **workflow output, not code changes**, and do NOT require `make test`, `make test-unit`, `pytest`, or any repository test command:

- Strategy artifacts in `artifacts/` or `local/` (strat-tasks, strat-reviews, strat-originals, strat-skipped.md, strat-tickets.md)
- Frontmatter and strategy-history updates on artifact files
- Review attachments and review comment files
- Jira field updates, label changes, and comment posts
- Architecture context fetches into `.context/`
- State files in `tmp/`

Strategy skills (`strategy-create`, `strategy-refine`, `strategy-review`) produce only workflow output during normal operation. Do not run repository tests after completing a strategy workflow unless the workflow also modified a repository implementation file listed above.

## Architecture Context

Strategy skills fetch architecture context from opendatahub-io/architecture-context into `.context/architecture-context/`. Used during refinement and review to ground feedback in real platform architecture.

```bash
# Fetch from remote (default)
bash scripts/fetch-architecture-context.sh

# Use a local checkout (e.g., to test overlays before pushing)
bash scripts/fetch-architecture-context.sh /path/to/local/architecture-context
```

When a local path is provided, the script copies the local architecture context into `.context/architecture-context/` instead of cloning from remote. This lets staff engineers test overlay changes locally before pushing upstream.

### Architecture Context Overlays

Overlays are cross-strategy architectural patches that live in the `overlays/` directory of the architecture-context repo. They capture facts that emerged between architecture context regeneration cycles (version bumps, maturity changes, dependency shifts). The fetch script includes `overlays/` in the sparse checkout automatically.

See the [Overlays README](https://github.com/opendatahub-io/architecture-context/blob/main/overlays/README.md) for the format and lifecycle.

## Eval Dataset Anonymization

Files under `eval/dataset/` are built by `eval/scripts/build_dataset.py` from real RHAIRFE→RHAISTRAT triples and **must be anonymized** before they are committed or shared (they are git-ignored precisely because the raw build contains internal content). Never include real customer/partner names, individual names, email addresses, internal Slack / Google Docs / Miro / workshop links, or other personally identifiable or confidential information.

Replace each with a fictional equivalent. The two directions have opposite rules, so keep them apart:

**Replacements you invent — always web-search, every one.** A made-up name is not derived from the source data, so querying it discloses nothing, and it is the only check that catches a placeholder colliding with a real company or person (e.g. "Meridian Bank" → "Cobalt National Bank" only after verifying the latter is fictional). This is not hypothetical: rfe-creator shipped six "fictional" names that turned out to be real companies and had to redo them. No match is necessary but not sufficient, so prefer clearly invented compounds over plausible-sounding ones.

**Originals from the data — keep them out of search engines.**

- **Never search** email addresses, usernames, internal links, ticket contents, or any string combined with context (the product, "customer", the RFE text). A name plus context is a disclosure even when the name alone is public.
- **Never search a person's name to find out whether they are real.** Individuals are replaced unconditionally, so there is nothing to verify and no reason to send the name anywhere.
- An organisation may be checked with a **bare-name query only**, and only when it is genuinely unclear whether the name is already fictional.

This applies to test inputs, annotations, and all reference files. Keep names consistent across files (same real entity → same fictional entity; derive handles/nicknames from the person's new name).

**Kept as-is (not PII):** Red Hat and its products, upstream/OSS projects, public competitor products, generic role/team names (e.g. PSAP, Platform/Monitoring), RHAIRFE/RHAISTRAT ticket keys, and public GitHub/docs URLs.
