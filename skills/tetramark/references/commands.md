# `tm` command reference

Every command-line form in the project. A command operates on exactly one **ledger directory** (the
directory holding `.dolt/` and `ledger.yaml`). Global options must come **before** the subcommand.

## Global options

```
tm [--ledger|-l PATH] [--branch|-b BRANCH] [--dry-run] [--no-commit]
   [--json] [--format|-f table|json|csv] [--version] COMMAND ...
```

| Option | Meaning |
|---|---|
| `--ledger PATH` / `-l PATH` | Ledger directory (or its `ledger.yaml` file). Omit it to search upward from the current directory to the nearest ledger. |
| `--branch BRANCH` / `-b BRANCH` | Session branch for every read, write and commit of this command. Default: `branch:` in `ledger.yaml`, else the checked-out branch. A missing branch is a usage error; it is never created. |
| `--dry-run` | Print/collect the SQL of a write and change nothing. Takes no write lock. |
| `--no-commit` | Execute the write but leave it uncommitted (advanced). |
| `--json` | Print reads and write results as JSON. |
| `--format table\|json\|csv` | Read output format (default `table`). |
| `--version` | Print the version. |

There is **no** `-w/--workspace`, no ledger name and no environment variable that selects a ledger:
`-l PATH` wins, otherwise the upward walk finds the ledger from the current directory. Errors are
usage errors (exit 2) that name the path/cwd; a 0.4 `workspace.yaml` met before any ledger produces
the P4 message telling you to `cd` into the ledger directory (or pass `--ledger PATH`).

Writes are serialized per ledger: a write command takes an exclusive `flock` on
`<ledger>/.dolt/tetramark.lock` and holds it through the read → validate → write → commit window. A
second writer waits up to 10 s (`$TETRAMARK_LOCK_TIMEOUT`), then exits 2 with
`ledger busy: pid … (tm …) is writing …`. Reads and `tm status` never lock. All commands use the
embedded driver; there is no `dolt sql-server` to start or stop.

Exit codes: `0` ok (help included), `1` business/validation error, `2` usage error, `3` database
unreachable. Help (Tetramark-03j): `-h` and `--help` print the same text on every command and group
and exit 0; a bare `tm` or bare group prints its help and exits 0; misuse exits 2.

## Ledger lifecycle, status and presets

```sh
tm init PATH --preset base [--preset molbio] [--name N] [--id-prefix 12] \
  [--author "Name <email>"]
tm adopt PATH --preset molbio [--name N] [--id-prefix 12] \
  [--author "Name <email>"] [--force-config]
tm status [--json]
tm presets list
tm presets show NAME|PATH
```

- `init PATH` creates the ledger **directory** at PATH (relative to the cwd or absolute): `.dolt/`,
  `ledger.yaml` and `presets/`. Missing parents are created; an existing empty directory is reused;
  `ledger.yaml`'s `name:` defaults to the basename (override with `--name`; the path is not identity).
- `adopt PATH` formats an existing bare Dolt database in place (the 0.4 lab databases): same schema as
  init, history kept, one `adopt(ledger): NAME` commit and `adopted_from_commit` recorded. It needs an
  empty schema on the selected branch (no user tables, no views, no `dolt_tests` rows), a clean
  working set and no `ledger.yaml` (`--force-config` overwrites a stray one); nothing is deleted, and
  a failed adopt rolls back without removing the directory. See spec §3.1.
- Both commands take a repeatable `--preset` and refuse a nested target (`Ledgers do not nest`, P5).
  `tm --dry-run init|adopt …` prints the plan and SQL and writes nothing.
- `--preset` resolution: a value containing `/` or ending in `.yaml`/`.yml` is a **path** used as-is
  from the cwd. A bare value resolves through `$TETRAMARK_PRESETS_DIR` when set, otherwise the presets
  bundled with tetramark (the wheel ships `tetramark/presets/`); a missing source is a usage error.
  The preset and its `include:` closure are copied into `<ledger>/presets/`, and the ledger opens only
  that copy afterwards.
