# 26 · `lineage: true` on ref fields and `tm lineage`
Depends on: 23 (can run in parallel with 25) · Design: `docs/preset-parts.md` §5 · Issue: git-bug `0a9acec`

Goal: show where material came from and what derives from it. This is the descent graph of entities and
stocks (lineage, not phylogeny).

## Decisions (settled, do not re-open)

- New field attribute `lineage: true`, valid **only** on `type: ref` fields (load error otherwise),
  allowed on entity and stock kinds. It changes no DDL. It is part of the schema fingerprint and appears
  in `tm schema` output.
- Edges (child → parent): stock → its entity; `stock.parent_stock_id`; `stock.source_aliquot_id`
  (child stock → that aliquot → its stock); every `lineage: true` ref (target an entity kind, a stock
  kind, or core `entity` / `stock` / `aliquot`). Refs without the flag are not edges.
- Nodes: entities and stocks. A stock node shows kind, id, entity name and
  `aliquots_in_stock/aliquots_total`. Before ticket 25, read whatever count columns `v_stock_<kind>` has
  through one small accessor, so 25 only changes the accessor. An aliquot is a node only as an edge
  endpoint.
- CLI `tm lineage ID [--up | --down] [--depth N] [--json] [--format text|dot]`:
  - `ID` resolves like `tm find` (id, then alias) to an entity, stock or aliquot. An aliquot starts at
    that aliquot.
  - default both directions, depth unlimited; cycles are impossible by FK direction but guard anyway.
  - text: indented tree. A node reached a second time is printed as `<id> (see above)`, with no
    subtree.
  - `--json`: `{"root": …, "nodes": [{id, object, kind, label, counts?}], "edges": [{from, to, via}]}`,
    where `via` ∈ `entity | parent_stock | source_aliquot | <kind>.<field>`.
  - `--format dot`: a valid Graphviz digraph, with the edge label = `via`.
- Mark these existing fields `lineage: true` in this ticket: molbio `plasmid.construct`, `cell_line.parental`
  (wherever those kinds live when this ticket lands; if ticket 27 has moved them, edit the part file).
  Organism `parental` / `dam` / `sire` come in ticket 29.
- Read-only command, no commit. Respects `--branch` like the other read commands.

## Files

- `src/tetramark/schema/{model.py,loader.py}` (attribute + validation), `src/tetramark/ops/lineage.py` (new),
  `src/tetramark/cli/lineage.py` (new) + registration in `cli/app.py`
- `presets/*.yaml` (the two flags; no `version:` bump: the flag changes no stored shape, so note in the
  README of `migration/` that flags affecting only reads need no step)
- tests: `tests/test_lineage.py` (new)
- `docs/spec.md` (CLI section, field attributes), `skills/tetramark/SKILL.md`

## Constraints

- No schema/DDL change and no `SPEC_VERSION` bump.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty.

## Verify

- Fixture: construct → plasmid (`construct` ref) → bacterial stock → child stock (`parent_stock_id`) →
  stock derived from an aliquot (`source_aliquot_id`); a `cell_line` with a `parental` line.
  `tm lineage <plasmid>` shows the construct above and the stocks below; `--up` / `--down` / `--depth 1`
  trim correctly.
- A diamond (two `lineage` refs reaching one node) prints `(see above)` once, and the JSON lists the node
  once.
- `--format dot` output parses with `dot -Tsvg` if Graphviz is installed (skip that assertion if it isn't).
- `lineage: true` on a string field fails to load.
