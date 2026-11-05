# 6. CLI (`tm`)

A command always operates on exactly one **ledger directory** — the directory that holds `.dolt/` and
`ledger.yaml`. There is no workspace layer, no ledger registry and no default ledger outside it.

Global options go **before** the subcommand:

```
tm [-l|--ledger PATH] [-b|--branch BRANCH] [--dry-run] [--no-commit]
   [--json] [--format|-f table|json|csv] [--version] COMMAND ...
```

| Option | Meaning |
|---|---|
| `-l`, `--ledger PATH` | Ledger directory (the one holding `.dolt/` and `ledger.yaml`); `PATH` may also be the `ledger.yaml` file itself. Omit it to search upward from the current directory. |
| `-b`, `--branch BRANCH` | Session branch for every read, write and commit of this command (see below). |
| `--dry-run` | Print/collect the SQL of a write and change nothing. |
| `--no-commit` | Execute the write but leave it uncommitted (advanced). |
| `--json` | Print reads and write results as JSON. |
| `--format table\|json\|csv` | Read output format (default `table`). |
| `--version` | Print the version. |

**Resolution** (P3): an explicit `-l/--ledger PATH` wins and is validated directly; otherwise the CLI
walks up from the current directory and stops at the nearest directory containing both `.dolt/` and
`ledger.yaml`. There is deliberately **no environment variable** (0.4's `$TETRAMARK_LEDGER` and
`$TETRAMARK_WORKSPACE` are gone). Resolution errors are usage errors (exit 2) and name the path/cwd:

- nothing up to the filesystem root → `no ledger found from <cwd> … Pass --ledger PATH, cd into a
  ledger directory, or create one with 'tm init'`;
- a directory with `.dolt/` but no `ledger.yaml` → `is not a ledger … 'tm adopt' formats an existing
  bare Dolt database in place` (P9);
- **P4 migration guard** — a 0.4 `workspace.yaml` met before any ledger is an error that names the
  old workspace and tells you to `cd` into one of its ledger directories (or pass `--ledger`).
  Nothing is converted: `workspace.yaml` is not a ledger marker, and no setting has to move because
  `ledger.yaml` already carries the author;
- `tm init`/`tm adopt` refuse a PATH inside an existing ledger: `Ledgers do not nest` (P5).

Exit codes: `0` ok (help included), `1` business/validation error, `2` usage error, `3` database
unreachable.

Help (Tetramark-03j): `-h` and `--help` print the same text on every command and every command group,
and exit 0; a bare `tm`, or a bare group such as `tm box`, prints its help and also exits 0. Asking
for help is not a usage error. Misuse — an unknown option, an unknown command, a missing argument —
still exits 2.

