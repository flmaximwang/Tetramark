# 10 · Object operations
Depends on: 06  · Spec: §1, §6 (entity/stock/aliquot/box/alias commands), §6.1

`core/objects.py` + `core/events.py` + CLI groups `entity`, `stock`, `aliquot`, `box`, `alias`:
- Parse `--set field=value` with type coercion from the kind schema (enum check, sequence normalisation,
  ref resolution by id or alias, pattern check). Unknown field → usage error listing valid fields.
- entity add/edit/ls/show; stock add (entity kind restriction, parent/source lineage, default 1 aliquot,
  `--aliquots N`, placement with `--box/--slot` repeated, quantity/unit applied to each aliquot), edit/ls/show.
- aliquot add/move/use/status/purge with `aliquot_event` rows; `use` decrements quantity (unit must match),
  reaching 0 → status consumed; placement checks: slot exists, capacity, no child box in slot, slotless capacity,
  stored in slotted box needs slot.
- box add (materialise `slot` rows from geometry; `--in PARENT --slot S` with exclusivity + acyclicity), move, ls.
- alias add/rm with uniqueness per object type.
- Every command = one `Ledger.write` (one commit), supports `--dry-run`/`--no-commit`; `--json` output of the
  created/changed objects (ids).

Tests (use `library` test preset from 03 or the base preset if 07 merged): happy paths + every rejection rule;
commit count increments by exactly 1 per command; dry-run leaves `dolt_status` clean; datecode ids sequence within a day.
