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
tm skill path [NAME] [--json]
tm skill version [NAME] [--file PATH] [--json]
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
- `skill path [NAME]` prints where the copy of the skill (`tetramark` or `tetramark-schema`, default
  `tetramark`) lives: `$TETRAMARK_SKILLS_DIR` first, then the checkout's `skills/`, then installed
  package data. `--json` prints `name`, `path` and `source` (`env`/`checkout`/`package`). It never
  opens a ledger, so it works outside a ledger directory.
- `skill version [NAME]` compares the copy's frontmatter `version:` with `tm --version` and prints
  `skill <v> (<SKILL.md>); tm <v>; match|mismatch`; `--json` prints `name`, `skill_version`,
  `skill_path`, `tm_version` and `match`. `--file PATH` checks another copy instead (a `SKILL.md` or
  its directory): a stale 0.4-era copy warns `describes an older CLI` on stderr and still exits 0,
  and a copy without a version warns that it predates versioned skills. Only a missing file or a file
  without YAML frontmatter exits 1. See spec §6.5 and §10.
- A new ledger is **spec 6**: the shipped presets declare no status vocabulary, and `tm init` starts
  its tubes `stored` (or whatever the creation `--status` says). An older ledger still opens with its
  own stored preset copies — spec 5's per-kind status sets keep working — and `tm` warns
  `ledger is spec N, tm is spec N+1: follow migration/spec/N-to-N+1/README.md to upgrade it manually`.
  The spec 5 → 6 step is **manual** (`migration/spec/5-to-6/README.md`): every pending historical
  preset step runs first, then the whole preset closure is replaced with the status-free copies, the
  statuses are remapped (one `migrated: <old status>` event per remapped aliquot), and
  `tm schema plan`/`apply` writes `spec_version` 6, the preset versions, the `aliquot.status` `CHECK`
  and the regenerated tests in one commit. Past events are never rewritten; the new enum stock
  properties are not backfilled.

## Schema

```sh
tm schema show [KIND] [--json]
tm schema plan [--json]
tm schema apply [--allow-destructive]
```

- `show` prints the effective (presets + local kinds/rules) schema as YAML; with `KIND`, only that kind.
- `plan` diffs YAML against the live DB, tags each step `additive` or `destructive`, prints SQL.
- `apply` executes the plan in one transaction and one Dolt commit. Destructive steps are refused
  unless `--allow-destructive` is passed. After apply, `plan` must say `schema is up to date`. On a
  format upgrade the same commit writes `ledger_meta.spec_version`/`preset_versions`, the fixed-status
  `CHECK` and the regenerated `dolt_tests`.

`kinds_dir` and `rules_dir` in `ledger.yaml` are resolved against the ledger directory and must stay
inside it (P13): an absolute path, a `..` escape or a symlink leaving the ledger is a usage error, so
the ledger stays copyable. See `skills/tetramark-schema/` for authoring and evolution rules.

## Aliquot status (fixed)

Every aliquot has one status from one hardcoded set that every stock kind shares (spec §1.1). No
preset or ledger declares `status_sets:`/`statuses:` any more; `tm schema show KIND` prints the same
four-entry `statuses:` block for every stock kind.

| status | in stock | can change to |
|---|---|---|
| `pending` | no | `stored`, `missing`, `consumed` |
| `stored` | yes | `missing`, `consumed` |
| `missing` | no | `stored`, `consumed` |
| `consumed` | no | none (final) |

Creation (`--status` on `tm stock add`/`tm aliquot add`, the CSV `status` column) accepts any of the
four; only `stored` counts as in stock, so `tm aliquot list` shows `stored` tubes by default and only
a `stored` tube may carry a box position. A later change (`tm aliquot status ALQ NEW`) must follow the
table. `--force` requires `--note TEXT`, overrides the transition check and records
`aliquot_event.forced = TRUE`; putting a wrongly `consumed` tube back means placing it again — a
`move` of a not-in-stock tube restores `stored`.

