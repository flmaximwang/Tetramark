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
- [`docs/guides/upgrading-0.5.0-to-0.5.1.md`](docs/guides/upgrading-0.5.0-to-0.5.1.md) — the 0.5.0 → 0.5.1 upgrade note (full-word subcommands).
- [`docs/guides/upgrading-0.5.1-to-0.6.0.md`](docs/guides/upgrading-0.5.1-to-0.6.0.md) — the 0.5.1 → 0.6.0 upgrade note (the interactive form).
- `skills/tetramark/SKILL.md` — day-to-day operating manual for agents and humans; its frontmatter states the Tetramark version it describes, and `tm skill version` checks that against `tm --version`.
- `skills/tetramark-schema/SKILL.md` — writing presets, kinds, fields, geometry and rules; it states the Tetramark version it describes (`tm skill version tetramark-schema`; `--file` checks a copy elsewhere).

## Install

Requires Python >= 3.11 and the [`dolt`](https://docs.dolthub.com/introduction/installation) binary
(>= 2.3.5) on `PATH`. Install `tm` with [`uv`](https://docs.astral.sh/uv/) from the pinned release
tag — replace `v0.8.18` with the release you want:

```sh
uv tool install --reinstall "git+https://github.com/flmaximwang/Tetramark@v0.8.18"
tm --version
```

That is a normal, non-editable install: the wheel bundles the presets and both skills, so a bare
`--preset molbio`, `tm skill path` and the agent manuals work from the installed tool with no
checkout. `$TETRAMARK_PRESETS_DIR` can point at another directory of preset files instead (§8 of
the spec).

If you prefer a single downloadable file, install the wheel attached to the same release (the same
`--reinstall` form):

```sh
uv tool install --reinstall \
  "https://github.com/flmaximwang/Tetramark/releases/download/v0.8.18/tetramark-0.8.18-py3-none-any.whl"
```

To **upgrade**, run the same command with the new tag (or the new wheel URL): `--reinstall`
replaces the previous tool in place.

For development from a checkout, use the editable install — **development only**, never a tool you
rely on:

```sh
uv sync
uv run tm --help
uv tool install -e .          # development only: this checkout's tm on PATH
```

The editable install follows the checkout: editing the code changes `tm` immediately, and
`tm --version` warns when the installed metadata and the code drift apart.

### Troubleshooting

**`tm: ignoring PYTHONPATH entries from another Python: …`** — your shell's `PYTHONPATH` points at
another interpreter's `site-packages`. `tm` drops those entries and runs normally; `unset
PYTHONPATH` (or fix the export) to silence the line.

**`warning: installed metadata says tetramark X but the code is Y`** — `tm` is an editable install
or a stale copy whose metadata no longer matches its code. Install the pinned release command above;
it replaces the tool with a versioned wheel.

**`no ledger found from /Users/you`** — `tm` looks for a ledger in the current directory and its
parents; `$HOME/.dolt` is Dolt's global configuration directory, not a ledger. `cd` into the ledger
directory (the one with `ledger.yaml` and `.dolt/`) or pass `-l PATH`; `tm init PATH` creates a new
one. `tm adopt PATH` is only for an existing bare Dolt database, never for `$HOME`.

**Where is the installed skill?** `tm skill path` prints the directory of the packaged
`tetramark` skill, so an agent can read the manual that belongs to this exact `tm`.

### Testing

The suite runs against a real `dolt` binary (no mocks of Dolt). **One command runs everything**:

```sh
uv run pytest                    # full real-Dolt suite (including the embedded-driver budget tests)
uv run pytest -q -m fast         # Dolt-free unit tests: seconds, 30 s session budget
```

The full run is a single pytest session with both real backends: ledger tests use a managed
`dolt sql-server` (the test harness default; the product itself still defaults to the embedded
driver), and the `embedded_driver` process-budget tests switch to the embedded
one-`dolt sql`-process backend per test. Nothing is skipped to save time. The session enforces its
own limits:

* **300 s wall clock** — a slower run fails with the elapsed time and the slowest tests
  (`TETRAMARK_SUITE_BUDGET_SECONDS=600` overrides for profiling; an empty value disables the guard);
* **60 s per test** (`pytest-timeout`) — a hung Dolt server cannot stall the run;
* **30 s** for `-m fast`.

Measured on the reference machine (10-core macOS): **~212 s, 1351 passed, 1 skipped**, zero leftover
`dolt sql-server`. The skip is the optional 0.4.2 lab-ledger snapshot, absent from this checkout.

`tests/test_*_round_trips*.py` pin the embedded driver's process budgets, so batching cannot
regress. `scripts/test-lanes.sh` runs the same plain commands (fast + full) and adds the
`dolt sql-server` start count and leftover-process check; `scripts/test-lanes.sh embedded` runs the
whole suite on the product's default backend as a release check. The cost model, the server
lifecycle and the rules for new real-Dolt tests are in
[`docs/guides/test-harness.md`](docs/guides/test-harness.md).

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

tm aliquot show 1226L401.1           # the tube's <stock>.<seq> id, as `stock add` printed it
tm aliquot list                      # the tubes an experiment can use (in stock by default)
tm box show WFL-12-01
tm find pZET035                      # id → alias → name/text, case-insensitive
tm find -E '^pZET'                   # -E: case-insensitive ICU regex over the same places
tm entity list --where 'name~(?i)^pzet'   # --where: exact, regex, inequality, ordered comparison
tm check
```

From anywhere else, point at the directory explicitly:

```sh
tm --ledger ~/lab/lab101 find pZET035      # -l ~/lab/lab101 works too
```

`aliquot show` prints one tube's status, quantity, aliases, box path and event timeline (the id is
the `<stock>.<seq>` that `stock add` printed), `aliquot list` prints the tubes an experiment can use
(in stock by default; `--all` includes consumed, missing and other history), `box show` prints the
grid, `find` resolves the alias and its box location, and `check` runs the generated Dolt tests
(exit 0 when no non-info test fails). `find -E` searches with the database's ICU regex dialect, and
`--where` filters the entity/stock/box lists or `tm find` with `field=value` (exact),
`field~pattern` (ICU regex), `field!=value`, `field!~pattern` or an ordered comparison such as
`--where 'created_at>=2026-01-01'`; write the filter in single quotes, because `<`, `>` and `!` are
special in the shell. See [`docs/spec/06-cli.md` §6.4](docs/spec/06-cli.md#64---where-filters-and-tm-find).
Writes to one ledger are serialized by a per-ledger lock: a
second writer waits briefly, then exits with a typed `ledger busy` error instead of interleaving
(`$TETRAMARK_LOCK_TIMEOUT` changes the wait). Reads never lock.

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

### For people: the interactive form

Those commands are the machine-facing ones: they need field names and flag syntax. A person at a
terminal can fill the same data in through a **form** instead:

```sh
tm entity add-form
tm stock add-form
tm aliquot add-form
tm box add-form
```

The form asks for the kind, the fields (with their descriptions), the box and the slots, driven by
the ledger's own schema, and checks every value as it is typed. It writes the same row
`tm <object> add` writes. `Enter` moves on, `Shift+Tab` goes back to the previous step, `Tab`
completes, `Ctrl+E` opens your editor on the current field, `Ctrl+C` aborts, and a footer lists the
keys; a last screen shows every value and writes it when you confirm with `y`. Without a terminal
the form refuses (exit 2) and points at `tm <object> add`, so scripts and agents keep using the
flags.

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

0.5.1 is CLI-only as well: subcommands are spelled out in full (`list`/`remove`, not
`ls`/`rm`), the old short subcommands are unknown commands that exit 2, and ledgers and data
stay as they are — see
[`docs/guides/upgrading-0.5.0-to-0.5.1.md`](docs/guides/upgrading-0.5.0-to-0.5.1.md).

0.6.0 is additive too: it adds the interactive form (`tm <object> add-form`) and the read-only
`long:` preset key, while ledgers, schema and data stay as they are — see
[`docs/guides/upgrading-0.5.1-to-0.6.0.md`](docs/guides/upgrading-0.5.1-to-0.6.0.md).

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
Manage branches with `tm branch list`, `tm branch create NAME [--from REF]` and
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
