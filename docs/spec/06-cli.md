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
| `--version` | Print `tetramark <version>` on stdout. When the installed package metadata disagrees with the code (an editable checkout), also print one warning on stderr naming both versions, the checkout and the pinned reinstall command. |

**Resolution** (P3): an explicit `-l/--ledger PATH` wins and is validated directly; otherwise the CLI
walks up from the current directory and stops at the nearest directory containing both `.dolt/` and
`ledger.yaml`. There is deliberately **no environment variable** (0.4's `$TETRAMARK_LEDGER` and
`$TETRAMARK_WORKSPACE` are gone). Resolution errors are usage errors (exit 2) and name the path/cwd:

- nothing up to the filesystem root → `no ledger found from <cwd> … Pass --ledger PATH, cd into a
  ledger directory, or create one with 'tm init'`;
- a directory with a real Dolt database (`.dolt/noms/` or `.dolt/repo_state.json`) but no
  `ledger.yaml` → `is not a ledger … 'tm adopt' formats an existing bare Dolt database in place` (P9);
- a `.dolt/` that holds no Dolt database — Dolt's global `~/.dolt` configuration directory — is not a
  ledger: the upward walk steps past it (the error is `no ledger found from <cwd> …`, never an
  `tm adopt` hint), and passed explicitly with `-l/--ledger` it is a usage error that names it as
  Dolt's global configuration directory. `tm adopt` refuses it the same way, before any `dolt` call;
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

# Inventory: entity, stock, aliquot, box, parent, child
tm entity add KIND --set field=value ... [--name N] [--alias A]
tm entity show ID|ALIAS ; tm entity list [KIND] [--where FILTER ...] [--where-any FILTER ...] ; tm entity edit ID --set f=v
tm entity note ID|ALIAS [TEXT] [--append] [--clear]   # same as tm stock note; one commit
tm entity rename ID|ALIAS NEW_NAME          # old name kept as an alias; one commit
tm entity purge ID|ALIAS                    # hard delete, explicit only; one commit
tm stock add KIND --entity ID|ALIAS --set f=v [--parent STOCK] [--source-aliquot ALQ]
                 [--aliquots N] [--status S] [--box NAME --slot A1 [--slot A2 ...]]
                 [--quantity 500 --unit ml]
tm stock show|list|edit ...                 # list [KIND] [--all] [--where FILTER ...] [--where-any FILTER ...]
tm stock note REF [TEXT] [--append] [--clear]   # notes in $VISUAL/$EDITOR; TEXT only with --append; one commit
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
                 # legacy id → strategy-shaped id(s); old ids stay as aliases; one commit
tm stock purge ID|ALIAS                     # hard delete, explicit only; one commit
tm stock aliquot STOCK AMOUNT UNIT [--count N] [--status S] [--box NAME --slot S ...]   # = tm aliquot add --quantity AMOUNT --unit UNIT.
tm aliquot add STOCK [--count N] [--status S] [--box NAME --slot S ...] [--quantity Q --unit U]
tm aliquot show ALQ                          # one tube: status, quantity, stock, box path, events
tm aliquot edit ALQ --set field=value ... [-m MSG]   # correct quantity/unit/created_at; one edit event, one commit
tm aliquot list [STOCK] [--kind KIND] [--box NAME] [--status S ...] [--all | --not-in-stock]
                [--where FILTER ...] [--where-any FILTER ...]   # in-stock shelf view; --all/--status widen
tm aliquot move ALQ --box NAME [--slot S] [--status S] -m MSG   # MSG required
tm aliquot use ALQ [AMOUNT UNIT] -m MSG      # no amount/unit = use everything left; 0 → consumed
tm aliquot status ALQ NEW [--force] [-m MSG]   # NEW: pending|stored|missing|consumed (§1.1); MSG required with --force
tm aliquot label ALQ LABEL [--replace]       # set label; --replace keeps the old label as an alias
tm aliquot purge ALQ                                  # hard delete, explicit only
tm box add KIND NAME [--in PARENT [--slot S]] [--set f=v ...]   # PARENT: a named or slotless box, never a grid
tm box show NAME            # terminal grid / list; --json gives {id, box, kind, geometry, path, aliases, slots:[...]}
tm box list [--where FILTER ...] [--where-any FILTER ...] ; tm box move NAME --in PARENT [--slot S]   # never into a grid box
tm box rename NAME NEW_NAME   # old name kept as an alias; v_box_<old> → v_box_<new>; one commit
tm box edit NAME [--set f=v ...]   # notes + the box kind's fields; name is rename-only; one commit; structural fields refused
tm box note NAME|ID|ALIAS [TEXT] [--append] [--clear]   # same as tm stock note; box by name first; one commit
tm box purge NAME|ID|ALIAS    # hard delete, explicit only; drops v_box_<name>; one commit
tm entity|stock|aliquot|box add-form      # interactive form for people (§6.3)
tm parent REF_A [REF_B] [--set] [--clear] [--via FIELD] [--slot S]   # read A's parents or the A-B relation; --set: parent of A is B; --clear removes it; one commit per write (§6.7)
tm child REF_B [REF_A] [--set] [--via FIELD] [--slot S]              # read B's children or the same relation; --set: the same write stated inversely (§6.7)

# Lookup: find, alias, history, lineage
tm find [QUERY] [-E|--regex] [--where FILTER ...] [--where-any FILTER ...] [--with-rewrite]   # §6.4: id → alias → name/text; regex; filters; rewrites
tm alias add OBJ_TYPE REF ALIAS ; tm alias remove ...   # REF: id, alias or unique id prefix; a box also by its name
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

`tm history ID` renders its event section with the §6.6 event lines and its `--json` events carry
the same keys as `tm aliquot show` (the history lines add the tube id token).

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
`--force` requires `-m/--message`, records `aliquot_event.forced = TRUE` and is the only way to make
an unforced-illegal change; without `--force` the CLI refuses and names the allowed next status(es).
Without `--force`, `-m/--message` is optional; a given value is trimmed, refused when empty or
whitespace-only, and stored in `aliquot_event.message`.

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
event text, stored in `aliquot_event.message` (spec 7), never the ASCII commit message. `use`'s
former purpose and note options are removed outright — no aliases; `tm aliquot status` takes the same
`-m/--message` (optional, required with `--force`); its former note option is removed outright since
0.8.37 — no alias. `move` never had either flag.

An aliquot's box position is not a parent/child relation: `tm parent`/`tm child` (§6.7) never move
a tube, and `tm aliquot move` stays the only command that changes `box_id`/`slot`.

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
`ledger.aliquots.move(ref, *, box=..., slot=..., status=..., message)`; both take no event-text
argument other than `message`, and a `move` message is required and validated like a `use` message.
`ledger.aliquots.status(ref, status, *, message=None, force=False)` takes the same optional message;
`note=` is removed (it raises `TypeError`).

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
`parent_slot` change through `tm box move REF --in PARENT [--slot S]`. The parent/child reference
columns — `stock.parent_stock_id`, `stock.source_aliquot_id`, a box's `parent_box_id`/`parent_slot`
and a kind's ref fields — can also be read, set and cleared with `tm parent`/`tm child` (§6.7), which
add the cycle checks an `edit` does not make. `--set field=` (the value
exactly empty) clears an optional column to NULL, a required field cannot be cleared, and a
whitespace-only value is not empty: it is coerced like any other value, never cleared. One edit is
one Dolt commit, `--json` returns the loaded record and `--dry-run` writes nothing.

