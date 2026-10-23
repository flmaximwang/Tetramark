# 03 · Schema model & loader
Depends on: 01  · Spec: §2, §1.1 (ledger.yaml)

- `schema/model.py`: pydantic models: `Field` (type enum per §2.1, modifiers, `to`, `expr`, `sql_type`,
  `values`, `renamed_from`), `Geometry` (grid/named/slotless with range parser `"A-J!I"`, `"1-12"`, lists),
  `Kind` (object entity|stock|box, fields, mixins, entity_kinds, aliquot_fields, geometry, slot_capacity, renamed_from),
  `Rule`, `Preset`, `LedgerConfig` (ledger.yaml), `EffectiveSchema` (resolved kinds by object, rules, statuses).
- `schema/loader.py`: load bundled presets (importlib.resources `doltledger/presets/<name>/`) or a path;
  resolve `include` (cycle → error), apply mixins (field collision → error), merge local `kinds_dir` and
  `rules_dir`; validate cross-refs: `ref.to` exists, `entity_kinds` are entity kinds, geometry only on box kinds,
  aliquot_fields only on stock kinds, identifiers are `[a-z][a-z0-9_]*` and not SQL reserved/core column names.
- Errors carry file + YAML path (e.g. `molbio/preset.yaml: kinds.plasmid.fields.construct.to`).
- `EffectiveSchema.fingerprint()` → stable sha256 of the normalised schema.

Tests: fixture presets under `tests/data/presets/` covering every field type, each geometry, include chain,
mixins, every validation error; range parser cases (`A-J!I` → 9 labels, `1-12`, lists).
No DB needed.
