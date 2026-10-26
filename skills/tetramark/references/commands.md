# `tm` command reference

Every command-line form in the project. Global options must come **before** the subcommand.

## Global options

```
ledger [--workspace|-w PATH] [--ledger|-l NAME] [--dry-run] [--no-commit]
       [--json] [--format|-f table|json|csv] [--version] COMMAND ...
```

| Option | Meaning |
|---|---|
| `--workspace PATH` | Workspace data directory; default: search upward for `workspace.yaml`, then `$TETRAMARK_WORKSPACE`. |
| `--ledger NAME` | Ledger to open; default: `default_ledger` from `workspace.yaml`. Required when there is no default. |
| `--dry-run` | Print/collect the SQL of a write and change nothing. |
| `--no-commit` | Execute the write but leave it uncommitted (advanced). |
| `--json` | Print reads and write results as JSON. |
| `--format table\|json\|csv` | Read output format (default `table`). |
| `--version` | Print the version. |

Exit codes: `0` ok, `1` business/validation error, `2` usage error, `3` database unreachable.

## Workspace and server

```sh
tm workspace init [PATH] [--port 13308] [--host 127.0.0.1] \
  [--default-ledger NAME] [--author "Name <email>"]
tm server start [-d|--daemon]
tm server stop
tm server status
```

A workspace is one data directory with `workspace.yaml`. Every direct subdirectory containing `.dolt`
is a ledger. `server start` refuses to run when there is no ledger yet, so create the first ledger
first. `status` lists ledgers and prints the `mysql://root@host:port` endpoint.

## Ledger lifecycle and presets

```sh
tm init NAME --preset base [--preset molbio] [--id-prefix 12] \
  [--author "Name <email>"] [--status pending --status stored ...]
tm presets list
tm presets show NAME
```

`init` creates the Dolt database, writes `ledger.yaml` and the meta row, generates DDL/views/tests and
commits once. `--preset` is repeatable; a value can also be a path to a local preset directory or a
preset YAML file.
`--id-prefix` feeds the default date-code stock strategy (`1226FP01` for prefix `12`, 2026-06-16).
`--status` replaces the default vocabulary `pending, stored, missing, consumed`.

`presets show` prints the include-resolved preset as YAML.

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

See `skills/tetramark-schema/` for authoring and evolution rules.

## Entities

```sh
tm entity add KIND [--name NAME] [--set FIELD=VALUE ...] [--alias ALIAS ...] \
  [--id EXPLICIT_ID] [--json]
tm entity show ID|ALIAS [--json]
tm entity edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm entity ls [KIND] [--where FIELD=VALUE ...] [--json]
```

`--set` and `--alias` are repeatable. `--name` also fills a kind's `name` field when it has one.
`--id` is for back-filling and migration; it must match the entity ID strategy (UUID by default).
`--where` filters match the kind's columns and can be repeated (AND).
`show` includes the entity's aliases (sorted `aliases` list; an `aliases` row in table output).

## Stocks

```sh
tm stock add KIND --entity ID|ALIAS \
  [--set FIELD=VALUE ...] [--aliquot-set FIELD=VALUE ...] \
  [--parent STOCK] [--source-aliquot ALQ] \
  [--aliquots N] [--box NAME --slot A1 [--slot B2 ...]] \
  [--quantity Q --unit U] [--id EXPLICIT_ID] [--json]
tm stock show ID|ALIAS [--json]
tm stock edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm stock ls [KIND] [--where FIELD=VALUE ...] [--json]
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
```

- `--aliquots N` creates N tubes (default 1; `0` for a batch with no tube yet).
- `--box` with `--slot` places the newly created aliquots; slots are matched to aliquots in order.
- `--quantity`/`--unit` set the quantity of **each** new aliquot.
- `--aliquot-set` sets kind-specific aliquot fields (e.g. `glycerol_pct=15`).
- `--parent` records lineage to another stock; `--source-aliquot` records which tube was used.
- `stock show` includes the aliquot list and the stock's sorted aliases (`aliases`; an `aliases`
  row in table output).

`stock renumber` repairs ids that predate (or ignore) the ledger's stock id strategy. Give one REF and
`--id NEW` for a single rename, one or more REFs with `--auto` to generate strategy-shaped ids, or
`--map PATH` for a batch file: one `OLD<TAB>NEW` (tab or comma) per line, or `OLD` alone when combined
with `--auto`; blank lines and `#` comments are ignored. New ids are validated against `ids.stock` and
must not collide with an existing stock id or a stock alias. Only one of `--id`/`--auto`/`--map`
supplies the ids (`--map` may be combined with `--auto`), and `--id` takes exactly one REF.