`tm stock note REF [TEXT] [--append] [--clear]` writes a stock's core `notes` column without shell
quoting. A bare `REF` opens the current note in `$VISUAL`, then `$EDITOR`, then `vi`; the temp file
carries the value above a git-style scissors line whose metadata is stripped on the way back, and
lines starting with `#` are part of the note. An unchanged buffer prints exactly `no changes`, exits
0 and writes nothing. `--append TEXT` adds a line to the note: the separator is one `"\n"` and it is
omitted when the note is empty. A bare `--append` opens an **empty** buffer and appends its result;
`--clear` sets the notes to `NULL`. An empty result without `--clear` is a usage error (exit 2), so
a stray `:wq` cannot wipe a note by accident, and `TEXT` without `--append` as well as `--append`
with `--clear` are usage errors too. `TEXT` is a **positional** argument, before or after
`--append`, because this typer release cannot express an option with an optional value
(`tm stock note REF --append "text"` still appends that text). REF resolves exactly as
`tm stock edit` resolves it — full id, exact alias or unique id prefix; an ambiguous prefix is a
usage error. Each successful call is one write session and one Dolt commit
(`edit(stock): <kind> <id>`; the note text stays out of the ASCII-only summary), `--json` (also the
global one) prints the loaded record and `--dry-run` writes nothing.