- `status` prints the resolved ledger: path, name, branch, `spec_version`, presets, author, write-lock
  holder and portability warnings (human block), or the fixed JSON shape with `--json`
  (`path`, `name`, `branch`, `spec_version`, `presets`, `author`, `lock`, `portability_warnings`).
  It never lists sibling ledgers; `--format csv` is a usage error. See spec §6.1.
- `presets list` lists the source a bare name resolves from; `presets show NAME|PATH` prints one
  include-resolved preset as YAML.

## Schema

```sh
tm schema show [KIND] [--json]
tm schema plan [--json]
tm schema apply [--allow-destructive]
```

- `show` prints the effective (presets + local kinds/rules) schema as YAML; with `KIND`, only that kind.
- `plan` diffs YAML against the live DB, tags each step `additive` or `destructive`, prints SQL.
- `apply` executes the plan in one transaction and one Dolt commit. Destructive steps are refused
  unless `--allow-destructive` is passed. After apply, `plan` must say `schema is up to date`.

`kinds_dir` and `rules_dir` in `ledger.yaml` are resolved against the ledger directory and must stay
inside it (P13): an absolute path, a `..` escape or a symlink leaving the ledger is a usage error, so
the ledger stays copyable. See `skills/tetramark-schema/` for authoring and evolution rules.

## Entities

```sh
tm entity add KIND [--name NAME] [--set FIELD=VALUE ...] [--alias ALIAS ...] \
  [--id EXPLICIT_ID] [--json]
tm entity show ID|ALIAS [--json]
tm entity edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm entity rename ID|ALIAS NEW_NAME [--json]
tm entity list [KIND] [--where FIELD=VALUE ...] [--json]
```

