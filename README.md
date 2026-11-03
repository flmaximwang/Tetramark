# Tetramark

A general-purpose, version-controlled inventory ledger on [Dolt](https://www.dolthub.com/).

Four core objects — **entity**, **stock**, **aliquot**, **box** — are extended per domain by
declarative YAML **schemas** called **presets**. Pick presets, run `tm init PATH`, and you have a
validated, versioned ledger. Biology is one preset (`molbio`), not the scope: `library` keeps books,
`base` keeps generic lab items.

A ledger is **one self-contained directory**: the Dolt database (`.dolt/`), `ledger.yaml`, and the
preset copies `presets/` live together and move together. There is no workspace layer, no server to
start, and no registry: `tm` finds the ledger from the current directory or from
`-l/--ledger PATH`. Copy the directory (`cp -a`, `rsync`, `dolt clone`) to share or back it up,
exactly like a git repository.

Presets are YAML schema documents shipped with Tetramark (`tetramark/presets/` in the wheel) and
kept in this repository's `presets/` directory (`base.yaml`, the `nucleic_acids`, `bacteria`,
`proteins`, `cell_culture`, `immuno`, `viral_vectors` and `biosamples` parts, the `molbio`,
`cellbio` and `angenet` bundles, `library.yaml`). A preset is a **part** (an includable slice of the
model) or a **bundle** (only an `include:` list, such as `molbio` or `cellbio`);
`presets/README.md` maps them and explains how to add one. `tm init` copies the chosen preset **and
its `include:` closure** into `<ledger>/presets/`. From then on the ledger resolves presets only from
that copy, so a ledger is self-contained and keeps the schema it was created with even if the source
preset changes later. The copy is a plain directory next to `ledger.yaml`, not part of the Dolt
database: when you copy or clone a ledger, copy its `presets/` directory too.

## Why "Tetramark"

*Tetra* (Greek, "four") + *mark*: four marks, one for each core object.

| Mark | Object | Answers |
|---|---|---|
| 1 | **entity** | *What* is it? (a plasmid design, a book title, a reagent) |
| 2 | **stock** | *Which batch*? (one preparation, lot or culture of that entity) |
| 3 | **aliquot** | *Which unit*? (one physical tube, vial or copy: generic location/quantity/unit/status/label/notes; material properties live on the stock) |
| 4 | **box** | *Where*? (the container and slot that holds it) |

A *mark* is also what you put on a tube or a shelf so you can find the thing again, and every mark
here is versioned by Dolt. The CLI is `tm`.

- [`docs/spec/`](docs/spec/README.md) — the specification (one file per chapter).
- [`docs/guides/migrating-a-legacy-ledger.md`](docs/guides/migrating-a-legacy-ledger.md) — moving an old zsqlab ledger onto Tetramark.
- [`docs/guides/upgrading-0.4-to-0.5.md`](docs/guides/upgrading-0.4-to-0.5.md) — the 0.4.2 → 0.5.0 upgrade note.
- `skills/tetramark/SKILL.md` — day-to-day operating manual for agents and humans.
- `skills/tetramark-schema/SKILL.md` — writing presets, kinds, fields, geometry and rules.

## Install