`tm entity note` and `tm box note` take the same flags, resolution rules and behaviour as
`tm stock note` above: a bare REF opens the current note, `--append TEXT` adds a line (a bare
`--append` opens an empty buffer), `--clear` sets the notes to NULL, and an empty result without
`--clear` is a usage error. They print their group's `edit` line instead — `Edited entity <kind>
<id>` and `Edited box <name> (<kind>)` — and a box REF resolves by its human name first, then id or
alias. All three commands share one CLI body and the generic `note_object` writer; each call is one
Dolt commit (`edit(entity): <kind> <id>` and `edit(box): <kind> <id>` summaries included).

`tm aliquot edit ALQ --set field=value …` corrects the recorded fields of one tube. `ALQ` is the
full aliquot id (`<stock>.<seq>`) or one of the tube's aliases; a shortened id prefix is refused,
as on `tm aliquot show`. Exactly three columns are editable: `quantity`, `unit` and `created_at`
(the tube's own `notes` column is gone in spec 8: an aliquot borrows its stock's note, which
`tm aliquot list`/`show` print). A column not listed is not editable through `edit` by default: `id`, `stock_id` and
`seq` are identity data (an aliquot id *is* `<stock_id>.<seq>`) and have no substitute command;
`label` changes through `tm aliquot label ALQ LABEL [--replace]`; `status` through
`tm aliquot status ALQ NEW [--force -m MSG]`; and the position, `box_id`/`slot`, through
`tm aliquot move ALQ --box NAME [--slot S] -m MSG`. Where a command owns the column, the refusal
names it; identity data has no substitute. Derived values (`path`, `in_stock`, the owning stock's
`stock_kind`) are not columns and are never editable.

An edit **corrects the current value**; it does not replay or rewrite history. The tube's
create/use/move events stay as they were, and the remaining quantity is not recomputed from past
consumption: a tube recorded with 500 ul, used down by 100 ul, then edited to `quantity=600` now
holds 600 ul, while the use event still records 100 ul. Editing is allowed after a `use` or a
`move`. Each successful edit appends one `edit` event whose `amount`, `unit`, position and status
columns stay empty; its `message` holds the summary of the changed fields, with the optional `-m`
reason appended as `; reason: MSG` (a blank reason is refused). The exact column-by-column diff is
kept by Dolt history rather than duplicated on the event, and the whole edit is one Dolt commit.

Validation applies to the **final record** after every `--set`, and a failure writes nothing:
`quantity`, when given a value, must be finite and greater than zero (setting it to zero is
refused with a hint to `tm aliquot use`; negative, non-numeric and infinite values are refused).
`quantity` and `unit` must end up both present or both empty: changing just one is allowed while
the final pair stays valid, clearing either means clearing both, and adding one to a tube that has
neither must be done in the same edit. `unit` must not be empty and is at most 32 characters;
`created_at` takes a naive ISO datetime (no time zone) and cannot be cleared. The whole `--set` set is validated as one change, so one invalid field leaves no
partial update, event or commit, and an edit with no actual change is refused. `--dry-run` prints
the planned SQL and writes nothing; `--json` returns the loaded tube record with the corrected
values overlaid.

`tm export` reads a box from `box` joined with `box_<kind>` — the per-kind `v_box_<kind>` views are
gone (spec 5). `tm import` creates each new box's `v_box_<name>` view and applies the name and
grid rules above, in the same commit as the rows.

`tm entity show`, `tm stock show`, `tm aliquot show` and `tm box show` include the object's aliases as
a sorted `aliases` list in JSON output and print an `aliases` row or line in table output; `list`
**table** output stays narrow. `tm aliquot list --json` is the exception among lists: an aliquot has
no per-kind view to project, so its items are the `tm aliquot show` record without `events` (a list
must not read every tube's event trail) and without `related` (the related links are a show
convenience; a listing stays the narrow per-tube view) and carry `aliases` too. The generated views
expose the same data as `alias_list` / `occupant_alias_list` (§3).

`tm aliquot show ALQ` shows one tube. `ALQ` is the full aliquot id (`<stock>.<seq>` — never a
shortened prefix) or one of the tube's aliases (including the old ids `tm stock renumber` keeps).
The record holds the aliquot row (`id`, `stock_id`, `seq`, `status`, `quantity`, `unit`, `label`,
`created_at`), the owning stock's `notes` as `stock_notes` (the tube has no notes of its own in
spec 8: it borrows the stock's), the sorted `aliases`, the owning stock's `kind`
and `entity_id`, the owning entity as `related.entity` (below), the tube's `box_id` and `slot`, the `path` read from
`v_aliquot_location` (§3, `Freezer-1/Shelf-2/Box:A3`), the derived `in_stock` flag (true exactly
when the status is `stored`, §1.1), and finally the tube's `events`, oldest first (`[]` when the
tube has none). Each event carries `id`, `event`, `at`, `amount`, `unit`, `from_box_id`, `from_box`,
`from_box_resolved`, `from_slot`, `to_box_id`, `to_box`, `to_box_resolved`, `to_slot`,
`old_status`, `new_status`, `forced` (a bool), `actor` and `message`; `from_box`/`to_box` are the
live box name, else the stored id, and `from_box_resolved`/`to_box_resolved` say which (`true` for
a live box row, `false` otherwise, `null` when the side has no box id). Event lines follow §6.6.
`events` is not a key/value row: text output prints the field block, then the `*` lines — the
`* entity` related line, then the `* inherited from stock <id>` line that names the stock the note
comes from — then the `path:` line (when the tube has a position), then `events  N` (two spaces
before the count) and one line per event, oldest first, rendered by the same `event_line` helper
`tm history` uses. In the field block the stock's note is the `* notes` row (the `*` marks a value
that is not the tube's own property) and it prints the **full** note, never the list preview. `related.entity` holds `{id, kind, name}`, or `{id, missing: true}` when the entity row is
gone. `--json` prints the whole record, related and events included; `--format csv` stays the flat
single-row record and drops `related` and `events` (event rows are `tm history ALQ --format csv`),
adding `stock_notes` to its columns (clean field name, full text). The Dolt commits stay in `tm history`:
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
and includes the box plus every box nested inside it. `--where FILTER` (repeatable, AND) and
`--where-any FILTER` (repeatable, one OR group ANDed with the `--where` filters) take the shared
filter syntax of §6.4 with all eight operators — `=`, `~`, `!=`, `!~`, `<`, `<=`, `>`, `>=` — on the
aliquot core columns `id`, `stock_id`, `seq`, `box_id`, `slot`, `quantity`, `unit`, `label`,
`created_at` and `stock_notes` (the owning stock's note, read from the joined `stock`
row); an aliquot has no kind row, so there are no kind fields, and `status` stays
the `--status` filter (`--where status=…` is a usage error pointing at `--status`, in either group).
`=` and `!=` compare the raw text as they always have, so an empty value (`FIELD=`) means the column
is empty: `NULL` or `''` for `id`, `stock_id`, `box_id`, `slot`, `unit`, `label` and
`stock_notes`, and `NULL` only for `seq`, `quantity` and `created_at` (a stored `0` never looks
empty). An ordered
comparison is typed: `seq` is numeric, `quantity` is a decimal, `created_at` is an ISO date or
datetime (a date means its midnight) and the other columns compare in the database's collation
order; a non-numeric ordered value for `seq`/`quantity` is a usage error naming the filter. The
Python API takes the same two groups as `where=` / `where_any=` (a mapping is equality only, with
`None`/`''` meaning "is empty"; a list of strings takes every operator). The groups only narrow the
rows: `--all`/`--not-in-stock`/`--status`/`--kind`/`--box` decide the scope and apply unchanged.
The table and CSV print the narrow rows `id stock_kind status quantity unit label path
stock_notes` (`path` is empty when the tube is not in stock). The table header for the last column
is `*notes` (no space, so the column keeps its width) and the cell is the shared notes preview —
the first non-empty line, whitespace-stripped, cut at 40 terminal cells with `…` when longer, plus
` (+N lines)` when N further non-empty lines follow (see **List table columns**); every row repeats
its stock's note. CSV keeps the clean name `stock_notes` and the full text, and so does `--json`,
which prints the same record `tm aliquot show` prints without `events`, one per tube, in
`stock_id`/numeric-`seq` order. The command is read-only
and changes no ledger format — `spec_version` is untouched.

`tm stock show` embeds the stock's tubes. Each row carries `id`, `seq`, `status`, `quantity`, `unit`,
`label`, `box_id`, `slot`, the slot-inclusive `path` (`NULL` when the tube is not in stock), the
sorted `aliases` and the derived `in_stock`; the rows, paths and aliases ride in the same read batch
as the stock. Text output prints the stock's field block, then its related lines (entity, parent
stock, source aliquot, in that order), then `aliquots  N` and the tube **cards**, flowing left to
right and wrapping at the console width: the title is `<id> · <status>` (coloured by status), then
the quantity/unit, a `label:` line when the tube has one, the path or `not in stock`, and an
`aliases:` line when it has aliases. Every card keeps the rounded border; only its colour follows
the status. `--json` keeps the enriched rows in the record and adds a `related` object holding
`entity: {id, kind, name}`, `parent_stock: {id, kind, entity_id, entity_name, entity_missing}` and
`source_aliquot: {id, seq, status, stock_kind, entity_id, entity_name, entity_missing}`; a NULL
optional reference is `null`, a deleted one is `{id, missing: true}`. `--format csv` stays the
machine form and drops `related`, keeping exactly its previous columns; the tabular tube view is
`tm aliquot list`.

**Related-object lines.** Both commands print related objects as plain text lines outside the rich
tables, starting at column zero with `* ` and the label padded to a 14-character column, so every
value starts at column 18:

```text
* entity          plasmid · pUC19
* parent stock    bacterial_glycerol · 745b8daa (pUC19)
* source aliquot  <full aliquot id> · stored · bacterial_glycerol of pUC19
```

UUID ids use the same eight-character prefixes as the lists; aliquot ids stay full `<stock>.<seq>`
values. A line is omitted entirely when its reference column is NULL and its JSON value is `null`.
A deleted parent stock or source aliquot prints `<8-char id> (missing)` (the source keeps its full
aliquot id) and is `{id, missing: true}`; a deleted entity prints `<8-char entity id> (missing)` in
the entity line and is `{id, missing: true}` in `related.entity`. An existing entity with no name
prints `kind · <8-char entity id> (unnamed)` and has JSON `name: null`. Parent-stock and
source-aliquot lines resolve their **own** entity, never the displayed stock's: the parent's entity
prints `(name)`, `(unnamed entity <8-char id>)` or `(missing entity <8-char id>)`, and the
source's prints `of <name>`, `of unnamed entity <8-char id>` or `of missing entity <8-char id>`;
their JSON keeps `entity_id`, sets `entity_name: null` for an unnamed entity and `entity_missing`
to `false` for it or `true` when the entity is gone. Only the missing part is marked — a deleted
object is not the same as an object whose entity is gone. Missing references never fail an
otherwise successful show (exit 0). In these lines actual control characters (newline, carriage
return, tab) become the literal separator ` | `, never backslash escapes; ordinary spaces,
punctuation and Unicode stay. JSON keeps the original values and CSV is untouched: it excludes
`related` and adds no columns or lines. The lookups are bounded: stock show resolves all three
relations as extra statements inside the stock's own read batch (no extra process, no growth with
the tube count), and aliquot show resolves its entity in the query it already runs.

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
`via` ∈ `entity | parent_stock | source_aliquot | <kind>.<field>`. Its `parent_stock`,
`source_aliquot` and lineage-ref edges are the ones `tm parent --set`/`--clear` write (§6.7).
`tm lineage` respects `--branch`, writes nothing and never commits.

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
`.name`, `.entities`, `.stocks`, `.aliquots`, `.boxes`, `.relations`, `.find()`, `.check()`); the
CLI is a thin layer over it. `tetramark.init_ledger(path, name=None, presets=...)` and
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
`list` prints what can be used now, not the whole history. `tm aliquot list` hides the tubes that
left stock unless `--all` or an explicit `--status` names them. `tm stock list` prints only the
stocks whose derived status is `active` (at least one aliquot in stock) unless `--all`; `--all` adds
the `empty` and `depleted` stocks. `status` is a derived view column, **not** a `--where` field:
`tm stock list --where 'status=active'` is the unknown-field usage error (exit 2), with or without
`--all`. The Python API mirrors the CLI — `ledger.stocks.ls(all_statuses=False)` is the default
scope and `all_statuses=True` is `--all`. The entity and box lists have no availability notion and
keep printing every row.

List tables abbreviate uuid ids the way git abbreviates hashes: `tm entity list` and `tm stock list`
print the shortest **unique** prefix of at least 8 characters (ids that share the first 8 grow
together until they differ). `tm stock list` shortens both its `id` and its `entity_id` column when
that value is a uuid id, so a ledger with a `uuid` stock strategy gets short stock ids too.
`tm <object> show`, JSON and CSV always print the full 36-character id, so it remains the
cross-command identifier, and datecode/sequence ids (`0826FP01`, `S00042`) and aliquot ids
(`0826FP01.1`) are never shortened.

**List table columns.** A list command's default table and its `--format csv` output print the same
fixed, readable subset of the record; `--json` keeps the full record. The leading columns identify
the row and the rest hold the values a person decides on, with long identifying values (an entity
name) printed in full, never truncated. The trailing `notes` column is the one deliberate table-only
shortening; `tm <object> show`, `--json` and `--format csv` keep the full note.

`notes` is the last column of all three list tables and is always shown. A note prints its first
non-empty line, whitespace-stripped (leading blank lines are skipped), cut at 40 terminal cells when
longer — measured with `rich.cells.cell_len`, so CJK and Greek text aligns, and never splitting a
wide character or combining sequence — with `…` appended when cut. When N further non-empty lines
follow, ` (+N lines)` is appended; blank lines are not counted. A `None`, empty or all-blank note
prints an empty cell.

- `tm entity list` prints `id kind name stocks_available aliquots_in_stock notes`. `stocks_available`
  is the number of the entity's stocks whose derived status is `active` (at least one aliquot in
  stock); depleted and empty stocks are not counted. `aliquots_in_stock` sums the in-stock aliquots
  of all the entity's stocks. Both are `0` for an entity without stocks.
- `tm stock list` prints `id kind entity_id entity_name aliquots_in_stock status notes`. By default
  it lists only the `active` rows (at least one aliquot in stock); `--all` adds the `empty` and
  `depleted` rows, with the same columns. `entity_name` is the owning design's name. `status` is
  derived from the aliquots: `empty` when the stock has none, `active` when at least one aliquot is
  in stock, and `depleted` when it has aliquots but none of them is in stock (a tube that was never
  placed in a box is not in stock either).
- `tm box list` prints `id name kind path occupied capacity notes`. The table shortens `id` to a
  unique prefix of at least eight characters among the listed rows; CSV and JSON keep the full UUID.
  `occupied` counts the aliquots and the immediate child boxes directly in the box, counting items
  rather than slots; the contents of nested boxes do not roll up into the parent. `capacity` is the
  number of slots times the kind's `slot_capacity` for `grid`/`named` geometry, the declared
  capacity for slotless geometry, and blank (`null` in JSON) when a slotless box is unlimited.

`tm box show` starts with `<name>  <kind>  id: <short>  path: ...`, where `<short>` is the
first eight characters of the box UUID. JSON includes `id` as the full UUID; CSV keeps its
slot rows (`slot row col occupants`).

`tm aliquot --help` directs placement changes to `move`, quantity changes to `use`, and
status changes to `status`.

### 6.3 The interactive form — `tm <object> add-form`

`tm entity add-form`, `tm stock add-form`, `tm aliquot add-form` and `tm box add-form` open an
interactive form that asks for what a person does not know by heart: the kind, the fields, the
lineage references, the notes, the box, the slots and, for tubes, the creation status. The form
takes **no positional argument and no flags** — every question comes from the ledger's own schema.
It is the human counterpart of `tm <object> add`; **agents keep using `add`**, which is scriptable,
never blocks and writes exactly the same row.

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

**Every writable creation property.** Apart from the generated ids and timestamps, every property
`tm <object> add` can write is asked: `--id` stays an add-only option (back-filling and migration)
and is never a form question. A `notes` field declared by the chosen kind is a normal kind field,
asked in schema order, and is then the only notes question; when the kind has no `notes` field the
form asks the core `notes` question at the end (entity, stock and box) and stores it in the core
column. No step key is asked twice in one plan.

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

**The stock form** asks for the stock itself, in this order: the **kind**, the **entity** (a ref,
with search), the **kind's fields**, the optional **parent stock** (a ref, with search: the stock
this one was derived from; lineage, the same as `tm stock add --parent`), the optional **source
aliquot** (a ref, with search: the aliquot this stock was made from; the same as
`--source-aliquot`) and the optional **notes** (free text). Parent and source aliquot are
independent — give both, one or neither — and the core's own validation is the only cross-check.
`?term` searches and the resolved name is shown back, exactly like every other ref step. The form
never creates aliquots: it writes exactly what `tm stock add KIND --entity E --aliquots 0
[--parent P] [--source-aliquot A] [--notes N]` writes, so the new stock is empty, and then prints a
`Next:` line pointing at `tm aliquot add-form` (or `tm aliquot add <stock> --count N --quantity Q
--unit U`). Tubes, with their count, quantity, unit, box and slots, are always added through the
aliquot workflow; `tm stock add` keeps `--aliquots` (default 1) for agents and scripts. The other
forms ask for what their `add` command needs, in the same way.

**The aliquot form** asks, in this order, the **kind** (the stock kinds — an aliquot has
no kind of its own), the **stock** (a ref, with search), the **aliquot count** (default 1), the
**quantity**, the **unit**, the **box**, the **slots** and the **creation status**. The **box** is
optional and `?` lists the boxes; the **slots** ask one slot per new tube, and Enter accepts the
next free slots in order. Slots are asked only when a box was given (and the box has slots), and a
slotted box needs one slot per tube. The **quantity** is optional, with no default and nothing
pre-filled from the stock or from the tubes already there: Enter on the empty step skips it. When
given, it is the amount **each** of the new tubes holds — the same amount in every tube, never
divided between them — as with `tm aliquot add --quantity Q --unit U` and `tm stock aliquot STOCK AMOUNT UNIT`,
where `AMOUNT` is also per tube. A count of `3` with `2.5 ml` makes three 2.5 ml tubes, and `0` is
allowed. It is checked as typed: a decimal number `>= 0` is accepted as written, a non-number is
refused with `quantity must be a decimal number (got '…')`, and a negative or infinite value with
`quantity must be a finite, non-negative number (got '…')`. The **unit** is asked only when a
quantity was given; then it is required, so an empty Enter is refused with `unit is required`, and
it is free text of 1–32 characters after trimming (a longer value is refused with
`unit must be at most 32 characters`), with no list to choose from, no normalisation, no conversion
and no default. Skipping the quantity skips the unit with it and stores no quantity and no unit
(`NULL`, exactly as before). The result is the same write as
`tm aliquot add --quantity Q --unit U`: the same tubes, the same amount per tube and the same
create events.

The **creation status** is optional and comes last. It offers exactly the statuses of the stock
kind's set, in the set's own order, picked by number or name (Tab completes); an invalid value is
refused with the enum message. The help line only says that Enter keeps the add command's default
for this placement — it makes no promise about `stored` or `pending`. That default is the core's
placement rule, unchanged and not re-implemented by the form: a tube placed in a box takes the
first of `stored` and `pending` that the set has; an unplaced tube takes the first of `pending` and
`stored`; a set with neither takes its first **in-stock** status; and a set with no in-stock status
takes its first status. Because an in-stock status needs a position (spec 5), skipping the status
on a set without a non-stock default can still be refused by the core when the tube is unplaced;
the form shows that refusal and returns to the step. A chosen status is stored and recorded as the
create event's `new_status`, exactly as `tm aliquot add --status` — including a non-default status
of a custom or legacy per-kind set. The result is the same write as
`tm aliquot add ... --status S`: the same tubes and the same create events.

**The review screen** lists every value, numbered, with long values in short form. Typing a number
jumps back to that step, `y` writes, `q` cancels.

**The write** is the same `ledger.<objects>.add(...)` call that `add` makes, with no separate
validation or write logic: the same message is printed, `--dry-run`, `--no-commit` and `--json`
behave as they do for `add`, and **the write lock is taken only when `y` confirms**. If the core
rejects the entry, the error is shown and the form goes back to the field that failed. There is no
"add another": the form ends after one write.

### 6.4 `--where` filters and `tm find`

`tm entity list`, `tm stock list`, `tm box list`, `tm aliquot list`, `tm find` and their Python
`where=` / `where_any=` arguments take one filter syntax with eight operators: `field=value` (exact),
`field~pattern` (regular expression), `field!=value`, `field!~pattern`, and the ordered comparisons
`field<value`, `field<=value`, `field>value` and `field>=value`. The operator is the one that
follows the field name, so a value may contain `=`, `~`, `!`, `<` and `>`; the field name before it must be an
identifier (a letter or `_`, then letters, digits or `_`), and it is case-sensitive. Whitespace
before the field name and between the name and the operator is skipped; the value is everything
after the operator, verbatim. A filter with no operator, or with a field name that is not an
identifier, is a usage error (exit 2). The same syntax serves the four lists (entity, stock, box and
aliquot) and `tm find`; the aliquot list differs only in its field set and its typed ordered values
(below).

`field=value` compares the column exactly. On a kind field the value is coerced by the field's schema
the way `--set` coerces it: an `enum` must be one of its values, a `ref` may be an id or an alias and
is resolved to the id it stores, an `int`/`decimal`/`bool`/`date`/`datetime` is parsed, a `sequence`
is normalised. On a core column the text is compared as written: `id`, `kind`, `name`, `created_at`,
`notes` and the core references (`entity_id`, `parent_stock_id`, `source_aliquot_id`, `parent_box_id`,
`parent_slot`) take the stored value itself, so a core `ref` such as `entity_id` takes the id, never
an alias.

`field!=value` matches the rows whose column holds another value, and the rows whose column is
`NULL`: `--where 'notes!=checked'` lists the rows whose `notes` is anything else, the rows without a
`notes` value included. It is the complement of `field=value` (see the three-valued logic below). On
a kind field the value is coerced by the schema exactly as it is for `=`, and a core column takes
the text as written; a computed kind field refuses `!=` the way it refuses `=`.

`field=` (an empty value) selects the rows whose field is empty: `NULL` or `''` on a text-like
column, and `NULL` only on a non-text column (a stored `0` never counts as empty). Text-like means
the kind field types `string`, `text`, `enum`, `sequence` and `ref`, and every core column except
`created_at`; non-text means the kind types `int`, `decimal`, `bool`, `date` and `datetime`, and the
core `created_at`. The empty value skips the schema coercion and the alias resolution above (an
empty `ref`, `enum` or `int` is not resolved, checked or parsed), and it does not mean the literal
text `none`/`null`, which still compares exactly like any other value. Only the truly empty string is
special: a whitespace-only value is not (`name=  ` compares the two spaces exactly). Its complement
is `field!=` (an empty value), which selects the rows that have a value and excludes the `NULL` and
the empty rows: that is how the user asks for "this field is filled in" (`--where 'notes!='`).

`field~pattern` matches the column's **text form** with a regular expression. It is a *search*, not a
full match: unanchored, so anchor it with `^`/`$` when needed (`'name~^pZET'` starts with,
`'name~ZET'` contains). It is case-sensitive; write `(?i)` in the pattern for case-insensitive
(`'name~(?i)^pzet'`). The database compiles the pattern (Dolt's ICU/MySQL 8 dialect), so `\d`,
`\p{L}`, classes, groups and lookarounds behave as there, not exactly as in Python `re`; the ledger's
`find.rewrites` rules (described below) are Python `re` patterns and do not share this dialect. The
text form of an `int`, a `decimal`, a `date`/`datetime` or `created_at` is its printed value
(`'length_bp~^15'`, `'created_at~^2026-11-04'`); a `ref` column, core or kind, is matched against the
id it stores, never its aliases; `NULL` never matches a `~` pattern, not even `'field~^$'` (use
`field=` to find empty values). A computed kind column can be addressed by `~` and `!~` (its text
form), while `=` and `!=` are refused by the computed-field usage error, empty value included.

`field!~pattern` matches the rows whose text form does not match the pattern, and the rows whose
column is `NULL`: `--where 'name!~GFP'` lists the rows whose `name` does not contain `GFP`, the rows
without a `name` included. It is the complement of `field~pattern` and takes the same pattern syntax
and database dialect.

`field<value`, `field<=value`, `field>value` and `field>=value` compare the column with the value in
the order of the field's type. A kind field's value is coerced like an `=` value (so
`--where 'count<abc'` is the integer usage error); a core column takes the text as written, except
`created_at`. An `int` or a `decimal` compares numerically and a `date` or a `datetime` compares
chronologically; a `string`, a `text` and a core text column compare in the database's collation
order, the same order `=` uses. The core `created_at` compares chronologically, and its value must be
an ISO date or a datetime — a date means its midnight — so anything else is a usage error (`invalid
--where 'created_at>yesterday': the value 'yesterday' is not an ISO date or datetime`). A `bool`, an
`enum`, a `ref`, a `sequence` and a computed field have no order: an ordered comparison on one of
them is a usage error (exit 2), and the declaration order of an `enum` is never used. A `NULL` never
matches an ordered comparison. On a listing or a `tm find` that spans kinds, the field's type in each
kind decides, so a kind that declares the field without an order makes the filter a usage error
rather than skipping that kind. `tm aliquot list` has no kind fields, so it types them itself: `seq`
compares as an integer, `quantity` as a decimal, `created_at` keeps the ISO date/datetime rule above
and every other column compares as text; its `=`/`!=` keep the raw text, so an input that was
accepted before is never rejected.

The negated operators complement their positive ones, but the empty value makes `field!=` a
different question from `field!=x`:

* `field!=value` is the complement of `field=value`: for every row in scope exactly one of the two
  matches, and `!=` also matches the `NULL` rows, so `field!=x` covers the `NULL` rows a plain
  `field=x` would miss;
* `field!~pattern` is the complement of `field~pattern`: for every row in scope exactly one of the
  two matches, and `!~` also matches the `NULL` rows;
* `field!=` (an empty value) is the complement of `field=` (the empty-field filter): it matches the
  rows that have a value and excludes the `NULL` and the empty rows. It is the way to ask for "this
  field is filled in", and it is not the same as `field!=x`.

The asymmetry is the point: `field!=x` is not "the field is set" (it includes the rows where the
field is `NULL`), while `field!=` with no value is exactly that.

The rules are the same everywhere (the two repetition rules are `--where`'s alone, see below):

* a field may be filtered with `=` once; a second `=` on the same field is a usage error
  (`field 'name' appears twice in --where`);
* `=` must not be mixed with another operator on one field (`field 'name' cannot be filtered with
  both '=' and '~' in --where`); every other operator may be repeated on one field and they are
  ANDed (`--where 'name~^p' --where 'name~9$'`,
  `--where 'created_at>=2026-01-01' --where 'created_at<2026-02-01'`);
* an empty pattern (`name~` or `name!~`) and an empty comparison value (`count<`) are usage errors;
  an invalid pattern is a usage error that names the filter (`invalid regular expression in the
  --where filter 'name~['`), never a raw database error;
* a field that is neither a core filter column nor a field of a kind in scope is a usage error that
  lists the valid fields (`unknown field(s) 'nope' in --where; valid fields: …`).

The valid fields are the core filter columns of the object type plus the fields of the kinds in scope:
`id`, `kind`, `name` (entity, box), `created_at` and `notes` for all three, `entity_id`,
`parent_stock_id` and `source_aliquot_id` for stocks, and `parent_box_id` and `parent_slot` for boxes.
`tm aliquot list` has no kind rows, so its valid set is the fixed allowlist `id`, `stock_id`,
`seq`, `box_id`, `slot`, `quantity`, `unit`, `label`, `created_at` and `stock_notes`:
nothing is dropped per kind, no view-derived column is filterable and `status` is refused
(`--status` owns it).
A computed kind column is accepted by `~` and `!~` as described above; view-derived columns (`path`,
`capacity`, `occupied`, `status`, the counts) are not filterable. A field that no kind in scope has is
the unknown-field usage error above. When a field is declared by only some of the kinds being listed,
only those kinds are read: a kind without **every** filtered field contributes no rows rather than
ignoring the filter (`tm stock list --where 'induction=iptg'` reads the stock kinds that declare
`induction` and not the others; like every `tm stock list`, it shows active stocks, so add `--all`
for every status). On `tm find` the valid set is wider — the core columns of entity,
stock and box and the fields of every entity/stock/box kind — and a filtered field that only some
kinds have simply excludes the other kinds from the result.

`--where-any FILTER` (no short flag; the four lists, `tm find` and Python `where_any=`) is the one
**OR group** of an invocation: a row must satisfy every `--where` filter **and** at least one
`--where-any` filter, as in
`tm stock list --where 'kind=plasmid_prep' --where-any 'resistance=kan' --where-any 'resistance=amp'`
(which reads as `kind=plasmid_prep AND (resistance=kan OR resistance=amp)`) or
`tm aliquot list --where 'quantity>=100' --where 'created_at>=2026-01-01' --where-any 'label~^cl'
--where-any 'label~^ctl'`.
Without `--where-any` nothing changes. The group takes the same syntax, operators, coercion, type
rules and empty-value rules as `--where`, and the rules above apply to it with one exception: the
per-field repetition rules do not, because repeating a field is how an alternative is written (the
`=` at most once rule and the `=`/other-operator rule are `--where`'s alone, and there is no
cross-check between the two groups). Per kind, an alternative whose field the kind does not declare
is dropped (it is false for that kind); a kind left with no surviving alternative contributes no
rows. Errors name the option the filter came from (`invalid regular expression in the --where-any
filter 'name~['`, `unknown field(s) 'nope' in --where-any; valid fields: …`). The surviving
alternatives become one `(cond OR cond …)` condition appended to the kind's AND conditions, with
their parameters in order, in the same single `query_many` batch.

Every `--where` filter is ANDed and `--where-any` is the one OR group; `--where` (also `-w`) and
`--where-any` are repeatable. The listings and `tm find` build all statements of the read first and
fetch them in one `query_many` batch, so neither group costs a process per kind. A few paths still
cost extra queries: naming an invalid pattern (the error path probes the patterns to name the bad
one), a field combination no kind can express (nothing compiles the patterns, so each pattern filter
(`~` or `!~`) is probed once up front), and an `=` or `!=` filter on a kind `ref` (the alias may be
resolved). A listing that matches nothing prints `(none)` (an empty list in JSON) and exits 0.

`tm find [QUERY] [-E|--regex] [--where FILTER ...] [--where-any FILTER ...] [--with-rewrite]`
searches the whole ledger. Without `-E`, `QUERY`
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

Both filter groups apply to every hit after the search — every `--where` filter (AND) and at least
one `--where-any` filter (OR) — so even an id or alias hit must satisfy them. Aliquots are excluded
whenever either group is given (they have no kind row to filter); `tm find 1226L401.1 --where
'id~^12'` therefore reports no match. `--where` takes the same eight operators here, so a core
timestamp narrows every object type (`tm find --where 'created_at>=2026-01-01'`). `QUERY` is optional
with a filter: `tm find --where 'resistance=kan'` and `tm find --where-any 'resistance=kan'
--where-any 'resistance=amp'` list every matching object with `match` = `filter`, sorted by object
type (entity, stock, box), kind and id. Passing neither a `QUERY` nor a filter is a usage error
(`the search query must not be empty; pass a QUERY or at least one --where or --where-any filter`). A
search that finds nothing exits 1: `no match for '<QUERY>'`, or — when only filters were given —
`no objects match the --where filters` for a `--where`-only invocation and `no objects match the
--where/--where-any filters` once `--where-any` is given; filter syntax/field errors and an invalid
`-E` pattern exit 2.

**`find.rewrites` rules.** A ledger may declare rewrite rules for literal `tm find` queries in
`ledger.yaml` (`find.rewrites`, §1.2): each rule has a unique `name`, a Python `re` `pattern` matched
against the whole trimmed `QUERY` with `re.fullmatch`, and a `replacement` expanded with
`match.expand` (`\1`/`\g<name>` back-references). That is the Python dialect — deliberately not the
database (ICU) dialect of `-E` and the `--where` patterns (`~` and `!~`, in either group) — and the
rules are validated when the ledger opens, with the rule named in the error (a bad pattern, a
replacement that names a group the pattern lacks, a missing or duplicate `name`, or an empty
`rewrites:` list; exit 1). The rules rewrite `QUERY` only: `--where` and `--where-any` filters are
never rewritten (they still narrow every hit, rewrite hits included), `-E` is never rewritten, and
single-object resolution (`show`, `history`, `lineage`, `--entity`, alias resolution) stays exact: a
plain not-found there ends with `try tm find <REF>` (the command that does apply the rules), while
wrong-type, ambiguous and empty refs keep their own error unchanged.
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

The Python API mirrors the CLI: `ledger.find(query=None, *, regex=False, where=(), where_any=(),
with_rewrite=False)`, `ledger.entities.ls(kind=None, *, where=(), where_any=())`,
`ledger.stocks.ls(kind=None, *, where=(), where_any=(), all_statuses=False)` and
`ledger.boxes.ls(...)`; the stock default lists only active stocks, exactly like the CLI, and
`all_statuses=True` is `--all`. A `where`/`where_any` **mapping** is equality
only (`where={"name": "pUC19"}`): its key is the field and its value is the value, so an operator is
never parsed from either (`{"name": "pUC19~x"}` compares the text `pUC19~x`), and a `None` or `""`
value still means "the field is empty". A list of strings accepts every operator
(`where=["name~(?i)^puc", "od600>=2", "notes!="]`, `where_any=["resistance=kan", "resistance=amp"]`).
Parsing, coercion and errors are the same code as the CLI.

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

### 6.6 Event lines — `tm history` and `tm aliquot show`

`tm history` prints its `aliquot_event` rows in an `Events (N):` section and `tm aliquot show`
prints the tube's own rows under `events  N`. Both call the same `event_line` renderer, so one row
reads identically in the two commands: `tm history` adds the `aliquot_id` token because one history
answer can mix tubes, while `tm aliquot show` already names the tube and omits it. The `--json`
event objects of both commands carry the same keys, and the text grammar below is the single
contract.

**Grammar.** Tokens, in this fixed order, joined by one space; an absent token is omitted:

```
<at> <event> [<aliquot_id>] [amount=<amount> [<unit>]] [from=<place>] [to=<place>]
[(forced)] [status=<old>-><new> | status=<new> | status=<old>] [by=<actor>]
[message="<stored text>"]
```

(The grammar is wrapped for reading; the rendered event is exactly one physical line.)

1. `at` — the event time, as stored.
2. `event` — the event kind, e.g. `create`, `move`, `use`, `status`.
3. `aliquot_id` — only when the event object carries it: `tm history` yes, `tm aliquot show` no.
4. `amount=<amount>` plus ` <unit>` when the unit is not NULL; absent when `amount` is NULL.
5. `from=<place>` — the source place, when there is one.
6. `to=<place>` — the destination place, when there is one.
7. `(forced)` — only when the row's `forced` flag is true.
8. `status=...` — `status=<old>-><new>` when both statuses exist and differ, `status=<new>` when only
   the new status exists, `status=<old>` when only the old status exists.
9. `by=<actor>` — the stored actor, raw and unquoted (actor rendering is unchanged).
10. `message="<stored text>"` — always last; see below.

Tokens 1-9 render exactly as in 0.8.29, except the unresolved-place rule below.

**`message`.** The token is present exactly when the stored `message` is not NULL and is not the
empty string. Its value is the complete stored text (no trimming) encoded as a JSON string with
`json.dumps(message, ensure_ascii=False)`: it is always double-quoted and escapes `"`, `\`, `\n`,
`\r`, `\t`, `\b`, `\f` and every control character below U+0020 as JSON escapes. In addition
U+0085, U+2028 and U+2029 are written as `\u0085`, `\u2028` and `\u2029`, so the whole event line is
exactly one physical line (`str.splitlines()` of the line has length 1). Non-ASCII text such as CJK
is left literal. `json.loads` applied to the text after `message=` returns the stored message
exactly.