`--set` and `--alias` are repeatable. `--name` also fills a kind's `name` field when it has one.
`--id` is for back-filling and migration; it must match the entity ID strategy (UUID by default).
`--where` filters match the kind's columns and can be repeated (AND).
`rename` sets the name (and the kind's `name` field) and keeps the old name as an alias in the same
commit, so scripts and notes that use the old name still resolve. It refuses when the old name is
already another entity's alias.
`show` includes the entity's aliases (sorted `aliases` list; an `aliases` row in table output).
The default **list tables** abbreviate a UUID id to the shortest prefix that is unique in that table
(at least 8 characters, git style; colliding ids grow together). `show`, `--json` and `--format csv`
always print the full 36-character id, and datecode/sequence/aliquot ids are never shortened.

## Stocks

```sh
tm stock add KIND --entity ID|ALIAS \
  [--set FIELD=VALUE ...] \
  [--parent STOCK] [--source-aliquot ALQ] \
  [--aliquots N] [--status S] [--box NAME --slot A1 [--slot B2 ...]] \
  [--quantity Q --unit U] [--id EXPLICIT_ID] [--json]
tm stock show ID|ALIAS [--json]
tm stock edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm stock list [KIND] [--where FIELD=VALUE ...] [--json]
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
```

- `--aliquots N` creates N tubes (default 1; `0` for a batch with no tube yet).
- `--status` sets the creation status of the new aliquots (any status of the stock kind's set).
- `--box` with `--slot` places the newly created aliquots; slots are matched to aliquots in order.
- `--quantity`/`--unit` set the quantity of **each** new aliquot.
- `--parent` records lineage to another stock; `--source-aliquot` records which tube was used.
- `stock list` prints `id kind entity_id entity_name aliquots_in_stock status`; `entity_name` is the
  owning design's name, printed in full. `status` is derived from the aliquots: `empty` (none),
  `active` (at least one in stock) or `depleted` (every aliquot has left stock).
- `stock show` includes the aliquot list and the stock's sorted aliases (`aliases`; an `aliases`
  row in table output).

`stock renumber` repairs ids that predate (or ignore) the ledger's stock id strategy. Give one REF and
`--id NEW` for a single rename, one or more REFs with `--auto` to generate strategy-shaped ids, or
`--map PATH` for a batch file: one `OLD<TAB>NEW` (tab or comma) per line, or `OLD` alone when combined
with `--auto`; blank lines and `#` comments are ignored. New ids are validated against `ids.stock` and
must not collide with an existing stock id or a stock alias. Only one of `--id`/`--auto`/`--map`
supplies the ids (`--map` may be combined with `--auto`), and `--id` takes exactly one REF.

The stock id, all its aliquot ids (`<new>.<seq>`, seq unchanged), `aliquot_event` rows, kind
tables, `parent_stock_id`/`source_aliquot_id` references and existing aliases are all rewritten in **one** transaction and **one** Dolt commit (`renumber(stock): OLD -> NEW`, or
`renumber(stock): N stock(s)` for a batch). The old stock id and old aliquot ids are kept as aliases, so
`tm find OLD` still resolves and nothing is lost. Foreign keys are `NO ACTION`, so the primary-key
rename runs with `SET FOREIGN_KEY_CHECKS=0` and restores it in the same session before the commit;
`--dry-run` prints the mapping and the SQL and writes nothing. `--json` prints the old → new mapping.

## Aliquots

```sh
tm aliquot add STOCK [--count N] [--status S] [--box NAME --slot S ...] \
  [--quantity Q --unit U] [--json]
tm aliquot show ALQ [--json]
tm aliquot move ALQ --box NAME [--slot S] [--status S] [--json]
tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT] [--note TEXT] [--json]
tm aliquot status ALQ STATUS [--note TEXT] [--force] [--json]
tm aliquot purge ALQ [--json]
```

- Alias strings like `STOCK`/`ALQ` mean a full ID (`1226FP01`) or an alias (`p035`).
- `show` prints one tube: the aliquot row, its sorted `aliases`, the owning stock's `stock_kind` and
  `entity_id`, the `box_id`/`slot`, the `path` down the box nesting (`Freezer-1/Shelf-2/Box:A3`,
  printed on its own line) and the derived `in_stock` flag. `ALQ` is the full `<stock>.<seq>` id or
  one of the tube's aliases, including the old ids `tm stock renumber` keeps; `--json` prints the
  same record with the full ids. A tube that is not in stock has no position (`box_id`, `slot` and
  `path` are empty; `null` in JSON). The tube's events are in `tm history ALQ`; the stock kind's own
  fields are in `tm stock show`.
- `use` subtracts `AMOUNT` in the aliquot's own unit and sets `consumed` when the quantity reaches
  zero; the event stores the purpose.
- `status` sets one of the stock kind's status vocabulary words; `--force` requires `--note` and is
  the only way to make an unforced-illegal change.
- An aliquot's **position follows its status**: a status with `in_stock: true` needs a box (and a slot
  in a slotted box), and any other status clears `box_id`/`slot` in the same write. The status/use
  event keeps the old position in its `from_*` columns, so `history` still shows where the tube was.
- `move` of a not-in-stock aliquot also sets its in-stock status; when the stock kind's set has
  several in-stock statuses it asks for `--status` instead of guessing.
- `purge` hard-deletes an aliquot and its events. It is the only hard delete; prefer `status consumed`.

## Boxes

```sh
tm box add KIND NAME [--in PARENT [--slot S]] [--notes TEXT] [--json]
tm box show NAME|ID|ALIAS [--json]
tm box list [--kind KIND] [--where FIELD=VALUE ...] [--json]
tm box move NAME --in PARENT [--slot S] [--json]
tm box rename NAME|ID|ALIAS NEW_NAME [--json]
```

`box add` materialises the slots implied by the geometry (`grid8x12` → 96, `grid9x9` → 81, `rack` →
the named positions, `shelf`/`freezer`/`drawer` → none). `--slot` is only valid for a slotted parent;
slotless parents take one child box each (up to `capacity`). Nesting must stay acyclic, and a **grid
box holds tubes only**, so `--in` a grid box is refused. Every box name must be **view-safe**: the
generated view `v_box_<name>` must not collide with a core view, the name is at most 58 characters,
and it must not differ only in case from an existing box name (`tm box add`, box import and the CSV
path refuse it).

