# spec 3 → 4 — status sets per stock kind, forced status events, in-stock views

Counter: `ledger_meta.spec_version` 3 → 4. Tag before writing: `pre-migrate-spec-3`.
One commit: `migrate(ledger): spec 3 -> 4`.

## What changes

Spec 4 (ticket 25) moves the aliquot status vocabulary out of the ledger config and
into each **stock kind**: a preset (or the ledger's `kinds/`) declares top-level `status_sets:`, and
every stock kind names one with `statuses:`. Each status says whether it counts as **in stock** and
which statuses it may change to (`to:`). The generated shape changes four times:

1. `ledger.yaml` loses `statuses:`. The step removes it from the file and from the
   `ledger_meta.ledger_yaml` copy **only when it still holds the spec-3 default**
   `[pending, stored, missing, consumed]`.
2. the global `aliquot.status` CHECK is dropped (a status belongs to the stock kind's set, which a
   CHECK cannot express; the generated `core_aliquot_status_known` test enforces it instead);
3. `aliquot_event` gains `forced BOOLEAN NOT NULL DEFAULT FALSE`. `tm aliquot status --force --note`
   writes `TRUE`; the generated `core_status_event_allowed` test accepts forced events and fails
   unforced ones that break a `to:` rule;
4. every generated view is recreated in the spec-4 shape. `v_stock_<kind>`, `v_stock_status` and
   `v_box_slots` replace ticket 23's `aliquots_stored`, `quantity_stored` and five-value `status`
   with `aliquots_in_stock`, `aliquots_total`, `quantity_in_stock` + `unit` (NULL when the in-stock
   tubes mix units) and `status` ∈ {`active`, `depleted`, `empty`}.

The step refuses when an existing aliquot's status is not in its stock kind's set: those rows would
fail the new generated test immediately, and guessing a status would lose data. It lists them, and
nothing is written.

## Preconditions

- `ledger_meta.spec_version = 3` (the script refuses any other value, and a second run therefore
  refuses).
- A clean working set (`dolt_status` empty).
- `<ledger>/presets/` holds this checkout's **wave-10** preset copies (files, not Dolt content). The
  spec-4 loader refuses a stock kind without `statuses:`, so the previous copies cannot even be
  opened. Replace them first, e.g. for a `molbio` ledger:

  ```sh
  LEDGER=/path/to/ledger
  # molbio is a bundle: copy it and every part it includes, or the copy will
  # not reopen ("include: preset 'nucleic_acids' not found").
  cp presets/base.yaml presets/library.yaml presets/molbio.yaml \
     presets/nucleic_acids.yaml presets/bacteria.yaml presets/proteins.yaml \
     presets/cell_culture.yaml "$LEDGER/presets/"
  ```

  The wave-10 preset steps (`migration/presets/*/N-to-N+1/`) also place their copies before opening
  the ledger, but they refuse a spec-3 ledger: this step has to run first, and it can only open the
  ledger once the copies are current. So copy the files by hand, run this step, then run the preset
  steps to bump their counters.
- A local `kinds/` file that still declares stock kinds without `statuses:` must be edited by hand
  (same loader refusal): add a `status_sets:` block and give each stock kind `statuses: <set>`.
- A **custom** statuses list cannot be removed by this step. Convert it first: declare the list as a
  `status_sets:` entry in the ledger's `kinds/` (same status names; `in_stock: true` for the ones
  that used to mean "on the shelf"), give every stock kind `statuses: <set name>`, delete the
  `statuses:` line from `ledger.yaml`, then run the step. The step refuses while the list differs
  from the default and says so.
- `tm check` is expected to pass afterwards; any hand-written rule that selects the removed view
  columns (`aliquots_stored`, `quantity_stored`, `total_quantity`/`aliquot_count` from
  `v_stock_status`) must be edited first (see Manual steps).
- A backup/branch rule for real lab ledgers: work on a branch (`--branch tetramark-upgrade`), never
  on `main`, never push.
- The tag `pre-migrate-spec-3` must not exist at a different commit; the script refuses if it points
  anywhere other than HEAD.

## Running it

```sh
# 1. inspect: the tag, the DDL, the meta bump; writes nothing (restores ledger.yaml)
uv run migration/spec/3-to-4/migrate.py --ledger /path/to/ledger --dry-run

# 2. apply (one commit, tag first)
uv run migration/spec/3-to-4/migrate.py --ledger /path/to/ledger

# 3. on a branch of a served ledger
uv run migration/spec/3-to-4/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

The spec-3 `ledger.yaml` still carries `statuses:`, and the spec-4 loader refuses to open such a
config on purpose. The step therefore strips the default list from the file for the duration of the
run, restores it byte-for-byte when it refuses or when `--dry-run` finishes, and keeps the stripped
file (and the matching `ledger_meta.ledger_yaml` update) on a real apply.

## Manual steps

1. **Place the wave-10 preset copies** (see Preconditions).
2. **Custom status lists**: convert them into a `status_set` in the ledger's `kinds/` before running
   the step (see Preconditions); the step will keep refusing until it equals the default list.
3. **Hand-written rule SQL** naming the removed view columns or the old five-value status is not
   rewritten. Grep the ledger's own schema and rules (expected output after the upgrade: only the new
   names, or nothing):

   ```sh
   LEDGER=/path/to/ledger
   grep -rnE "aliquots_stored|quantity_stored|aliquot_count|stored_count|pending_count|missing_count|consumed_count|total_quantity" \
       "$LEDGER/kinds" "$LEDGER/rules"
   ```

4. **Counters**: after this step the ledger is spec 4 but its preset copies are newer than
   `preset_versions`. Run the matching `migration/presets/<name>/N-to-N+1/` step for every preset
   whose version was bumped: `base/2-to-3` and `molbio/2-to-3` for a molbio ledger (in either
   order), `library/1-to-2` for a library ledger, and the older `1-to-2` steps a pre-0.2.0 ledger
   still carries.

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 4
# 2. exactly one migration commit and its message
dolt log -n 1                                                                      # migrate(ledger): spec 3 -> 4
# 3. restore point
dolt tag | grep pre-migrate-spec-3
# 4. the CHECK is gone, forced exists
dolt sql -q "SELECT COUNT(*) FROM information_schema.check_constraints WHERE constraint_name = 'ck_aliquot_status'"  # 0
dolt sql -q "SHOW COLUMNS FROM \`aliquot_event\` LIKE 'forced'"                     # tinyint(1) NOT NULL DEFAULT '0'
# 5. the new stock-view columns carry values
dolt sql -q "SHOW COLUMNS FROM \`v_stock_lot\`"
dolt sql -r csv -q "SELECT \`id\`, \`aliquots_in_stock\`, \`aliquots_total\`, \`quantity_in_stock\`, \`unit\`, \`status\` FROM \`v_stock_lot\`"
# 6. validation: no FAIL
uv run tm -w /path/to/workspace -l <ledger> check
```

## Failure, rollback, caveats

- **A custom list or an out-of-set aliquot status is a refusal, not a failure.** Nothing is written
  (no tag, no commit, no DDL; `ledger.yaml` is restored): fix the data/configuration and retry.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. A hard reset restores tracked tables (including the
  `dolt_schemas` rows of the old views); Dolt leaves *untracked* objects created by the failed
  attempt behind — check `SHOW FULL TABLES` afterwards.
- **Rollback after success:** reset the branch to the tag (`dolt reset --hard pre-migrate-spec-3`)
  and use the older tool; the preset copies under `<ledger>/presets/` are files and are not restored
  by the reset, so copy the matching older presets back.
