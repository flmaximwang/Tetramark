# 6. CLI (`tm`)

Global: `--workspace PATH` (default: search upward for `workspace.yaml`, else `$TETRAMARK_WORKSPACE`),
`--ledger NAME` (default from workspace), `-b/--branch BRANCH` (session branch, see below),
`--format table|json|csv` on reads, `--json` shorthand,
`--dry-run` (print SQL, no write) and `--no-commit` on writes. Exit codes: 0 ok, 1 business/validation
error, 2 usage error, 3 database unreachable.

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

```
tm workspace init [PATH] [--port 13308]
tm server start [-d] | stop | status
tm init NAME --preset base --preset molbio [--id-prefix 12]
tm init NAME --adopt --preset molbio [--id-prefix 12] [--force-config]   # §3.1
tm presets [list|show NAME]
tm schema show [KIND] | plan | apply [--allow-destructive]

tm entity add KIND --set field=value ... [--name N] [--alias A]
tm entity show ID|ALIAS ; tm entity edit ID --set f=v ; tm entity ls [KIND] [--where f=v]
tm stock add KIND --entity ID|ALIAS --set f=v [--parent STOCK] [--source-aliquot ALQ]
                 [--aliquots N] [--box NAME --slot A1 [--slot A2 ...]] [--quantity 500 --unit ml]
tm stock show|edit|ls ...
tm stock renumber [--id NEW | --auto | --map PATH] [--dry-run] [--json] [REF ...]
                 # legacy id → strategy-shaped id(s); old ids stay as aliases; one commit
tm aliquot add STOCK [--count N] [--box NAME --slot S ...] [--quantity Q --unit U]
tm aliquot move ALQ --box NAME [--slot S]
tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT]      # decrements quantity; reaching 0 → consumed
tm aliquot status ALQ stored|missing|consumed|pending
tm aliquot purge ALQ                                  # hard delete, explicit only
tm box add KIND NAME [--in PARENT --slot S]
tm box show NAME            # terminal grid / list; --json gives {box, geometry, path, aliases, slots:[...]}
tm box ls ; tm box move NAME --in PARENT --slot S
tm alias add OBJ_TYPE ID ALIAS ; tm alias rm ...
tm find QUERY               # id → alias → name/text match across all objects and kinds
tm history ID               # aliquot_event + dolt_log entries touching the object
tm lineage ID [--up | --down] [--depth N] [--json] [--format text|dot]   # descent graph (see below)
tm check [-v] [--group G]
tm import KIND FILE.csv [--object entity|stock|aliquot|box]   # all-or-nothing, one commit
tm export KIND|all [--out DIR]
tm remote add NAME URL | push | pull ; tm backup [DIR]
tm branch ls | create NAME [--from REF] | delete NAME
```

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
