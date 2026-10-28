# molbio 2 → 3 — the preset becomes a bundle of reusable parts

Counter: `ledger_meta.preset_versions.molbio` 2 → 3. Tag before writing: `pre-migrate-molbio-2`.
One commit: `migrate(ledger): molbio 2 -> 3`.

## What changes

Molbio preset version 3 (ticket 27, design §2) stops holding kinds itself: it is a **bundle** whose
`include:` is `[base, nucleic_acids, bacteria, proteins, cell_culture]`. `cellbio` and `angenet`
(tickets 28/29) can then share those parts, and one ledger can list two bundles without duplicate
kind errors. **No kind or table name of an existing molbio ledger changes.**

The only new objects are the `nucleic_acids` additions:

| table | what it holds |
|---|---|
| `entity_oligo` | a synthetic oligo design: `sequence`, `direction` (`fwd`/`rev`), `target`, `modification`, `purification`, `tm_c`, and `length_nt`/`gc_pct` computed from the sequence |
| `stock_oligo_stock` | a tube of resuspended oligo: `concentration_um`, `solvent`, `statuses: tube` |
| `v_entity_oligo`, `v_stock_oligo_stock` | the generated kind views |

The placed `base` v3 copy also adds the slotless `incubator` box kind
([base 2 → 3](../../base/2-to-3/README.md) owns that counter).

`cell_culture` comes from the **frozen `cell_culture-1.yaml` next to `migrate.py`**, not from the
repository: [ticket 28](../../../../docs/tickets/28-cellbio-bundle.md) bumps `presets/cell_culture.yaml`
to v2, but molbio v3 shipped with cell_culture 1 and this step records exactly that counter — the
`1 → 2` step is [`migration/presets/cell_culture/1-to-2`](../../cell_culture/1-to-2/README.md). The
other parts are still the current repository version here; when one of them is bumped by a later
ticket, freeze its v1 copy next to this `migrate.py` the same way.

The step

1. places the whole current preset closure the ledger lists (`base` v3, the four parts, `molbio`
   v3) **before** the runner opens the ledger — files are not Dolt content, and the spec-4 loader
   needs every stock kind's `statuses:` — then puts the frozen cell_culture v1 over its own copy
   (files are not Dolt content, so this is the version the ledger now reads);
2. applies the **additive** diff plan (oligo tables, the base box kind and their views); a
   destructive plan is refused before anything is written, because this step never drops a molbio
   table or column;
3. bumps `preset_versions.molbio` to 3 **and** records a counter per part at version 1
   (`nucleic_acids`, `bacteria`, `proteins`, `cell_culture`) in the same commit;
4. writes the target fingerprint and regenerates `dolt_tests` from the same schema (shared runner).

`preset_versions.base` stays at 2 here: the base step bumps it, exactly as molbio 1 → 2 left base's
counter to `migration/presets/base/1-to-2`. `preset_versions.cell_culture` stays at 1:
`migration/presets/cell_culture/1-to-2` bumps it.

## Preconditions

- `ledger_meta.spec_version = 4`. Run [`migration/spec/3-to-4`](../../../spec/3-to-4/README.md) first
  when the ledger is still spec 3 (the step refuses an older spec).
- `ledger_meta.preset_versions.molbio = 2` (a second run refuses).
- A clean working set.
- The ledger lists `molbio` (and may list other presets; their current copies are placed too, and
  their counters are bumped by their own steps).
- A directory copy `<ledger>/presets/molbio/` (or any part) would shadow the flat `*.yaml` files this
  step places; convert it to a file first.

## Running it

```sh
uv run migration/presets/molbio/2-to-3/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/molbio/2-to-3/migrate.py --ledger /path/to/ledger
uv run migration/presets/molbio/2-to-3/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger stays
byte-identical; the printed plan is what a real apply would execute.

## Manual steps

No kind or field is renamed, so there is nothing to rewrite. Two things can need a hand edit:

1. A ledger-local `kinds/` file that redefines one of the molbio kinds must be removed (the loader
   rejects a duplicate kind) — the parts now own them.
2. A hand-written view or rule that expected the old single-file `molbio` copy cannot be fixed
   automatically; grep the ledger's own schema for the new names, expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rnE "entity_oligo|stock_oligo_stock" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# counter: molbio 3 and every part at 1
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# one migration commit and its message
dolt log -n 1                                                                        # migrate(ledger): molbio 2 -> 3
# restore point
dolt tag | grep pre-migrate-molbio-2
# the new tables exist, every old one is kept
dolt sql -q "SHOW TABLES LIKE 'entity_oligo'"
dolt sql -q "SHOW TABLES LIKE 'stock_oligo_stock'"
dolt sql -q "SELECT COUNT(*) FROM entity_plasmid"     # unchanged
# validation
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
```

Expected: no `FAIL`, every row of the v2 ledger still present, and `tm schema plan` reports no
pending steps.

## Failure, rollback, caveats

- **A destructive plan or an unprepared copy is a refusal.** Nothing is written: no tag, no commit,
  and the placed preset copies are restored byte for byte.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-molbio-2` and copy the v2
  `molbio.yaml`/`base.yaml` back into `<ledger>/presets/` (the frozen copies in
  [`migration/presets/molbio/1-to-2`](../1-to-2/README.md) are that version); preset copies are
  files, not Dolt content. This step's own `cell_culture-1.yaml` is the cell_culture copy it placed,
  if that one needs restoring too.