Branch selection (`-b/--branch`, placed before the subcommand): every read, write and commit of that
command happens on BRANCH. The default is `branch:` in `ledger.yaml`, else the branch checked out in
the repository. A branch that does not exist is a usage error — it is never created implicitly.
Selection is session-scoped and never moves the branch other sessions (or a user-run `dolt
sql-server`) see (see §7). `--dry-run` prints the target branch with the SQL. It also selects the
branch that `tm adopt` formats (§3.1). `tm remote push/pull -b` is a different option with the same
letter: it names the branch on the **remote**, while the global `-b` selects the local session
branch. `tm branch list|create NAME [--from REF]|delete NAME` manage branches; merging is not part of
`tm` (cut over with Dolt's native `CALL DOLT_MERGE(branch, '--ff-only')`).

Every successful write = exactly one Dolt commit. Message is ASCII: `<verb>(<object>): <summary>`;
non-ASCII user text is kept in the event/notes, never in the commit message (known Dolt byte-truncation
bug). Author from `ledger.yaml` → Git `user.name`/`user.email` (P6); there is no workspace tier.

**One writer per ledger** (P11): a write command takes an exclusive `flock` on
`<ledger>/.dolt/tetramark.lock` before it opens the ledger and holds it through the whole
read → validate → write → commit window. A second writer retries briefly, then exits 2 with a typed
busy error: `ledger busy: pid 4711 (tm stock add) is writing (waited 10.0s; set
TETRAMARK_LOCK_TIMEOUT=<seconds> to wait longer)`. `$TETRAMARK_LOCK_TIMEOUT` (seconds, default 10)
overrides the wait. Reads never lock; `tm status` probes the lock without taking it, and `--dry-run`
writes nothing and does not lock.

The root help groups the commands into five panels — Setup, Inventory, Lookup, Data and Versioning —
in workflow order. `Setup` is `presets`, `init`, `adopt`, `schema`, `config`, `status`, `skill`; there
is no `workspace` and no `server` command group in 0.5.0. The listing below follows the same order.

```
# Setup: presets, init, adopt, schema, config, status, skill
tm presets [list|show NAME]
tm init PATH --preset base [--preset molbio] [--name N] [--id-prefix 12] [--author ...]
tm adopt PATH --preset molbio [--name N] [--id-prefix 12] [--author ...] [--force-config]  # §3.1
tm schema show [KIND] | plan | apply [--allow-destructive]
tm config get [author] ; tm config set author "Name <email>"   # the only setting
tm status [--json]                        # one ledger at a glance (6.1)
tm skill path [NAME] [--json]             # where the skill copy lives (6.5)
tm skill version [NAME] [--file PATH] [--json]   # skill stamp vs tm --version (6.5)

# Inventory: entity, stock, aliquot, box
tm entity add KIND --set field=value ... [--name N] [--alias A]
tm entity show ID|ALIAS ; tm entity list [KIND] [--where FILTER ...] ; tm entity edit ID --set f=v
tm entity rename ID|ALIAS NEW_NAME          # old name kept as an alias; one commit
tm entity purge ID|ALIAS                    # hard delete, explicit only; one commit
tm stock add KIND --entity ID|ALIAS --set f=v [--parent STOCK] [--source-aliquot ALQ]
                 [--aliquots N] [--status S] [--box NAME --slot A1 [--slot A2 ...]]
                 [--quantity 500 --unit ml]
tm stock show|list|edit ...                 # list [KIND] [--where FILTER ...]
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
                 # legacy id → strategy-shaped id(s); old ids stay as aliases; one commit
tm stock purge ID|ALIAS                     # hard delete, explicit only; one commit
tm stock aliquot STOCK AMOUNT UNIT [--count N] [--status S] [--box NAME --slot S ...]   # = tm aliquot add --quantity AMOUNT --unit UNIT.
tm aliquot add STOCK [--count N] [--status S] [--box NAME --slot S ...] [--quantity Q --unit U]
tm aliquot show ALQ                          # one tube: status, quantity, stock, box path, events
tm aliquot list [STOCK] [--kind KIND] [--box NAME] [--status S ...] [--all | --not-in-stock]
                [--where FIELD=VALUE ...]    # in-stock shelf view; --all/--status widen
tm aliquot move ALQ --box NAME [--slot S] [--status S] -m MSG   # MSG required
tm aliquot use ALQ [AMOUNT UNIT] -m MSG      # no amount/unit = use everything left; 0 → consumed
tm aliquot status ALQ NEW [--force --note TEXT]   # NEW: pending|stored|missing|consumed (§1.1)
tm aliquot label ALQ LABEL [--replace]       # set label; --replace keeps the old label as an alias
tm aliquot purge ALQ                                  # hard delete, explicit only
tm box add KIND NAME [--in PARENT [--slot S]] [--set f=v ...]   # PARENT: a named or slotless box, never a grid
tm box show NAME            # terminal grid / list; --json gives {id, box, kind, geometry, path, aliases, slots:[...]}
tm box list [--where FILTER ...] ; tm box move NAME --in PARENT [--slot S]   # never into a grid box
tm box rename NAME NEW_NAME   # old name kept as an alias; v_box_<old> → v_box_<new>; one commit
tm box edit NAME [--set f=v ...]   # notes + the box kind's fields; name is rename-only; one commit; structural fields refused
tm box purge NAME|ID|ALIAS    # hard delete, explicit only; drops v_box_<name>; one commit
tm entity|stock|aliquot|box add-form      # interactive form for people (§6.3)

# Lookup: find, alias, history, lineage
tm find [QUERY] [-E|--regex] [--where FILTER ...] [--with-rewrite]   # §6.4: id → alias → name/text; regex; filters; rewrites
tm alias add OBJ_TYPE ID ALIAS ; tm alias remove ...
tm history ID               # aliquot_event + dolt_log entries touching the object
tm lineage ID [--up | --down] [--depth N] [--json] [--format text|dot]   # descent graph (see below)

# Data: import, export, check
tm import KIND FILE.csv [--object entity|stock|aliquot|box]   # all-or-nothing, one commit
                                # a 'status' column is a creation status (any of the four, §1.1)
tm import all DIRECTORY ; tm export KIND|all [--out DIR]
tm check [-v] [--group G] [--format table|json|csv]

# Versioning: branch, remote, backup
tm branch list | create NAME [--from REF] | delete NAME
tm remote add NAME URL | list | push [NAME] [--branch B] | pull [NAME] [--branch B] | remove NAME
tm backup [DIR] [--name NAME]     # DIR required unless a backup is already configured
tm backup --list                  # same as: tm backup list
```

**Subcommand names are full words.** `ls` and `rm` are not abbreviations of any command: a listing
subcommand is `list` and a removal subcommand is `remove`, and neither has a short spelling. A new
subcommand is named with its full word from the start.

`tm init PATH` creates a ledger **directory** at PATH (relative to the cwd or absolute). It creates
`.dolt/`, `ledger.yaml` and `presets/`; missing parents are created and an existing **empty**
directory is reused. `ledger.yaml`'s `name:` defaults to the directory basename and can be set with
`--name`; the path is not identity (P2). `tm adopt PATH` is the entry point for a bare pre-0.5 Dolt
database: same schema as `init`, history kept, one `adopt(ledger): NAME` commit (§3.1). Both commands
take a repeatable `--preset` (bare name or path, §8) and refuse a nested target with a usage error
(P5). `tm --dry-run init|adopt …` prints the plan and the SQL and writes nothing.

`tm backup` has **no default directory** (P10): a backup next to the ledger shares its disk and is
lost with it. Pass a directory (`tm backup DIR`; `--name` sets the backup name, default the ledger
name), or sync a backup already configured on the ledger (`tm backup` = the ledger-named configured
backup, `tm backup --name NAME` for another, `tm backup --list` to list). A missing directory /
configured backup is a usage error; nothing is created implicitly. A directory literally named
`list` must be passed as `./list`, so it is not read as a subcommand.

Creating aliquots accepts any of the four fixed statuses `pending`, `stored`, `missing`, `consumed`
(`--status`, or the import `status` column); changing one follows the fixed transitions (§1.1).
`--force` requires `--note`, records `aliquot_event.forced = TRUE` and is the only way to make an
unforced-illegal change; without `--force` the CLI refuses and names the allowed next status(es).

`tm stock aliquot STOCK AMOUNT UNIT [--count N] [--status S] [--box NAME --slot S ...]` is
positional sugar for `tm aliquot add STOCK --quantity AMOUNT --unit UNIT` with the same options:
`AMOUNT` is the quantity of **each** new tube (zero is allowed) and `UNIT` is its unit. This is
the only reading that needs no new concept: the aliquot row stores a quantity with its unit, and a
unit without a quantity is not representable, so the two positionals always come together. Both
commands call the same write — `ledger.aliquots.add(stock, quantity=AMOUNT, unit=UNIT, …)` — print
the same human line and JSON and make the same single commit
(`add(aliquot): <stock_id> x<N>`). A negative `AMOUNT` is Click's usage error (exit 2); there is no
`--` escape. Use `tm stock aliquot` when every new tube has a known amount, and `tm aliquot add`
for tubes without a quantity.

An aliquot's **position follows its status** (spec 5): `stored` needs a position (a box, plus a slot
when that box is slotted), and changing to any other status clears `box_id`/`slot` in the same write.
A `status`/`use` change that leaves stock records the old position in the event's
`from_box_id`/`from_slot` (with `to_*` NULL), so `tm history` still shows where the tube was;
changing to `stored` without a position is refused with a hint to run
`tm aliquot move ALQ --box NAME --slot S -m MSG`. `tm aliquot move` of an aliquot that is not
`stored` also sets it to `stored`; a move with `--status` runs the normal transition check, and the
result must be `stored`.

