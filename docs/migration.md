# Migrating an old zsqlab ledger

This is the rulebook for moving a hand-written zsqlab Dolt database (the `Lookup-XX.xlsx` era) onto
Tetramark. Migration is **manual and guided**, not automatic: old databases encode project-specific
conventions, so every column gets an explicit landing place or an explicit decision to drop it.

The worked example uses **zsqlab08** because its tables are documented in
`<legacy-zsqlab-checkout>/README.md`. The same steps apply to zsqlab10 with the extra
`stock_strains`/`stock_libraries`/`box_table` tables.

## Non-goals

- No generator converts the old DB; do not build one.
- Do not migrate views (`box_01`, `box_map_all`, `checks`, …), triggers, generated-formula columns,
  registries or `dolt_tests`: all are derived state in Tetramark (`v_box_slots`,
  `v_aliquot_location`, `v_stock_status`, generated `*_sha256`/computed columns, generated tests).
- Do not keep two writable copies of the same inventory.

## 0. Prepare

1. Stop the old server before copying (moving an open Dolt database is unsafe):
   `dolt_stop.sh` in the old data directory.
2. Copy the old ledger directory to a scratch place; work from the copy and leave the original
   untouched until the new ledger is verified.
3. Create a new workspace and ledger. **Match the stock ID prefix to the project** so old date-code
   UIDs remain valid explicit IDs (zsqlab08 → prefix `08`; zsqlab10 → `10`):

```sh
mkdir -p ~/migration/zsqlab08
cd ~/migration/zsqlab08
tm workspace init . --default-ledger zsqlab08 --author "Name <you@example.com>" \
  --presets-dir "$HOME/tetramark/presets"
tm init zsqlab08 --preset molbio --id-prefix 08
tm schema show                 # confirm construct/plasmid/bacterial/plasmid_prep/pellet exist
tm box add grid9x9 WFL-08-01
tm box add grid9x9 WFL-08-02
```

`presets_dir` is the directory holding the preset YAML files (this repository's `presets/`,
or your own copy); `tm init --preset molbio` copies `molbio.yaml` and everything it includes
into `<ledger>/presets/`, and the ledger is then opened from that copy only — later edits to
the source preset do not change it. To change a ledger's presets on purpose, replace the files
under `<ledger>/presets/` in the same commit as the schema change (ticket 24's migration
script does this).

If you need columns the preset does not have, add a local kind/field first
(`skills/tetramark-schema/SKILL.md`), apply it, and only then import.

## 1. Inventory the old database

Read-only queries against the old copy (keep the output as the migration record):

```sh
dolt sql -r csv -q "SELECT COUNT(*) FROM entity_constructs"        # 27
dolt sql -r csv -q "SELECT COUNT(*) FROM entity_plasmids"          # 15
dolt sql -r csv -q "SELECT COUNT(*) FROM stock_bacteria"           # 27
dolt sql -r csv -q "SELECT COUNT(*) FROM stock_plasmids"           # 15
dolt sql -r csv -q "SELECT COUNT(*) FROM stock_proteins"           # 0
dolt sql -r csv -q "SELECT COUNT(*) FROM stock_registry"           # 42
dolt sql -r csv -q "SELECT COUNT(*) FROM entity_registry"          # 42
dolt sql -r csv -q "SELECT * FROM box_map_all" > old_box_map.csv
```

Row-count expectations for zsqlab08: **42 entities (27 construct + 15 plasmid), 42 tubes (27
bacteria + 15 plasmid, 0 protein), 2 boxes × 81 slots.** Any mismatch after migration is a mapping
bug, not a rounding error.

Export every old data table as CSV for the transform step:

```sh
for table in entity_constructs entity_plasmids stock_bacteria stock_plasmids stock_proteins; do
  dolt sql -r csv -q "SELECT * FROM $table" > "old_$table.csv"
done
```

## 2. Map old tables to kinds

| Old table (zsqlab08) | Rows | New object | New kind | Notes |
|---|---:|---|---|---|
| `entity_registry` | 42 | — | — | Discriminator half of the old FK; Tetramark `entity` + kind tables replace it. |
| `entity_constructs` | 27 | entity | `construct` | `entity_uuid` → `id`; `sequence`, `mature_sequence`, `aliases`, MW, pI, ε columns, `tag`, `notes`. |
| `entity_plasmids` | 15 | entity | `plasmid` | `entity_uuid` → `id`; `sequence`, `plasmid_backbone` → `backbone`, `construct_uuid` → `construct`, `description`, `notes`. |
| `stock_registry` | 42 | — | — | Same tube as the kind table below; the registry disappears (stock + aliquot carry the position). |
| `stock_bacteria` | 27 | stock | `bacterial` | `stock_uid` → `id`; `entity_uuid` → `entity_id`; `strain`, `batch`, `notes`; `box`+`row`+`col` → box/slot. |
| `stock_plasmids` | 15 | stock | `plasmid_prep` | It is a DNA preparation, not a bacterial stock. `stock_uid` → `id`; `entity_uuid` → `entity_id`; `sample_type`/`stock_type`/`batch` → `notes` or local fields. |
| `stock_proteins` | 0 | stock | `protein_prep` | Empty in zsqlab08; maps directly if data exists (`entity_uuid` → a `construct`). |
| `box_01`, `box_02` | 81 each | box | `grid9x9` | Names `WFL-08-01`, `WFL-08-02`; slots are the old `row`+`col`. |