`box rename` applies the same name rules (a case-only change of the box's own name is allowed),
refuses a name that is another box's name or alias, keeps the old name as an alias, and replaces
`v_box_<old>` with `v_box_<new>` in the same commit.

`box show` prints a terminal grid with an `aliases: …` line when the box has aliases (or `--json` for
`{box, kind, geometry, path, aliases, slots:[...]}`) and works for slotless boxes too (it lists child
boxes). `--format csv` prints one row per slot.

## The interactive form (for people)

```sh
tm entity add-form
tm stock add-form
tm aliquot add-form
tm box add-form
```

The form asks for what a person does not know by heart — the kind, the fields, the box, the slots —
and writes the same row `add` writes. It takes **no positional argument and no flags**: every
question comes from the ledger's own schema. **Agents use `add`**, which is scriptable, never
blocks and writes the same thing.

- Step 1 is always the numbered list of kinds (shown even with one kind; Enter accepts it), then the
  kind's fields in schema order, each with its description and `*` when it is required. Computed
  fields are never asked; sequence fields are.
- Values are checked as you type: an enum by number or value, a bool as `y`/`n`, a date/datetime in
  ISO form (`today`/`now` accepted), an int/decimal parsed, a `pattern` checked, and a `ref` resolved
  right away with its name shown back — `?term` searches the way `tm find` does and you pick a
  result by number. Multi-value inputs (aliases, slots) take one value per line or comma-separated;
  an empty line ends the list.
- A `long: true` field is shown in short form, never in full:
  `[pasted 5,432 chars · 12 lines · ATGCGTACGT…TTAGCCGGAA]`. A bracketed paste is taken as it is,
  pasting again replaces it, Backspace on the placeholder clears it, and a short typed value shows
  inline.
- Keys: `Enter` goes to the next step (an optional field may be skipped and a default accepted; a
  required field left empty keeps the step and says `required`), `Shift+Tab` goes back to the
  previous step with its value pre-filled, `Alt+Enter`/`Ctrl+J` inserts a newline, `Tab` completes
  enum values and ref ids/aliases, `Ctrl+E` edits the current field in `$VISUAL` → `$EDITOR` → `vi`
  (the field name, its description and its pattern go below a git-style scissors line and are removed
  from what you edit, while `#` lines are kept), `Ctrl+C` aborts and writes nothing. A footer lists
  the keys.
- The stock form asks: entity (ref, with search) → the kind's fields → aliquot count → box (`?`
  lists the boxes) → slots (the next free slots are filled in by default; `?` shows the free slots).
- The review screen numbers every value (long values in short form): a number jumps back to that
  step, `y` writes, `q` cancels. The write lock is taken only on `y`; the write is the same
  `ledger.<objects>.add(...)` as `add`, prints the same message, and `--dry-run`/`--no-commit`/
  `--json` behave as they do for `add`. A rejection from the core shows the error and returns to
  the field that failed.

Without a terminal the command refuses (exit 2) and points at `tm <object> add`.

## Aliases, search, history

```sh
tm alias add OBJECT_TYPE OBJECT_ID ALIAS [--json]
tm alias remove [OBJECT_TYPE] ALIAS [--json]
tm find QUERY [--json]
tm history ID|ALIAS|BOX [--json]
```

`OBJECT_TYPE` is `entity`, `stock`, `aliquot` or `box`; aliases are unique per object type.
`alias remove ALIAS` removes that alias everywhere, `alias remove stock ALIAS` only from stocks.
An incomplete object id is accepted wherever a command takes an `ID|ALIAS`: when its prefix matches
exactly one object it resolves, and a prefix that matches several is a usage error (exit 2) naming
the candidates — a full id or exact alias always wins first.
`find` matches ids exactly and aliases, names and kind string/text fields by exact, prefix or
substring text (case-insensitive, literal), ranks exact id → exact alias → exact name → name prefix
→ alias prefix → name substring → alias substring → fields, and shows summary and location. `history` merges aliquot events and the Dolt commits that touched the object.

