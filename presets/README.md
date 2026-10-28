# Presets: parts and bundles

A **part** is an ordinary preset file in this directory (`presets/<name>.yaml`): it has its own
`version:`, `kinds:`, `mixins:`, `rules:` and `status_sets:`, and it includes only the parts it
references. A **bundle** is a preset whose only content is `include:`; it adds no kinds and no rules.
Includes are loaded **once each** (`schema/loader.py`), so two bundles can share a part and one
ledger can list two bundles (`presets: [molbio, cellbio]`) without duplicate-kind errors. Kind names
stay one global namespace.

`molbio` keeps its name because existing ledgers already list it: it is a bundle now, and the
`migration/presets/molbio/2-to-3/` step moves a v2 ledger onto it without renaming a single table.

## The catalogue

The table is the target catalogue from `docs/preset-parts.md` §2. The directory holds `base`,
`proteins`, `nucleic_acids`, `bacteria`, `cell_culture`, `library` and the `molbio` bundle today;
the `immuno`/`viral_vectors`/`biosamples`/organism rows arrive with tickets 28 and 29.

| part | kinds | molbio | cellbio | angenet |
|---|---|:-:|:-:|:-:|
| `base` | item, lot; boxes grid9x9, grid96, rack, shelf, freezer, drawer, incubator; status sets `tube`, `cryo`, `animal` | ✓ | ✓ | ✓ |
| `nucleic_acids` | plasmid, plasmid_prep, oligo, oligo_stock | ✓ | ✓ | ✓ |
| `bacteria` | bacterial_strain, bacterial_glycerol, bacterial_pellet | ✓ | | |
| `proteins` | protein_construct, protein_prep | ✓ | | |
| `cell_culture` | cell_line, cell_vial, cell_pellet | ✓ | ✓ | |
| `immuno` | antibody, antibody_lot | | ✓ | |
| `viral_vectors` | virus, virus_prep | | ✓ | |
| `biosamples` | biosample (tissue / blood / DNA / RNA) | | ✓ | ✓ |
| `drosophila`, `c_elegans`, `zebrafish`, `mouse` | organism kinds (see `docs/preset-parts.md` §3) | | | ✓ |

The bundles:

- `molbio` — `include: [base, nucleic_acids, bacteria, proteins, cell_culture]`, version 3.
- `cellbio`, `angenet` — later tickets (28/29); each lists the parts it needs.

Each part's `description:` says what it covers and which bundles include it. New parts start at
`version: 1` and grow through `migration/presets/<name>/N-to-N+1/`, never by editing a shipped
version in place. Every part that adds kinds adapted from [BioManager](https://github.com/gaspolymerase/biomanager)
(MIT) ends its description with "Adapted from BioManager (MIT)." and its new kinds do the same.

## The include graph

```
base
├── proteins          (sequence_fields mixin; protein_construct, protein_prep)
│   └── nucleic_acids (plasmid, plasmid_prep, oligo, oligo_stock)
│       ├── bacteria  (bacterial_strain, bacterial_glycerol, bacterial_pellet)
│       └── cell_culture (cell_line, cell_vial, cell_pellet)
```

- **`nucleic_acids` includes `proteins`** because `plasmid.construct` is a `ref` to
  `protein_construct`: the design a plasmid expresses is a protein design, so the plasmid's part
  must load the protein part. The other direction would be a cycle.
- **`sequence_fields` lives in `proteins`** — the deepest part that both `plasmid` (via
  `nucleic_acids`) and `protein_construct` load. `base` is lower in the graph, but it is the
  domain-neutral inventory part; biology-specific mixins do not belong there. A
  `bacteria`/`cell_culture` part that needs sequence fields therefore reaches them through
  `nucleic_acids → proteins`.
- **A part never includes a bundle.** Includes point down the graph only, so a bundle is always
  the outermost file a ledger lists. The loader would accept the reverse only by accident; do not
  rely on it.

## Naming rule: entity = what it is, stock = what you can hold

- An **entity kind** is the design or the organism: `plasmid` (the construct map), `protein_construct`
  (the amino-acid design), `oligo` (the sequence you ordered), `cell_line` (the line), `bacterial_strain`
  (the genotype). An entity has no location, quantity or status.
- A **stock kind** is one physical form of it: `plasmid_prep` (DNA in a tube), `oligo_stock` (a
  resuspended primer), `bacterial_glycerol` (frozen cells), `cell_vial` (frozen cells of a line),
  `cell_pellet` (harvested cells). Every stock kind sets `statuses:` (spec §2.4) and names the
  `entity_kinds:` it can hold; the aliquots are the tubes.

The sugars in the names are deliberate: `bacterial_glycerol`, not `bacterial`; `plasmid_prep`, not
`plasmid`. A short kind name must never be mistaken for the old zsqlab table or the entity.

## Adding a part

1. Create `presets/<name>.yaml` with `preset: <name>`, `version: 1`, a `description:` naming the
   bundles that will include it, and the `include:` list of parts it references (never a bundle).
2. Put each kind in exactly one part. If two parts need it, it belongs in their lowest common
   include, not in both. A shared `mixins:` entry follows the same rule (see `sequence_fields`).
3. Give every kind and field a `description:`; end new BioManager-adapted kinds with
   "Adapted from BioManager (MIT).".
4. Give every **stock** kind `statuses:` (a name from `base`'s sets or an inline set) and
   `entity_kinds:`; entity and box kinds get neither.
5. Move the rules that query the part's tables into the same part, keeping their ids (`MB01` …).
6. Add the part to every bundle that should load it (`include:`), and add a line to the table above.
7. Add `migration/presets/<name>/1-to-2/` only when you later change a **shipped** version; a new
   file needs no step. Then run the tests: `uv run pytest -q -n 2 tests/test_presets.py`.
8. Record the decision in the part's `description:`/comments if the include direction or the home
   of a shared mixin was arguable.

## What is not modelled

Adapted from BioManager's inventory lists only; these are **time-based activity records**, not stored
material, and are deliberately left out. They may come back later as **plugins**, not core features:

- body-weight series, water logs;
- cage flip, weaning and turnover schedules, due-lists;
- health checks;
- genotype-call history with gel images;
- plants and BioManager's "custom" organism (a ledger's own `kinds/` already covers "custom").
