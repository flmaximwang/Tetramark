---
name: tetramark
description: Use when working with a Tetramark ledger or a lab sample ledger — finding where a tube is, registering a plasmid/strain/prep, adding or moving samples and boxes, consuming part of a pellet, auditing inventory, importing/exporting CSVs. Triggers include 台账, 样品, 盒, 冻存, 库存, 管, 分装.
version: "0.8.36"
spec: "06-cli"
updated: 2026-11-05
---

> Tetramark 0.8.36 skill — if `tm --version` differs, use the copy at `tm skill path`.

# Tetramark operating manual

Tetramark is a version-controlled inventory ledger: **entity** (the design) → **stock** (one
preparation/batch) → **aliquot** (one physical tube) → **box** (a slotted container). Every write is
one Dolt commit; every read is a normal `tm` command.

## Before you start

1. Find the ledger: `tm status` inside the ledger directory (the one holding `.dolt/` and `ledger.yaml`).
   Elsewhere pass `-l/--ledger PATH` **before** the subcommand; no workspace layer and no default ledger when several exist.
2. Inspect before writing: `tm schema show`, `tm box list`, `tm find QUERY`.
3. Prefer an alias/ID from `find` over a guessed ID.
4. Global flags (`-l/--ledger PATH`, `-b/--branch`, `--dry-run`, `--no-commit`, `--json`,
   `--format`) go before the command: `tm -l ~/lab/lab101 --json entity list plasmid`.
5. Every command uses the embedded driver (one `dolt sql` process per statement); there is no server
   to start or stop. Writes are serialized by a per-ledger lock (`$TETRAMARK_LOCK_TIMEOUT` changes the
   ~10 s wait); reads never lock, but never run two writers on one ledger.
6. Presets ship with tetramark (wheel `tetramark/presets/`) and live in the repo's `presets/`; a bare
   `tm init --preset NAME` resolves through `$TETRAMARK_PRESETS_DIR` (when set), then the bundled
   copies, and copies the preset plus its `include:` closure into `<ledger>/presets/`. Every later
   command opens only that copy; to change a ledger's schema edit `<ledger>/presets/*.yaml` (or a
   local `kinds_dir`/`rules_dir`, which must stay relative inside the ledger) — editing a checkout's
   `presets/` does not affect existing ledgers. The bundles assemble parts: `molbio` = `base`,
   `nucleic_acids`, `bacteria`, `proteins`, `cell_culture`; `cellbio` = `base`, `nucleic_acids`,
   `cell_culture`, `immuno`, `viral_vectors`; `angenet` = `base`, `nucleic_acids`, `biosamples` and
   the four organism parts. One ledger can list any combination and the shared parts load once.
7. A new ledger is **spec 7** (`base` version 6) and no shipped preset declares statuses. An older
   ledger still opens with its own preset copies and `tm` warns `ledger is spec N, tm is spec N+1:
   follow migration/spec/N-to-N+1/README.md`; spec 5 → 6 is a manual README step, 6 → 7 scripted.

## Decision table

| Intent | Command |
|---|---|
| See the effective kinds/fields/rules | `tm schema show [KIND]` |
| Fill in a form at the terminal (a person) | `tm entity\|stock\|aliquot\|box add-form` |
| Register an abstract thing (design, item, strain) | `tm entity add KIND --name N [--set f=v] [--alias A]` |
| Register a preparation/batch | `tm stock add KIND --entity ID\|ALIAS [--set f=v]` |
| Put its tube in a box | `tm stock add … --box NAME --slot A1` |
| Take a sample from a tube (lineage edge) | `tm stock add cell_biosample --entity LINE --source-aliquot ALQ` (cellbio; `animal_biosample` in angenet) |
| Add more tubes to a stock | `tm aliquot add STOCK --count N [--box NAME --slot S …]`; `tm stock aliquot STOCK AMOUNT UNIT [--count N]` when the per-tube amount is known |
| See one tube and its events, or list the usable ones | `tm aliquot show ALQ [--json]`; `tm aliquot list [STOCK] [--box NAME] [--all]` (in stock by default) |
| Move one tube | `tm aliquot move ALQ --box NAME [--slot S] [--status S] -m MSG` (also restores a not-in-stock tube) |
| Use part of a tube | `tm aliquot use ALQ [AMOUNT UNIT] -m MSG` (omit amount/unit = use everything left) |
| Mark lost / consumed / planned, or record a detailed life state | `tm aliquot status ALQ pending\|stored\|missing\|consumed` (fixed transitions); `tm stock edit STOCK --set fate=euthanized` (mouse/fish), `fate=discarded` (fly), `--set condition=starved` (worm), or an event `--note` |
| Override a transition rule | `tm aliquot status ALQ NEW --force --note "why"` (records a forced event; a wrongly consumed tube goes back in with `tm aliquot move … -m MSG`) |
| Hard-delete an object added by mistake (rare) | `tm entity\|stock\|aliquot\|box purge REF` (refused while referenced; a box must be empty) |
| Fix a legacy / hand-made stock id | `tm stock renumber REF --id NEW` or `--auto` (old ids stay findable as aliases) |
| See what is in a box, or move the box itself | `tm box show NAME` (grid) or `--json`; `tm box move NAME --in PARENT [--slot S]` |
| Rename an entity or box, or edit an entity, stock or box | `tm entity\|box rename REF NEW_NAME` (old name stays findable as an alias); `tm entity\|stock\|box edit REF --set f=v` (notes + kind fields; a name is rename-only — `--set name=…` is refused) |
| Find anywhere / filter by column | `tm find -E '^pZET'` (ICU regex: ids, aliases, names, kind text) or `tm entity list --where 'name~(?i)^pzet'` / `tm find --where 'resistance!=amp'` (`=` exact, `!=` inequality, `~`/`!~` case-sensitive regex `(?i)`, ordered comparisons; `field=` empty, `field!=` filled in; `--where-any` is one OR group, e.g. `tm find --where-any 'resistance=kan' --where-any 'resistance=amp'`) |
| What happened to an object | `tm history ID\|ALIAS\|BOX` |
| Register an organism line and its colony, or record a cross / pedigree link | `tm entity add mouse_strain --name N` then `tm stock add mouse_cohort --entity N --box CAGE --quantity 1 --unit mouse`; `tm stock add fly_culture … --set dam=ALQ --set sire=ALQ` (refs to aliquots, `lineage: true`) |
| Where material came from / what derives from it | `tm lineage ID [--up \| --down] [--depth N]` |
| Validate the ledger | `tm check [-v] [--group G]` |
| Bulk load / dump | `tm import …`, `tm export …` |
| Mirror / back up | `tm remote add … && tm remote push`, `tm backup DIR` (or a configured backup) |
| Change the schema | `skills/tetramark-schema/SKILL.md` |