Aliases are also visible in the generated views: every `v_entity_<kind>`, `v_stock_<kind>`,
`v_stock_status` and `v_aliquot_location` row has a sorted, `;`-separated `alias_list` (NULL when
there are none), and `v_box_slots` has `occupant_alias_list` for the slot's occupant (the aliquot's
aliases for a tube, the box's aliases for a nested box). The column is not called `aliases` because
some kinds have a domain field with that name (e.g. `protein_construct.aliases`).

Since spec 5 every **existing box** has its own `v_box_<box name>` view (grid pivot with a `row`
column and one column per column label; `slot | occupants` for a named box; one `occupant` column
for a slotless box). There are no `v_box_<kind>` views any more.

Every `v_stock_<kind>` (ledger format 4) also shows the stock's own facts, before `alias_list`:
`entity_kind`/`entity_name` (the owning design), `aliquots_in_stock` (tubes on the shelf),
`aliquots_total`, `quantity_in_stock` + `unit` (sum of the in-stock quantities; both NULL when they
mix units), and `status` — the same derived status `v_stock_status` reports (`active`, `depleted`,
`empty`). Aliquots have no kind: a property of the material belongs on the stock kind.

## Validation

```sh
tm check [-v|--verbose] [--group|-g GROUP] [--format|-f table|json|csv] [--json]
```

Runs the generated `dolt_tests` and exits `1` when a test with severity `fail` fails. Tests whose
severity is `info` are reported but never change the exit code. `-v` prints offending rows for each
failure. `--group` accepts a group (`core`, `rules`, `schema:bacterial_glycerol`) or a prefix
(`rules:local`). The `portability:info` rule reports absolute `file://` remote/backup URLs that point
outside the ledger (P13); it is informational and never fails the command.

## CSV import / export

```sh
tm import KIND FILE.csv [--object|-o entity|stock|aliquot|box] [--json]
tm import all DIRECTORY [--json]
tm export KIND|all [--out|-o DIRECTORY] [--json]
```

`import` validates every row first and writes all-or-nothing in one commit. `KIND` is a kind name or
a core object (`entity`, `stock`, `aliquot`, `box`); `--object` forces the object type when the file
has no `kind` column. `import all` expects the file names `export all` writes (`boxes.csv`,
`aliases.csv`, one CSV per kind) and imports them in dependency order: entities, boxes, stocks,
aliquots, aliases.

`export all` writes one file per kind plus `boxes.csv`, `aliquots.csv` and `aliases.csv`; `export KIND`
writes one file. The default output directory is the current directory.

## Remotes and backups

```sh
tm remote add NAME URL
tm remote list
tm remote remove NAME
tm remote push [NAME] [--branch BRANCH]
tm remote pull [NAME] [--branch BRANCH]
tm backup [DIR] [--name NAME]
tm backup --list        # same as: tm backup list
```

`remote push`/`pull` default to `origin` (or the only remote) and the current branch. A local URL is
created by Dolt on push.

`backup` has **no default directory** (P10): a backup next to the ledger shares its disk and is lost
with it. `tm backup DIR` configures a Dolt backup (name: the ledger name, or `--name`) and syncs it;
a later `tm backup` syncs the ledger-named configured backup, `tm backup --name NAME` another one, and
`tm backup --list` lists what is configured. With no DIR and no configured backup the command is a
usage error and nothing is written.

## Configuration

```sh
tm config get [author]
tm config set author "Name <email>"
```

The effective author is `ledger.yaml` → Git `user.name`/`user.email` (P6); there is no workspace
tier. Setting it updates `ledger.yaml`, the meta copy in the DB, and creates one commit. `author` is
the only configuration key (`tm config get host` / `presets_dir` are usage errors).
