# proteins 3 → 4 — a protein construct's `sequence` is amino acids

Counter: `ledger_meta.preset_versions.proteins` 3 → 4. Tag before writing:
`pre-migrate-proteins-3`. One commit: `migrate(ledger): proteins 3 -> 4`.

## What changes

Ticket rq9 (proteins 2) put the nucleotide pattern
`[ACGTURYKMSWBDHVN]+` on `sequence_fields.sequence`, the mixin shared by
`protein_construct` and `plasmid` (`nucleic_acids`).  A plasmid really does
hold a DNA sequence, but a protein construct's `sequence` is the full-length
**amino-acid** sequence it was imported from (`entity_constructs.sequence` in
the legacy ledger, see
[`docs/guides/migrating-a-legacy-ledger.md`](../../../../docs/guides/migrating-a-legacy-ledger.md)).
The nucleotide pattern was therefore a false positive for every construct, and
`length_bp` ("base pairs") computed a residue count.

Version 4 splits the two:

| kind | field | before (v3) | after (v4) |
|---|---|---|---|
| `protein_construct` | `sequence` | shared mixin, nucleotide pattern | own field, amino-acid pattern `[ACDEFGHIKLMNPQRSTVWYXBZUO*]+` |
| `protein_construct` | `length_bp` | computed `CHAR_LENGTH(sequence)` | renamed `length_aa`, same expression |
| `plasmid` | `sequence`, `length_bp` | nucleotide mixin | unchanged |

Nothing else changes: no kind, no other field, no rule, no stored value.  The
length column is a generated `STORED` column, so the rename is in place — no
drop, no retype, no data movement.  `tm check` stops reporting the legacy
constructs and validates them against the amino-acid alphabet instead.

## Preconditions

- `ledger_meta.spec_version` = 6 **or newer** — the v4 preset belongs to the
  spec-6 closure, and the step's `REQUIRED_SPEC` only refuses a ledger still
  below 6 (the current format is spec 7).  A spec-5 ledger runs the manual
  [`spec/5-to-6`](../../../spec/5-to-6/README.md) upgrade first; a spec-6 ledger
  runs [`spec/6-to-7`](../../../spec/6-to-7/README.md) first
  (`uv run migration/spec/6-to-7/migrate.py --ledger "$LEDGER" --apply`),
  because the base step below needs the current spec.
- `ledger_meta.preset_versions.proteins = 3` (a second run refuses).
- The ledger's copies are the base-6 ones (`base` 6): this step places only
  `proteins.yaml`; every other copy is already the one the target schema reads.
  A base-5 ledger runs [`presets/base/5-to-6`](../../base/5-to-6/README.md)
  first — the frozen `proteins-4.yaml` here includes the repo's base, so a
  base-5 copy would pull the capacity change into this step's plan, and the
  step's `base` refusal names that README.
- A clean working set and a backup of the ledger directory (the restore point
  is the tag, but the preset copies are files, not Dolt content).

## Running it

```sh
uv run migration/presets/proteins/3-to-4/migrate.py --ledger PATH [--branch B] [--dry-run]
```

The step

1. places the frozen [`proteins-4.yaml`](proteins-4.yaml) over
   `<ledger>/presets/proteins.yaml` **before** the runner opens the ledger
   (files are not Dolt content; `presets/` only ever holds the current version,
   so a later bump must not change what this historical step installs);
2. applies the diff plan with the explicit rename map from
   [`renames.yaml`](renames.yaml): `length_bp` → `length_aa` on
   `entity_protein_construct`, plus the regenerated `v_entity_protein_construct`
   view.  Any other destructive step refuses the run before anything is
   written;
3. bumps `preset_versions.proteins` to 4, writes the target fingerprint and
   regenerates the derived `dolt_tests` from the same schema — all in the one
   commit.

## Verification

```sh
LEDGER=/path/to/ledger
uv run tm -l "$LEDGER" check           # no FAIL
uv run tm -l "$LEDGER" schema plan     # schema is up to date
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"   # proteins: 4
dolt sql -q "SELECT \`column_name\` FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name = 'entity_protein_construct'
               AND \`column_name\` IN ('length_bp', 'length_aa')"      # only length_aa
dolt sql -q "SELECT COUNT(*), SUM(LENGTH(\`sequence\`)) FROM \`entity_protein_construct\`"  # unchanged
```

A ledger with local kinds/views may still name the old column:

```sh
grep -rn "length_bp" "$LEDGER/kinds" "$LEDGER/rules"   # expected: empty
```

Rename it there by hand; a local view or rule is not rewritten by this step.

## Rollback

```sh
dolt reset --hard pre-migrate-proteins-3
cp migration/presets/proteins/2-to-3/proteins-3.yaml "$LEDGER/presets/proteins.yaml"
```

The preset copies are files, not Dolt content, so they have to be restored by
hand; the frozen [`../2-to-3/proteins-3.yaml`](../2-to-3/proteins-3.yaml) is
exactly the content this step moves a ledger from.