Detailed life states are **stock properties or event messages**, not statuses: optional enum `fate`
on `mouse_cohort` and `fish_group`/`fish_clutch` (euthanized, found_dead, transferred, cancelled) and
on `fly_culture` (discarded, lost), and optional enum `condition` on `worm_culture` (growing,
starved, contaminated). Set them with `tm stock edit STOCK --set fate=euthanized` or
`--set condition=starved`, or record a one-off in the event's `--note`. `thawed` is `consumed`: a cryo
vial is `stored` in the freezer, and `missing` or `consumed` after it leaves.

## Filters (`--where`, `tm find`)

`tm entity list`, `tm stock list`, `tm box list` and `tm find` share one filter syntax; the Python
`where=` argument is the same code.

```sh
tm entity list [KIND] [--where FILTER ...] [--json]
tm stock list [KIND] [--where FILTER ...] [--json]
tm box list [--where FILTER ...] [--json]
tm find [QUERY] [-E|--regex] [--where FILTER ...] [--with-rewrite] [--json]
```

- `FILTER` is one of `field=value` (exact), `field~pattern` (regex), `field!=value`,
  `field!~pattern`, and the ordered comparisons `field<value`, `field<=value`, `field>value`,
  `field>=value`. The operator is the one that follows the field name, so the value may contain
  `=`, `~`, `!`, `<` and `>`; the field name must be an identifier and is case-sensitive;
  whitespace before the name and between the name and the operator is skipped, and the value is
  everything after the operator, verbatim.
- `=` applies the field's schema coercion on a kind field (`enum` checked, `ref` resolves an id or
  alias, numbers/dates parsed); a core column is compared as written, so `entity_id` takes the stored
  id, not an alias.
- `field!=value` is the complement of `field=`: another value, and the `NULL` rows (the same
  coercion, the same core-column rule). `--where 'notes!=checked'` finds the rows whose `notes` is
  something else, including rows without `notes`.
- `field=` (an empty value) finds the rows where the field is empty: `NULL` or `''` on a text-like
  column (kind `string`/`text`/`enum`/`sequence`/`ref`, and every core column except `created_at`),
  `NULL` only on a non-text one (kind `int`/`decimal`/`bool`/`date`/`datetime`, core `created_at`;
  a stored `0` never matches).
  It skips the schema coercion and alias resolution above, so an empty `ref`/`enum`/`int` is not
  resolved or parsed; `none`/`null` stay literal and whitespace-only values are not special.
  `field!=` (an empty value) is its complement: the rows that have a value, the `NULL` and empty
  rows excluded — the "this field is filled in" filter.
- `~` matches the column's **text form** with a regex: a *search* (unanchored; anchor with `^`/`$`),
  **case-sensitive** (`(?i)` ignores case), compiled by Dolt in its ICU dialect (`\d`, `\p{L}`,
  lookarounds; not Python `re`). An `int`/`date`/`datetime`/`created_at` column matches its printed
  value (`'length_bp~^15'`, `'created_at~^2026-11-04'`); a `ref` matches the stored id, never an
  alias; `NULL` never matches a `~` pattern (not even `'field~^$'`); use `field=` to find empty
  values.
- `field!~pattern` is the complement of `~`: the rows whose text form does not match, plus the
  `NULL` rows. `--where 'name!~GFP'` finds the rows without `GFP` in the name, unnamed rows included.
