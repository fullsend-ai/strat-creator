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
| `rfe-strategy` | RHAIRFE Feature Request → RHAISTRAT Feature (Cloners link) | today's pipeline, byte-for-byte; every value is a live literal with its `file:line @ cc1ba8c` |

## Selecting a type

rfe-creator's ladder, `scripts/type_registry.py` `resolve()`: `--type <name>`, else the artifact's frontmatter `type:`,
else the artifact's directory, else the ids' prefixes (a type's own key prefixes, then its inputs'), else
`rfe-strategy`. A headless or CI run (`STRAT_CREATOR_HEADLESS`, `CI` or `GITHUB_ACTIONS` set) never guesses: an id no
type owns is an error there. `list-rfe-ids.py --type` and the review and sign-off skills (`--type` in their
arguments) pick the type today; `generate-report.py` and `extract-pipeline-data.py` list every type's files; the other
scripts read `rfe-strategy`'s values, and a comment in each says how it would pick the type per call.

## Adding a type

1. Copy the closest descriptor to `types/<name>/type.yaml` and set `type: <name>` (it must equal the directory).
2. Change `identity` (the tracker binding must be unique across types), `inputs[0]` (upstream type, relation,
   skip_if), `discovery.settings` (a settings file in `config/pipeline-settings.yaml`'s shape: the intake gate, skip
   lists and batch size) and whatever the type does differently. Everything else is the station's — keep it.
3. `make lint`, then `make test-unit`.
4. Select it: `--type <name>`, or ids it owns.
5. Not every value is read yet: `tests/test_type_registry_pins.py` lists the fields scripts still carry as literals.
   Until that consumer migrates, a new type's value for the field has no effect.

## Inspecting

```bash
python3 scripts/type_registry.py list
python3 scripts/type_registry.py show <type> [--json]
python3 scripts/type_registry.py get <type> <dotted.path> [--json]
python3 scripts/type_registry.py resolve [--type T] [--artifact PATH] [--headless] [--json] [ID ...]
```
