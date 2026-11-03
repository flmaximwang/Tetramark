# 1. Concepts

| Object | Meaning | ID |
|---|---|---|
| **entity** | The abstract thing (a plasmid design, a protein construct, a book title, a chemical). | UUID v4 |
| **stock** | A logical batch / preparation event of exactly one entity (a glycerol stock, a culture's pellet, a protein prep, a purchase lot). Carries lineage. | Configurable; default date code |
| **aliquot** | A physical unit of a stock (a tube, a vial, a copy). The only thing that occupies a box slot. A **generic** tube: location, quantity, unit, status, label, notes — it has no kind. | `<stock_id>.<n>`, n = 1, 2, … |
| **box** | A container with a geometry. Boxes nest (rack in a freezer shelf). | UUID internally + unique human `name` (e.g. `WFL-08-01`) |

Rules:
- A stock references exactly one entity. Its kind may restrict which entity kinds are allowed.
- A stock may have `parent_stock_id` (derived from) and `source_aliquot_id` (which physical unit was used).
- A stock has 0..N aliquots. Commands that create a stock create one aliquot by default (`--aliquots N`
  to create more, `--aliquots 0` for none), so single-tube users never think about aliquots.
- Stock status is **derived** from its aliquots (view), never stored.
- **All aliquots of one stock are the same**: they differ only in location, quantity, unit, status and
  label. Tubes that differ in a property (concentration, freeze-thaw cycles, glycerol %) are different
  **stocks**; a property of the material belongs on the stock kind, never on a per-tube table.
- A slot holds either aliquots (up to the box kind's `slot_capacity`, default 1) or one child box —
  never both. Box nesting must be acyclic.
- "Removing" a tube means status → `consumed` (soft). Hard delete only via explicit `--purge`.

### 1.1 Ledger

- **Ledger** = one self-contained directory. It holds the Dolt database (`.dolt/`), the
  human-editable configuration `ledger.yaml`, the preset copies `presets/` created at init, and —
  when the ledger declares them — its local `kinds/` and `rules/`. It lives next to the docs or
  repository it describes; the directory moves, copies and backs up as one unit. There is no
  workspace or Dolt data directory around it.
- **Identity is `ledger.yaml`'s `name:`** (P2), not the path and not the directory basename. Copy or
  rename the directory freely; the basename is only the Dolt database name the embedded driver uses
  internally.
- **Resolution** (P3): `tm -l/--ledger PATH` selects a ledger directory explicitly; otherwise the
  CLI walks up from the current directory to the nearest directory that holds both `.dolt/` and
  `ledger.yaml`. There is no environment variable, no registry and no default outside the ledger.
- **Creating a ledger**: `tm init PATH` creates one standalone ledger (Dolt database,
  `ledger.yaml`, preset copies). `tm adopt PATH` formats an existing bare Dolt database in place
  and keeps its history (P9, §3.1). Ledgers never nest: both commands refuse a PATH inside an
  existing ledger (P5).
- **Python API** (P15): `tetramark.open(path=None, branch=None)` opens, or discovers, a ledger;
  `Ledger.path` is the directory and `Ledger.name` is the `ledger.yaml` identity.
- **0.4 migration guard** (P4): `workspace.yaml` is not a ledger marker. If resolution meets one
  before any ledger it exits 2, names the old workspace and tells you to `cd` into the ledger
  directory (or pass `-l PATH`). Nothing is converted: `ledger.yaml` already carries the author and
  every other setting a ledger needs.

```yaml
# <ledger>/ledger.yaml
name: lab101
presets: [base, molbio]          # bare names; tm init copies them into <ledger>/presets/
kinds_dir: kinds/                # optional local kinds (YAML), relative to <ledger>
author: "Your Name <you@example.com>"   # fallback: git config user.name/email
ids:
  stock: {strategy: datecode, prefix: "12"}   # → 1226JT01
  entity: {strategy: uuid}
rules_dir: rules/                # optional custom validation rules (YAML), relative to <ledger>
```

Several ledgers can live next to each other (one project each, or a lab with a dozen projects);
nothing couples them — no shared `workspace.yaml`, no server, no registry. A command is always
about exactly one ledger directory, selected by `-l/--ledger PATH` or the upward walk.

### 1.2 Sharing and portability

A ledger is one self-contained directory, so **sharing a ledger means copying that directory** —
`cp -a`, `rsync`, `dolt clone` or `dolt backup` — exactly like a git repository. Two machines writing
one ledger over a network share is **not supported**: the embedded driver reads and writes through
separate `dolt sql` processes, so a shared directory has no single writer.

Two rules keep a copied ledger working:

- `kinds_dir` and `rules_dir` must be **relative paths inside the ledger directory**. An absolute
  path, a `..` escape or a symlink that leaves the ledger is refused with a usage error when the
  ledger is opened.
- A Dolt remote or backup stored as an absolute `file://` URL keeps pointing at the machine it was
  configured on. `tm check` reports such URLs as an **informational** rule — it never fails the
  command — so a moved ledger says what to re-configure. `tm status` lists the same findings.
