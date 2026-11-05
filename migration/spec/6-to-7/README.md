# spec 6 → 7 — `aliquot_event.message` replaces `note` and `purpose`

Counter: `ledger_meta.spec_version` 6 → 7. Tag before writing: `pre-migrate-spec-6`.
One commit: `migrate(ledger): spec 6 -> 7`.

**This is the only migration step with a step-local `--apply` flag.** Without it the
script is a **dry run** (`--dry-run` is accepted and means exactly the same); `--apply`
writes. Every other step takes `--dry-run` and writes by default.

## What changes

Spec 7 (`Tetramark-fga.1`) ends the naming mismatch spec 6 left behind: `tm aliquot use`
and `tm aliquot move` took `-m/--message`, but stored the text in `aliquot_event.note`,
and the unused `purpose` column confused the event record. The step changes the
`aliquot_event` shape in one commit:

1. **`note` is renamed to `message`** with
   ``ALTER TABLE `aliquot_event` RENAME COLUMN `note` TO `message` `` — a rename, never
   copy-and-drop, so the column keeps its position. After `purpose` is dropped the order
   is `..., forced, actor, message, at`, exactly a fresh spec-7 ledger's DDL
   (`tests/data/golden/ddl_full.sql`).
2. **`purpose` is folded into `message` and dropped.** One `UPDATE` runs after the
   rename; then ``ALTER TABLE `aliquot_event` DROP COLUMN `purpose` ``.

The read side follows in the same release: `tm aliquot show --json` event records and
`tm history --json` event rows carry `message`, and `tm history --format csv` has the
16-column header `section,at,event,aliquot_id,amount,unit,from_box,from_slot,to_box,
to_slot,old_status,new_status,forced,actor,commit_hash,message` where an event row fills
`message` with the event text and a commit row fills it with the commit message.

### The fold rule

`purpose` and `message` are both `MEDIUMTEXT`; "empty" below means `NULL` **or**
`TRIM(x) = ''`. Values are copied **verbatim**, not trimmed:

| purpose | resulting `message` |
| --- | --- |
| empty | unchanged |
| empty message, non-empty purpose | `message = purpose` |
| both non-empty | `message = CONCAT(purpose, '; ', message)` |

`N` in the printed line counts the rows with a non-empty purpose; the dry run and the
apply both print `purpose folded into message: N row(s)`.

**This fold supersedes the spec 5 → 6 promise that "past events stay untouched", for this
one column only.** The 5 → 6 step deliberately left old event rows and their vocabulary
alone; spec 7 has to move the two text columns into the one column the tool now reads, so
a non-empty `purpose` is merged into the message of its own row. The original values are
not lost: they remain in Dolt history, and the `pre-migrate-spec-6` tag names the commit
they were read from.

## The column matrix

The shape is read from `information_schema` (like the spec 3 → 4 step), because a ledger
may have been hand-patched. `N` = `note`, `M` = `message`, `P` = `purpose`:

| live shape | what the step does |
| --- | --- |
| N, no M, P | `RENAME COLUMN note TO message`; fold; `DROP COLUMN purpose` |
| N, no M, no P | `RENAME COLUMN note TO message` |
| M, no N, P | fold; `DROP COLUMN purpose` |
| M, no N, no P | `MODIFY COLUMN message MEDIUMTEXT AFTER actor` (normalises the position and gives the runner its at-least-one statement) |
| N **and** M | **refuse** — ambiguous, nothing written, no tag |
| neither N nor M | **refuse** — damaged, nothing written, no tag |

The two refusals happen in `plan`, i.e. before the tag and the commit: nothing is
written.

## Preconditions

- `ledger_meta.spec_version` = **6** (the script refuses any other counter; a second run
  refuses because the counter is 7).
- A clean Dolt working set: the migration is the only thing in its commit.
- For a **real lab ledger**: dry-run first, let the operator review the plan, and apply
  on a Dolt branch (`--branch tetramark-upgrade`). Merge only after the operator's OK and
  never push to `origin` without it.
- The ledger's `<ledger>/presets/` copies are not touched: this step changes only Dolt
  content. A spec-6 ledger spends no stock field or preset change here.
- Run it after `migration/spec/5-to-6/README.md` (and every pending preset step that
  belongs to it). The `pre-migrate-spec-6` tag is the restore point.

## Running it

```sh
LEDGER=/path/to/ledger

# 1. dry run (the default; prints the tag, the rename/fold/drop, the meta bump and the
#    fold count, and writes nothing)
uv run migration/spec/6-to-7/migrate.py --ledger "$LEDGER"
uv run migration/spec/6-to-7/migrate.py --ledger "$LEDGER" --dry-run   # identical

# 2. apply: one commit, tag first
uv run migration/spec/6-to-7/migrate.py --ledger "$LEDGER" --apply

# 3. on a branch of a served ledger
uv run migration/spec/6-to-7/migrate.py --ledger "$LEDGER" --branch tetramark-upgrade --apply
```

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter, commit, tag
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 7
dolt log -n 1                                                                      # migrate(ledger): spec 6 -> 7
dolt tag | grep pre-migrate-spec-6
# 2. the column set and its order: ..., forced, actor, message, at; no note/purpose
dolt sql -q "SELECT column_name, ordinal_position FROM information_schema.columns
             WHERE table_schema = DATABASE() AND table_name = 'aliquot_event'
             ORDER BY ordinal_position"
# 3. the folded values (N above; the first rows are the ones that had a purpose)
dolt sql -r csv -q "SELECT id, message FROM aliquot_event
                    WHERE message IS NOT NULL ORDER BY id"
# 4. validation: no FAIL
uv run tm -l "$LEDGER" check
# 5. no leftover event-column use in the ledger's own kinds/rules
grep -rn '\bpurpose\b\|\bnote\b' "$LEDGER"/kinds "$LEDGER"/rules || true
```

`tm check` must report no `FAIL`. A hand-written view over `aliquot_event.note` or
`aliquot_event.purpose` must be updated by the operator — this step does not touch
hand-written objects.

## Failure, rollback, caveats

- **A refusal is not a failure.** The ambiguous/damaged shapes write nothing: no tag,
  no commit, no DDL. Fix the column set and retry.
- **Dolt DDL is not transactional.** If a statement fails mid-way the earlier statements
  stay in the working set; the script makes **no** commit and prints the last good commit
  plus `dolt reset --hard <last-good-commit>`. The `pre-migrate-spec-6` tag stays as the
  restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-spec-6` (or the tag's hash),
  then open the ledger with the older tool (0.8.7). The step changed only Dolt content
  (one table shape, event rows, `ledger_meta`, `dolt_tests`), so an older tool reopens the
  ledger after the reset; its `aliquot_event.note`/`purpose` come back with the reset.
- **The old values stay in Dolt history.** The tag commits the pre-migration shape, so the
  original `note`/`purpose` columns are recoverable with `dolt diff pre-migrate-spec-6` or
  `dolt show <tag>` even after the fold.
- **Nothing else changes.** `tm export`/`tm import` column sets, presets and the frozen
  historical steps are untouched; `purpose` was never written by `use`/`move`, so only
  hand-written rows carry a foldable value.