Columns that do **not** migrate:

- `name_concat`, `file_name_concat`, `marker`, `sequence_sha256`, `*_0p1pct` → derived; Tetramark
  computes `sequence_sha256`, `length_bp` and the ε columns. Re-typing them would create a second
  source of truth.
- `entity_kind`/`stock_kind` discriminators → replaced by the kind table.
- `project*`, `series*`, `body*`, `version*`, `tag_description`, `file_name`: keep the pieces you
  actually search in a local field (or `notes`), do not bulk-copy descriptions with no consumer.
- Old registries, views, triggers, tests: not data.

## 3. Keep old UIDs

- **Entities**: old `entity_uuid` values are UUIDs, so pass them as `--id` — every downstream old
  reference stays resolvable.
- **Stocks**: old `stock_uid` is `<prefix><YY><M><D><NN>` (e.g. `0826FP02`). With the matching
  `--id-prefix` (`08`), these pass the date-code validator and become the new stock ID verbatim. The
  single old tube becomes stock `0826FP02` with one aliquot `0826FP02.1`.
- **UIDs that do not fit the strategy** (pellet short codes such as `4a334ac2`, per-table schemes,
  IDs from another project) must not be forced into `--id`. Create the object with a generated ID
  and attach the old UID as an alias: `tm alias add stock <new-id> 4a334ac2`. Search with
  `tm find 4a334ac2` then continues to work.
- If one ledger must hold tubes from several project prefixes, either use one ledger per project or
  keep the old UID as an alias; a date-code strategy pins one prefix.

## 4. Map positions

Old `(box, row, col)` → new **box name + slot label**:

| Old | New |
|---|---|
| `WFL-08-01`, row `A`, col `2` | box `WFL-08-01`, slot `A2` |
| `WFL-08-02`, row `J`, col `9` | box `WFL-08-02`, slot `J9` |
| no position (`NULL`/`/`) | aliquot with no box/slot; status `pending` |

`grid9x9` materialises rows `A-J` skipping `I` and columns `1-9`, exactly the old grid geometry. If
an old box is not 9×9 (custom rack, `WFL-4C-1`, concatenated labels such as `1-2`), define a local box
kind with `named` or slotless geometry; do not squeeze it into a grid.

Old `box_01`/`box_02` are *views* of the same tube positions; `box_map_all` is the slot universe. Use
`box_map_all`/`stock_uid_map` (one row per tube) as the position source, not the grid views.

## 5. Map status

The old zsqlab workbooks have no status column on the tube tables: a tube either exists in a slot or
it does not. Where a workbook used the three-state convention (在库 / 用完 / 丢失), map it as
`stored` / `consumed` / `missing`. Map explicitly, never by inference:

| Old evidence | New status |
|---|---|
| tube row with a complete slot | `stored` |
| tube row with no slot (old `/` or blank) | `pending` |
| documented as used up in notes | `consumed` (keep the note in the event) |
| documented as lost / not found | `missing` |
| newly registered, not aliquoted yet | `pending` |

A tube missing from the old table is **not** evidence of consumption; it may have been deleted. Put
that decision in the migration record. `consumed`/`missing` tubes still keep their row and history;
`purge` is only for rows created by mistake.

## 6. Map pellet aliquots (zsqlab10)

Old `stock_pellets` + `stock_pellets_aliquots`: one pellet (culture) with N tube rows, each holding a
`volume_ml`. New model:

- one `pellet` stock per culture, quantity carried per aliquot;
- one aliquot per old pellet aliquot (`<stock_id>.1`, `.2`, …);
- `volume_ml` → aliquot `quantity`, unit `ml`;
- the old pellet short code (`4a334ac2`) → alias on the stock;
- old volume-usage records → `tm aliquot use <aliquot> <amount> ml --purpose ...` on the matching
  aliquot, or a `consumed` status with a note if only the end state is known.

Do not keep a stock-level scalar volume next to the aliquot volumes: the tubes are the inventory.

## 7. Load it