**Place.** A place is the box part followed by an optional `:<slot>`; it is the slot alone when
there is no box id, and it is omitted when both box id and slot are NULL. The box part depends on
the event's `*_box_resolved` key:

- `true` — the **current live name** of the box row with the stored id (`*_box`);
- `false` — `[unresolved box <FULL stored box id>]`, keeping the complete id so two different refs
  cannot be conflated;
- absent (a legacy dict caller) — the pre-0.8.32 behaviour: `*_box`, else `*_box_id`.

An unresolved place never says "deleted" and never names a cause: the reason a reference is
unresolved (purged, legacy, imported, missing) is not known and is not inferred or printed. The full
form is e.g. `from=[unresolved box 930b7cc2-6f06-4dee-aac5-161f329bfe6e]:A2`.

**Renamed boxes.** A resolved box reference shows the current live name of the box row with the
stored id. An old name remains an alias that `tm find` still resolves; there is no
name-at-event-time reconstruction, no `dolt_history`/`dolt_diff` name read, no migration and no
historical event rewrite — `spec_version` is unchanged.

**Bounded lookup.** Both commands resolve these names inside the read batch they already issue.
`tm aliquot show` keeps its single batch and computes the two resolved flags from the box joins it
already has. `tm history` replaces the unbounded all-box name read with box joins scoped to the
matching event rows, in the same batch; its kind-diff follow-up batch is unchanged. Neither command
performs per-event SQL, adds a Dolt process, or adds a round trip, and a history answer does not read
every box in the ledger.