`tm aliquot move` and `tm aliquot use` are **irreversible physical actions**, so both require
`-m/--message` (§1). The message is trimmed before it is stored and has no length limit; a missing
option is a usage error, and a value that is empty or whitespace-only after trimming is refused. It is
event text, never the ASCII commit message. `use`'s old `--purpose` and `--note` flags are removed
outright — no aliases; `tm aliquot status --force --note` is unchanged, and `move` never had either
flag. **Temporary naming mismatch:** the CLI says `--message`, while the value is stored in
`aliquot_event.note` and new `use`/`move` events leave `purpose` NULL; `tm history --json` keeps the
`note`/`purpose` event keys. A follow-up change will align the names.

`tm aliquot use ALQ [AMOUNT UNIT] -m MSG` consumes part or all of a tube. `AMOUNT` and `UNIT` come
together: passing only one is a usage error. With an amount, the unit must match the tube's unit and
the amount may not exceed what remains. Passing neither means "use everything left": the event records
the tube's remaining quantity and unit, and the result is identical to passing them explicitly. A tube
whose quantity is NULL can only be used whole — the event records a NULL amount and the tube is
consumed — while a partial use of such a tube is refused. `use` works on every aliquot, whatever its
status, and is refused only when the tube is already `consumed`. Reaching zero moves it to `consumed`
and clears its box position in the same write (the old position stays on the event). The human line
names the amount used and ends with the message, e.g. `Used all 200 ml from X (consumed): <msg>`;
`--json` adds `used_amount`, `used_unit` and `message` to the result (`used_amount` is NULL for a whole
use of a tube with no quantity).

The Python API mirrors the CLI: `ledger.aliquots.use(ref, amount=None, unit=None, *, message)` and
`ledger.aliquots.move(ref, *, box=..., slot=..., status=..., message)`; both take no `purpose` or
`note` argument, and a `move` message is required and validated like a `use` message.

`tm entity purge`, `tm stock purge`, `tm aliquot purge` and `tm box purge` are explicit **hard
deletes**, only for an object added by mistake: the rows leave the tables and Dolt history keeps
them. Each makes exactly one Dolt commit (`purge(<object>): <summary>`), resolves REF like
`show`/`edit` (id, unique ID prefix where that object allows one, alias; a box also by name), takes
the write lock, supports `--json` (`{object, id, kind, purged: true, …counts}`; an aliquot has no
`kind`, and a box carries its `name`) and honours the
global `--dry-run`/`--no-commit`; the object's alias rows go in the same write. A purge checks core
foreign keys and user `type: ref` kind fields that point at the object or its kind, and refuses with a
business error (exit 1), writing nothing, while anything still references it; the message names the
referents (capped) and the command that clears them. Purging never cascades beyond a stock's own
aliquots. `tm entity purge` refuses while a stock points at the entity; `tm stock purge` refuses on a
child stock, a stock naming one of its aliquots as `source_aliquot_id`, or a kind ref field pointing
at the stock or one of its aliquots, and otherwise removes its own aliquots (their events and
aliases too); `tm box purge` refuses while the box holds an aliquot or a child box and otherwise
removes the slot rows, the `box_<kind>` row and the box row and drops `v_box_<name>` in the same
commit. `tm aliquot purge` also deletes the tube's aliases and refuses when a stock's
`source_aliquot_id` or a kind `ref` field still references the tube. The Python API mirrors the four
purges: `ledger.entities.purge(ref)`, `ledger.stocks.purge(ref)`, `ledger.aliquots.purge(ref)` and
`ledger.boxes.purge(ref)`.

