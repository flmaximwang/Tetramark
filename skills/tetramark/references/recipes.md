# Recipes

All examples assume a workspace and a default ledger; otherwise prefix `--ledger NAME`. Placeholders
such as `$STOCK` mean "the ID printed by the previous command".

## 1. Register a new plasmid and its glycerol stock in a box

```sh
tm entity add plasmid --name pZET035 \
  --set backbone=pZET035 --set resistance=kan \
  --set sequence='ATGAAACCCGGGTTT' --alias p035

tm stock add bacterial_glycerol --entity p035 \
  --set strain='BL21(DE3)' --set medium=LB --set batch=01 \
  --set glycerol_pct=15 \
  --box WFL-12-01 --slot A1 --quantity 500 --unit ul
```

What happened:

- one entity row (`entity` + `entity_plasmid`), with a UUID;
- one stock (`stock` + `stock_bacterial_glycerol`) with a date-code ID such as `1226FP01`;
- one aliquot `1226FP01.1` at `WFL-12-01:A1`, quantity `500 ul`, status `stored`;
- one Dolt commit for the entity and one for the stock (each command = one commit).

Verify:

```sh
tm box show WFL-12-01
tm find p035
tm stock show 1226FP01
```

If you do not know which box/slot is free, run `tm box show WFL-12-01` first; each grid cell shows
the occupant or is blank.

## 2. Add more tubes and spread them over slots

```sh
tm aliquot add 1226FP01 --count 3 \
  --box WFL-12-02 --slot B1 --slot B2 --slot B3 \
  --quantity 500 --unit ul

tm aliquot move 1226FP01.2 --box WFL-12-03 --slot C3
```

The slots are consumed in the order given; one slot per new aliquot. A move keeps the aliquot's
status and quantity and records a `move` event with the old and new positions.

## 3. Use part of a bacterial pellet

A `bacterial_pellet` stock is one culture; its **aliquots are the tubes and hold the volume**. The stock ID is
printed by `stock add`; the examples below use `1226KO02` as that ID.

```sh
tm stock add bacterial_pellet --entity p035 --aliquots 3 \
  --quantity 500 --unit ml \
  --set culture_volume_ml=1500 --set od600=2.4 --set induction=iptg

tm aliquot use 1226KO02.1 200 ml --purpose "first purification"
tm stock show 1226KO02
```

`1226KO02.1` now has `300 ml` and stays `stored`. Using the remaining `300 ml` (or `use … 500 ml`)
sets it to `consumed`; `1226KO02.2`/`.3` are untouched. If a tube is missing rather than used:

```sh
tm aliquot status 1226KO02.3 missing --note "not in the box on 2026-10-23"
```

Never encode consumption by editing the quantity with raw SQL: `use` writes the event and flips the
status automatically. Use `purge` only when the tube never existed in the first place.

## 4. Move a box and trace it

```sh
tm box add shelf-1 shelf-1
tm box move WFL-12-02 --in shelf-1
tm box show shelf-1
tm history WFL-12-02
```

Moving a box keeps its aliquots, child boxes and slots. `box show` on the new parent lists it, and
`find` reports the full path (`shelf-1/WFL-12-02:B2`). `history` shows the commit that performed the
move; `box show --json` gives the machine-readable geometry and slot list.

## 5. Find where X is

```sh
tm find p035                 # alias
tm find pZET035              # name
tm find 1226FP01             # exact id
tm find "BL21"               # text in kind fields
tm find 0826FP02 --json      # machine-readable result with score and location
```

Results show `object`, `kind`, `id`, a summary, the location (path plus slot) and why it matched
(`id`, `alias`, `name` or `text`). Then:

```sh
tm history 1226FP01
tm stock show 1226FP01
```

If a tube was moved, `history` shows both the old and new position; if several tubes exist, `find`
lists each location.

## 6. Audit

```sh
tm check
tm check -v
tm check --group core
tm check --group rules --format csv
```

`check` exits `0` only when no `fail` test fails; `info` failures are printed but do not change the
exit code. `-v` adds the offending rows for each failure (each generated test has a companion detail
query). Groups:

| Group | What it covers |
|---|---|
| `core` | slot capacity/exclusivity, box nesting, stored-aliquot slot consistency |
| `schema:<kind>` | kind restrictions (`entity_kinds`), patterns, required refs |
| `rules` | all preset and local rules (`rules:preset`, `rules:local`, `rules:preset:info`) |

Common failures:

- `core_slot_exclusive` — two things in one slot; `box show` the box, move one away.
- `core_stored_aliquot_slot` — a `stored` aliquot has no slot; move it into a box or mark it
  `missing`/`pending`.
- `schema_<kind>_entity_kinds` — a stock points at an entity kind the stock kind does not accept
  (e.g. a `bacterial_pellet` of a `protein_construct`; bacterial pellets accept
  `plasmid`/`bacterial_strain`).
- `rule_MB01` — a plasmid has a protein construct but no sequence; fill `sequence` with `entity edit`.

After a bulk import always run `tm check -v` before calling it done.

## 7. Export, edit in a spreadsheet, import

```sh
tm export all --out exports/
# edit exports/bacterial_glycerol.csv in a spreadsheet, keeping the id column
tm --dry-run import all exports/     # validates first, prints nothing on failure
tm import all exports/               # all-or-nothing, one commit
tm check
```

`import` refuses the whole batch when any row is invalid and prints every row number. Existing IDs are
rejected rather than overwritten, so delete rows you do not want to re-import or use `entity edit` /
`stock edit` for in-place changes. The supported columns are the ones `export` writes; `alias` is an
optional extra column.

## 8. Back up and push

```sh
tm backup                      # <data_dir>/backups/<ledger>, then sync on later runs
tm backup --ls
tm backup /path/to/archive/lab101

tm remote add origin /path/to/archive/lab101.git
tm remote push
tm remote pull               # on the other machine, after adding the same remote
```

`backup` copies the Dolt database, history included; restoring is a normal `dolt backup restore` (or
copy the directory back while no server runs). A push needs a reachable Dolt/git remote; use the same
remote name on both sides.

## 9. Change the schema

Do not edit tables by hand. Edit `ledger.yaml`, a local `kinds/*.yaml`, a local `rules/*.yaml` or the
preset, then:

```sh
tm schema plan
tm schema apply
tm check
```

See `skills/tetramark-schema/SKILL.md` for additive/destructive rules and worked examples.
