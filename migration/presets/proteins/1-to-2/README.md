# proteins 1 → 2 — IUPAC patterns on the shared sequence fields

Counter: `ledger_meta.preset_versions.proteins` 1 → 2. Tag before writing: `pre-migrate-proteins-1`.
One commit: `migrate(ledger): proteins 1 -> 2`.

## What changes

Ticket rq9 makes the bundled sequence fields validate their alphabet:

| field | pattern |
|---|---|
| `sequence_fields.sequence` (loaded by `protein_construct` here and `plasmid` in `nucleic_acids`) | `[ACGTURYKMSWBDHVN]+` — IUPAC nucleotide codes |
| `protein_construct.mature_sequence` | `[ACDEFGHIKLMNPQRSTVWYXBZUO*]+` — IUPAC amino-acid codes plus the ambiguity/stop symbols |

A `pattern` is validation-only: the app checks it after sequence normalisation
(`re.fullmatch`, `core/objects.py`) and the generated Dolt test
`schema_<kind>_pattern_<field>` checks the same thing, but no table or column
changes. The step

1. places the whole current preset closure the ledger lists **before** the
   runner opens the ledger (files are not Dolt content), then puts the **frozen
   copies** shipped next to `migrate.py` over their repository versions:
   **`proteins-2.yaml`** (the repository's `presets/` only holds the current
   version) and **`nucleic_acids-1.yaml`** (that part's counter is still 1; its
   own step owns the bump to 2, and placing v2 here would install newer content
   behind its counter);
2. re-creates the generated views from the ledger's own (now v2) schema, so the
   commit has statements of its own — it changes no DDL;
3. bumps `preset_versions.proteins` to 2, writes the target fingerprint and
   regenerates the derived `dolt_tests` from the same schema. `tm check` then
   reports existing rows whose sequence does not match the patterns, with `-v`
   naming the offending ids.

The retrofit rule in [`migration/README.md`](../../../README.md) also applies to
the older wave-10 preset steps: `base/2-to-3`, `base/3-to-4`,
`cell_culture/1-to-2`, `library/1-to-2` and `molbio/2-to-3` now ship and place
frozen `proteins-1.yaml` / `nucleic_acids-1.yaml` copies too, so they cannot
install v2 content behind the part counters.

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md)
  first (the step refuses a spec-4 ledger).
- `ledger_meta.preset_versions.base = 4`. Run
  [`migration/presets/base/3-to-4`](../../base/3-to-4/README.md) first: this step
  installs the repository's base v4 copy (the `grid96` → `grid8x12` rename is
  that step's job) and refuses a ledger still at base 3.
- `ledger_meta.preset_versions.proteins = 1` (a second run refuses).
- `ledger_meta.preset_versions.nucleic_acids`, when the ledger loads it, is 1:
  run this step **before**
  [`migration/presets/nucleic_acids/1-to-2`](../../nucleic_acids/1-to-2/README.md);
  the reverse order is refused.
- A clean working set. Row fixes for `tm check` happen **after** the migration,
  in a normal write session.

## Running it

```sh
uv run migration/presets/proteins/1-to-2/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/proteins/1-to-2/migrate.py --ledger /path/to/ledger
uv run migration/presets/proteins/1-to-2/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger
stays byte-identical; the printed plan is what a real apply would execute.

## Manual steps

No table or column is renamed, so there is nothing to rewrite. Existing rows
that now fail the pattern are reported by `tm check`; fix them with normal
writes (`tm entity update …`) rather than hand-edited SQL. A ledger-local
`kinds/` file that redefines a sequence field keeps its own pattern (the loaded
schema's local fields win); check it, expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rn "sequence" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# counter: base 4, proteins 2, nucleic_acids still 1
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# one migration commit and its message
dolt log -n 1                                                                        # migrate(ledger): proteins 1 -> 2
# restore point
dolt tag | grep pre-migrate-proteins-1
# the generated tests are the v2 ones
dolt sql -q "SELECT \`test_name\` FROM \`dolt_tests\` WHERE \`test_name\` LIKE 'schema_%_pattern_%'"
# validation; a ledger with rows outside the alphabet FAILs those tests
uv run tm -w /path/to/workspace -l <ledger> check
uv run tm -w /path/to/workspace -l <ledger> check -v                                  # ids of offending rows
```

Expected: exactly one commit and one tag; the preset copies are byte-identical
to [`proteins-2.yaml`](proteins-2.yaml) and
[`nucleic_acids-1.yaml`](nucleic_acids-1.yaml); `tm schema plan` reports no
pending steps; any pre-existing non-IUPAC row fails
`schema_protein_construct_pattern_sequence` / `schema_plasmid_pattern_sequence`
/ `schema_protein_construct_pattern_mature_sequence` until it is corrected.

## Failure, rollback, caveats

- **An unprepared ledger is a refusal.** Nothing is written: no tag, no commit,
  and the placed preset copies are restored byte for byte.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the
  statements already executed stay in the working set; the script makes **no**
  commit and prints the last good commit plus `dolt reset --hard <last-good-commit>`.
  The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-proteins-1` and copy
  the frozen v1 copies (`proteins-1.yaml` next to the other retrofit steps, or
  the repository `presets/` at an older checkout) back into
  `<ledger>/presets/`; preset copies are files, not Dolt content. The next open
  then matches the old counter again.