A box name must be **view-safe**, because each existing box gets a generated view (§3):
`v_box_<name>` must not equal a core view (for example `slots`), the name is at most 58 characters
(64 − `v_box_`), and it must not differ only in case from another box's name. `tm box add` and CSV
box import refuse such a name, and so does `tm box rename` (which may change only the case of the
box's own name). **Grid boxes hold tubes only**, so
`tm box add … --in GRIDBOX` and `tm box move … --in GRIDBOX` are refused; a box nests only in a
named or slotless box — for example a freezer, `tm box move WFL-12-02 --in FR-1` — and a box never
nests in a grid box.

`tm box add KIND NAME --set field=value …` fills the box kind's fields at creation, coerced exactly
as `tm box edit` coerces them: `--set` is repeatable and long-only (`-s` is `--slot`), a required
field without a default must be given, and `name` and the structural fields are refused.

`tm entity edit`, `tm stock edit` and `tm box edit` change the editable fields of one object and
nothing else: `notes` plus the fields the object's kind declares, coerced and validated exactly as
at add time. A **name changes only through `tm <object> rename`** — `--set name=…` is refused with
an error that names the rename command, because a rename keeps the old name as an alias (and
rebuilds a box's view) and a plain column update cannot. Structural fields are refused as well:
`id`, `kind` and `created_at` are identity and creation data, and a box's `parent_box_id` and
`parent_slot` change through `tm box move REF --in PARENT [--slot S]`. `--set field=` (the value
exactly empty) clears an optional column to NULL, a required field cannot be cleared, and a
whitespace-only value is not empty: it is coerced like any other value, never cleared. One edit is
one Dolt commit, `--json` returns the loaded record and `--dry-run` writes nothing.

`tm export` reads a box from `box` joined with `box_<kind>` — the per-kind `v_box_<kind>` views are
gone (spec 5). `tm import` creates each new box's `v_box_<name>` view and applies the name and
grid rules above, in the same commit as the rows.

`tm entity show`, `tm stock show`, `tm aliquot show` and `tm box show` include the object's aliases as
a sorted `aliases` list in JSON output and print an `aliases` row or line in table output; `list`
**table** output stays narrow. `tm aliquot list --json` is the exception among lists: an aliquot has
no per-kind view to project, so its items are the `tm aliquot show` record without `events` (a list
must not read every tube's event trail) and carry `aliases` too. The generated views expose the same
data as `alias_list` / `occupant_alias_list` (§3).

`tm aliquot show ALQ` shows one tube. `ALQ` is the full aliquot id (`<stock>.<seq>` — never a
shortened prefix) or one of the tube's aliases (including the old ids `tm stock renumber` keeps).
The record holds the aliquot row (`id`, `stock_id`, `seq`, `status`, `quantity`, `unit`, `label`,
`notes`, `created_at`), the sorted `aliases`, the owning stock's `kind` and `entity_id`, the
tube's `box_id` and `slot`, the `path` read from `v_aliquot_location` (§3,
`Freezer-1/Shelf-2/Box:A3`), the derived `in_stock` flag (true exactly when the status is
`stored`, §1.1), and finally the tube's `events`, oldest first (`[]` when the tube has none). Each
event carries `id`, `event`, `at`, `amount`, `unit`, `from_box_id`, `from_box`, `from_slot`,
`to_box_id`, `to_box`, `to_slot`, `old_status`, `new_status`, `forced` (a bool), `actor`, `purpose`
and `note`; `from_box`/`to_box` are the box names, falling back to the box id when the box row is
gone. `events` is not a key/value row: text output prints the field block, then the `path:` line
(when the tube has a position), then `events  N` (two spaces before the count) and one line per
event, oldest first, rendered by the same `event_line` helper `tm history` uses. `--json` prints
the whole record, events included; `--format csv` stays the flat single-row record and drops
`events` (event rows are `tm history ALQ --format csv`). The Dolt commits stay in `tm history`:
show carries events only. A tube that is not in stock has `box_id`, `slot` and `path` NULL. The
stock kind's own fields stay in `tm stock show`. The command is read-only and changes no ledger
format — `spec_version` is untouched.

`tm aliquot list` enumerates tubes. With no filter it prints the tubes an experiment can use — the
aliquots whose status is `stored` (§1.1) — not the whole history, because a list answers "what is
available"; `--all` adds every status and `--not-in-stock` shows only the complement (the
missing/consumed audit). An explicit `--status S` (repeatable, OR) is already a deliberate
choice, so it turns the implicit in-stock scope off; combined with `--not-in-stock` it intersects.
`STOCK` (one stock id or alias; the incomplete-id rule applies) limits the listing to one stock and
never follows child stocks; `--kind` limits it to a stock kind; `--box` takes a box name/id/alias
and includes the box plus every box nested inside it; `--where FIELD=VALUE` (repeatable, AND) matches
aliquot core columns exactly, except `status` (`--where status=…` is a usage error pointing at
`--status`). An empty value (`FIELD=`) means the column is empty: `NULL` or `''` for `id`, `stock_id`,
`box_id`, `slot`, `unit`, `label` and `notes`, and `NULL` only for `seq`, `quantity` and `created_at`.
This is the aliquot's own exact-only `--where`; the shared `field=value` / `field~pattern` syntax of
§6.4 applies to the entity/stock/box lists and `tm find`. The table and CSV print the narrow
rows `id stock_kind status quantity unit label path`
(`path` is empty when the tube is not in stock); `--json` prints the same record `tm aliquot show`
prints without `events`, one per tube, in `stock_id`/numeric-`seq` order. The command is read-only
and changes no ledger format — `spec_version` is untouched.

`tm stock show` embeds the stock's tubes. Each row carries `id`, `seq`, `status`, `quantity`, `unit`,
`label`, `box_id`, `slot`, the slot-inclusive `path` (`NULL` when the tube is not in stock), the
sorted `aliases` and the derived `in_stock`; the rows, paths and aliases ride in the same read batch
as the stock. Text output prints the stock's field block, then `aliquots  N` and the tube **cards**,
flowing left to right and wrapping at the console width: the title is `<id> · <status>` (coloured by
status), then the quantity/unit, a `label:` line when the tube has one, the path or `not in stock`,
and an `aliases:` line when it has aliases. Every card keeps the rounded border; only its colour
follows the status. `--json` keeps the enriched rows
in the record; `--format csv` stays the machine form, and the tabular tube view is `tm aliquot list`.

A reference to the wrong object type is a not-found error that names the other verb: `tm aliquot
show <stock>` answers `<stock> is a stock; try tm stock show <stock>`, and `tm stock show
<aliquot>` answers `<aliquot> is an aliquot; try tm aliquot show <aliquot>`. Both use the same
error class and exit code as any other not-found error (exit 1).

`tm lineage ID` is the read-only **descent** graph of entities and stocks (lineage/pedigree, not
phylogeny). `ID` resolves like `tm find` (id, then alias) to an entity, a stock or an aliquot; an aliquot
starts the walk at that tube. Edges point **child → parent** and come from the core links — every stock
to its entity, `stock.parent_stock_id`, and `stock.source_aliquot_id` (child stock → that aliquot → the
stock the aliquot belongs to) — plus every `type: ref` field declared `lineage: true` (§2.1), whose
`via` in the output is `<kind>.<field>`. Nodes are entities and stocks; a stock node shows its kind, id,
entity name and the in-stock/total aliquot counts of `v_stock_<kind>` (§3). An aliquot is a
node only where a relation names one (`source_aliquot_id` or a lineage ref): it then has an upstream
edge to the stock that owns it, carrying the same `via`, so a `source_aliquot` path reads child stock →
source aliquot → that aliquot's stock. The default is both directions and unlimited depth; `--up` walks
ancestors, `--down` descendants, `--depth N` stops after N edges (0 = the root alone). A node reached a
second time prints `<id> (see above)` with no subtree, and is listed once in the JSON. `--format dot`
prints a Graphviz digraph whose edge labels are the `via`; `--json` prints
`{"root": <id>, "nodes": [{id, object, kind, label, counts?}], "edges": [{from, to, via}]}` with
`via` ∈ `entity | parent_stock | source_aliquot | <kind>.<field>`. `tm lineage` respects `--branch`,
writes nothing and never commits.

`tm stock renumber` is the audited way to repair ids that do not follow the ledger's stock id strategy
(legacy imports, hand-made ids). It renumbers one stock (`REF --id NEW`), many (`REF ... --auto`), or a
wholesale batch (`--map PATH`, one `OLD<TAB>NEW` per line, `OLD` alone when combined with `--auto`).
New ids are validated against `ids.stock` and against the alias namespace; the old stock id and every
old aliquot id are kept as aliases, so `tm find OLD` and `tm find OLD_ALIQUOT` keep working and no
handle is lost. Everything — `stock.id`, `aliquot.id`/`stock_id`, `aliquot_event.aliquot_id`, kind
tables, `ref` fields, `parent_stock_id`, `source_aliquot_id` and existing
aliases — is rewritten in one transaction and one Dolt commit (`renumber(stock): OLD -> NEW` for one
row, `renumber(stock): N stock(s)` for a batch). Because the foreign keys are `NO ACTION`, the primary
key rename runs with `SET FOREIGN_KEY_CHECKS=0` and the checks are restored in the same session before
the commit; `--dry-run` prints the mapping table and the SQL and writes nothing.

Python API mirrors the CLI (`tetramark.open(path=None, branch=None)` → `Ledger` with `.path`,
`.name`, `.entities`, `.stocks`, `.aliquots`, `.boxes`, `.find()`, `.check()`); the CLI is a thin
layer over it. `tetramark.init_ledger(path, name=None, presets=...)` and
`tetramark.adopt_ledger(path, name=None, presets=...)` are the lifecycle functions behind
`tm init`/`tm adopt`.

### 6.1 `tm status` — one ledger at a glance

`tm status` prints the ledger it resolves (`-l/--ledger PATH` or the upward walk) and **never lists
sibling ledgers**. It replaces 0.4's `tm server status` / workspace ledger listing. Human output is
one aligned block:

```
path            /Users/me/lab/zsqlab_10
name            zsqlab_10
branch          main
spec_version    5
presets         molbio
author          wangfanlin1 <wangfanlin2022@sibcb.ac.cn>
write lock      free
portability     no warnings
```

- a held P11 lock reads `write lock      pid 4711 (tm stock add) since …Z on host`; the probe is
  non-blocking and never creates or steals the lock;
- portability warnings list the absolute `file://` remote/backup URLs that point outside the ledger
  (P13), one indented line per finding; `no warnings` when there are none;
- missing values render `(unknown)` / `(none)`.

`--json` (or global `--json` / `--format json`) prints exactly this shape, with stable types:

```json
{
  "path": "/abs/ledger",
  "name": "zsqlab_10",
  "branch": "main",
  "spec_version": 5,
  "presets": ["molbio"],
  "author": "Name <email>",
  "lock": {"held": false, "pid": null, "command": null, "since": null, "host": null},
  "portability_warnings": [
    {"kind": "backup", "name": "old", "url": "file:///old/backup", "path": "/old/backup"}
  ]
}
```

`path`/`name` are strings, `branch`/`author` are string-or-null, `spec_version` is int-or-null,
`presets` is a list of strings, `lock` always has all five keys, and every warning has
`kind`/`name`/`url`/`path`. There are only two shapes: `--format csv` is a usage error (exit 2).

### 6.2 ID strategies

- `uuid` — uuid4 string.
- `datecode` — `<prefix><YY><M><D><NN>`: M = month marker `A–M` skipping I; D = day `1–9` then `A–W`
  skipping I (port of `~/.local/bin/date_code.py`); NN = **smallest unused** 2-digit sequence for that
  day prefix (configurable `reuse: false` → max+1). Example: prefix `08`, 2026-06-24 → `0826FP01`.
- `sequence` — `<prefix><NNNNN>` monotonic.
- Explicit IDs are always accepted (back-filling / migration) if unique and well-formed (pattern from the strategy).
- An **incomplete ID** is accepted wherever a command takes an `ID|ALIAS` reference when its prefix
  matches exactly one object; a full ID or an exact alias always wins first. A prefix that matches
  several objects is a usage error (exit 2) that names the candidates, so nothing is picked silently.
- Aliquot IDs: `<stock_id>.<seq>`, seq = max+1 within the stock.

**A list verb defaults to what is available.** When the object has an availability notion, its
`list` prints what can be used now, not the whole history: `tm aliquot list` hides the tubes that
left stock unless `--all` or an explicit `--status` names them. Future list verbs follow the same
rule; the entity/stock/box lists have no availability notion and keep printing every row.

List tables abbreviate uuid ids the way git abbreviates hashes: `tm entity list` and `tm stock list`
print the shortest **unique** prefix of at least 8 characters (ids that share the first 8 grow
together until they differ). `tm stock list` shortens both its `id` and its `entity_id` column when
that value is a uuid id, so a ledger with a `uuid` stock strategy gets short stock ids too.
`tm <object> show`, JSON and CSV always print the full 36-character id, so it remains the
cross-command identifier, and datecode/sequence ids (`0826FP01`, `S00042`) and aliquot ids
(`0826FP01.1`) are never shortened.

**List table columns.** A list command's default table and its `--format csv` output print the same
fixed, readable subset of the record; `--json` keeps the full record. The leading columns identify
the row and the rest hold the values a person decides on, with long values (an entity name) printed
in full, never truncated.

- `tm entity list` prints `id kind name stocks_available aliquots_in_stock`. `stocks_available` is
  the number of the entity's stocks whose derived status is `active` (at least one aliquot in stock);
  depleted and empty stocks are not counted. `aliquots_in_stock` sums the in-stock aliquots of all the
  entity's stocks. Both are `0` for an entity without stocks.
- `tm stock list` prints `id kind entity_id entity_name aliquots_in_stock status`. `entity_name` is
  the owning design's name. `status` is derived from the aliquots: `empty` when the stock has none,
  `active` when at least one is in stock, and `depleted` when every aliquot has left stock.
- `tm box list` prints `id name kind path occupied capacity`. The table shortens `id` to a unique
  prefix of at least eight characters among the listed rows; CSV and JSON keep the full UUID.
  `occupied` counts the aliquots and the immediate child boxes directly in the box, counting items
  rather than slots; the contents of nested boxes do not roll up into the parent. `capacity` is the number of slots times the kind's
  `slot_capacity` for `grid`/`named` geometry, the declared capacity for slotless geometry, and blank
  (`null` in JSON) when a slotless box is unlimited.

`tm box show` starts with `<name>  <kind>  id: <short>  path: ...`, where `<short>` is the
first eight characters of the box UUID. JSON includes `id` as the full UUID; CSV keeps its
slot rows (`slot row col occupants`).

`tm aliquot --help` directs placement changes to `move`, quantity changes to `use`, and
status changes to `status`.

### 6.3 The interactive form — `tm <object> add-form`

`tm entity add-form`, `tm stock add-form`, `tm aliquot add-form` and `tm box add-form` open an
interactive form that asks for what a person does not know by heart: the kind, the fields, the box,
the slots. The form takes **no positional argument and no flags** — every question comes from the
ledger's own schema. It is the human counterpart of `tm <object> add`; **agents keep using `add`**,
which is scriptable, never blocks and writes exactly the same row.

Without a terminal (stdin is not a TTY) the command refuses with a usage error (exit 2) that points
at `tm <object> add`. Nothing is written, and no lock is taken.

**The steps.** The first step is always the **numbered list of kinds** of that object, built from
the ledger's effective schema. It is shown even when there is only one kind, and Enter accepts the
only entry. For an aliquot the list shows the **stock** kinds: an aliquot has no kind of its own.
Then come the **fields of the chosen kind in schema order**, each with its description and a `*`
when it is required (for a box they follow its name and precede its parent). **Computed** fields are
never asked; **sequence** fields are.

At the kind step of `tm box add-form` each box kind also shows structured facts under its
description: its geometry, the resolved row/column or slot labels, its slot count and capacity, and
whether it holds boxes. They come from the effective schema only — the capacity is the same
`tm box list` formula — so choosing a kind never queries occupancy.

**Per-type checks** happen as you type, so a mistake is caught on the field that caused it:

| type | what the form accepts |
|---|---|
| `enum` | the value itself, or its number in the list |
| `bool` | `y` / `n` |
| `date`, `datetime` | ISO (`2026-11-03`, `2026-11-03T09:30`); `today` / `now` are accepted |
| `int`, `decimal` | parsed as a number |
| `string` with `pattern:` | the pattern is checked as you type |
| `ref` | resolved immediately, and the target's name is shown back; `?term` searches the way `tm find` does and you pick a result by number |
| multi-value (`aliases`, `slots`) | one value per line or comma-separated; an empty line ends the list |

**Long fields** — the fields marked `long: true` in the preset (§2) — are never shown in full but in
a short form: `[pasted 5,432 chars · 12 lines · ATGCGTACGT…TTAGCCGGAA]`. A bracketed paste is
accepted as it is, pasting again **replaces** the value, and Backspace on the placeholder clears
it. A short typed value is shown inline. A value the schema rejects shows the error and leaves you
on the step.

**Keys.** A footer lists them:

| key | effect |
|---|---|
| `Enter` | next step: an optional field may be skipped and a default accepted; a required field left empty keeps the step and says `required` |
| `Shift+Tab` | previous step, with its value pre-filled |
| `Alt+Enter` / `Ctrl+J` | newline |
| `Tab` | completes enum values and ref ids/aliases |
| `Ctrl+E` | edit the current field in `$VISUAL` → `$EDITOR` → `vi`; it works on every field. The field name, its description and its pattern are put below a git-style scissors line and removed from what you edit, while `#` lines are kept |
| `Ctrl+C` | abort: nothing is written |

**The stock form** is the worked example of the steps a form needs beyond the fields, in this order:
the **entity** (a ref, with search), the **kind's fields**, the **aliquot count**, the **box** (`?`
lists the boxes) and the **slots** (the next free slots are filled in by default; `?` shows the free
slots). The other forms ask for what their `add` command needs, in the same way.