**JSON and API (additive).** Every event in `tm aliquot show --json`, `tm history --json`,
`ledger.aliquots.show` and `ledger.history` gains `from_box_resolved` and `to_box_resolved`: `null`
when the side's `*_box_id` is NULL, `true` when a live box row has that id, `false` otherwise.
`from_box`/`to_box` are unchanged: the live box name, else the raw stored id, `null` when the id is
NULL. In show records the resolved keys sit right after `from_box` and `to_box` respectively. No
other key is added, removed or renamed; show events still have no `aliquot_id`, and `message` stays
raw (unescaped) in JSON. The history event objects carry the same keys.

**CSV.** `tm history --format csv` keeps exactly the 16 `HISTORY_COLUMNS` in the same order; the
`message` column is the raw stored text, quoted only by the CSV writer, and there are no resolved
columns. `tm aliquot show --format csv` still drops `events` entirely.

**Examples.**

```
2026-10-26 11:11:44 create amount=50.000000 ml to=Rack-3:B4 status=stored by=alice
2026-11-04 09:20:44 move 1026KR08.1 from=Rack-3:B4 to=Freezer-B:A2 status=stored by=alice message="re-rack tubes"
2026-11-05 00:08:28 use amount=50.000000 ml from=[unresolved box 930b7cc2-6f06-4dee-aac5-161f329bfe6e]:A2 (forced) status=stored->consumed by=alice message="上样 Capto 柱\n第二轮"
```

