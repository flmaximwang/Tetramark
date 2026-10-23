# Tetramark

A general-purpose, version-controlled inventory ledger on [Dolt](https://www.dolthub.com/).

Four core objects — **entity**, **stock**, **aliquot**, **box** — are extended per domain by
declarative YAML **schemas** bundled into **presets**. Pick presets, run `tm init`, and you have a
validated, versioned ledger. Biology is one preset (`molbio`), not the scope: `library` keeps books,
`base` keeps generic lab items.

## Why "Tetramark"

*Tetra* (Greek, "four") + *mark*: four marks, one for each core object.

| Mark | Object | Answers |
|---|---|---|
| 1 | **entity** | *What* is it? (a plasmid design, a book title, a reagent) |
| 2 | **stock** | *Which batch*? (one preparation, lot or culture of that entity) |
| 3 | **aliquot** | *Which unit*? (one physical tube, vial or copy, with status and quantity) |
| 4 | **box** | *Where*? (the container and slot that holds it) |

A *mark* is also what you put on a tube or a shelf so you can find the thing again, and every mark
here is versioned by Dolt. The CLI is `tm`.

- `docs/spec.md` — the design.
- `docs/migration.md` — moving an old zsqlab ledger onto Tetramark.
- `skills/tetramark/SKILL.md` — day-to-day operating manual for agents and humans.
- `skills/tetramark-schema/SKILL.md` — writing presets, kinds, fields, geometry and rules.

## Install

Requires Python >= 3.11 and the [`dolt`](https://docs.dolthub.com/introduction/installation) binary
(>= 2.3.5) on `PATH`.

```sh
uv tool install .
tm --version
```

For development:

```sh
uv sync
uv run tm --help
uv run pytest
```

## Quickstart

Create a workspace (one Dolt data directory), a ledger with the `molbio` preset, and a 96-well box.
`tm init` uses the embedded driver, so it runs before the server exists; once the server is up,
every command below goes through it.

```sh
mkdir -p ~/ledger-workspace
cd ~/ledger-workspace

tm workspace init . --port 13308 \
  --default-ledger zsqlab12 --author "Your Name <you@example.com>"
tm init zsqlab12 --preset molbio --id-prefix 12

tm server start -d

tm box add grid96 WFL-12-01

tm entity add plasmid --name pZET035 \
  --set backbone=pZET035 --set resistance=kan --alias p035

tm stock add bacterial --entity p035 \
  --set strain='BL21(DE3)' --set medium=LB \
  --box WFL-12-01 --slot A1 --quantity 500 --unit ul

tm box show WFL-12-01
tm find pZET035
tm check

tm server stop
```

`box show` prints the grid, `find` resolves the alias and its box location, and `check` runs the
generated Dolt tests (exit 0 when no non-info test fails). Without a running server the same commands
still work through the embedded driver — just don't run two writers against one ledger at once.

More flows: register a design and its glycerol stock, aliquot a pellet and use part of it, move boxes,
audit, import/export, back up. See `skills/tetramark/references/recipes.md`.

## Concepts in one table

| Object | What it is | ID |
|---|---|---|
| **entity** | The design or abstract thing (plasmid, construct, book title, item). | UUID v4 |
| **stock** | One preparation/batch of exactly one entity. Carries lineage. | date code, e.g. `1226FP01` |
| **aliquot** | One physical tube/vial; the only thing in a box slot. | `<stock_id>.<n>` |
| **box** | A container with a geometry; boxes nest. | unique name, e.g. `WFL-12-01` |

Every successful write is exactly one Dolt commit, so `tm history` shows both aliquot events and
the commits that produced them.