The **form** (`tm <obj> add-form`) is for people at a terminal; **agents use `add`** — no flags, refuses without a TTY.

Full flags, filters and `tm find -E`: `references/commands.md`. Multi-step flows: `references/recipes.md`.

## Short recipes

```sh
tm entity add plasmid --name pZET035 --set backbone=pZET035 --set resistance=kan --alias p035
tm stock add bacterial_glycerol --entity p035 --set strain='BL21(DE3)' --set medium=LB \
  --box WFL-12-01 --slot A1 --quantity 500 --unit ul
tm aliquot use 1226KO02.1 500 ml -m purification   # decrements; zero = consumed
tm box move WFL-12-02 --in shelf-1
tm aliquot move 1226KO01.2 --box WFL-12-03 --slot C3 -m "moved to WFL-12-03"
tm lineage p035 --up                                      # lineage:true edges + core links
```

The stock gets a date-code ID; its default one aliquot is `<stock_id>.1`. Full multi-step flows
(new plasmid + glycerol stock, pellet consumption, moving and lineage formats): `references/recipes.md` §1-4.

### Organisms: line, colony, aliquot (angenet)

A line is an entity, a colony a stock, and an aliquot either one animal (quantity 1, in a `cage`)
or one housing unit's population (quantity = headcount, unit `individuals`). A cross **is** its
progeny stock: `dam`/`sire` are refs to parent aliquots and both are `lineage: true` edges. A
litter is a `mouse_cohort` with `genotype` NULL and one group aliquot; at genotyping link each
genotype × sex stock through `--parent` and finish the litter aliquot:

```sh
tm stock add mouse_cohort --entity b6j --box MC-02 --quantity 7 --unit individuals \
  --set dam=ALQ_F --set sire=ALQ_M                            # genotype stays NULL
tm stock add mouse_cohort --entity b6j --parent LITTER --box MC-03 --aliquots 3 \
  --quantity 1 --unit mouse --set genotype=G+ --set sex=f     # one per genotype x sex
tm aliquot status LITTER.1 consumed --note "genotyped and split"   # gone for good
tm stock edit LITTER --set fate=euthanized                   # optional detail, not a status
tm lineage PUP --up                                           # litter and both parents
```

Other angenet commands: `tm entity add fly_line …`, then `tm stock add fly_culture … --quantity 30
--unit individuals` (one animal: `--quantity 1 --unit mouse` in a `cage`); record a cross by pointing
the progeny stock at parent aliquots (`--set dam=ALQ --set sire=ALQ`); thaw a cryo vial with
`tm aliquot status VIAL consumed` (`thawed` is `consumed`; a vial is `stored` in the freezer).
Detailed states are optional enum stock properties: `fate` on mouse/fish/fly stocks, `condition` on
worm cultures (growing, starved, contaminated), or an event `--note`. Full recipes - register a
colony, record a cross, split a litter, thaw a cryo vial: `references/recipes.md` §10-13.

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
- **One fixed status set for every aliquot.** `pending` (planned, not here) → `stored` (physically
  here; the only in-stock status) → `missing` (may come back) or `consumed` (gone for good, final),
  and `missing` → `stored`/`consumed`; creation accepts any of the four. No preset or kind declares a
  status set — there is no `status_sets:`, no per-kind `statuses:` and no `tube`/`cryo`/`animal`/`fly`/
  `worm` vocabulary; `thawed` is `consumed`. A later change must follow the transitions; override only
  deliberately with `--force --note "why"` (the event carries `forced = TRUE`). Detailed life states
  are stock properties (`fate`, `condition`) or event messages.
- **A tube has a position exactly when it is `stored`.** Changing to any other status clears its
  box/slot (the event keeps the old position); `stored` without a position is refused — place it with
  `tm aliquot move ALQ --box NAME --slot S -m MSG` (a move of a not-in-stock tube also restores `stored`).
- **Box names are view-safe and grid boxes hold tubes only.** A `box add` name must not collide with
  a core view, is at most 58 characters and must differ from existing names beyond letter case; a box
  can be nested in a named or slotless box, not in a grid.
- **Soft-remove by default.** `aliquot status … consumed|missing` keeps history; the explicit
  `tm entity|stock|aliquot|box purge` is a hard delete with no undo (only Dolt history recovers it),
  for an object added by mistake; it refuses while anything still references the object.
- **Do not move a tube by editing fields.** `aliquot move` records the event and checks slot capacity
  and exclusivity; `stock edit` only changes stock metadata. `use`/`move` need `-m/--message`.
- **Check after imports and migrations**, and spot-check `box show` against the old grid.