The `move` line is the `tm history` form, which carries the tube id; `tm aliquot show` prints the
same event without the `aliquot_id` token. The last line keeps a newline plus CJK text inside one
physical line by escaping the newline as `\n`.

### 6.7 tm parent/tm child — the relation between two objects

`tm parent` and `tm child` read, set and clear one parent/child edge between two objects through
reference columns that already exist. They are the general read/write path for relations that
previously could only be set at creation (`tm stock add --parent`/`--source-aliquot`), changed
through `tm box move`, or not changed at all. No table or column is added: the commands are a CLI
contract over the existing schema, so `docs/spec/02-schema-language.md` and
`docs/spec/03-physical-model.md` are untouched.

**(a) Command surface and orientation.**

```
tm parent REF_A [REF_B] [--set] [--clear] [--via FIELD] [--slot S]
tm child  REF_B [REF_A] [--set] [--via FIELD] [--slot S]
```

`tm parent A B` reads "the parent of A is B": the first positional of `tm parent` is always the
**child** and the second the **parent**. `tm child B A` names the same pair inversely: the first
positional of `tm child` is always the **parent** and the second the **child**, and
`tm child B A --set` is the same write as `tm parent A B --set`.

| Form | Effect |
|---|---|
| one ref, no flag | Read: list every relation in which that object is the child (`tm parent A`) or the parent (`tm child B`), one line per `(field, object)`. Never writes. |
| two refs, no flag | Read: report the relation(s) between the two objects, field name included; none is `no relation between <child id> and <parent id>` and exit 1. Never writes. |
| two refs + `--set` | Write: the parent form sets "parent of A is B"; the child form has the same effect stated inversely. One write lock, one Dolt commit. |
| `tm parent A --clear` | Write: remove A's parent relation (with `REF_B`: only when B is the current parent). |
| `tm child … --clear` | Usage error: removal is `tm parent <CHILD> --clear`; there is no ambiguous "clear all children". |
| `--set` + `--clear` | Usage error. |
| `--set` with one ref | Usage error: `--set` needs the child and the parent. |

