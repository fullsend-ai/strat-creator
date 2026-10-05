# types/ — work-item type descriptors

One directory per work type, `types/<name>/type.yaml`, validated against `types/_schema/type.schema.json`
(`make lint` runs `scripts/validate_types.py`: schema, name/directory match, one tracker binding per type). The
vocabulary is rfe-creator's — the stations share configuration vocabulary, not
code — and the strat-specific blocks (`discovery`, `workspace`, `files`, `summary_prefix`,
`label_categories`, `status_enum`, `verdict_rules`, `lock`, `body_overflow`, `section_ownership`) use the key names
from rfe-creator PR #172 §3.5.

## Shipped types

| type | source → derived | status |
|---|---|---|
| `rfe-strategy` | RHAIRFE Feature Request → RHAISTRAT Feature (Cloners link) | today's pipeline, plus the `type:` stamp on task files; every value is a live literal with its `file:line @ cc1ba8c` |
| `initiative-strategy` | RHOAIENG Initiative → the same ticket (`relation.kind: self`) | draft twin; the open questions are marked inline |

## Selecting a type

rfe-creator's ladder, `scripts/type_registry.py` `resolve()`: `--type <name>`, else the artifact's frontmatter `type:`,
else the artifact's directory, else the ids' prefixes (a type's own key prefixes, then its inputs'), else
`rfe-strategy`. A headless or CI run (`STRAT_CREATOR_HEADLESS`, `CI` or `GITHUB_ACTIONS` set) never guesses: an id no
type owns is an error there. `list-rfe-ids.py --type` and the create, refine, review and sign-off skills (`--type` in
their arguments; refine falls back to the task file's `type:`) pick the type today; `generate-report.py` and
`extract-pipeline-data.py` list every type's files; the other scripts read `rfe-strategy`'s values, and a
comment in each says how it would pick the type per call.

## Self-describing artifacts

Strategy task files say their type in frontmatter, `type: rfe-strategy` (rfe-creator PR 184's field name and enum
rule). The writers stamp the files they write: the three `frontmatter.py set` blocks in strategy-create, and
`pull_strategy.py`, which writes the local task file whole on every pull; nothing back-fills a file no writer touches.
The field is appended after the fields the writer passes (schema defaults the CLI fills in follow it) and has no
default. Each type's schema allows only its own name and refuses ids and fields of another type. Review files are not
stamped: strategy-review and strategy-signoff attach them to Jira whole, and a checkout from before the stamp refuses a
field it does not know. `frontmatter.py` and the `artifact_utils.py` read/write helpers read the stamp: a strategy
file validates against its own type's schema, picked by its `type:`, else by the id its file name starts with
(`RHOAIENG-1.md`, `RHOAIENG-1-review.md`). `rfe-strategy`'s schemas keep the names `strat-task` and `strat-review`;
every other type's are `<type>-task` and `<type>-review` (rfe-creator's names, `frontmatter.py schema <name>`). There
is no `tracker_ref`: `jira_key` (`identity.tracker_key_field`) already holds the tracker key.

## Adding a type

1. Copy the closest descriptor to `types/<name>/type.yaml` and set `type: <name>` (it must equal the directory).
2. Change `identity` (the tracker binding must be unique across types), `inputs[0]` (upstream type, relation,
   skip_if), `discovery.settings` (a settings file in `config/pipeline-settings.yaml`'s shape: the intake gate, skip
   lists and batch size) and whatever the type does differently. Everything else is the station's — keep it. With
   `relation.kind: self` the strategy runs on the input ticket: strategy-create names the task file by its key and
   clones nothing.
3. `make lint`, then `make test-unit`. `tests/test_type_registry.py` lists the shipped types: add yours to
   `SHIPPED_ALL` and bump the counts in `test_shipped_types` and `test_gate1_passes_on_the_shipped_types`.
4. Select it with `--type <name>`: `list-rfe-ids.py` and the create, refine, review and sign-off skills take it. The
   strategy-file schemas follow a file's `type:` or its id (Self-describing artifacts), and strategy-refine a file's
   `type:` when `--type` is absent;
   `python3 scripts/type_registry.py resolve` shows what the other steps would resolve to.
5. Most scripts read `rfe-strategy`'s values, not the selected type's (see Selecting a type), and
   `tests/test_type_registry_pins.py` lists the values that scripts, skills and `CLAUDE.md` still carry as literals.
   Until a consumer picks the type per call, a new type's value has no effect there. The strategy-file schemas are
   the exception: each type's come from its own descriptor (`identity`, `inputs[0].source_ref_field`, `schema`).

## Inspecting

```bash
python3 scripts/type_registry.py list
python3 scripts/type_registry.py show <type> [--json]
python3 scripts/type_registry.py get <type> <dotted.path> [--json]
python3 scripts/type_registry.py resolve [--type T] [--artifact PATH] [--headless] [--json] [ID ...]
```