Requires Python >= 3.11 and the [`dolt`](https://docs.dolthub.com/introduction/installation) binary
(>= 2.3.5) on `PATH`. The wheel bundles the presets, so a bare `--preset molbio` works from an
installed tool; `$TETRAMARK_PRESETS_DIR` can point at another directory of preset files instead
(§8 of the spec).

```sh
uv tool install .
tm --version
```

For development:

```sh
uv sync
uv run tm --help
```

### Testing

The suite runs against a real `dolt` binary (no mocks of Dolt) and is **embedded-only**: every
statement is a `dolt sql` subprocess in the ledger directory. There is no server harness. A ledger
fixture builds each standard shape once per session and hands out directory copies, so a fresh test
ledger costs a copy rather than a full init.

```sh
uv run pytest -q -m fast                  # Dolt-free unit tests: seconds; the inner loop
uv run pytest                             # full suite: -n auto from pyproject addopts
uv run pytest tests/test_objects.py -n0   # focused loop
scripts/test-lanes.sh                     # both lanes + --durations + wall-time budget
```

`scripts/test-lanes.sh` is the measurement and enforcement point (this repo has no CI): it runs the
fast and full lanes, prints their wall times, and fails when the full lane exceeds the recorded
baseline by more than 20 % on the same machine. The 0.5.0 baseline, the cost model and the rules for
new real-Dolt tests are in [`docs/guides/test-harness.md`](docs/guides/test-harness.md).

## Quickstart

Create one ledger directory with the `molbio` preset, then work **inside it**: with no flags, `tm`
walks up from the current directory to the ledger.

```sh
mkdir -p ~/lab
cd ~/lab

tm init lab101 --preset molbio --id-prefix 12 \
  --author "Your Name <you@example.com>"

cd lab101                            # a ledger is one directory: work in it
tm status                            # path, name, branch, spec, presets, author, lock
tm box add grid8x12 WFL-12-01

tm entity add plasmid --name pZET035 \
  --set backbone=pZET035 --set resistance=kan --alias p035

tm stock add bacterial_glycerol --entity p035 \
  --set strain='BL21(DE3)' --set medium=LB \
  --box WFL-12-01 --slot A1 --quantity 500 --unit ul

tm box show WFL-12-01
tm find pZET035
tm check
```

From anywhere else, point at the directory explicitly:

```sh
tm --ledger ~/lab/lab101 find pZET035      # -l ~/lab/lab101 works too
```

`box show` prints the grid, `find` resolves the alias and its box location, and `check` runs the
generated Dolt tests (exit 0 when no non-info test fails). Writes to one ledger are serialized by a
per-ledger lock: a second writer waits briefly, then exits with a typed `ledger busy` error instead
of interleaving (`$TETRAMARK_LOCK_TIMEOUT` changes the wait). Reads never lock.

`tm init --preset NAME` resolves a bare `NAME` through `$TETRAMARK_PRESETS_DIR` when set, otherwise
through the presets bundled with tetramark; a value containing `/` or ending in `.yaml` is a path
instead. The resulting `<ledger>/presets/` copy is what every later `tm` command opens; if it is
missing, `tm` fails and names the expected file rather than silently reading a different preset.
`tm presets list|show` inspect the resolved preset source.

`tm adopt PATH` formats an existing bare Dolt database in place (the 0.4 lab databases): same schema,
history kept, one `adopt(ledger): NAME` commit. It needs an empty schema on the selected branch (no
user tables, no views, no `dolt_tests` rows), a clean working set, and no `ledger.yaml` (pass
`--force-config` to overwrite a stray one). Adopt never deletes anything and refuses nested
directories; see [`docs/spec/03-physical-model.md`](docs/spec/03-physical-model.md) §3.1.

More flows: register a design and its glycerol stock, aliquot a bacterial pellet and use part of it,
move boxes, audit, import/export, back up. See `skills/tetramark/references/recipes.md`. Legacy
stock ids can be repaired in place with `tm stock renumber OLD --auto`; the old ids stay findable as
aliases.

## Upgrading from 0.4.2

0.4.2 ledgers open unchanged (format, `spec_version` and the preset copies are untouched). The
upgrade is CLI/API only:

- **`cd` into the ledger directory** and run `tm …`; `-w/--workspace` and `tm workspace` are gone.
  `-l/--ledger` now takes a **path**, not a ledger name.
- A stale `workspace.yaml` is not read. If discovery meets one before any ledger, `tm` exits 2,
  names the old workspace and tells you to `cd` into the ledger directory (or pass `--ledger PATH`).
  Nothing has to move: `ledger.yaml` already carries the author.
- Author order is `ledger.yaml` → Git `user.name`/`user.email`; the workspace author tier is gone.
- Backup has no default directory: pass one (`tm backup DIR`) or sync a configured backup.
- One writer per ledger at a time; a copy (`cp -a`/`rsync`) is the sharing/backup unit. Two machines
  writing one ledger over a network share is unsupported.
- A future server mode is a separate plan; 0.5.0 neither ships nor promises it.

The full note, including what to do with each 0.4 layout, is
[`docs/guides/upgrading-0.4-to-0.5.md`](docs/guides/upgrading-0.4-to-0.5.md).

## Command overview

`tm --help` groups the commands into five panels, in workflow order:

| Panel | Commands |
|---|---|
| **Setup** | `presets`, `init`, `adopt`, `schema`, `config`, `status` |
| **Inventory** | `entity`, `stock`, `aliquot`, `box` |
| **Lookup** | `find`, `alias`, `history`, `lineage` |
| **Data** | `import`, `export`, `check` |
| **Versioning** | `branch`, `remote`, `backup` |

Each group has its own subcommands — `tm <group> --help` lists them. The full command reference is
in [`docs/spec/06-cli.md`](docs/spec/06-cli.md).

## Versioning and branches

Every ledger records its format version and tool provenance in `ledger_meta`:

| key | meaning |
|---|---|
| `spec_version` | ledger **format** version, the key an upgrade pipeline reads; it only moves when the core tables, the `ledger_meta` key set or the generated objects change shape |
| `tetramark_version` | the tool version that last wrote `ledger_meta` (`schema apply` refreshes it) |
| `created_with` | the tool version that first formatted the ledger; never changes |
| `adopted_from_commit` | only `tm adopt`: the branch HEAD the database was adopted from |

`tm schema show` prints them in its header, and a ledger whose metadata predates these keys shows a
"meta refresh" hint in `tm schema plan` instead of an error.

A change of **format** (not of a preset) ships with a hand-run step under `migration/`, one folder per
counter bump: `migration/spec/2-to-3/` is the 0.2.0 step that dropped the old `aliquot_<kind>`
extension tables, and `migration/spec/3-to-4/` is the step that moved the status vocabulary into the
presets (dropping the global `aliquot.status` CHECK, adding `aliquot_event.forced` and recreating the
in-stock views). A preset's `version:` bump ships a `migration/presets/<name>/N-to-N+1/` step the same
way. `migration/README.md` and the step READMEs carry the preconditions, the manual steps, verification
and the rollback path.

### Branches

Dolt branches are first-class: `tm -b BRANCH <command>` runs every read, write and commit of that
command on BRANCH, without moving the branch other sessions see — it selects a Dolt revision
database (`` `db/branch` ``) for the session. The default is `branch:` in `ledger.yaml`, else the
checked-out branch; a branch that does not exist is a usage error (it is never created implicitly).
Manage branches with `tm branch ls`, `tm branch create NAME [--from REF]` and
`tm branch delete NAME`. Merging is deliberately not part of `tm`: cut over with Dolt's own
`CALL DOLT_MERGE('<branch>', '--ff-only')`.

So an in-place schema upgrade is: create a branch, adopt/apply/import on it, verify, then
fast-forward `main`. `main` stays readable until the cutover.

## Concepts in one table

| Object | What it is | ID |
|---|---|---|
| **entity** | The design or abstract thing (plasmid, protein construct, book title, item). | UUID v4 |
| **stock** | One preparation/batch of exactly one entity. Carries lineage. | date code, e.g. `1226FP01` |
| **aliquot** | One physical tube/vial; the only thing in a box slot. A **generic** tube — its material's properties live on the stock kind, because all aliquots of one stock are the same. | `<stock_id>.<n>` |
| **box** | A container with a geometry; boxes nest. | unique name, e.g. `WFL-12-01` |

Every successful write is exactly one Dolt commit, so `tm history` shows both aliquot events and
the commits that produced them.
