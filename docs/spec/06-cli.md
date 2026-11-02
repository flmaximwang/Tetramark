# 6. CLI (`tm`)

Global: `--workspace PATH` (default: search upward for `workspace.yaml`, else `$TETRAMARK_WORKSPACE`),
`--ledger NAME` (default from workspace), `-b/--branch BRANCH` (session branch, see below),
`--format table|json|csv` on reads, `--json` shorthand,
`--dry-run` (print SQL, no write) and `--no-commit` on writes. Exit codes: 0 ok (help included),
1 business/validation error, 2 usage error, 3 database unreachable.

Help: `-h` and `--help` print the same text on every command and every command group, and exit 0; a
bare `tm`, or a bare group such as `tm box`, prints its help and also exits 0. Asking for help is not
a usage error, so the exit code is 0 even where the command would otherwise need arguments. Misuse —
an unknown option, an unknown command, a missing argument — still exits 2.

Branch selection (`-b/--branch`, placed before the subcommand like `-w`/`-l`): every read, write and
commit of that command happens on BRANCH. The default is `branch:` in `ledger.yaml`, else the branch
checked out in the repository. A branch that does not exist is a usage error — it is never created
implicitly. Selection is session-scoped and never moves the branch other sessions or the server see
(see §7). `--dry-run` prints the target branch with the SQL. It also selects the branch that
`tm init --adopt` formats (§3.1). `tm remote push/pull -b` is a different
option with the same letter: it names the branch on the **remote**, while the global `-b` selects the
local session branch. `tm branch ls|create NAME [--from REF]|delete NAME` manage branches; merging is
not part of `tm` (cut over with Dolt's native `CALL DOLT_MERGE(branch, '--ff-only')`).

Every successful write = exactly one Dolt commit. Message is ASCII: `<verb>(<object>): <summary>`;
non-ASCII user text is kept in the event/notes, never in the commit message (known Dolt byte-truncation bug).
Author from `ledger.yaml` → `workspace.yaml` → git config.

The root help groups the commands into five panels — Setup, Inventory, Lookup, Data and Versioning —
in workflow order; the listing below follows the same order.

```
# Setup: workspace, server, presets, init, schema, config
tm workspace init [PATH] [--port 13308]
tm server start [-d] | status | stop
tm presets [list|show NAME]
tm init NAME --preset base --preset molbio [--id-prefix 12]
tm init NAME --adopt --preset molbio [--id-prefix 12] [--force-config]   # §3.1
tm schema show [KIND] | plan | apply [--allow-destructive]
tm config get KEY ; tm config set KEY VALUE   # KEY: author | presets_dir

# Inventory: entity, stock, aliquot, box
tm entity add KIND --set field=value ... [--name N] [--alias A]
tm entity show ID|ALIAS ; tm entity ls [KIND] [--where f=v] ; tm entity edit ID --set f=v
tm stock add KIND --entity ID|ALIAS --set f=v [--parent STOCK] [--source-aliquot ALQ]
                 [--aliquots N] [--status S] [--box NAME --slot A1 [--slot A2 ...]]
                 [--quantity 500 --unit ml]
tm stock show|ls|edit ...
tm stock renumber [--id NEW | --auto | --map PATH] [--dry-run] [--json] [REF ...]
                 # legacy id → strategy-shaped id(s); old ids stay as aliases; one commit
tm aliquot add STOCK [--count N] [--status S] [--box NAME --slot S ...] [--quantity Q --unit U]
tm aliquot move ALQ --box NAME [--slot S] [--status S]
tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT]      # decrements quantity; reaching 0 → consumed
tm aliquot status ALQ NEW [--force --note TEXT]   # NEW: a status of the stock kind's set
tm aliquot purge ALQ                                  # hard delete, explicit only
tm box add KIND NAME [--in PARENT [--slot S]]   # PARENT: a named or slotless box, never a grid
tm box show NAME            # terminal grid / list; --json gives {box, geometry, path, aliases, slots:[...]}
tm box ls ; tm box move NAME --in PARENT [--slot S]   # never into a grid box

# Lookup: find, alias, history, lineage
tm find QUERY               # id → alias → name/text match across all objects and kinds
tm alias add OBJ_TYPE ID ALIAS ; tm alias rm ...
tm history ID               # aliquot_event + dolt_log entries touching the object
tm lineage ID [--up | --down] [--depth N] [--json] [--format text|dot]   # descent graph (see below)

# Data: import, export, check
tm import KIND FILE.csv [--object entity|stock|aliquot|box]   # all-or-nothing, one commit
                                # a 'status' column is a creation status (any set status)
tm export KIND|all [--out DIR]
tm check [-v] [--group G]

# Versioning: branch, remote, backup
tm branch ls | create NAME [--from REF] | delete NAME
tm remote add NAME URL | push | pull
tm backup [DIR]
```

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
box import refuse such a name; there is no rename command yet. **Grid boxes hold tubes only**, so
`tm box add … --in GRIDBOX` and `tm box move … --in GRIDBOX` are refused; a box nests only in a
named or slotless box — for example a freezer, `tm box move WFL-12-02 --in FR-1` — and a box never
nests in a grid box.

`tm export` reads a box from `box` joined with `box_<kind>` — the per-kind `v_box_<kind>` views are
gone (spec 5). `tm import` creates each new box's `v_box_<name>` view and applies the name and
grid rules above, in the same commit as the rows.

`tm entity show`, `tm stock show` and `tm box show` include the object's aliases as a sorted
`aliases` list in JSON output and print an `aliases` row or line in table output; `ls` output stays
narrow and unchanged. The generated views expose the same data as `alias_list` /
`occupant_alias_list` (§3).

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

Python API mirrors the CLI (`tetramark.open(workspace, ledger, branch=None)` → `Ledger` with
`.entities`, `.stocks`, `.aliquots`, `.boxes`, `.find()`, `.check()`); the CLI is a thin layer over it.

### 6.1 ID strategies

- `uuid` — uuid4 string.
- `datecode` — `<prefix><YY><M><D><NN>`: M = month marker `A–M` skipping I; D = day `1–9` then `A–W`
  skipping I (port of `~/.local/bin/date_code.py`); NN = **smallest unused** 2-digit sequence for that
  day prefix (configurable `reuse: false` → max+1). Example: prefix `08`, 2026-06-24 → `0826FP01`.
- `sequence` — `<prefix><NNNNN>` monotonic.
- Explicit IDs are always accepted (back-filling / migration) if unique and well-formed (pattern from the strategy).
- Aliquot IDs: `<stock_id>.<seq>`, seq = max+1 within the stock.
