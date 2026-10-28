---
name: tetramark
description: Use when working with a Tetramark workspace or a lab sample ledger — finding where a tube is, registering a plasmid/strain/prep, adding or moving samples and boxes, consuming part of a pellet, auditing inventory, importing/exporting CSVs. Triggers include 台账, 样品, 盒, 冻存, 库存, 管, 分装.
---

# Tetramark operating manual

Tetramark is a version-controlled inventory ledger: **entity** (the design) → **stock** (one
preparation/batch) → **aliquot** (one physical tube) → **box** (a slotted container). Every write is
one Dolt commit; every read is a normal `tm` command.

## Before you start

1. Find the workspace: `tm server status` (or run inside the directory holding `workspace.yaml`).
   If several ledgers exist, pass `--ledger NAME` **before** the subcommand; otherwise the workspace
   default is used.
2. Inspect before writing: `tm schema show`, `tm box ls`, `tm find QUERY`.
3. Prefer an alias/ID from `find` over a guessed ID.
4. Global flags (`--workspace`, `--ledger`, `--dry-run`, `--no-commit`, `--json`, `--format`) go
   before the command: `tm --ledger lab101 --json entity ls plasmid`.
5. On a server-backed workspace every command uses the server; with no server it falls back to the
   embedded driver. Never run two writers against one ledger at the same time.
6. Presets are files, not part of the tool. `tm init --preset NAME` looks `NAME` up in the
   workspace's `presets_dir` (set with `tm workspace init --presets-dir ...`; see
   `tm config get presets_dir`) and copies it, plus its `include:` closure, into
   `<ledger>/presets/`. Every later command opens only that copy, so to change a ledger's schema you
   edit `<ledger>/presets/*.yaml` (or a local `kinds_dir`/`rules_dir`) — editing a checkout's
   `presets/` does not affect existing ledgers.

## Decision table

| Intent | Command |
|---|---|
| See the effective kinds/fields/rules | `tm schema show [KIND]` |
| Register an abstract thing (design, item, strain) | `tm entity add KIND --name N [--set f=v] [--alias A]` |
| Register a preparation/batch | `tm stock add KIND --entity ID\|ALIAS [--set f=v]` |
| Put its tube in a box | `tm stock add … --box NAME --slot A1` |
| Add more tubes to a stock | `tm aliquot add STOCK --count N [--box NAME --slot S …]` |
| Move one tube | `tm aliquot move ALQ --box NAME --slot S` |
| Use part of a tube | `tm aliquot use ALQ AMOUNT UNIT [--purpose TEXT]` |
| Mark lost / consumed without a quantity | `tm aliquot status ALQ missing\|consumed\|stored\|pending` |
| Hard-delete a tube (rare) | `tm aliquot purge ALQ` |
| Fix a legacy / hand-made stock id | `tm stock renumber REF --id NEW` or `--auto` (old ids stay findable as aliases) |
| See what is in a box | `tm box show NAME` (grid) or `--json` |
| Move a whole box | `tm box move NAME --in PARENT [--slot S]` |
| Find anything | `tm find ID\|ALIAS\|TEXT` |
| What happened to an object | `tm history ID\|ALIAS\|BOX` |
| Validate the ledger | `tm check [-v] [--group G]` |
| Bulk load / dump | `tm import …`, `tm export …` |
| Mirror / back up | `tm remote add … && tm remote push`, `tm backup` |
| Change the schema | `skills/tetramark-schema/SKILL.md` |

Full flags: `references/commands.md`. Multi-step flows: `references/recipes.md`.

## Short recipes

### New plasmid + glycerol stock in a box

```sh
tm entity add plasmid --name pZET035 \
  --set backbone=pZET035 --set resistance=kan --alias p035
tm stock add bacterial --entity p035 \
  --set strain='BL21(DE3)' --set medium=LB \
  --box WFL-12-01 --slot A1 --quantity 500 --unit ul
tm box show WFL-12-01
tm find p035
```

The stock gets an ID from the date-code strategy; the default one aliquot is `<stock_id>.1` and sits
in `A1`. Use `--aliquots 0` when you only want the logical batch.

### Consume part of a pellet

```sh
tm stock add pellet --entity p035 --aliquots 3 \
  --quantity 500 --unit ml \
  --set culture_volume_ml=1500 --set od600=2.4 --set induction=iptg
tm aliquot use 1226KO02.1 500 ml --purpose purification   # ID printed by stock add
```

`use` decrements the quantity and flips the status to `consumed` at zero; the event records the
purpose.

### Move a box, then a tube

```sh
tm box move WFL-12-02 --in shelf-1
tm aliquot move 1226KO01.2 --box WFL-12-03 --slot C3
```

Moving a box moves its whole subtree; moving a tube changes only that aliquot.

### Audit

```sh
tm check            # exit 0 = no FAIL; info failures never change the exit code
tm check -v         # print offending rows for each failure
tm check --group core
```

## Guardrails

- **No raw SQL writes.** Do not `INSERT`/`UPDATE`/`DELETE` through `dolt sql`, Workbench or a driver.
  The CLI supplies IDs, aliquot events, box-slot bookkeeping and the one commit per change; raw SQL
  bypasses all of it and makes `tm check` fail.
- **Never hand-edit `dolt_tests`.** Generated tests are derived state, rewritten by
  `tm schema apply`. To change a rule, edit the schema (see the schema skill).
- **`--dry-run` first for bulk work**, then run it for real:
  `tm --dry-run import all exports/`.
- **Soft-remove by default.** `aliquot status … consumed|missing` keeps history; `purge` is a hard
  delete with no undo (only Dolt history can recover it).
- **One writer at a time.** Do not write through the CLI while another dolt process writes the same
  ledger; stop the server before offline maintenance.
- **Do not move a tube by editing fields.** `aliquot move` records the event and checks slot capacity
  and exclusivity; `stock edit` only changes stock metadata.
- **Check after imports and migrations**, and spot-check `box show` against the old grid.
