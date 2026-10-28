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
presets: [base, molbio]          # bare names; tm init copies them into <ledger>/presets/
kinds_dir: kinds/                # optional local kinds (YAML), same format as presets
author: "Your Name <you@example.com>"   # fallback: git config user.name/email
ids:
  stock: {strategy: datecode, prefix: "12"}   # → 1226JT01
  entity: {strategy: uuid}
statuses: [pending, stored, missing, consumed]  # aliquot status vocabulary (override allowed)
rules_dir: rules/                # optional custom validation rules (YAML)
```

---
