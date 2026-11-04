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
in workflow order. `Setup` is `presets`, `init`, `adopt`, `schema`, `config`, `status`; there is no
`workspace` and no `server` command group in 0.5.0. The listing below follows the same order.

```
# Setup: presets, init, adopt, schema, config, status
tm presets [list|show NAME]
tm init PATH --preset base [--preset molbio] [--name N] [--id-prefix 12] [--author ...]
tm adopt PATH --preset molbio [--name N] [--id-prefix 12] [--author ...] [--force-config]  # §3.1
tm schema show [KIND] | plan | apply [--allow-destructive]
tm config get [author] ; tm config set author "Name <email>"   # the only setting
tm status [--json]                        # one ledger at a glance (6.1)

# Inventory: entity, stock, aliquot, box
tm entity add KIND --set field=value ... [--name N] [--alias A]
tm entity show ID|ALIAS ; tm entity list [KIND] [--where f=v] ; tm entity edit ID --set f=v
tm entity rename ID|ALIAS NEW_NAME          # old name kept as an alias; one commit
tm stock add KIND --entity ID|ALIAS --set f=v [--parent STOCK] [--source-aliquot ALQ]
                 [--aliquots N] [--status S] [--box NAME --slot A1 [--slot A2 ...]]
                 [--quantity 500 --unit ml]
tm stock show|list|edit ...
tm stock renumber [--id NEW | --auto | --map PATH] [--json] [REF ...]
                 # legacy id → strategy-shaped id(s); old ids stay as aliases; one commit
tm aliquot add STOCK [--count N] [--status S] [--box NAME --slot S ...] [--quantity Q --unit U]
tm aliquot show ALQ                          # one tube: status, quantity, stock, box path
tm aliquot move ALQ --box NAME [--slot S] [--status S]
tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT]      # decrements quantity; reaching 0 → consumed
tm aliquot status ALQ NEW [--force --note TEXT]   # NEW: a status of the stock kind's set
tm aliquot purge ALQ                                  # hard delete, explicit only
tm box add KIND NAME [--in PARENT [--slot S]]   # PARENT: a named or slotless box, never a grid
tm box show NAME            # terminal grid / list; --json gives {box, geometry, path, aliases, slots:[...]}
tm box list ; tm box move NAME --in PARENT [--slot S]   # never into a grid box
tm box rename NAME NEW_NAME   # old name kept as an alias; v_box_<old> → v_box_<new>; one commit
tm entity|stock|aliquot|box add-form      # interactive form for people (§6.3)

# Lookup: find, alias, history, lineage
tm find QUERY               # id → alias → name/text match across all objects and kinds
tm alias add OBJ_TYPE ID ALIAS ; tm alias remove ...
tm history ID               # aliquot_event + dolt_log entries touching the object
tm lineage ID [--up | --down] [--depth N] [--json] [--format text|dot]   # descent graph (see below)

# Data: import, export, check
tm import KIND FILE.csv [--object entity|stock|aliquot|box]   # all-or-nothing, one commit
                                # a 'status' column is a creation status (any set status)
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

Creating aliquots accepts any status of the stock kind's set (`--status`, or the import `status`
column); changing one follows the set's `to:` rules. `--force` requires `--note`, records
`aliquot_event.forced = TRUE` and is the only way to make an unforced-illegal change; without
`--force` the CLI refuses and names the allowed next status(es).

An aliquot's **position follows its status** (spec 5): a status with `in_stock: true` needs a
position (a box, plus a slot when that box is slotted), and changing to any other status clears
`box_id`/`slot` in the same write. A `status`/`use` change that leaves stock records the old
position in the event's `from_box_id`/`from_slot` (with `to_*` NULL), so `tm history` still shows
where the tube was; changing to an in-stock status without a position is refused with a hint to run
`tm aliquot move ALQ --box NAME --slot S`. `tm aliquot move` of a not-in-stock aliquot also sets the
single in-stock status of its stock kind's set; when the set has more than one in-stock status it
refuses and lists them unless `--status` names one. If the aliquot is already in stock, a move with
`--status` runs the normal transition check and the result must stay in stock.

A box name must be **view-safe**, because each existing box gets a generated view (§3):
`v_box_<name>` must not equal a core view (for example `slots`), the name is at most 58 characters
(64 − `v_box_`), and it must not differ only in case from another box's name. `tm box add` and CSV
box import refuse such a name, and so does `tm box rename` (which may change only the case of the
box's own name). **Grid boxes hold tubes only**, so
`tm box add … --in GRIDBOX` and `tm box move … --in GRIDBOX` are refused; a box nests only in a
named or slotless box — for example a freezer, `tm box move WFL-12-02 --in FR-1` — and a box never
nests in a grid box.

`tm export` reads a box from `box` joined with `box_<kind>` — the per-kind `v_box_<kind>` views are
gone (spec 5). `tm import` creates each new box's `v_box_<name>` view and applies the name and
grid rules above, in the same commit as the rows.

`tm entity show`, `tm stock show`, `tm aliquot show` and `tm box show` include the object's aliases as
a sorted `aliases` list in JSON output and print an `aliases` row or line in table output; `list`
output stays narrow and unchanged. The generated views expose the same data as `alias_list` /
`occupant_alias_list` (§3).

`tm aliquot show ALQ` shows one tube. `ALQ` is the full aliquot id (`<stock>.<seq>` — never a
shortened prefix) or one of the tube's aliases (including the old ids `tm stock renumber` keeps).
The record holds the aliquot row (`id`, `stock_id`, `seq`, `status`, `quantity`, `unit`, `label`,
`notes`, `created_at`), the sorted `aliases`, the owning stock's `kind` and `entity_id`, the
tube's `box_id` and `slot`, the `path` read from `v_aliquot_location` (§3,
`Freezer-1/Shelf-2/Box:A3`) and the derived `in_stock` flag (the status has `in_stock: true` in
its stock kind's status set, §2.4). A tube that is not in stock has `box_id`, `slot` and `path`
NULL. The stock kind's own fields stay in `tm stock show`, and the tube's events stay in
`tm history ALQ`: `show` carries no event timeline. The command is read-only and changes no
ledger format — `spec_version` is untouched.

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

List tables abbreviate uuid ids the way git abbreviates hashes: `tm entity list` and `tm stock list`
print the shortest **unique** prefix of at least 8 characters (ids that share the first 8 grow
together until they differ). `tm stock list` shortens both its `id` and its `entity_id` column when
that value is a uuid id, so a ledger with a `uuid` stock strategy gets short stock ids too.
`tm <object> show`, JSON and CSV always print the full 36-character id, so it remains the
cross-command identifier, and datecode/sequence ids (`0826FP01`, `S00042`) and aliquot ids
(`0826FP01.1`) are never shortened.

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
when it is required. **Computed** fields are never asked; **sequence** fields are.

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
