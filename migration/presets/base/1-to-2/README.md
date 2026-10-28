# base 1 → 2 — status sets `tube`, `cryo`, `animal`; `lot` uses `tube`

Counter: `ledger_meta.preset_versions.base` 1 → 2. Tag before writing: `pre-migrate-base-1`.
One commit: `migrate(ledger): base 1 -> 2`.

## What changes

Spec 4 (ticket 25) moves the aliquot status vocabulary from `ledger.yaml` into the presets. Base
preset version 2:

- defines the top-level `status_sets`:
  - `tube` — `pending → stored/missing/consumed`, `stored → missing/consumed`,
    `missing → stored/consumed`, `consumed` final; `stored` is in stock;
  - `cryo` — `stored → thawed/missing`, `missing → stored/thawed`, `thawed` final; `stored` is in
    stock;
  - `animal` — `planned → alive/cancelled`, `alive → euthanized/found_dead/transferred`, the rest
    final; `alive` is in stock;
- gives the stock kind `lot` `statuses: tube`.

No table changes: the status sets only drive the generated views and `dolt_tests`, and an aliquot's
allowed statuses moved from the ledger-wide list into the presets. The step therefore places the
current preset copies (files, not Dolt content — the spec-4 loader refuses a stock kind without
`statuses:`), recreates the generated views from the ledger's own (now v2) schema, and bumps
`preset_versions.base` + the fingerprint; the shared runner regenerates the derived `dolt_tests` in
the same commit.

## Preconditions

- `ledger_meta.spec_version = 4`. Run [`migration/spec/3-to-4`](../../../spec/3-to-4/README.md) first
  (the step refuses a spec-3 ledger).
- `ledger_meta.preset_versions.base = 1` (a second run refuses; the shared runner checks it).
- A clean working set.
- The ledger lists `base`. A ledger that lists other presets whose copies are still pre-wave-10
  (e.g. `molbio`) cannot be loaded by the spec-4 tool at all: the step places **every bundled preset
  copy the ledger lists** before it opens the ledger, so run the other presets' steps afterwards to
  bump their counters (the molbio 2 → 3 step belongs to ticket 27).
- Local `kinds/` files must already use `status_sets`/`statuses:` (see spec 3 → 4 manual steps).

## Running it

```sh
uv run migration/presets/base/1-to-2/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/base/1-to-2/migrate.py --ledger /path/to/ledger
uv run migration/presets/base/1-to-2/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger stays
byte-identical; the printed plan is what a real apply would execute.

## Verification

```sh
LEDGER=/path/to/ledger
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"  # {"base": 2, ...}
dolt log -n 1                                                                        # migrate(ledger): base 1 -> 2
dolt tag | grep pre-migrate-base-1
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
```

Expected: no `FAIL`, and `tm schema plan` reports no pending steps.
