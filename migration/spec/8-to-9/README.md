# spec 8 → 9 — reversible aliquot use: `revert_use` and `reverts_event_id`

Counter: `ledger_meta.spec_version` 8 → 9. Tag before writing: `pre-migrate-spec-8`.
One commit: `migrate(ledger): spec 8 -> 9`.

## What changes

Spec 9 (`Tetramark-3w6`) lets an operator undo an aliquot `use` without deleting
audit history: `tm aliquot use REF --revert-last -m MESSAGE` writes a **new** event of kind
`revert_use` that names the `use` it undoes, while the original row stays exactly
as it was. Everything changes on `aliquot_event`:

1. the `event` ENUM gains `revert_use`. The value is **appended last**, so every
   stored value keeps its ENUM index and no data has to be rewritten — the
   statement is a plain `MODIFY COLUMN` with the same list plus one value;
2. a nullable `reverts_event_id BIGINT` column names the `use` event a
   `revert_use` reverses, placed right after `event` (`AFTER event`, the column
   position a fresh spec-9 ledger has);
3. three constraints turn the reference into an invariant:

   | constraint | what it enforces |
   | --- | --- |
   | `uq_aliquot_event_reverts_event_id` (`UNIQUE`) | a `use` is reversed at most once — including two concurrent attempts: the loser gets a duplicate-key error |
   | `ck_aliquot_event_reverts_event_id` (`CHECK ((event = 'revert_use') = (reverts_event_id IS NOT NULL))`) | the reference is present exactly on `revert_use` rows, so no ordinary event can carry one |
   | `fk_aliquot_event_reverts_event_id` (self `FOREIGN KEY ... ON DELETE CASCADE`) | the target row exists; deleting the original `use` (which is what `aliquot purge` does, in one `DELETE` over all of an aliquot's events) takes its reversal with it |

`record_event` checks inside the writing transaction that the target is a `use`
and belongs to the same aliquot. The generated `core_revert_event_allowed` and
`core_revert_event_unique` tests let `tm check` detect invalid manual changes.
The self-reference uses the event ID alone so `aliquot renumber` can update all
of the aliquot's events together. Dolt 2.6.1 rejects that update with a composite
foreign key on `(reverts_event_id, aliquot_id)`.

No other table, no `ledger_meta` key and no preset moves. Existing rows and
event ids are preserved exactly.

## Preconditions

- `ledger_meta.spec_version = 8` (the script refuses any other value, so a second
  run refuses too).
- A clean working set (`dolt_status` empty).
- `tm check` passes before the upgrade; nothing here rewrites a failing row.
- A backup/branch rule for real lab ledgers: work on a branch
  (`--branch tetramark-upgrade`), never on `main`, never push.
- Preset copies need no change — this step touches no preset and no
  per-preset counter.

## Running it

```sh
# 1. inspect: the statements this ledger needs; writes nothing
uv run migration/spec/8-to-9/migrate.py --ledger /path/to/ledger --dry-run

# 2. apply (one commit, tag first)
uv run migration/spec/8-to-9/migrate.py --ledger /path/to/ledger

# 3. on a branch of a served ledger
uv run migration/spec/8-to-9/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

Unlike `migration/spec/6-to-7`, this step writes **by default** (as every other
step here): `--dry-run` prints the plan, the tag and the meta bump and changes
nothing; a run without it applies. The script reads the live shape through
`information_schema` and only adds what is missing, so a half-patched ledger can
be finished by re-running it.

## Manual steps

None for a ledger the script can open. Two notes:

1. **A hand-patched spec-8 ledger.** If you added the column or a constraint by
   hand, the script skips exactly the pieces that already exist. Do not add a
   *different* constraint under the same name — the step would keep it.
2. **Custom `dolt_tests`.** The step regenerates the derived tests, so a
   hand-written test that is not in the generated set is gone (same as every
   spec step). Re-add it with `tm test` afterwards.

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 9
# 2. exactly one migration commit and its message
dolt log -n 1                                                                      # migrate(ledger): spec 8 -> 9
# 3. restore point
dolt tag | grep pre-migrate-spec-8
# 4. the column sits after `event` and is nullable
dolt sql -q "SHOW COLUMNS FROM \`aliquot_event\` LIKE 'reverts_event_id'"           # bigint, YES, NULL
# 5. the ENUM knows the new value, and the old ones kept their order
dolt sql -q "SHOW COLUMNS FROM \`aliquot_event\` LIKE 'event'"                      # ... 'edit','revert_use'
# 6. the three constraints exist
dolt sql -q "SELECT \`constraint_name\` FROM information_schema.table_constraints WHERE \`table_name\` = 'aliquot_event' AND \`constraint_name\` LIKE '%revert%'"
# 7. existing events and their ids are untouched
dolt sql -r csv -q "SELECT COUNT(*), MIN(\`id\`), MAX(\`id\`) FROM \`aliquot_event\` WHERE \`event\` <> 'revert_use'"
# 8. validation: no FAIL (the two reversal rules are installed by the step)
uv run tm -w /path/to/workspace -l <ledger> check
```

## Failure, rollback, caveats

- **A counter other than 8 is a refusal, not a failure.** Nothing is written (no
  tag, no commit, no DDL).
- **Dolt DDL is not transactional.** If a statement fails mid-way (for example
  an existing duplicate `reverts_event_id` value), the statements already
  executed stay in the working set; the script makes **no** commit and prints the
  last good commit plus `dolt reset --hard <last-good-commit>`. A hard reset
  restores tracked tables; Dolt leaves *untracked* objects created by the failed
  attempt behind — check `SHOW FULL TABLES` afterwards.
- **An interrupted migration keeps the ledger at spec 8.** `tm schema apply` reads
  the *whole* spec-9 shape (the column, the `revert_use` ENUM value and the
  UNIQUE/CHECK/self foreign key) before it stamps `spec_version`; a ledger with
  only some of it keeps its stored counter, so this step still runs afterwards and
  finishes the missing pieces. Check with
  `SELECT value FROM ledger_meta WHERE key = 'spec_version'`: it must still read
  `8`. If it reads `9` on a half-patched ledger, the counter was stamped by a
  tool older than 0.8.57 — set it back (`UPDATE ledger_meta SET value = '8' WHERE
  key = 'spec_version'`, one commit) and rerun this step; the plan adds only what
  is missing.
- **Rollback after success:** reset the branch to the tag
  (`dolt reset --hard pre-migrate-spec-8`) and use the older tool. Data written
  by the newer tool is the risk: a `revert_use` row does not parse against the
  spec-8 ENUM, so reset first and re-apply the events you want to keep.
- **`aliquot purge` order.** Purging an aliquot deletes all of its events in one
  `DELETE`; the `ON DELETE CASCADE` on the self foreign key is what makes that
  order-safe, so do not drop that constraint on a ledger you keep.