The stock id, all its aliquot ids (`<new>.<seq>`, seq unchanged), `aliquot_event` rows, kind and
aliquot-extension tables, `parent_stock_id`/`source_aliquot_id` references and existing aliases are all
rewritten in **one** transaction and **one** Dolt commit (`renumber(stock): OLD -> NEW`, or
`renumber(stock): N stock(s)` for a batch). The old stock id and old aliquot ids are kept as aliases, so
`tm find OLD` still resolves and nothing is lost. Foreign keys are `NO ACTION`, so the primary-key
rename runs with `SET FOREIGN_KEY_CHECKS=0` and restores it in the same session before the commit;
`--dry-run` prints the mapping and the SQL and writes nothing. `--json` prints the old → new mapping.

## Aliquots

```sh
tm aliquot add STOCK [--count N] [--box NAME --slot S ...] \
  [--quantity Q --unit U] [--aliquot-set FIELD=VALUE ...] [--json]
tm aliquot move ALQ --box NAME [--slot S] [--json]
tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT] [--note TEXT] [--json]
tm aliquot status ALQ STATUS [--note TEXT] [--json]
tm aliquot purge ALQ [--json]
```

- Alias strings like `STOCK`/`ALQ` mean a full ID (`1226FP01`) or an alias (`p035`).
- `use` subtracts `AMOUNT` in the aliquot's own unit and sets `consumed` when the quantity reaches
  zero; the event stores the purpose.
- `status` sets one of the ledger's status vocabulary words.
- `purge` hard-deletes an aliquot and its events. It is the only hard delete; prefer `status consumed`.

## Boxes

```sh
tm box add KIND NAME [--in PARENT [--slot S]] [--notes TEXT] [--json]
tm box show NAME|ID|ALIAS [--json]
tm box ls [--kind KIND] [--where FIELD=VALUE ...] [--json]
tm box move NAME --in PARENT [--slot S] [--json]
```

`box add` materialises the slots implied by the geometry (`grid96` → 96, `grid9x9` → 81, `rack` → the
named positions, `shelf`/`freezer`/`drawer` → none). `--slot` is only valid for a slotted parent;
slotless parents take one child box each (up to `capacity`). Nesting must stay acyclic.

`box show` prints a terminal grid with an `aliases: …` line when the box has aliases (or `--json` for
`{box, kind, geometry, path, aliases, slots:[...]}`) and works for slotless boxes too (it lists child
boxes). `--format csv` prints one row per slot.

## Aliases, search, history

```sh
tm alias add OBJECT_TYPE OBJECT_ID ALIAS [--json]
tm alias rm [OBJECT_TYPE] ALIAS [--json]
tm find QUERY [--json]
tm history ID|ALIAS|BOX [--json]
```

`OBJECT_TYPE` is `entity`, `stock`, `aliquot` or `box`; aliases are unique per object type.
`alias rm ALIAS` removes that alias everywhere, `alias rm stock ALIAS` only from stocks.
`find` tries exact id, then alias, then name/kind text (case-insensitive) and shows summary and
location. `history` merges aliquot events and the Dolt commits that touched the object.

Aliases are also visible in the generated views (ledger format `spec_version` 2): every
`v_<object>_<kind>`, `v_stock_status` and `v_aliquot_location` row has a sorted, `;`-separated
`alias_list` (NULL when there are none), and `v_box_slots` has `occupant_alias_list` for the slot's
occupant (the aliquot's aliases for a tube, the box's aliases for a nested box). The column is not
called `aliases` because some kinds have a domain field with that name (e.g. `construct.aliases`).

## Validation

```sh
tm check [-v|--verbose] [--group|-g GROUP] [--format|-f table|json|csv] [--json]
```

Runs the generated `dolt_tests` and exits `1` when a test with severity `fail` fails. Tests whose
severity is `info` are reported but never change the exit code. `-v` prints offending rows for each
failure. `--group` accepts a group (`core`, `rules`, `schema:bacterial`) or a prefix (`rules:local`).

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
tm remote ls
tm remote rm NAME
tm remote push [NAME] [--branch BRANCH]
tm remote pull [NAME] [--branch BRANCH]
tm backup [DIR]
tm backup --ls          # same as: tm backup ls
```

`remote push`/`pull` default to `origin` (or the only remote) and the current branch. A local URL is
created by Dolt on push. `backup` uses `<data_dir>/backups/<ledger>` by default and syncs an existing
destination; `backup --ls` lists configured backups.

## Configuration

```sh
tm config get [author]
tm config set author "Name <email>"
```

The effective author is `ledger.yaml` → `workspace.yaml` → Git `user.name`/`user.email`. Setting it
updates `ledger.yaml`, the meta copy in the DB, and creates one commit.