**(b) Reference resolution.** Every ref resolves by the `_resolve_core` rules across object kinds:
(1) a full id in any object type (`entity`, `stock`, `aliquot`, `box`, in that order), (2) a box
name or an exact alias, (3) a unique id prefix. A lower tier is consulted only when the higher one
has no hit. A deciding tier with more than one hit is a usage error (exit 2) that names the
candidates with their object types and never picks one, e.g.
`object reference '77' is ambiguous: it matches aliquot 7726LB01.1, stock 7726LB01 (lot); use the full id`.
A per-type ambiguous prefix keeps the `_resolve_core` wording:
`stock id '77' is ambiguous: it prefixes 2 objects (…); use a longer prefix or the full id`.
No hit is a business error (exit 1): `unknown object '<ref>'`. `--via` narrows the child search to
the object type(s) that declare that field.

**(c) Scope matrix (the documented whitelist).**

| Child | Parent | Field (`--via`) | Stored in |
|---|---|---|---|
| `stock` | `stock` | `parent_stock` | `stock.parent_stock_id` |
| `stock` | `aliquot` | `source_aliquot` | `stock.source_aliquot_id` |
| `box` | `box` | `parent_box` | `box.parent_box_id` (+ `box.parent_slot` via `--slot`) |
| any kind | the kind or core object named by the field's `to:` | the field's bare name (e.g. `dam`, `sire`, `parental`, `construct`) | that field's column in the kind's own table; `type: ref` fields only |

Every edge is one existing column: no new table, no schema change, and no ad-hoc overload of an
unrelated column.

**(d) Field selection.** Without `--via`, the candidates for a `(child, parent)` pair are the child
object's core reference fields of the matrix plus every `type: ref` field of the child's kind that
is declared `lineage: true` and whose `to:` accepts the parent (a core object name equal to the
parent's object type, or a kind name equal to the parent's kind). Exactly one candidate is inferred.
More than one is a usage error (exit 2) that lists the candidates sorted and requires `--via`; the
shipped text is

```
mouse_cohort -> aliquot has more than one relation field: dam, sire, source_aliquot; pass --via FIELD
```

(the `mouse_cohort` stock kind declares `dam` and `sire` to `aliquot` with `lineage: true`, next to
`source_aliquot`). Zero candidates is a business error (exit 1):
`no relation field connects <child label> to <parent label>; pass --via FIELD (see docs/spec/06-cli.md 6.7 for the matrix)`.

`--via FIELD` is always accepted, even when the pair is unique. It takes the bare lineage-style
field name (`parent_stock`, `source_aliquot`, `parent_box`, `dam`, `sire`, `parental`, …), never a
table-qualified one: `stock.parent_stock` is refused with a usage error that shows `--via
parent_stock`. A field outside the child's legal set is a usage error that lists the legal fields:
`object stock <id> (lot) has no relation field 'nope'; choose from: parent_stock, source_aliquot`.
A `type: ref` field without `lineage: true` is reachable only through an explicit `--via`. `--slot`
is valid only for a `parent_box` field with `--set`; anywhere else it is a usage error.

**(e) Reading.** `tm parent A` lists every in-scope relation in which A is the child: the non-NULL
core columns of A plus every non-NULL `type: ref` field of A's kind (the `lineage:` flag is
irrelevant for listing), one line per `(field, parent)`. `tm child B` lists every relation in which
B is the parent: stocks whose `parent_stock_id` is B, stocks whose `source_aliquot_id` is B, boxes
whose `parent_box_id` is B, and rows of every kind whose `type: ref` field targets B's object/kind
and holds B's id. The one-ref line shape is

```
<field>  <object> <kind> <id>[ slot <S>]
```

