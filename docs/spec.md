# Tetramark — Specification (v1)

A general-purpose, version-controlled inventory ledger on [Dolt](https://www.dolthub.com/).
Four core objects — **entity**, **stock**, **aliquot**, **box** — are extended per domain by
declarative YAML **schemas** bundled into **presets**. Pick presets, run `tm init`, and you have a
validated, versioned ledger. Biology is one preset (`molbio`), not the scope.

Origin: generalises the hand-written zsqlab ledgers in `<legacy-zsqlab-checkout>`
(per-DB generators, incremental SQL scripts, `stock_pellets`, `find_entity.sh`). Their proven patterns
(registry + per-kind tables with discriminator FKs, Dolt tests as the pass/fail authority, one Dolt
commit per change) are kept; their pain points (one generator per DB, bash fragility, trigger-only
logic, schema drift by hand) are removed.

Non-goals for v1: GUI, multi-entity/mixture stocks, kind-on-kind inheritance, automatic conversion of
the old zsqlab databases (migration is manual, guided by `docs/migration.md`).

---

## 1. Concepts

| Object | Meaning | ID |
|---|---|---|
| **entity** | The abstract thing (a plasmid design, a protein construct, a book title, a chemical). | UUID v4 |
| **stock** | A logical batch / preparation event of exactly one entity (a glycerol stock, a culture's pellet, a protein prep, a purchase lot). Carries lineage. | Configurable; default date code |
| **aliquot** | A physical unit of a stock (a tube, a vial, a copy). The only thing that occupies a box slot. Carries status and quantity. | `<stock_id>.<n>`, n = 1, 2, … |
| **box** | A container with a geometry. Boxes nest (rack in a freezer shelf). | UUID internally + unique human `name` (e.g. `WFL-08-01`) |

Rules:
- A stock references exactly one entity. Its kind may restrict which entity kinds are allowed.
- A stock may have `parent_stock_id` (derived from) and `source_aliquot_id` (which physical unit was used).
- A stock has 0..N aliquots. Commands that create a stock create one aliquot by default (`--aliquots N`
  to create more, `--aliquots 0` for none), so single-tube users never think about aliquots.
- Stock status is **derived** from its aliquots (view), never stored.
- A slot holds either aliquots (up to the box kind's `slot_capacity`, default 1) or one child box —
  never both. Box nesting must be acyclic.
- "Removing" a tube means status → `consumed` (soft). Hard delete only via explicit `--purge`.

### 1.1 Workspace and ledger

- **Workspace** = one Dolt `data_dir` served by one `dolt sql-server`. Config: `workspace.yaml` in the
  data_dir (host, port, default ledger, author).
- **Ledger** = one Dolt database = one direct subdirectory of the data_dir. Its config `ledger.yaml`
  lives inside the ledger directory and is also stored in the DB (`ledger_meta`) so the DB is self-describing.
  A ledger can be created fresh (`tm init`) or formatted in place from an existing empty database
  (`tm init --adopt`, §3.1), which keeps that database's history.

```yaml
# <data_dir>/<ledger>/ledger.yaml
name: lab101
presets: [base, molbio]          # bundled or path to a local preset dir
kinds_dir: kinds/                # optional local kinds (YAML), same format as presets
author: "Your Name <you@example.com>"   # fallback: git config user.name/email
ids:
  stock: {strategy: datecode, prefix: "12"}   # → 1226JT01
  entity: {strategy: uuid}
statuses: [pending, stored, missing, consumed]  # aliquot status vocabulary (override allowed)
rules_dir: rules/                # optional custom validation rules (YAML)
```

---

## 2. Schema language (YAML)

A **preset** is a single YAML file (`presets/<name>.yaml`) containing kinds, mixins, and rules; a
local preset may instead be a directory, optionally with `kinds/` and `rules/` files. Presets can
`include` other presets. A ledger's effective schema = union of its presets + local kinds; name
collisions are an error.

```yaml
# presets/molbio.yaml
preset: molbio
version: 1
include: [base]
mixins:
  sequence_fields:
    sequence: {type: sequence, unique: true}
    length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
kinds:
  plasmid:
    object: entity
    mixins: [sequence_fields]
    fields:
      name: {type: string, required: true, unique: true}
      backbone: {type: string}
      resistance: {type: enum, values: [amp, kan, cm, spec, tet]}
      construct: {type: ref, to: construct}          # FK → entity_construct
  bacterial:
    object: stock
    entity_kinds: [plasmid, strain]
    fields:
      strain: {type: string, required: true}
      medium: {type: string}
    aliquot_fields:
      glycerol_pct: {type: decimal, default: 15}
  grid81:
    object: box
    geometry: {type: grid, rows: "A-J!I", cols: "1-9"}   # A..J skipping I
rules:
  - id: MB01
    description: every plasmid with a construct has a sequence
    severity: fail
    sql: "SELECT COUNT(*) FROM entity_plasmid WHERE construct IS NOT NULL AND sequence IS NULL"
    expect: {single_value: 0}
```

### 2.1 Field types

| type | SQL | notes |
|---|---|---|
| `string` | `VARCHAR(n)` (default 255, `length:`) | |
| `text` | `TEXT` / `MEDIUMTEXT` | not indexable |
| `int` | `BIGINT` | |
| `decimal` | `DECIMAL(p,s)` (default 18,6) | |
| `bool` | `TINYINT(1)` | |
| `date`, `datetime` | `DATE`, `DATETIME` | |
| `enum` | `VARCHAR` + `CHECK (col IN (...))` | adding a value is additive |
| `sequence` | `MEDIUMTEXT` + generated `<col>_sha256 CHAR(64) STORED` | `unique: true` → UNIQUE on the sha column; values normalised (uppercase, whitespace stripped) by the app |
| `ref` | `VARCHAR(64)` + FK | `to:` a kind name (FK → that kind table) or a core object (`entity`/`stock`/`aliquot`/`box`) |
| `computed` | generated column `STORED` | `expr:` SQL over same-row columns, `sql_type:` required |

Modifiers: `required` (NOT NULL), `unique`, `default`, `pattern` (regex, enforced by app and by a
generated Dolt test — Dolt CHECK has no REGEXP guarantee), `description`, `length`.

### 2.2 Box geometry

| type | spec | slots |
|---|---|---|
| `grid` | `rows`, `cols` as ranges (`"A-H"`, `"1-12"`, `"A-J!I"` = exclude I) or explicit lists; `label: "{row}{col}"` default | materialised row×col |
| `named` | `slots: [top, middle, bottom]` | materialised list |
| `slotless` | optional `capacity: N` (max aliquots + child boxes) | none; slot is NULL |

Optional on box kind: `slot_capacity` (default 1).

### 2.3 Rules

Custom rule = `id`, `description`, `severity: fail|info`, `sql` (read-only SELECT), `expect`:
`{single_value: X}` | `{rows: 0}` | `{rows_at_most: N}`. Compiled into `dolt_tests` (§4).

---

## 3. Physical model (generated DDL)

Core tables (always present, created by `tm init`):

```
ledger_meta(key PK, value)                       -- schema hash, preset versions, ledger.yaml copy, spec version
entity(id PK, kind, name, created_at, notes, UNIQUE(id,kind))
stock(id PK, kind, entity_id FK→entity, parent_stock_id FK→stock, source_aliquot_id FK→aliquot,
      created_at, notes, UNIQUE(id,kind))
box(id PK, name UNIQUE, kind, parent_box_id FK→box, parent_slot, created_at, notes, UNIQUE(id,kind))
slot(box_id FK→box, label, row_label, col_label, ordinal, PK(box_id,label))
aliquot(id PK, stock_id FK→stock, seq, box_id FK→box, slot, status CHECK IN vocab,
        quantity DECIMAL, unit, label, created_at, notes, UNIQUE(stock_id,seq),
        FK(box_id,slot)→slot)
aliquot_event(id AUTO PK, aliquot_id FK, event ENUM(create,use,move,status,edit), amount, unit,
              from_box_id, from_slot, to_box_id, to_slot, old_status, new_status,
              actor, purpose, note, at)
alias(object_type, object_id, alias, PK(object_type, alias))
```

`ledger_meta` also carries the version stamps:

- `spec_version` is the **ledger format version** — the key an upgrade pipeline
  dispatches on. It is bumped only when the generated shape changes: the core
  table set, the `ledger_meta` key set, or generated objects (views,
  `dolt_tests`). A pure bug-fix release does not change it. Version 2 adds the
  `alias_list`/`occupant_alias_list` columns to the generated views; a v1
  ledger upgrades (`tm schema plan` → `tm schema apply`) with an additive view
  replacement plus the usual `ledger_meta` refresh, never a destructive step.
- `tetramark_version` records which tool version last wrote the metadata
  (`tm init` and every `tm schema apply` refresh it).
- `created_with` records the tool version that first formatted the ledger
  (`tm init`/`tm init --adopt`); it never changes afterwards.
- `adopted_from_commit` is written only by `tm init --adopt`: the HEAD hash of
  the adopted database before formatting.

`tetramark_version`, `created_with` and `adopted_from_commit` are provenance
records: they are shown by `tm schema show` but drive no migration logic.

### 3.1 Adopting an existing database (`tm init --adopt`)

`tm init NAME --adopt --preset …` formats a Dolt database that already exists in the workspace:
it performs the same steps as `tm init` (write `ledger.yaml`, create the core and kind tables, write
`ledger_meta`, install the generated `dolt_tests`) but skips `mkdir`/`dolt init` and commits
`adopt(ledger): NAME` on top of the existing history instead of starting a new root commit.
`ledger_meta.adopted_from_commit` records the branch HEAD from *before* that commit; the rest of the
history is untouched, so old commits stay reachable (e.g. `AS OF <old tag>`).

Preconditions — any failure refuses the adopt and writes nothing:

- the selected branch's working set is clean (`dolt_status` has no rows);
- that branch has **no user tables** (no `information_schema.tables` BASE TABLE outside `dolt_*`),
  **no views**, and **no `dolt_tests` rows**;
- the ledger directory has no `ledger.yaml`, or `--force-config` was given (it is overwritten, never
  deleted).

Adopt runs on the branch selected by `-b/--branch` (default: the checked-out branch), so a migration
can format a branch while `main` keeps the old schema. A branch that does not exist is an error — it is
never created. Adopt never drops an existing object: dropping the legacy schema is a separate,
auditable commit made before the adopt.

A failed adopt rolls back only its own writes: it drops the tables/views it had created (Dolt's
`DOLT_RESET --hard` leaves untracked tables behind, so a plain reset is not enough), resets the branch
to the pre-adopt HEAD, restores or removes `ledger.yaml`, and leaves the working set clean. It must
**never** remove the database directory: unlike a failed `tm init`, which deletes its own half-created
directory, adopt operates on a real database and answering a failure with `rmtree` would destroy it.

`tm --dry-run init NAME --adopt` runs the read-only preconditions (including the legacy-schema refusal) and
prints the branch, the HEAD that would be recorded, and the DDL; nothing is written.

Kind tables (generated from schema):
- `entity_<kind>(id PK, kind CHAR const DEFAULT '<kind>' CHECK(kind='<kind>'), <fields…>,
  FK(id,kind)→entity(id,kind))` — the discriminator FK: an id can belong to only one kind table.
- `stock_<kind>(…)` and `box_<kind>(…)` likewise.
- `aliquot_<stockkind>(id PK FK→aliquot, <aliquot_fields…>)` when the stock kind has `aliquot_fields`.

Views (generated):
- `v_box_slots` — one row per slot (empties included): box name, slot, occupant type/id, stock, entity, status,
  and `occupant_alias_list` (the occupant's aliases: the aliquot's aliases when the occupant is an
  aliquot, the box's aliases when it is a nested box, NULL for an empty slot).
- `v_aliquot_location` — aliquot with full box path (`Freezer-1/Shelf-2/WFL-08-01:A3`) and `alias_list`.
- `v_stock_status` — stock + derived status (`active` if any aliquot stored; `pending` if any pending
  and none stored; `depleted` if all consumed; `missing` if all missing/consumed with ≥1 missing; `empty` if 0 aliquots) + aliquot counts + total quantity per unit + `alias_list`.
- `v_<object>_<kind>` — kind table joined with its core row (convenience for humans/Workbench); the final
  column is `alias_list`.

Every view that represents an object aggregates the `alias` table into a sorted, `;`-separated
`alias_list` (NULL when the object has no aliases; `v_box_slots` names it `occupant_alias_list`
because the row describes a slot and its occupant, not only the box). The column is deliberately
**not** called `aliases`: a kind may already have a domain field named `aliases` — the bundled
`construct` kind does, holding semicolon-separated alternative construct names — and a view cannot
expose two columns with the same name. After a `tm stock renumber` the old stock and aliquot ids are
aliases (§6), so these columns are how a view reader still sees the pre-renumber handles.

What the DB enforces vs the app:
- DB: PK/UNIQUE/FK/CHECK/NOT NULL, discriminator FKs, slot existence (`FK(box_id,slot)→slot`), enum values.
- App + Dolt tests (cannot be expressed as DB constraints): slot capacity & aliquot-vs-child-box exclusivity,
  nesting acyclicity, stock→entity kind restriction, `pattern`, status/slot consistency
  (`stored` in a slotted box ⇒ slot NOT NULL), slotless box capacity.

No triggers (Dolt 2.3.x triggers are single-statement only; all logic lives in the app).

---

## 4. Validation

`tm check` runs Dolt's `dolt_tests` (same source as Workbench's Tests page). Exit 0 = all PASS,
1 = any FAIL. `-v` prints offending rows (each generated test has a companion detail query).

Tests are generated into `dolt_tests` on `init` / `init --adopt` / `schema apply`, grouped:
- `core` — the app-enforced invariants in §3.
- `schema:<kind>` — patterns, required refs, entity_kinds restriction.
- `rules:<preset|local>` — custom rules. `severity: info` tests are reported but never fail the exit code
  (stored with a group suffix `:info`).

Generated tests are fully replaced on each apply (they are derived state); never hand-edit them.

---

## 5. Schema evolution

`tm schema plan` diffs the effective YAML schema against the live DB (introspection via
`information_schema` + `ledger_meta`) and prints an ordered SQL plan, each step tagged
`additive` or `destructive`. `tm schema apply` executes it in one transaction + one Dolt commit.

- Additive (applied freely): new kind, new nullable/defaulted field, new enum value, new view/rule/test,
  new aliquot_fields table, relaxing NOT NULL.
- Destructive (refused without `--allow-destructive`): drop/rename kind or field, type change, tightening
  (new NOT NULL without default, new UNIQUE, removing enum value). Renames are expressed in YAML as
  `renamed_from: old_name` so they become `ALTER … RENAME` instead of drop+add.
- Generated columns and FKs that depend on a changed column are dropped and rebuilt automatically
  (lesson from zsqlab10's slot-column relaxation).
- After apply, the plan must be empty (idempotency is a test).

---

## 6. CLI (`tm`)

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

`tm stock renumber` is the audited way to repair ids that do not follow the ledger's stock id strategy
(legacy imports, hand-made ids). It renumbers one stock (`REF --id NEW`), many (`REF ... --auto`), or a
wholesale batch (`--map PATH`, one `OLD<TAB>NEW` per line, `OLD` alone when combined with `--auto`).
New ids are validated against `ids.stock` and against the alias namespace; the old stock id and every
old aliquot id are kept as aliases, so `tm find OLD` and `tm find OLD_ALIQUOT` keep working and no
handle is lost. Everything — `stock.id`, `aliquot.id`/`stock_id`, `aliquot_event.aliquot_id`, kind
tables, aliquot extension tables, `ref` fields, `parent_stock_id`, `source_aliquot_id` and existing
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

---

## 7. Driver layer

`Driver` interface: `query(sql, params) -> list[dict]`, `execute(sql, params)`, `transaction()` context,
`commit(message, author)`, `database` property. Two implementations:
- `ServerDriver` — PyMySQL to `dolt sql-server` (`127.0.0.1`, no TLS). Default when the workspace server is up.
- `EmbeddedDriver` — `dolt sql -r json -q` subprocess in the ledger dir. Used when no server runs and in tests.
  Must detect errors from output, not only exit codes (dolt can exit 0 on error).

Optional branch selection (`ServerDriver(..., branch=...)`, `EmbeddedDriver(..., branch=...)`) uses Dolt's
**revision database** `` `db/branch` ``; it is a session-scoped checkout and never moves the branch the
repository has checked out (so parallel sessions and the server keep seeing the original branch):

- Server: after `USE \`db\``, the driver verifies the branch in `dolt_branches`, then executes
  `USE \`db/branch\`` on its own connection. Reads, writes, transactions and `DOLT_COMMIT` all target that
  revision database, so commits land on the branch tip.
- Embedded: `dolt sql --help` on the installed Dolt 2.3.5 has **no** `--branch`-style flag (verified; open
  question Q-03), and `CALL DOLT_CHECKOUT` is forbidden because it moves the repository's working set for
  every process. Each `dolt sql` invocation is its own process, i.e. its own session, so the driver prefixes
  every statement batch with `USE \`db/branch\`;`. That is the same session-scoped mechanism as the server,
  and the driver verifies the branch exists first.

A missing branch raises a usage error in the driver constructor; it is never created implicitly. The
checked-out branch of the repository is never changed by either driver.

`tm server start|stop|status` ports `dolt_start.sh`/`dolt_stop.sh`: pid/log in data_dir, readiness
probe, port-busy detection, lists ledgers (subdirs with `.dolt`).

Target Dolt ≥ 2.3.5. All SQL must work on 2.3.5 (no multi-statement trigger bodies, no REGEXP in CHECK).

---

## 8. Presets shipped in v1

- `base` — box kinds `grid9x9` (A–J!I × 1–9), `grid96` (A–H × 1–12), `rack` (named), `shelf`/`freezer`/`drawer`
  (slotless); entity kind `item` (name, category, description); stock kind `lot` (supplier, catalog_no,
  received_on, expires_on).
- `molbio` (includes `base`) — derived from zsqlab08/10: entities `construct` (protein: sequence,
  molecular_weight_da, extinction coefficients as computed), `plasmid` (sequence, backbone, resistance,
  ref construct), `strain` (genotype, species); stocks `bacterial` (entity_kinds plasmid|strain;
  strain, medium; aliquot glycerol_pct), `plasmid_prep` (concentration_ng_ul, prep_method), `protein_prep`
  (concentration_mg_ml, buffer, purification_id), `pellet` (culture volume, OD600, induction; aliquots carry
  volume via core quantity).
- `library` (includes `base`) — entity `book` (title, authors, isbn pattern, year); stock `copy`
  (acquired_on, condition enum). Exists to prove generality; used in tests.

---

## 9. Repository layout

```
pyproject.toml            # uv; deps: typer, pyyaml, pydantic>=2, pymysql, rich
src/tetramark/
  driver/                 # base.py, server.py, embedded.py, workspace.py (server start/stop)
  schema/                 # model.py (pydantic), loader.py (presets/include/mixins), ddl.py, diff.py, tests_gen.py
  core/                   # ids.py, ledger.py (open/init), objects.py (entity/stock/aliquot/box ops), events.py
  ops/                    # find.py, render.py (grid), io_csv.py, remote.py
  cli/                    # app.py (Typer root) + one module per command group
  presets/                # base.yaml, molbio.yaml, library.yaml (package data)
skills/tetramark/SKILL.md (+ references/)
skills/tetramark-schema/SKILL.md (+ references/)
docs/spec.md, docs/migration.md, docs/tickets/
tests/                    # pytest; fixtures create real dolt repos in tmp_path
```

Testing: pytest against real `dolt` (no mocks of Dolt). Fixture `ledger_factory(presets=[...])` makes a
temp workspace + ledger via EmbeddedDriver; a session-scoped optional fixture runs a sql-server on a
random free port for ServerDriver tests. `uv run pytest` must pass after every ticket.

## 10. Skills & docs

- `skills/tetramark` — operating manual: concepts, find/query recipes, add/move/use/status flows, check,
  history, what not to do (raw SQL writes, hand-editing dolt_tests). Chinese trigger words in description
  (台账, 样品, 盒, 冻存, 库存, 管).
- `skills/tetramark-schema` — writing presets/kinds/rules, schema plan/apply, destructive changes.
- `docs/migration.md` — rulebook for moving an old zsqlab DB: map old tables → kinds, keep old UIDs as
  explicit IDs (or aliases when they don't fit), `box/row/col` → box name + slot label, `status` 三态 →
  status vocab, pellet aliquots → aliquots with quantity, export old tables to CSV → `tm import`,
  checklist (row counts per kind, `tm check` all PASS, spot-check `box show` vs old grid views).
