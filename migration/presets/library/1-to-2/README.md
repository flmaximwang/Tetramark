# library 1 → 2 — `copy` uses the `tube` status set

Counter: `ledger_meta.preset_versions.library` 1 → 2. Tag before writing: `pre-migrate-library-1`.
One commit: `migrate(ledger): library 1 -> 2`.

## What changes

Spec 4 (ticket 25) moves the aliquot status vocabulary from `ledger.yaml` into the presets. Library
preset version 2 gives its stock kind `copy` `statuses: tube`; the set itself lives in `base`
(version 2), which `library` includes.

No table changes: the step places the current preset copies (files, not Dolt content — the spec-4
loader refuses a stock kind without `statuses:`), recreates the generated views from the ledger's
own (now v2) schema, and bumps `preset_versions.library` + the fingerprint; the shared runner
regenerates the derived `dolt_tests` in the same commit.

## Preconditions

- `ledger_meta.spec_version = 4`. Run [`migration/spec/3-to-4`](../../../spec/3-to-4/README.md) first.
- `ledger_meta.preset_versions.library = 1` (a second run refuses).
- A clean working set.
- `base` is at version 2 too: the step places both copies (library and the base it includes), but
  `preset_versions.base` is bumped by
  [`migration/presets/base/1-to-2`](../../base/1-to-2/README.md) — run that step as well.
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