e.g. `parent_stock  stock lot 7726LB01`, `parent_box  box rack 5f4c… slot top`,
`dam  aliquot mouse_cohort 7726LB02.1`. `<kind>` is `-` when no kind is known. A box relation
whose `parent_slot` is set ends with ` slot <S>`. Nothing set prints `no parent relations` /
`no child relations` and exits 0. The two-ref read prints the same lines, limited to the relation(s)
between the two objects: the parent form prints the parent-side lines, the child form the
child-side lines; none exits 1 with `no relation between <child id> and <parent id>`.

`--json` prints `{"relations": [{"via": "<field>", "child": {"object", "kind", "id"},
"parent": {"object", "kind", "id"}, "slot": <S-or-null>}]}`; `slot` is present only for a
`parent_box` relation, and an object with nothing set prints `{"relations": []}`. Reads take no
write lock, open no write session, create no commit and write no row. `--via` is accepted on a read
as a filter (on the child form it must name a field that can point at that parent); `--slot` on a
read is a usage error.

**(f) Writing.** `--set` requires exactly two refs: `field F of A becomes B`, where F is the
selected relation field. A write replaces an existing value silently, like `tm box move` does today;
there is no `--replace` flag, and Dolt history is the audit trail. A `--set` whose target is already
the current parent still writes and commits (the same no-op rule as `tm box move` to the same
parent). The text and `--json` output report the previous and the new parent:

```
Set parent_stock of stock 7726LB01 to stock 7726LB02 (was unset)
Set parent_stock of stock 7726LB01 to stock 7726LB03 (was stock 7726LB02)
Set parent_box of box FR-2 to box RACK-1 slot top (was unset)
Cleared parent_stock of stock 7726LB01 (was stock 7726LB03)
Cleared parent_box of box FR-2 (was box RACK-1 slot top)
```

The write `--json` shape is `{"action": "set"|"clear", "via": "<field>", "child": {"object",
"kind", "id"}, "parent": {"object", "kind", "id"} or null, "previous": {"object", "kind", "id"} or
null}` plus `"slot"`/`"previous_slot"` for a `parent_box` relation. A clear with nothing set is a
read-only no-op: exit 0 with `no parent relations` (JSON `{"action": "clear", "via": null,
"child": …, "parent": null, "previous": null}`) and no session or commit. `tm parent A --clear`
removes A's single currently set in-scope field; when several are set it is a usage error requiring
`--via` (the candidate list is the same one `--set` uses). With two refs, `--clear` is allowed only
when B is the current parent in the selected field, otherwise `no relation between …` and exit 1.
`--slot S` is validated exactly as `tm box move --slot`: a slotted parent requires it, a slotless
parent forbids it, and an occupied slot or a grid box parent is refused. Re-parenting keeps the
object's id and its aliases; it needs no purge plus re-add. A required kind field cannot be cleared
(`field 'x' is required and cannot be cleared`, exit 1, nothing written).

**(g) Validation and errors.** Usage errors (exit 2, nothing written, no commit):

| Error | Text |
|---|---|
| `--set` with `--clear` | `--set and --clear cannot be combined` |
| `--clear` on the child form | `tm child does not clear a parent relation: use tm parent <CHILD> --clear` |
| `--set` with one ref | `--set requires two refs: tm parent CHILD PARENT --set` |
| more than one candidate field | `<child> -> <parent> has more than one relation field: <fields>; pass --via FIELD` |
| unknown/qualified `--via` | `object … has no relation field 'X'; choose from: …` / `--via takes a bare field name, not 'stock.parent_stock'; use --via parent_stock` |
| `--slot` outside `parent_box`/`--set` | `--slot is only valid for a parent_box relation with --set` |
| ambiguous ref | `object reference '<ref>' is ambiguous: it matches …; use the full id` |
| several set fields on `--clear` | `object … has more than one parent relation set: …; pass --via FIELD` |

Business errors (exit 1, nothing written, no commit): `unknown object '<ref>'`; `no relation
between <A> and <B>`; `no relation field connects <child> to <parent>`; the `--via` field does not
match the parent (`--via 'parent_stock' points at stock, but the parent is item: the field and the
parent do not match`); a grid box as parent (`box 'X' is a grid box: grid boxes hold tubes only, so a
box cannot be nested in it`); a missing, forbidden or occupied slot; clearing a required field; a
cycle.

Cycle rules are mandatory. A box containment edge reuses `_check_acyclic`: a box never nests in
itself or a descendant. A stock edge is checked over **both** `parent_stock_id` and
`source_aliquot_id` together — the edges `tm lineage` walks, child stock → aliquot → owning stock —
and a self-parent is included; the walk is bounded by a seen-set, so a corrupted pre-existing cycle
ends the walk politely. A kind ref field is checked along that same field and kind table only. Every
rejection is a `LedgerError` naming the offending ids, raises before the UPDATE, and leaves the
commit count unchanged, e.g.
`cannot set parent_stock of stock 'A' to stock 'B': it would create a cycle in the parent_stock/source_aliquot graph`.

**(h) Deferred rules.** No same-entity rule is enforced: a stock's parent may belong to another
entity, exactly as `tm stock add --parent` already allowed. The ledger-local rules **L02** and
**L03** (`rules/legacy.yaml`, pellet <-> glycerol) are not enforced at write time and no `tm check`
runs after a write — run `tm check` yourself. Both deferrals are recorded as `TODO(Tetramark-ka8)` in
the code (at the validation site) and here; there is no post-write check and no rollback.

**(i) Flags and the write contract.** A write takes the ledger's one-writer lock (P11) and makes
exactly one Dolt commit through the same write session as every other command; the message is
ASCII and built from ids and the field only: `parent(<object>): <child> <field> -> <parent>` for a
set, `parent(<object>): <child> <field> cleared` for a clear. A read never locks and never commits.
`-b/--branch` is respected like everywhere else. **For these two commands only**, `--dry-run` and
`--no-commit` both *preview* the intended SQL and write nothing, for `--set` and for `--clear` alike:
no row changes, the working set stays clean, no Dolt commit is made and no write lock is taken. The
text output is the intended SQL (with `-- target branch: …`); with `--json` the preview is
`{"dry_run": true, "branch": "…", "sql": "…", "result": <the same result object a real write
reports>}`. This is command-specific and differs from the global options table: on every other
command `--no-commit` executes the write and leaves it uncommitted, and that behaviour is
unchanged. All validation (ref resolution, field selection, structural checks, cycle checks) still
runs under either flag, so an invalid call fails with the same error and exit code.

**(j) Python API.** The CLI is a thin layer over `ledger.relations`:

- `get_parent(ref, *, via=None)` → `{"relations": [...]}` — every relation in which `ref` is the
  child (the one-ref `tm parent` shape).
- `get_children(ref, *, via=None)` → `{"relations": [...]}` — every relation in which `ref` is the
  parent (the one-ref `tm child` shape).
- `get_relation(child, parent, *, via=None)` → `{"relations": [...]}`, or a `LedgerError` naming
  both ids when there is none (the two-ref read).
- `set_parent(child, parent, *, via=None, slot=None)` → the write result object above.
- `clear_parent(child, *, via=None)` → the write result object above; the no-op clear returns
  `via: null`.
- `clear_relation(child, parent, *, via=None)` → the two-ref clear result.

The return objects are the same ones the CLI prints with `--json`.

**(k) Out of scope.**

| Edge | Owning command | Reason |
|---|---|---|
| aliquot → box placement | `tm aliquot move` | irreversible physical action, requires `-m`, writes an event row |
| stock → entity binding (`stock.entity_id`) | none yet (separate ticket) | identity binding, not a parent/child edge; re-pointing can invalidate `entity_kinds` and orphan lineage |
| aliquot/box identity columns, scalar (non-ref) fields, box name | `tm <object> edit`, `tm <object> rename` | `rename` keeps the old name as an alias |
| any edge with no existing column | none | it would get a dedicated relation table, never an overload of an unrelated column |