- Ordered comparisons (`--where 'od600>=2'`) are typed by the field: a kind field's value is coerced
  like an `=` value (so `--where 'count<abc'` is the integer usage error) and a core column takes the
  text as written, except `created_at`. `int`/`decimal` compare numerically, `date`/`datetime`
  chronologically, and `string`/`text`/core text columns in the database's collation order (the
  order `=` uses); core `created_at` is chronological and takes an ISO date or datetime, a date
  meaning its midnight (`tm find --where 'created_at>=2026-01-01'`); `bool`, `enum`, `ref`, `sequence`
  and computed fields have no order (usage error, exit 2; an enum's declaration order is not used); a
  `NULL` never matches. A kind that declares a filtered field with no order makes the whole listing
  or `tm find` fail instead of skipping that kind.
- The negated operators are the complements of their positive ones: `!=` of `=`, `!~` of `~`, and
  `field!=` of `field=`. `field!=x` includes the `NULL` rows, so it is not "the field is set"; only
  `field!=` is.
- Rules: one `=` per field (a second is a usage error), no `=` mixed with another operator on one
  field, any number of the other operators per field (ANDed), no empty pattern (`name~`) or
  comparison value (`count<`), an invalid pattern is a usage error that names the filter, and an
  unknown field is a usage error listing the valid fields.
- Valid fields: the object's core filter columns (`id`, `kind`, `name` for entity/box, `created_at`,
  `notes`, plus `entity_id`/`parent_stock_id`/`source_aliquot_id` for stock and
  `parent_box_id`/`parent_slot` for box) and the fields of the kinds in scope. A listing only reads
  kinds that have every filtered field. A computed kind column works with `~` and `!~` (its text
  form) but not `=` or `!=`; derived output columns (`path`, `capacity`, `occupied`, `status`) are not
  filterable. `tm find` accepts the union over entity/stock/box kinds, applies the filters to every
  hit, and excludes aliquots whenever a filter is given.
- With no `QUERY`, `tm find --where ...` lists every match (`match` = `filter`) sorted by object type,
  kind and id; passing neither a QUERY nor `--where` is a usage error. No match exits 1 for `find`
  (`no match for '<Q>'`, or `no objects match the --where filters`); a listing with no rows prints
  `(none)` (empty in JSON) and exits 0. A bad filter or regex exits 2.
- Python: `where=` takes a mapping (equality only; an operator is never parsed from a key or a value,
  and a `None`/`""` value means empty, e.g. `{"name": "pUC19"}` or `{"notes": None}`) or a list of
  strings with every operator (`where=["od600>=2", "notes!="]`). Listings and `find` read in one
  batch.

`tm find -E` makes QUERY a case-insensitive ICU regex over the same places as the literal search (ids
of all four object types, aliases, names, kind `string`/`text` fields); an invalid pattern is a usage
error with no literal fallback. The `--where` patterns (`~` and `!~`) and `-E` use the database (ICU)
dialect; the ledger's `find.rewrites` rules are Python `re` and do not share it (see **Aliases,
search, history** below).

`tm aliquot list --where` is its own exact-only `FIELD=VALUE` filter, not this syntax; it does not
take the new operators in this release.

## Entities

```sh
tm entity add KIND [--name NAME] [--set FIELD=VALUE ...] [--alias ALIAS ...] \
  [--id EXPLICIT_ID] [--json]
tm entity show ID|ALIAS [--json]
tm entity edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm entity rename ID|ALIAS NEW_NAME [--json]
tm entity purge ID|ALIAS [--json]
tm entity list [KIND] [--where FILTER ...] [--json]
```