Recommended path for a few hundred rows: transform the old CSVs into the `tm export` column
layout, then `tm import all`.

1. Produce one CSV per kind plus `boxes.csv` and (if needed) `aliquots.csv`:

```
exports/
  construct.csv     # id,kind,name,sequence,mature_sequence,aliases,molecular_weight_da,pi,... ,alias
  plasmid.csv       # id,kind,name,sequence,construct,backbone,resistance,... ,alias
  bacterial.csv     # id,kind,entity_id,strain,medium,batch,box,slot,quantity,unit,alias
  plasmid_prep.csv  # id,kind,entity_id,prep_method,... ,box,slot,quantity,unit,alias
  protein_prep.csv
  boxes.csv         # name,kind,parent,slot,... (one row per box)
```

The easiest way to get the exact headers is to create one object of each kind in a scratch ledger and
run `tm export all --out layout/`; use those headers as the template.

2. Validate and load:

```sh
tm --dry-run import all exports/     # nothing written; errors name the file and row
tm import all exports/               # all-or-nothing, one commit
tm check
```

`import all` reads entities first, then boxes, stocks, aliquots and aliases, so forward references
inside the batch resolve. Existing IDs are rejected, not overwritten — migration must start from an
empty ledger.

For a handful of rows, explicit single commands are fine:

```sh
tm entity add construct --id cd0ae0ea-363f-4409-a432-bd50d5a2b7f3 \
  --name '0801_4_Ori_6H.1@pZET043' --set sequence=ATGC --set tag=6H
tm entity add plasmid --id 5fe23f35-21fe-4274-bae8-168a3225ac3c \
  --name '0801_5_Ori_6H.1@pZET043' --set sequence=ATGC --set backbone=pZET043 \
  --set construct=cd0ae0ea-363f-4409-a432-bd50d5a2b7f3 --alias 0801_5
tm stock add bacterial --id 0826FP02 --entity 5fe23f35-21fe-4274-bae8-168a3225ac3c \
  --set strain=Top10 --set batch=01 --box WFL-08-01 --slot A2
tm stock add plasmid_prep --id 0826FP21 --entity 5fe23f35-21fe-4274-bae8-168a3225ac3c \
  --set prep_method=miniprep --box WFL-08-02 --slot A1
```

## 8. Verify

Run all of these; keep the output with the migration record.

1. **Row counts per kind** match the old table counts:

```sh
tm --format csv entity ls construct | wc -l      # header + 27
tm --format csv entity ls plasmid | wc -l        # header + 15
tm --format csv stock ls bacterial | wc -l       # header + 27
tm --format csv stock ls plasmid_prep | wc -l    # header + 15
```

2. **Every old UID resolves** to the same entity/stock:

```sh
tm find 0826FP02
tm find cd0ae0ea-363f-4409-a432-bd50d5a2b7f3
```

3. **`tm check` has no FAIL** (`info` lines such as "constructs without molecular weight" are
   allowed; the exit code is 0):

```sh
tm check -v
tm check --group core
```

4. **Spot-check box grids** against the old `box_01`/`box_02` and `box_map_all`:
   `tm box show WFL-08-01` must show the same UIDs in the same slots; empty slots stay empty.
5. **Counts per slot**: empty + occupied = box slots (81 for `grid9x9`), no duplicate slot.
6. **Lineage**: for a few plasmids, `tm stock show` and `tm find` show the same construct
   link and box locations as the old `v_stock_all`.
7. **History**: `tm history 0826FP02` shows one `create` event and one commit per migrated tube
   (imports are one commit for the batch, so use `tm history` for events; the commit is the batch).

## 9. Retire the old database

Keep the old directory read-only until the new ledger has been in use for a while: copy it to an
archive location and back up the new ledger (`tm backup` / `tm remote push`). To roll back,
point the old server at the untouched copy; do not edit the old database in place after cut-over.
Document in the new ledger's notes which old database and date it came from, and keep the exported old
CSVs next to the migration record.

## Checklist (copy into the migration record)

- [ ] Old server stopped, old ledger copied, original untouched.
- [ ] New ledger: `--preset molbio`, stock prefix = old project prefix, author set.
- [ ] Boxes created with old names and matching geometry.
- [ ] Entity counts: construct 27, plasmid 15 (zsqlab08).
- [ ] Stock counts: bacterial 27, plasmid_prep 15, protein_prep 0.
- [ ] Every old UID resolves (`find`) with the expected kind and box location.
- [ ] `tm check` exit 0, no FAIL (info acknowledged).
- [ ] `tm box show` matches the old grid for every box, including empty slots.
- [ ] Pellet volumes (zsqlab10) are per aliquot, no stock-level scalar left behind.
- [ ] Backup/push done; old database archived read-only.
