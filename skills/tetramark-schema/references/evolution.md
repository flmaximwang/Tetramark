# Schema evolution: plan, apply, destructive changes

`tm schema plan` compares the effective YAML schema with the live DB (via `information_schema`
and `ledger_meta`) and prints ordered steps. Nothing is executed. `tm schema apply` executes those
steps in one transaction and one Dolt commit, then refreshes the fingerprint and regenerates
`dolt_tests`.

## Reading a plan

```
schema plan for ledger 'lab101'
  1. [additive] create_table entity_equipment
     new entity kind 'equipment'
     CREATE TABLE `entity_equipment` ( ... )
  2. [additive] add_foreign_key entity_equipment.fk_entity_equipment_id_kind
     references entity
     ALTER TABLE ... ADD CONSTRAINT ...
  3. [additive] create_view v_entity_equipment
     generated convenience view
     CREATE VIEW ...
3 step(s): 3 additive, 0 destructive
ledger_meta fingerprint will be refreshed on apply
```

- `[additive]` steps are safe and applied freely.
- `[destructive]` steps need `--allow-destructive`; `apply` refuses the whole plan otherwise.
- `schema is up to date` means the plan is empty; **after every apply, plan must print this**.
  It is the idempotency guarantee.
- A warning at open time that the fingerprint does not match is normal after editing YAML. Run
  `schema plan` to see what changed; it is not an error.

## Additive change: a local entity kind

1. Create `<ledger>/kinds/equipment.yaml`:

```yaml
kinds:
  equipment:
    object: entity
    mixins: [named_fields]
    fields:
      serial_no: {type: string, length: 64, unique: true}
      purchased_on: {type: date}
```

2. Add `kinds_dir: kinds/` to `<ledger>/ledger.yaml` and check the effective schema:

```sh
tm schema show equipment
```

3. Plan and apply:

```sh
tm schema plan
tm schema apply
tm schema plan          # -> schema is up to date
```

The apply created `entity_equipment`, its discriminator FK to `entity`, the convenience view
`v_entity_equipment`, and an entity-kinds test if relevant. It also regenerated every `dolt_tests`
row. Data is untouched.

4. The new kind is immediately usable:

```sh
tm entity add equipment --name "Centrifuge" --set serial_no=SN-1
```

To add a field to an existing kind, just add it to the YAML. A nullable or defaulted field is
additive. An enum's new value is additive; removing one is destructive.

## Destructive change: tightening a field

Adding `serial_required: {type: string, required: true}` to the kind above produces:

```
  2. [destructive] add_column entity_equipment.serial_required
     new required field
     ALTER TABLE `entity_equipment` ADD COLUMN `serial_required` VARCHAR(255) NOT NULL
3 step(s): 2 additive, 1 destructive
```

`tm schema apply` prints the plan and exits `1` without touching the database:

```
Error: refusing to apply 1 destructive step(s) without --allow-destructive
```

Safe procedure:

1. `tm backup /path/to/archive/NAME` (or a configured backup / push) first.
2. Make the field nullable, apply, and backfill (through `entity edit`, `import`, or a scripted
   export/edit/import).
3. Make it `required` and run `tm schema apply --allow-destructive`.
4. `tm check`.

`--allow-destructive` is a single flag for the whole plan; read every destructive step before using
it. The apply is one transaction, so a failure leaves the DB unchanged.

## Renaming instead of drop+add

Declare the old name so data survives:

```yaml
kinds:
  equipment:
    renamed_from: device          # kind rename: device -> equipment
    fields:
      purchased_on:
        type: date
        renamed_from: bought_on   # field rename
```

The plan emits `ALTER TABLE … RENAME TO` / `ALTER TABLE … RENAME COLUMN` steps (additive, because
data is preserved) instead of dropping the old table/column and creating an empty one. Renaming a
kind also renames its generated table, its convenience view and FK names.

Other destructive changes: dropping a kind/field, changing a type, adding UNIQUE, adding NOT NULL to
an existing nullable column, removing an enum value. Dolt rebuilds any generated column or FK that
depends on a changed column automatically.

## Local rules

`<ledger>/rules/*.yaml` documents contain only rules:

```yaml
rules:
  - id: EQ01
    description: equipment with a serial number records a purchase date
    severity: fail
    sql: >-
      SELECT COUNT(*) FROM entity_equipment
      WHERE serial_no IS NOT NULL AND purchased_on IS NULL
    expect: {single_value: 0}
```

Rules affect `dolt_tests`, not tables, so `schema plan` may say `schema is up to date` while the
fingerprint is stale; `schema apply` still regenerates the tests (and commits). Run
`tm check --group rules:local` afterwards. Local rules are grouped `rules:local`; preset rules
are `rules:preset`.

## Format upgrades and the manual steps

`ledger_meta` carries two counters: `spec_version` (the core ledger format, `SPEC_VERSION` in
`src/tetramark/schema/model.py`) and `preset_versions` (a JSON object, one entry per loaded preset).
Every bump ships exactly one folder under `migration/` in the same change, `spec/N-to-N+1/` or
`presets/<name>/N-to-N+1/`. A step is **scripted** (`migrate.py`) or **README-only** (the operator
runs the SQL/file edits by hand); the folder name and the README obligations are the same.

**Spec 5 → 6 (one fixed status set) is README-only**: `migration/spec/5-to-6/README.md`. It cannot be
a script because the change touches the ledger's preset copies as much as its rows. The order is:
run every pending historical preset step first (they target spec 5), replace the whole
`<ledger>/presets/` closure with the status-free copies, delete `status_sets:`/`statuses:` from local
`kinds/`, remap `aliquot.status` with one `migrated: <old status>` status event per remapped aliquot,
add the `ck_aliquot_status` `CHECK`, then `tm schema plan`/`apply`. The apply writes `spec_version` 6,
`preset_versions`, the fingerprint, the CHECK and the regenerated `dolt_tests` in one commit. Past
events are never rewritten — `core_status_event_allowed` only judges events whose two endpoints are
fixed names, so old words in history are ignored — and the new enum stock properties are not
backfilled. A closure that mixes the legacy per-kind sets with the fixed model is a load error.

`tm` never upgrades an older ledger implicitly; it warns
`ledger is spec N, tm is spec N+1: follow migration/spec/N-to-N+1/README.md to upgrade it manually`
and keeps working against the ledger's own stored preset copies.

## What apply rewrites

- generated kind tables, discriminator FKs, views and convenience views;
- `dolt_tests` — fully replaced from the effective schema each time (never hand-edit);
- on a format upgrade: `ledger_meta.spec_version`, the fixed-status `CHECK` if the plan contains it,
  `preset_versions`, the fingerprint and the `ledger.yaml` copy.

It does not rewrite `ledger.yaml` itself; that is the source document you edit.

## Checklist before applying anything

1. `tm backup /path/to/archive/NAME` (or a configured backup) / `tm remote push`.
2. `tm schema show` matches what you intended (includes/mixins resolved).
3. `tm schema plan` read line by line; only expected tags.
4. On a copy of the ledger if the change is big: restore a backup, apply, `tm check -v`,
   spot-check `box show`.
5. Apply, then `tm schema plan` (empty) and `tm check` (no FAIL).
6. `tm history` on a representative object if data was migrated.