`--set` and `--alias` are repeatable. `--name` also fills a kind's `name` field when it has one.
`--id` is for back-filling and migration; it must match the entity ID strategy (UUID by default).
`--where` takes the shared filters (see **Filters** above) and can be repeated (AND).
`rename` sets the name (and the kind's `name` field) and keeps the old name as an alias in the same
commit, so scripts and notes that use the old name still resolve. It refuses when the old name is
already another entity's alias.
`show` includes the entity's aliases (sorted `aliases` list; an `aliases` row in table output).
`purge` hard-deletes an entity added by mistake in one commit (`purge(entity): …`): its kind row, its
alias rows and the entity row. It refuses (exit 1, nothing written) while a stock or a kind `ref`
field still points at the entity, naming the referents (capped) and `tm stock purge REF` (or the edit
that clears the ref); the rows stay in Dolt history. `--json` adds the `aliases` count to
`{object, id, kind, purged: true}`.
The default **list tables** abbreviate a UUID id to the shortest prefix that is unique in that table
(at least 8 characters, git style; colliding ids grow together). `show`, `--json` and `--format csv`
always print the full 36-character id, and datecode/sequence/aliquot ids are never shortened.
`entity list` prints `id kind name stocks_available aliquots_in_stock`: the number of the entity's
stocks whose derived status is `active`, and the in-stock aliquots summed over its stocks. Both are
`0` for an entity without stocks; depleted and empty stocks are not counted.

## Stocks

```sh
tm stock add KIND --entity ID|ALIAS \
  [--set FIELD=VALUE ...] \
  [--parent STOCK] [--source-aliquot ALQ] \
  [--aliquots N] [--status S] [--box NAME --slot A1 [--slot B2 ...]] \
  [--quantity Q --unit U] [--id EXPLICIT_ID] [--json]
tm stock show ID|ALIAS [--json]
tm stock edit ID|ALIAS [--set FIELD=VALUE ...] [--json]
tm stock list [KIND] [--where FILTER ...] [--json]
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
tm stock purge ID|ALIAS [--json]
tm stock aliquot STOCK AMOUNT UNIT [--count N] [--status S] [--box NAME --slot S ...] [--json]
```

- `--aliquots N` creates N tubes (default 1; `0` for a batch with no tube yet).
- `--status` sets the creation status of the new aliquots: any of the four fixed statuses (spec
  §1.1); the default is `stored`.
- `--box` with `--slot` places the newly created aliquots; slots are matched to aliquots in order.
- `--quantity`/`--unit` set the quantity of **each** new aliquot.
- `stock aliquot STOCK AMOUNT UNIT [--count N] [--status S] [--box NAME --slot S ...]` is
  positional sugar for `tm aliquot add STOCK --quantity AMOUNT --unit UNIT`: `AMOUNT` is the
  quantity of **each** new tube and both positionals are required (the core rejects a unit
  without a quantity, so the amount and its unit always come together). Output, JSON, the single
  `add(aliquot): <stock_id> x<N>` commit and the exit codes are the same as `aliquot add` (see
  **Aliquots** below).
- `--parent` records lineage to another stock; `--source-aliquot` records which tube was used.
- `stock list` prints `id kind entity_id entity_name aliquots_in_stock status`; `entity_name` is the
  owning design's name, printed in full. `status` is derived from the aliquots: `empty` (none),
  `active` (at least one in stock) or `depleted` (every aliquot has left stock). `--where` takes the
  shared filters (see **Filters** above).
- `stock show` prints the stock's fields, its sorted aliases and its tubes: each tube's JSON row
  carries `id`, `seq`, `status`, `quantity`, `unit`, `label`, `box_id`, `slot`, the slot-inclusive
  `path`, its sorted `aliases` and the derived `in_stock`. In text output each tube is a **card**
  with a rounded border (`<id> · <status>`, quantity/unit, `label:`, path or `not in stock`,
  `aliases:`), not a JSON blob; cards flow left to right and wrap at the console width, and
  `tm aliquot list STOCK` is the tabular view.
- `purge` hard-deletes a stock added by mistake in one commit (`purge(stock): …`): its own aliquots —
  each tube's event rows, alias rows and row — then its kind row, alias rows and stock row. It refuses
  (exit 1, nothing written) while another stock has `parent_stock_id` = it, `source_aliquot_id` = one
  of its aliquots or a kind `ref` field still points at it or at one of its aliquots. The human line is
  `Purged stock X (N aliquot(s), M event(s) removed)`; `--json` adds the `aliquots`, `events` and
  `aliases` counts.

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
tm aliquot list [STOCK] [--kind KIND] [--box NAME] [--status S ...] \
  [--all | --not-in-stock] [--where FIELD=VALUE ...] [--json]
tm aliquot move ALQ --box NAME [--slot S] [--status S] -m MSG [--json]
tm aliquot use ALQ [AMOUNT UNIT] -m MSG [--json]
tm aliquot status ALQ STATUS [--note TEXT] [--force] [--json]
tm aliquot label ALQ LABEL [--replace] [--json]
tm aliquot purge ALQ [--json]
```

- `label` sets the tube's `label`. A tube that already has a different label is refused unless
  `--replace` is given; then the old label becomes an alias of the aliquot (it still resolves the
  tube) and the new label is set, in one commit.

- Alias strings like `STOCK`/`ALQ` mean a full ID (`1226FP01`) or an alias (`p035`).
- `list` prints the tubes an experiment can use: only `stored` tubes by default, because a list is
  the "what is available" view, not the history. `--all` adds every status and `--not-in-stock`
  shows only the tubes that left stock (the missing/consumed audit). `--status S` is repeatable (OR)
  and turns the default scope off, so `--status missing` works; `--all` with it is redundant but
  legal, `--not-in-stock` with it intersects. `STOCK` limits
  the listing to one stock (not its child stocks), `--kind` to a stock kind, and `--box` to a box
  plus every box nested inside it. `--where` matches aliquot core columns exactly and ANDs with the
  rest; `FIELD=` (an empty value) means the column is empty — `NULL` or `''` for
  `id`/`stock_id`/`box_id`/`slot`/`unit`/`label`/`notes`, `NULL` only for
  `seq`/`quantity`/`created_at`. It is the aliquot's own exact-only `FIELD=VALUE` filter, not the
  shared `field~pattern` syntax of **Filters** above. Filter status with `--status`, never
  `--where status=…`. The table and CSV print
  `id stock_kind status quantity unit label path` (path is empty when not in stock); `--json` prints
  the same record `show` prints without `events` (a list never reads a tube's event trail), one per
  tube, ordered by stock id and numeric seq.
- `show` prints one tube: the aliquot row, its sorted `aliases`, the owning stock's `stock_kind` and
  `entity_id`, the `box_id`/`slot`, the `path` down the box nesting (`Freezer-1/Shelf-2/Box:A3`,
  printed on its own line), the derived `in_stock` flag (true exactly when the status is `stored`)
  and the tube's `events`, oldest first (`[]` when there are none). Each event names its `event`,
  `at`, `amount`/`unit`, the `from_box`/`from_slot` and `to_box`/`to_slot` (names, falling back to
  ids), the `old_status`/`new_status`, `forced` and `actor`/`message`. The text output ends
  with `events  N` and one line per event rendered by the same `event_line` helper `history` uses;
  `--json` prints the whole record with the full ids, events included; `--format csv` keeps the flat
  single row and drops `events` (use `tm history ALQ --format csv` for the event rows). `ALQ` is the
  full `<stock>.<seq>` id or one of the tube's aliases, including the old ids `tm stock renumber`
  keeps. A tube that is not in stock has no position (`box_id`, `slot` and `path` are empty; `null`
  in JSON). The Dolt commits are in `tm history`; the stock kind's own fields are in `tm stock show`,
  and `list STOCK` is the rich per-stock tube listing.
- `use` consumes part or all of a tube. `AMOUNT` and `UNIT` come together: passing only one of them
  is a usage error, and with an amount the unit must match the tube's unit and the amount may not
  exceed what remains. Leaving both out means "use everything left" — the event records the tube's
  remaining quantity and unit, exactly as if you had passed them. A tube with a NULL quantity can
  only be used whole (the event records a NULL amount); a partial use of such a tube is refused.
  `use` works on every aliquot and is refused only when the tube is already `consumed`. Reaching
  zero sets `consumed` and clears the box position in the same write (the event keeps the old
  position). The human line names the amount used and ends with the message, e.g.
  `Used all 200 ml from X (consumed): <msg>`; `--json` adds `used_amount`, `used_unit` and `message`
  (`used_amount` is `null` for a whole use of a tube with no quantity).
- `move` and `use` are **irreversible physical actions**, so both require `-m/--message`: the value
  is trimmed, a blank value is refused, and there is no length limit. It is event text, stored in
  `aliquot_event.message` (spec 7) — never the ASCII commit message. `use`'s old `--purpose`/`--note`
  flags are removed outright (no aliases); `tm aliquot status --force --note` is unchanged.
- `status` moves a tube to one of the four fixed statuses and must follow the transition table above;
  `--force --note TEXT` overrides an illegal transition deliberately (the event records
  `forced = TRUE`). Putting a wrongly `consumed` tube back is a `move` into a box, not a status
  change.
- An aliquot's **position follows its status**: a `stored` aliquot needs a box (and a slot in a
  slotted box), and any other status clears `box_id`/`slot` in the same write. The status/use
  event keeps the old position in its `from_*` columns, so `history` still shows where the tube was.
- `move` of a not-in-stock aliquot also sets it to `stored`; with `--status` the normal transition
  check runs and the result must still be `stored`.
- `purge` hard-deletes a tube added by mistake in one commit (`purge(aliquot): …`): its event rows,
  alias rows and aliquot row. It refuses (exit 1, nothing written) while a stock's
  `source_aliquot_id` or a kind `ref` field still points at it, instead of surfacing a raw FK error;
  prefer `status consumed` for a tube that existed. `--json` adds the `events` and `aliases` counts.

### Move a tube to another box

Use `tm box list` to find the destination name or short id, then inspect its free slots:

```sh
tm box show WFL-12-03
tm aliquot move 1226FP01.2 --box WFL-12-03 --slot C3 -m "moved to WFL-12-03 C3"
```

Omit `--slot` for a slotless box. `--box` accepts a name, full id, alias or unique id prefix.
Change placement with `move`, quantity with `use`, and status with `status` (also shown by
`tm aliquot --help`). See the [tube move recipe](recipes.md#move-a-tube-to-another-box).

The Python API mirrors the CLI through `ledger.aliquots`:
`ledger.aliquots.use(ref, amount=None, unit=None, *, message)` and
`ledger.aliquots.move(ref, *, box=..., slot=..., status=..., message)`. Both take no event-text
argument other than `message`, and a missing, blank or whitespace-only `message` is refused exactly
like on the CLI.

## Boxes

```sh
tm box add KIND NAME [--in PARENT [--slot S]] [--notes TEXT] [--set f=v ...] [--json]
tm box show NAME|ID|ALIAS [--json]
tm box list [--kind KIND] [--where FILTER ...] [--json]
tm box move NAME --in PARENT [--slot S] [--json]
tm box rename NAME|ID|ALIAS NEW_NAME [--json]
tm box purge NAME|ID|ALIAS [--json]
```

`box list` prints `id name kind path occupied capacity`. The first column shows a unique short UUID
prefix (at least eight characters) in the table; CSV and JSON keep the full UUID. `occupied` counts
the aliquots and the immediate child boxes directly in the box (items, not slots; nested contents do
not roll up), and `capacity` is slots × `slot_capacity` for `grid`/`named` geometry, the declared
capacity for slotless geometry, or blank when unlimited. `--where` filters the core box columns and the box kind's fields; the derived
`path`/`occupied`/`capacity` columns are output, not filters (see **Filters** above).

`box add` materialises the slots implied by the geometry (`grid8x12` → 96, `grid9x9` → 81, `rack` →
the named positions, `shelf`/`freezer`/`drawer` → none). `--slot` is only valid for a slotted parent;
slotless parents take one child box each (up to `capacity`). Nesting must stay acyclic, and a **grid
box holds tubes only**, so `--in` a grid box is refused. Every box name must be **view-safe**: the
generated view `v_box_<name>` must not collide with a core view, the name is at most 58 characters,
and it must not differ only in case from an existing box name (`tm box add`, box import and the CSV
path refuse it). `--set` fills the box kind's fields — repeatable, long-only (`-s` is `--slot`) — a
required field without a default must be given, and `name`/the structural fields are refused.

`box rename` applies the same name rules (a case-only change of the box's own name is allowed),
refuses a name that is another box's name or alias, keeps the old name as an alias, and replaces
`v_box_<old>` with `v_box_<new>` in the same commit.

`box purge` hard-deletes an empty box added by mistake in one commit (`purge(box): …`); it never
cascades. It refuses (exit 1, nothing written) while the box holds an aliquot or a child box, or a
kind `ref` field still points at it, pointing at `tm aliquot move`, `tm box move`, `tm box purge` and
the reference clear path: re-point an entity/stock kind field with `tm entity|stock edit --set
FIELD=OTHER-ID`, and for a box kind field (which has no edit command) purge and re-create the empty
holder box. Otherwise it removes the slot rows, the `box_<kind>` row, the alias rows
and the box row, and runs `DROP VIEW IF EXISTS v_box_<name>` in the same commit. The human line names
the slots removed; `--json` adds the `slots` and `aliases` counts.

`box show` prints a terminal grid with an `aliases: …` line when the box has aliases (or `--json` for
`{id, box, kind, geometry, path, aliases, slots:[...]}` with the full UUID) and works for slotless
boxes too (it lists child boxes). The text header is `<name>  <kind>  id: <short>  path: ...`,
using the first eight UUID characters. `--format csv` prints one row per slot.

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
  fields are never asked; sequence fields are. At the box form's kind step each kind also shows its
  geometry, resolved labels, slots, capacity (the `tm box list` formula) and whether it holds boxes.
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
tm alias add OBJECT_TYPE REF ALIAS [--json]
tm alias remove [OBJECT_TYPE] ALIAS [--json]
tm find [QUERY] [-E|--regex] [--where FILTER ...] [--with-rewrite] [--json]
tm history ID|ALIAS|BOX [--json]
```

`OBJECT_TYPE` is `entity`, `stock`, `aliquot` or `box`; aliases are unique per object type.
`REF` is an id, an alias or a unique id prefix; a box is also accepted by its name.
`alias remove ALIAS` removes that alias everywhere, `alias remove stock ALIAS` only from stocks.
An incomplete object id is accepted wherever a command takes an `ID|ALIAS`: when its prefix matches
exactly one object it resolves, and a prefix that matches several is a usage error (exit 2) naming
the candidates — a full id or exact alias always wins first.
`find` without `-E` matches ids exactly and aliases, names and kind string/text fields by exact,
prefix or substring text (case-insensitive, literal), ranks exact id → exact alias → exact name → name
prefix → alias prefix → name substring → alias substring → fields, and shows summary and location.
`-E`/`--regex` makes QUERY a case-insensitive ICU regex over the same places, ranked id > alias > name
> field (prefix and substring matches merged; ties by object type, kind and id); an invalid pattern is
a usage error with no literal fallback. `--where` applies the shared filters (see **Filters** above) to every
hit and excludes aliquots; with no QUERY it lists every matching object (`match` = `filter`) sorted by
object type, kind and id. `history` merges aliquot events and the Dolt commits that touched the object.

A ledger can declare `find.rewrites` rules in `ledger.yaml` (`{name, pattern, replacement}`; Python
`re.fullmatch` + `match.expand`, validated at open with the rule named). They rewrite a **literal**
QUERY only: a query with no hits falls back to the first rule that hits — a stderr line names the rule
and the rewritten query — and `--with-rewrite` adds every rule's hits below the original ones (`match`
reads `<tier> via <rule>`; JSON adds `rewrite`). `-E`, `--where` patterns and single-object
`show`/`history`/`lineage`/`--entity`/alias resolution never rewrite; a plain not-found there ends
with `try tm find <ref>` (wrong-type, ambiguous and empty refs keep their own error).

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

`import` validates every row first and writes all-or-nothing in one commit. On an aliquot row the
`status` column is a creation status — any of the four fixed statuses (§1.1). `KIND` is a kind name
or a core object (`entity`, `stock`, `aliquot`, `box`); `--object` forces the object type when the file
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
