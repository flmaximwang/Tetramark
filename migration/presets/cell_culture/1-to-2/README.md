# cell_culture 1 → 2 — BioManager's cell-line and biosample extras

Counter: `ledger_meta.preset_versions.cell_culture` 1 → 2. Tag before writing:
`pre-migrate-cell_culture-1`. One commit: `migrate(ledger): cell_culture 1 -> 2`.

## What changes

Cell-culture preset version 2 (ticket 28, design §2.1) adds what
[BioManager](https://github.com/gaspolymerase/biomanager) (MIT) records about a cultured line and the
material taken from it:

| object | change |
|---|---|
| `stock_cell_vial` | new columns `selection` (VARCHAR), `mycoplasma` (enum `not_tested` / `negative` / `positive`, default `not_tested`) and `mycoplasma_tested_on` (DATE); `statuses: cryo` is unchanged |
| `stock_cell_biosample` | new kind: a biosample taken from a cell line (`statuses: tube`, `entity_kinds: [cell_line]`, fields `sample_type`, `tissue`, `collected_on`, `storage_temp_c`), plus its `v_stock_cell_biosample` view |
| views / `dolt_tests` | regenerated from the v2 schema (the new `cell_biosample` kind joins the in-stock views and its `entity_kinds` test) |

`cell_biosample` lives in `cell_culture`, not in `biosamples`: a stock's `entity_kinds` may name only
entity kinds the same ledger loads, and `biosamples` names the animal line kinds of the `angenet`
bundle (ticket 28's option (a), see `presets/README.md`). The sample's origin is the core
`stock.source_aliquot_id` (`tm stock add … --source-aliquot …`), not a schema field.

The step

1. places the whole current preset closure the ledger lists (the parts and the bundle)
   **before** the runner opens the ledger — files are not Dolt content, and the spec-5 loader needs
   every stock kind's `statuses:` — then puts the **frozen copies** shipped next to `migrate.py` over
   their repository versions: **`cell_culture-2.yaml`** (the repository's `presets/` only holds the
   current version), **`base-3.yaml`** (the repository copy is v4 since spec 5's `grid96` →
   `grid8x12` rename; this wave-10 step must keep installing the base content of its era, per the
   retrofit rule in [`migration/README.md`](../../../README.md)), and **`proteins-1.yaml`** /
   **`nucleic_acids-1.yaml`** (ticket rq9 bumps those parts to v2; their own steps own the counters);
2. applies the **additive** diff plan (three `stock_cell_vial` columns, the `stock_cell_biosample`
   table, their constraints and the regenerated views); a destructive plan is refused before
   anything is written;
3. bumps `preset_versions.cell_culture` to 2, writes the target fingerprint and regenerates the
   derived `dolt_tests` from the same schema;
4. and, when the plan is already empty (the schema was applied by hand or another step placed the
   same content), recreates the generated views so the commit has its own statements.

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md) first
  (the step refuses a spec-4 ledger). On a wave-11 upgrade the order is `spec/4-to-5`, then the
  preset steps (`base/2-to-3`, `molbio/2-to-3`, this step), then
  [`migration/presets/base/3-to-4`](../../base/3-to-4/README.md) after them — it renames the base v3
  copy this step keeps, so do not run it before this step (ticket rq9's pattern steps run after
  `base/3-to-4`).
- `ledger_meta.preset_versions.cell_culture = 1` (a second run refuses).
- A clean working set.
- The ledger already loads `cell_culture`, i.e. it lists `molbio` v3 (ticket 27) or `cellbio`. A
  molbio-v2 ledger keeps its cell kinds inside `molbio` and has no `cell_culture` counter: run
  [`migration/presets/molbio/2-to-3`](../../molbio/2-to-3/README.md) first; that step installs the
  frozen `cell_culture-1.yaml` this one upgrades.
- A directory copy `<ledger>/presets/cell_culture/` would shadow the flat file this step places;
  convert it to a file first.

## Running it

```sh
uv run migration/presets/cell_culture/1-to-2/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/cell_culture/1-to-2/migrate.py --ledger /path/to/ledger
uv run migration/presets/cell_culture/1-to-2/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger stays byte-identical;
the printed plan is what a real apply would execute.

## Manual steps

No table or column is renamed, so there is nothing to rewrite. Check for a ledger-local copy that
would shadow the v2 content; the expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rn "cell_culture" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# counter: cell_culture 2
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# one migration commit and its message
dolt log -n 1                                                                        # migrate(ledger): cell_culture 1 -> 2
# restore point
dolt tag | grep pre-migrate-cell_culture-1
# the new objects exist
dolt sql -q "SHOW TABLES LIKE 'stock_cell_biosample'"
dolt sql -q "SHOW COLUMNS FROM stock_cell_vial LIKE 'mycoplasma'"
# validation
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
```

Expected: no `FAIL`, every existing row still present, and `tm schema plan` reports no pending
steps.

## Failure, rollback, caveats

- **A destructive plan or an unprepared copy is a refusal.** Nothing is written: no tag, no commit,
  and the placed preset copies are restored byte for byte.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-cell_culture-1` and copy
  `cell_culture-1.yaml` (next to `migrate.py`, byte-identical to the file the repository shipped at
  cell_culture v1) back into `<ledger>/presets/cell_culture.yaml`; preset copies are files, not Dolt
  content. The next `molbio`/`cellbio` open then matches the old counter again.
