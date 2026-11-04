# 1. Concepts

| Object | Meaning | ID |
|---|---|---|
| **entity** | The abstract thing (a plasmid design, a protein construct, a book title, a chemical). | UUID v4 |
| **stock** | A logical batch / preparation event of exactly one entity (a glycerol stock, a culture's pellet, a protein prep, a purchase lot). Carries lineage. | Configurable; default date code |
| **aliquot** | One equal physical portion of a stock (a tube, a vial, a copy). The only thing that occupies a box slot. A **generic** tube: location, quantity, unit, status, label, notes — it has no kind. | `<stock_id>.<n>`, n = 1, 2, … |
| **box** | A container with a geometry. Boxes nest (rack in a freezer shelf). | UUID internally + unique human `name` (e.g. `WFL-08-01`) |

Rules:
- A stock references exactly one entity. Its kind may restrict which entity kinds are allowed.
- A stock may have `parent_stock_id` (derived from) and `source_aliquot_id` (which physical unit was used).
- A stock has 0..N aliquots. Commands that create a stock create one aliquot by default (`--aliquots N`
  to create more, `--aliquots 0` for none), so single-tube users never think about aliquots.
- Stock status is **derived** from its aliquots (view), never stored.
- **Aliquots are equal portions of one stock**: they differ only in location, quantity, unit, status
  and label. Anything that needs per-unit tracking — one animal, one plant, a tube measured
  separately — is its own **stock** (with one aliquot when it is a single physical unit); a property
  of the material belongs on the stock kind, and a detailed life state (euthanized, starved,
  contaminated) belongs in a stock property or an event message, never in a per-tube field or an
  extra status. The fixed status set is abstract for exactly this reason (§1.1).
- A slot holds either aliquots (up to the box kind's `slot_capacity`, default 1) or one child box —
  never both. Box nesting must be acyclic.
- "Removing" a tube means status → `consumed` (soft). Hard delete only via explicit `--purge`.
- **An action done in the physical world that cannot simply be reversed needs a message.** `tm aliquot
  use` (the material is gone) and `tm aliquot move` (the tube was physically relocated) therefore
  require `-m/--message`; `create`, `edit` and `status` do not. The message is recorded on the aliquot
  event, so the timeline keeps why the action happened, not just the resulting state.

### 1.1 Aliquot status

Every aliquot has exactly one status, from one **fixed, hardcoded set that every stock kind
shares**. It is part of the core model, not the schema: no preset or ledger declares statuses (§2.4).

| status | meaning | `in_stock` | allowed transitions |
|---|---|---|---|
| `pending` | ordered or planned; not physically here yet | no | `stored`, `missing`, `consumed` |
| `stored` | physically here and usable | yes | `missing`, `consumed` |
| `missing` | cannot be found now; may come back | no | `stored`, `consumed` |
| `consumed` | gone for good; final | no | none |

`stored` is the only in-stock status: the stock views count an aliquot in stock exactly when its
status is `stored`. Creating an aliquot (`tm stock add`, `tm aliquot add`, `tm import`) accepts any
of the four; changing one (`tm aliquot status`) must follow the table. `tm aliquot status ALQ NEW
--force --note TEXT` overrides the transition check, records `aliquot_event.forced = TRUE`, and is
the way to undo a wrong `consumed`; an unforced event that breaks a transition fails `tm check`
(§4).

**Upgrading older ledgers.** Earlier spec versions gave each stock kind its own status set (the
`tube`, `cryo`, `animal`, `fly` and `worm` vocabularies). Their words map to the fixed set as
follows; a word that already has a fixed-set name keeps it.

| old status | new status |
|---|---|
| `planned` | `pending` |
| `alive`, `crossed`, `growing`, `starved` | `stored` |
| `lost` | `missing` |
| `thawed`, `euthanized`, `found_dead`, `transferred`, `cancelled`, `discarded`, `contaminated` | `consumed` |

`thawed` means the same as `consumed`: a thawed cryo vial is gone. A cryo vial is `stored` while it
is in the freezer and `missing` or `consumed` after it leaves; there is no separate cryo vocabulary.
The detailed states those old words carried are stock properties or event messages now, not
statuses. The upgrade itself is a manual, documented step; it never rewrites past events.

### 1.2 Ledger

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

### 1.3 Sharing and portability

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