**The review screen** lists every value, numbered, with long values in short form. Typing a number
jumps back to that step, `y` writes, `q` cancels.

**The write** is the same `ledger.<objects>.add(...)` call that `add` makes, with no separate
validation or write logic: the same message is printed, `--dry-run`, `--no-commit` and `--json`
behave as they do for `add`, and **the write lock is taken only when `y` confirms**. If the core
rejects the entry, the error is shown and the form goes back to the field that failed. There is no
"add another": the form ends after one write.

### 6.4 `--where` filters and `tm find`

`tm entity list`, `tm stock list`, `tm box list`, `tm find` and their Python `where=` argument take
one filter syntax: `field=value` (exact) or `field~pattern` (regular expression). A filter is split at
its first `=` or `~`, so the value may contain either character; the field name before it must be an
identifier (a letter or `_`, then letters, digits or `_`), and it is case-sensitive. A filter with
neither operator, or with a field name that is not an identifier, is a usage error (exit 2).
`tm aliquot list` keeps its own exact-only `--where FIELD=VALUE` (above): this section is about the
shared syntax used by the three lists and by `tm find`.

`field=value` compares the column exactly. On a kind field the value is coerced by the field's schema
the way `--set` coerces it: an `enum` must be one of its values, a `ref` may be an id or an alias and
is resolved to the id it stores, an `int`/`decimal`/`bool`/`date`/`datetime` is parsed, a `sequence`
is normalised. On a core column the text is compared as written: `id`, `kind`, `name`, `created_at`,
`notes` and the core references (`entity_id`, `parent_stock_id`, `source_aliquot_id`, `parent_box_id`,
`parent_slot`) take the stored value itself, so a core `ref` such as `entity_id` takes the id, never
an alias.

