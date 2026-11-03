# nucleic_acids 1 → 2 — the IUPAC pattern on `oligo.sequence`

Counter: `ledger_meta.preset_versions.nucleic_acids` 1 → 2. Tag before writing:
`pre-migrate-nucleic_acids-1`. One commit: `migrate(ledger): nucleic_acids 1 -> 2`.

## What changes

Ticket rq9 makes the bundled sequence fields validate their alphabet.
`nucleic_acids` version 2 adds the pattern to its own sequence field:

| field | pattern |
|---|---|
| `oligo.sequence` | `[ACGTURYKMSWBDHVN]+` — IUPAC nucleotide codes |

The shared `sequence_fields` mixin (in `proteins`) already carries the same
pattern for `protein_construct` and `plasmid`; that part's step,
[`migration/presets/proteins/1-to-2`](../../proteins/1-to-2/README.md), bumps it
and must run first.

A `pattern` is validation-only: the app checks it after sequence normalisation
(`re.fullmatch`, `core/objects.py`) and the generated Dolt test
`schema_oligo_pattern_sequence` checks the same thing, but no table or column
changes. The step

1. places the whole current preset closure the ledger lists **before** the
   runner opens the ledger (files are not Dolt content; `proteins` is placed at
   version 2 from the repository), then puts the **frozen
   [`nucleic_acids-2.yaml`](nucleic_acids-2.yaml)** shipped next to
   `migrate.py` over its copy (the target content this counter bumps to; a
   later repository bump must not change it);
2. re-creates the generated views from the ledger's own (now v2) schema, so the
   commit has statements of its own — it changes no DDL;
3. bumps `preset_versions.nucleic_acids` to 2, writes the target fingerprint
   and regenerates the derived `dolt_tests` from the same schema. `tm check`
   then reports existing oligo rows whose sequence does not match the pattern,
   with `-v` naming the offending ids.

The retrofit rule in [`migration/README.md`](../../../README.md) applies to the
older wave-10 preset steps: `base/2-to-3`, `base/3-to-4`,
`cell_culture/1-to-2`, `library/1-to-2` and `molbio/2-to-3` now ship and place
frozen `proteins-1.yaml` / `nucleic_acids-1.yaml` copies too, so they cannot
install v2 content behind the part counters.

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md)
  first (the step refuses a spec-4 ledger).
- `ledger_meta.preset_versions.base = 4`. Run
  [`migration/presets/base/3-to-4`](../../base/3-to-4/README.md) first.
- `ledger_meta.preset_versions.proteins = 2`. Run
  [`migration/presets/proteins/1-to-2`](../../proteins/1-to-2/README.md) first
  (the step refuses a ledger whose proteins counter is still 1: the shared
  `sequence_fields` pattern belongs to that step).
- `ledger_meta.preset_versions.nucleic_acids = 1` (a second run refuses).
- A clean working set. Row fixes for `tm check` happen **after** the migration,
  in a normal write session.

## Running it

```sh
uv run migration/presets/nucleic_acids/1-to-2/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/nucleic_acids/1-to-2/migrate.py --ledger /path/to/ledger
uv run migration/presets/nucleic_acids/1-to-2/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger
stays byte-identical; the printed plan is what a real apply would execute.

## Manual steps

No table or column is renamed, so there is nothing to rewrite. Existing oligo
rows that now fail the pattern are reported by `tm check`; fix them with normal
writes (`tm entity update …`) rather than hand-edited SQL. A ledger-local
`kinds/` file that redefines `oligo.sequence` keeps its own pattern; check it,
expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rn "oligo" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# counter: base 4, proteins 2, nucleic_acids 2
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# one migration commit and its message
dolt log -n 1                                                                        # migrate(ledger): nucleic_acids 1 -> 2
# restore point
dolt tag | grep pre-migrate-nucleic_acids-1
# the generated test is the v2 one
dolt sql -q "SELECT \`test_name\` FROM \`dolt_tests\` WHERE \`test_name\` = 'schema_oligo_pattern_sequence'"
# validation; a ledger with rows outside the alphabet FAILs that test
uv run tm -w /path/to/workspace -l <ledger> check
uv run tm -w /path/to/workspace -l <ledger> check -v                                  # ids of offending rows
```

Expected: exactly one commit and one tag; the preset copy is byte-identical to
[`nucleic_acids-2.yaml`](nucleic_acids-2.yaml); `tm schema plan` reports no
pending steps; any pre-existing non-IUPAC oligo row fails
`schema_oligo_pattern_sequence` until it is corrected.

## Failure, rollback, caveats

- **An unprepared ledger is a refusal.** Nothing is written: no tag, no commit,
  and the placed preset copies are restored byte for byte.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the
  statements already executed stay in the working set; the script makes **no
  commit** and prints the last good commit plus `dolt reset --hard <last-good-commit>`.
  The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-nucleic_acids-1` and
  copy a frozen `nucleic_acids-1.yaml` (next to the retrofit steps, or the
  repository `presets/` at an older checkout) back into `<ledger>/presets/`;
  preset copies are files, not Dolt content. The next open then matches the old
  counter again.
