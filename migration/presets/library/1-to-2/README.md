# library 1 → 2 — `copy` uses the `tube` status set

Counter: `ledger_meta.preset_versions.library` 1 → 2. Tag before writing: `pre-migrate-library-1`.
One commit: `migrate(ledger): library 1 -> 2`.

## What changes

Spec 4 (ticket 25) moves the aliquot status vocabulary from `ledger.yaml` into the presets. Library
preset version 2 gives its stock kind `copy` `statuses: tube`; the set itself lives in `base`
(version 2), which `library` includes.

No table changes: the step places the current preset copies (files, not Dolt content — the spec-5
loader refuses a stock kind without `statuses:`), recreates the generated views from the ledger's
own (now v2) schema, and bumps `preset_versions.library` + the fingerprint; the shared runner
regenerates the derived `dolt_tests` in the same commit.

The `base` copy is the **frozen [`base-3.yaml`](base-3.yaml)** shipped next to `migrate.py`: the
repository's `presets/base.yaml` is v4 since spec 5 (the `grid96` → `grid8x12` rename), and this
wave-10 step must keep installing the base content its era reads. Ticket rq9 bumps `proteins` and
`nucleic_acids` to v2, so the step also ships and places their frozen
[`proteins-1.yaml`](proteins-1.yaml) / [`nucleic_acids-1.yaml`](nucleic_acids-1.yaml) copies. Freezing
the old target copy into every earlier step that places the whole closure is the retrofit rule in
[`migration/README.md`](../../../README.md).

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md) first
  (the step refuses a spec-4 ledger). On a wave-11 upgrade the order is `spec/4-to-5`, then the
  preset steps (`base/2-to-3`, `library/1-to-2`), then
  [`migration/presets/base/3-to-4`](../../base/3-to-4/README.md) after them — it renames the base v3
  copy this step keeps, so do not run it before this step (ticket rq9's pattern steps run after
  `base/3-to-4`).
- `ledger_meta.preset_versions.library = 1` (a second run refuses).
- A clean working set.
- `base` is installed from the frozen v3 copy here; its counter in `preset_versions` is bumped by
  [`migration/presets/base/2-to-3`](../../base/2-to-3/README.md) (a pre-spec-4 ledger reaches base 2
  through [`migration/presets/base/1-to-2`](../../base/1-to-2/README.md) with the older tool) — run
  that step as well.
- Local `kinds/` files must already use `status_sets`/`statuses:`.

## Running it

```sh
uv run migration/presets/library/1-to-2/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/library/1-to-2/migrate.py --ledger /path/to/ledger
```

`--dry-run` (and a refusal) restores the preset copies it placed.

## Verification

```sh
LEDGER=/path/to/ledger
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"  # {"base": 2, "library": 2}
dolt log -n 1                                                                        # migrate(ledger): library 1 -> 2
dolt tag | grep pre-migrate-library-1
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
```