`field=` (an empty value) selects the rows whose field is empty: `NULL` or `''` on a text-like
column, and `NULL` only on a non-text column (a stored `0` never counts as empty). Text-like means
the kind field types `string`, `text`, `enum`, `sequence` and `ref`, and every core column except
`created_at`; non-text means the kind types `int`, `decimal`, `bool`, `date` and `datetime`, and the
core `created_at`. The empty value
skips the schema coercion and the alias resolution above (an empty `ref`, `enum` or `int` is not
resolved, checked or parsed), and it does not mean the literal text `none`/`null`, which still
compares exactly like any other value. Only the truly empty string is special: a whitespace-only
value is not (`name=  ` compares the two spaces exactly). There is no "not empty" operator; ask for
it with `field~.`.

`field~pattern` matches the column's **text form** with a regular expression. It is a *search*, not a
full match: unanchored, so anchor it with `^`/`$` when needed (`name~^pZET` starts with, `name~ZET`
contains). It is case-sensitive; write `(?i)` in the pattern for case-insensitive (`name~(?i)^pzet`).
The database compiles the pattern (Dolt's ICU/MySQL 8 dialect), so `\d`, `\p{L}`, classes, groups and
lookarounds behave as there, not exactly as in Python `re`; the ledger's `find.rewrites` rules
(described below) are Python `re` patterns and do not share this dialect. The text form of an `int`, a
`decimal`, a `date`/`datetime` or `created_at` is its printed value (`length_bp~^15`,
`created_at~^2026-11-04`); a `ref` column, core or kind, is matched against the id it stores, never
its aliases; `NULL` never matches a `~` pattern, not even `field~^$` (use `field=` to find empty
values). A computed kind column can be addressed by `~` (its text form), while `=` is refused by the
computed-field usage error, empty value included.

The rules are the same everywhere:

* a field may be filtered with `=` once; a second `=` on the same field is a usage error
  (`field 'name' appears twice in --where`);
* `=` and `~` must not be mixed on one field (`field 'name' cannot be filtered with both '=' and '~'
  in --where`); any number of `~` filters may address one field and they are ANDed
  (`--where 'name~^p' --where 'name~9$'`);
* an empty pattern (`name~`) is a usage error; an invalid pattern is a usage error that names the
  filter (`invalid regular expression in the --where filter 'name~['`), never a raw database error;
* a field that is neither a core filter column nor a field of a kind in scope is a usage error that
  lists the valid fields (`unknown field(s) 'nope' in --where; valid fields: …`).

The valid fields are the core filter columns of the object type plus the fields of the kinds in scope:
`id`, `kind`, `name` (entity, box), `created_at` and `notes` for all three, `entity_id`,
`parent_stock_id` and `source_aliquot_id` for stocks, and `parent_box_id` and `parent_slot` for boxes.
A computed kind column is accepted by `~` as described above; view-derived columns (`path`,
`capacity`, `occupied`, `status`, the counts) are not filterable. A field that no kind in scope has is
the unknown-field usage error above. When a field is declared by only some of the kinds being listed,
only those kinds are read: a kind without **every** filtered field contributes no rows rather than
ignoring the filter (`tm stock list --where 'induction=iptg'` reads the stock kinds that declare
`induction` and not the others). On `tm find` the valid set is wider — the core columns of entity,
stock and box and the fields of every entity/stock/box kind — and a filtered field that only some
kinds have simply excludes the other kinds from the result.

Every filter is ANDed; `--where` (also `-w`) is repeatable. The listings and `tm find` build all
statements of the read first and fetch them in one `query_many` batch, so `--where` does not cost a
process per kind. A few paths still cost extra queries: naming an invalid pattern (the error path
probes the patterns to name the bad one), a field combination no kind can express (nothing compiles
the patterns, so each `~` pattern is probed once up front), and an `=` filter on a kind `ref` (the
alias may be resolved). A listing that matches nothing prints `(none)` (an empty list in JSON) and
exits 0.

`tm find [QUERY] [-E|--regex] [--where FILTER ...] [--with-rewrite]` searches the whole ledger. Without `-E`, `QUERY`
is a literal search: ids are matched exactly and case-sensitively, while aliases, names and
`string`/`text` kind fields are matched case-insensitively by exact, prefix and substring text, with
`%` and `_` as ordinary characters. The tiers are exact id, exact alias, exact name, name prefix,
alias prefix, name substring, alias substring, exact field and field substring. Its places are the
ids of all four object types, aliases, names and the `string`/`text` fields of every kind. With
`-E`/`--regex`, `QUERY` is a case-insensitive database regex over the same places (`(?i)` is added to
the pattern): a regex has no exact/prefix/substring sub-tiers, so the ranks collapse to id > alias >
name > field, with prefix and substring matches merged; ties are broken by object type, kind and id.
An invalid `-E` pattern is a usage error (`invalid regex '[': the pattern is not a valid regular
expression`) with no literal fallback — a pattern that does not compile never matches as if it were
text.

`--where` applies to every hit after the search (AND), so even an id or alias hit must satisfy it.
Aliquots are excluded whenever any `--where` is given (they have no kind row to filter);
`tm find 1226L401.1 --where 'id~^12'` therefore reports no match. `QUERY` is optional with `--where`:
`tm find --where 'resistance=kan'` lists every matching object with `match` = `filter`, sorted by
object type (entity, stock, box), kind and id. Passing neither a `QUERY` nor a `--where` is a usage
error (`the search query must not be empty; pass a QUERY or at least one --where filter`). A search
that finds nothing exits 1: `no match for '<QUERY>'`, or `no objects match the --where filters` when
only filters were given; `--where` syntax/field errors and an invalid `-E` pattern exit 2.

**`find.rewrites` rules.** A ledger may declare rewrite rules for literal `tm find` queries in
`ledger.yaml` (`find.rewrites`, §1.2): each rule has a unique `name`, a Python `re` `pattern` matched
against the whole trimmed `QUERY` with `re.fullmatch`, and a `replacement` expanded with
`match.expand` (`\1`/`\g<name>` back-references). That is the Python dialect — deliberately not the
database (ICU) dialect of `-E` and `--where ~` — and the rules are validated when the ledger opens,
with the rule named in the error (a bad pattern, a replacement that names a group the pattern lacks,
a missing or duplicate `name`, or an empty `rewrites:` list; exit 1). The rules rewrite `QUERY` only:
`--where` filters are never rewritten (they still narrow every hit, rewrite hits included), `-E` is
never rewritten, and single-object resolution (`show`, `history`, `lineage`, `--entity`, alias
resolution) stays exact: a plain not-found there ends with `try tm find <REF>` (the command that
does apply the rules), while wrong-type, ambiguous and empty refs keep their own error unchanged.
A rule that does not match, or whose expansion is the query itself or empty, is skipped silently;
rules never chain.

By default the rules are a **zero-hit fallback**: a literal query with hits is searched normally and
the rules are not used; only a query with zero hits tries every rule, in declaration order, and shows
the hits of the first rule that produces any. The CLI then prints to stderr
`no match for '<QUERY>'; showing matches for '<REWRITTEN>' (rule '<NAME>')`. `--with-rewrite` runs
every rule in addition to the original query on every literal search: the original hits rank above all
rewrite hits, rewrite hits follow in rule order with each rule's hits ordered by tier, and an object
found both ways keeps its original entry. Rewrite hits read `<tier> via <rule>` in the `match` column
(`id via type-code`) and carry `"rewrite": {"rule": "<NAME>", "query": "<REWRITTEN>"}` in JSON. A
rewrite search rides in the same `query_many` batch as the original (`--with-rewrite` is one batch);
the fallback costs one batch for the miss plus one batched rewrite search. When nothing is found, the
exit-1 message stays `no match for '<QUERY>'` and appends `(also tried: '<REWRITTEN>' via <NAME>,
...)` when rules produced rewrites.

The Python API mirrors the CLI: `ledger.find(query=None, *, regex=False, where=(), with_rewrite=False)`,
`ledger.entities.ls(kind=None, *, where=())`, `ledger.stocks.ls(...)` and `ledger.boxes.ls(...)`. A
`where` **mapping** is equality only (`where={"name": "pUC19"}`); a list of strings accepts both
operators (`where=["name~(?i)^puc", "resistance=amp"]`). Parsing, coercion and errors are the same
code as the CLI.

### 6.5 `tm skill` — which copy describes this CLI

A shipped skill states the Tetramark version it was written against in its frontmatter (§10), so a
stale copy is detectable instead of failing on its first removed command. These two commands read
that stamp and are **ledger-free**: they never resolve or open a ledger, so they work in a directory
that holds none.

- `tm skill path [NAME]` prints the bare directory of the copy `NAME` resolves to (`tetramark` by
  default). Resolution is `$TETRAMARK_SKILLS_DIR` first, then the checkout's `skills/`, then
  installed package data (once the wheel ships the skills; Tetramark-bfq). `--json` prints
  `{"name", "path", "source"}`, where `source` is `env`, `checkout` or `package`. A name that no
  root holds exits 1 with a message naming all three roots.
- `tm skill version [NAME] [--json]` prints one line comparing the copy's frontmatter `version:`
  with `tm --version`:

  ```
  skill 0.8.26 (/repo/skills/tetramark/SKILL.md); tm 0.8.26; match
  ```

  `--json` prints `{"name", "skill_version", "skill_path", "tm_version", "match"}`; on a match
  stderr is empty. `--file PATH` checks another copy instead -- a `SKILL.md` or the skill directory
  that holds it -- which is how a stale 0.4-era export is inspected.

A mismatch is a warning on stderr and exit 0, never a failure: an old skill is a documentation
problem, not a broken ledger. The warnings live only in `tm skill version` (no other command warns
about a stale copy):

- `warning: skill 0.4.1 describes an older CLI than tm 0.8.26; use the copy at $(tm skill path) instead`
  (`newer` when the skill claims a later CLI; exit 0 in both cases);
- `warning: <path> carries no version; it predates versioned skills (tetramark < 0.8.25)` -- a
  frontmatter without `version:`: `skill_version` is `null`, `match` is `false`, exit 0;
- `warning: skill version 'devel' is not comparable with tm 0.8.26` -- a version that is not dotted
  numeric, so no ordering is claimed; exit 0.

Only a missing file or a file without YAML frontmatter exits 1. The version is the package version:
there is no separate skill revision and no `rev:` field (§10).

