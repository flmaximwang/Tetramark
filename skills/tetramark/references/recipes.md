# Recipes

All examples assume you are **inside the ledger directory** (the one holding `.dolt/` and
`ledger.yaml`); from anywhere else prefix `-l/--ledger PATH`. Placeholders such as `$STOCK` mean "the
ID printed by the previous command".

## 0. Create, adopt or inspect a ledger

A ledger is one directory; there is no workspace and no server.

```sh
mkdir -p ~/lab && cd ~/lab
tm init lab101 --preset molbio --id-prefix 12 --author "Name <you@example.com>"
cd lab101                     # every later command runs inside the ledger (or use -l PATH)
tm status                     # path/name/branch/spec_version/presets/author/lock/warnings

# Format an existing bare 0.4 Dolt database in place, history kept:
tm adopt ~/lab/legacy_db --preset molbio --author "Name <you@example.com>"
```

`tm status --json` prints the fixed machine-readable shape. A stale 0.4 `workspace.yaml` is not read:
if the upward search meets one before a ledger, `tm` exits 2 telling you to `cd` into the ledger
directory (or pass `--ledger PATH`).

An older ledger is never upgraded implicitly: `tm` warns `ledger is spec N, tm is spec N+1: follow
migration/spec/N-to-N+1/README.md`. The current spec 5 → 6 step is **manual** — every pending
historical preset step first, then replace the whole preset closure with the status-free copies,
remap the statuses (one `migrated: <old status>` event per remapped aliquot) and let
`tm schema plan`/`apply` write `spec_version` 6, the preset versions and the `CHECK`; see
`migration/spec/5-to-6/README.md`.

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

tm aliquot move 1226FP01.2 --box WFL-12-03 --slot C3 -m "moved to WFL-12-03 C3"
```

When every new tube has the same known amount, the equivalent one-liner is:

```sh
tm stock aliquot 1226FP01 500 ul --count 3 --box WFL-12-02 --slot B1 --slot B2 --slot B3
```

The two forms are the same write (one `add(aliquot): 1226FP01 x3` commit).

The slots are consumed in the order given; one slot per new aliquot. A move keeps the aliquot's
status and quantity and records a `move` event with the old and new positions. It requires
`-m/--message` too: say why the tube moved.

### Move a tube to another box

```sh
tm box list
tm box show WFL-12-03
tm aliquot move 1226FP01.2 --box WFL-12-03 --slot C3 -m "moved to WFL-12-03 C3"
tm aliquot show 1226FP01.2
```

`box list` starts with an `id` column: unique short UUID prefixes in the table, full UUIDs in
CSV and JSON. `box show` includes a short id in its header and the full `id` in JSON. Choose a
free slot from the destination view; omit `--slot` for a slotless box. `--box` accepts the box
name, full id, alias or unique id prefix. Placement changes use `move`, quantity changes use
`use`, and status changes use `status`.

## 3. Use part of a bacterial pellet

A `bacterial_pellet` stock is one culture; its **aliquots are the tubes and hold the volume**. The stock ID is
printed by `stock add`; the examples below use `1226KO02` as that ID.

```sh
tm stock add bacterial_pellet --entity p035 --aliquots 3 \
  --quantity 500 --unit ml \
  --set culture_volume_ml=1500 --set od600=2.4 --set induction=iptg

tm aliquot use 1226KO02.1 200 ml -m "first purification"
tm stock show 1226KO02
```

`1226KO02.1` now has `300 ml` and stays `stored`. Using the remaining `300 ml` — or
`tm aliquot use 1226KO02.1 -m "second elution"`, which uses everything left — sets it to `consumed`;
`1226KO02.2`/`.3` are untouched. `-m/--message` is required on `use` and `move`: it is trimmed, may
not be blank and is recorded on the tube's event, so say why the material left the tube. A tube
registered without a quantity can only be used whole (`tm aliquot use ALQ -m "used it all"`); a
partial amount is refused. If a tube is missing rather than used:

```sh
tm aliquot status 1226KO02.3 missing --note "not in the box on 2026-10-23"
```

Never encode consumption by editing the quantity with raw SQL: `use` writes the event and flips the
status automatically. Use `purge` only when the object never existed in the first place; the same
explicit hard delete exists for entities, stocks and boxes (`tm entity|stock|box purge`).

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
tm find 0506-2.1-01          # rewrite rules may map a handwritten handle to the stored one
tm find 0506-2.1-01 --with-rewrite   # also show every rule's hits below the original ones

tm find -E '^pZET'           # regex: ids, aliases, names and kind text, case-insensitive
tm find --where 'resistance=kan'              # list every object with that field (no aliquots)
tm entity list --where 'name~(?i)^pzet'       # filter one object type by one column
tm stock list --where 'strain~BL21' --where 'medium=LB'   # filters AND
tm stock list --where 'od600>=2'              # ordered comparison: OD 2 or more
tm entity list --where 'name!~GFP'            # negation: every name without GFP
# AND a (B or C): every --where and only one OR group of --where-any
tm stock list --where 'kind=plasmid_prep' --where-any 'resistance=kan' --where-any 'resistance=amp'
```

Use `tm find` when you have a handle (id, alias, name or a piece of kind text); `-E` is the same
search with a case-insensitive ICU regex. Use `--where` when you know the column and want a set:
`=` is exact, `!=` is "another value or nothing", `~` is an unanchored, case-sensitive regex search
(anchor with `^`/`$`, use `(?i)` to ignore case), `!~` is its negation, and an ordered comparison
such as `'od600>=2'` compares numbers, dates and text in order. `field!=` (no value) asks for
"this field is filled in"; `field!=x` does not — it also matches the rows where the field is empty.
`--where-any` is the one OR group: a row must match one of its alternatives AND every `--where`, and
a field may repeat there (`--where-any 'resistance=kan' --where-any 'resistance=amp'`). The same
filters work on `tm entity|stock|box list`; `tm find --where` and `tm find --where-any` span every
object type and exclude aliquots. The aliquot list's own `--where` is exact-only and does not take
the new operators.

A ledger can declare `find.rewrites` rules in `ledger.yaml` (Python `re`): a literal query with no
hits falls back to the first rule that hits (a stderr line names the rule), and `--with-rewrite` adds
every rule's hits below the original ones. `-E`, `--where`/`--where-any` and single-object commands
never rewrite; a plain not-found there ends with `try tm find <ref>` (wrong-type, ambiguous and
empty refs keep their own error).

Results show `object`, `kind`, `id`, a summary, the location (path plus slot) and why it matched
(`id`, `alias`, `name`, `field`, or `filter` for a filter-only listing). Then:

```sh
tm history 1226FP01
tm stock show 1226FP01
```

If a tube was moved, `history` shows both the old and new position; if several tubes exist, `find`
lists each location.

To enumerate instead of search — what is available, and where everything is:

```sh
tm aliquot list                      # every in-stock tube (the "what can an experiment use" view)
tm aliquot list 1226FP01             # one stock's available tubes
tm aliquot list --box FR-1           # everything in the freezer, nested boxes included
tm aliquot list --not-in-stock       # the missing + consumed audit (there is no `thawed` status)
tm aliquot list --status missing     # one status (an explicit status leaves the shelf view)
```

A list is the availability view, not the history: `tm aliquot list` hides the tubes that left stock
unless `--all` or an explicit `--status` names them. Use `find` when you have a handle and want its
location; use `list` when you want a set.

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
tm backup /path/to/archive/lab101   # required: there is no default directory (P10)
tm backup --list                    # list configured backups
tm backup                           # later runs sync the configured ledger-named backup
tm backup --name pre-spec4          # sync a configured backup with another name

tm remote add origin /path/to/archive/lab101.git
tm remote push
tm remote pull               # on the other machine, after adding the same remote
```

`backup` copies the Dolt database, history included; restoring is a normal `dolt backup restore` (or
copy the directory back while no writer runs). A backup next to the ledger is not a backup — it
shares the disk and is lost with it — hence the explicit directory (or a configured destination). A
push needs a reachable Dolt/git remote; use the same remote name on both sides.

## 9. Change the schema

Do not edit tables by hand. Edit `ledger.yaml`, a local `kinds/*.yaml`, a local `rules/*.yaml` or the
preset, then:

```sh
tm schema plan
tm schema apply
tm check
```

See `skills/tetramark-schema/SKILL.md` for additive/destructive rules and worked examples.

## 10. Register a colony (angenet organisms)

The `angenet` bundle models a **line** (entity), a **colony** (stock) and the **aliquots**: one animal
for individually tracked species (mouse, tagged fish; quantity 1, in a `cage`) or one housing unit's
population for group-counted ones (fly vial, worm plate, fish tank; quantity = headcount, unit
`individuals`). Individuals of one stock are identical in every recorded property, so **sex,
genotype, birth date and cross are stock fields** — a breeding pair is two stocks, not two aliquots
of one.

```sh
# Fly: a line and a vial of 30 flies in an incubator.  The vial is one aliquot.
tm entity add fly_line --name w1118 --set balancers=CyO \
  --set stock_centre=BDSC --set stock_number=12345 --alias w1118
tm box add incubator INC-25
tm stock add fly_culture --entity w1118 --box INC-25 \
  --quantity 30 --unit individuals \
  --set temperature_c=25 --set generation=P1 --set food_type=standard
# -> fly_culture 7726KU06, aliquot 7726KU06.1 (stored)

# Worm: a strain on a plate, plus the frozen backup (the real colony).
tm entity add worm_strain --name N2 --set allele=N2 \
  --set lab_prefix=CB --set wormbase_id=WBStrain00000001 --alias N2
tm stock add worm_culture --entity N2 --box INC-25 \
  --quantity 200 --unit individuals \
  --set temperature_c=20 --set plate_type=NGM --set stage=adult
tm stock add worm_cryo --entity N2 --quantity 1 --unit vial \
  --set frozen_on=2026-01-10 --set method="-80 C soft agar"
# -> worm_cryo 7726KW01, aliquot 7726KW01.1 (stored)

# Fish: a tank is one group aliquot; a tagged fish would be one aliquot of
# quantity 1, its tag going in the core aliquot label on import/back-fill.
tm entity add fish_line --name Tg(flk1:GFP) --set zfin_name=Tg(flk1:GFP) \
  --set background=AB --alias flk1g
tm stock add fish_group --entity flk1g --box INC-25 \
  --quantity 25 --unit individuals \
  --set birth_on=2026-03-01 --set sex=u --set density_per_l=5

# Mouse: one mouse per aliquot, quantity 1, in a cage (slotless, capacity 5).
tm entity add mouse_strain --name B6J-Apobec --set background=C57BL/6J \
  --set supplier="Jackson Laboratory" --set mgi_id=MGI:3029029 --alias b6j
tm box add cage MC-01
tm stock add mouse_cohort --entity b6j --set sex=f --set birth_on=2026-05-01 \
  --quantity 1 --unit mouse --box MC-01 --status stored
```

Every aliquot uses the **same fixed status set** — `pending`, `stored`, `missing`, `consumed` — and
only `stored` is in stock. Creation accepts any of the four; a later change follows the transitions
(`tm aliquot status ALQ consumed` finishes a culture/vial/animal, `missing` parks one that may come
back). `tm schema show KIND` prints the same fixed set for every kind. The detailed life state is an
optional enum **stock property**, never a status: `tm stock edit FLY --set fate=discarded` (fly:
discarded/lost), `tm stock edit MOUSE_OR_FISH --set fate=euthanized` (mouse, fish:
euthanized/found_dead/transferred/cancelled), `tm stock edit WORM --set condition=starved` (worm:
growing/starved/contaminated). Use `tm aliquot use ALQ [AMOUNT UNIT] -m MSG` for quantities (the
message is required there; `status` needs none unless it is a forced change).

## 11. Record a cross (dam/sire are aliquots)

There is no cross object: the **progeny stock is the cross**, and its `dam`/`sire` fields are refs to
**aliquots** (a virgins vial for flies, a mouse, a breeding tank for fish). Both are `lineage: true`
edges, so `tm lineage` still walks the pedigree after the parents are gone.

```sh
# A virgins vial is just another fly_culture; its aliquot is the dam.
tm stock add fly_culture --entity w1118 --box INC-25 \
  --quantity 20 --unit individuals \
  --set temperature_c=25 --set generation=P0 \
  --set notes="virgin females collected 2026-10-20"
# -> 7726KU07, aliquot 7726KU07.1
tm stock add fly_culture --entity w1118 --box INC-25 \
  --quantity 10 --unit individuals \
  --set temperature_c=25 --set generation=P0
# -> 7726KU08, aliquot 7726KU08.1 (the male parent)

# The cross vial names the two parent aliquots.
tm stock add fly_culture --entity w1118 --box INC-25 \
  --quantity 5 --unit individuals \
  --set temperature_c=25 --set food_type=standard \
  --set virgins_collected_on=2026-10-20 \
  --set dam=7726KU07.1 --set sire=7726KU08.1
# -> cross vial 7726KU09; tm lineage 7726KU09 --up shows both parents
```

The same fields exist on `fish_group`, `fish_clutch` and `mouse_cohort`; `worm_culture` deliberately
has none (selfing hermaphrodites — the strain's `parental` ref carries the lineage).

## 12. Split a litter at genotyping

A litter is a `mouse_cohort` with **`genotype` NULL** and **one group aliquot** (quantity = pups).
Genotyping/weaning creates one stock per genotype × sex, each mouse its own aliquot, linked to the
litter through the core `parent_stock_id` (`--parent`); the litter aliquot is then finished.

```sh
tm entity add mouse_strain --name B6J-Apobec --set background=C57BL/6J --alias b6j
tm box add cage MC-01
tm stock add mouse_cohort --entity b6j --set sex=f --set birth_on=2026-05-01 \
  --quantity 1 --unit mouse --box MC-01 --status stored      # 7726KU01.1
tm stock add mouse_cohort --entity b6j --set sex=m --set birth_on=2026-05-01 \
  --quantity 1 --unit mouse --box MC-01 --status stored      # 7726KU02.1

# The litter: one stock, no --set genotype, one group aliquot of 7 pups.
tm box add cage MC-02
tm stock add mouse_cohort --entity b6j --box MC-02 \
  --quantity 7 --unit individuals --set birth_on=2026-07-01 \
  --set dam=7726KU01.1 --set sire=7726KU02.1                # 7726KU03, aliquot .1

# Genotyping: one stock per genotype x sex, parent_stock_id = the litter.
tm stock add mouse_cohort --entity b6j --parent 7726KU03 \
  --box MC-03 --aliquots 3 --quantity 1 --unit mouse \
  --set genotype=Apobec+/- --set sex=f --set birth_on=2026-07-01
tm stock add mouse_cohort --entity b6j --parent 7726KU03 \
  --box MC-04 --aliquots 4 --quantity 1 --unit mouse \
  --set genotype=Apobec-/- --set sex=m --set birth_on=2026-07-01

tm aliquot status 7726KU03.1 consumed --note "genotyped and split into G+ and G- stocks"
tm stock edit 7726KU03 --set fate=euthanized    # optional detail: what happened to the animals
tm lineage 7726KU04 --up      # pup stock -> litter -> dam and sire aliquots -> breeders
tm lineage 7726KU04 --down    # the split stocks below this one
```

The litter stock keeps its own history: `tm find 7726KU03` and `tm stock show 7726KU03` still show
the untyped litter and its finished aliquot.

## 13. Thaw a cryo vial

Frozen stocks (`worm_cryo`, `mouse_sperm_cryo`, `mouse_embryo_cryo`, `fish_sperm_cryo`, cellbio's
`cell_vial`) have no separate vocabulary: a vial is **`stored`** in the freezer, and thawing it makes
the tube **`consumed`** — `thawed` is `consumed`. Record the thaw on the aliquot, never by editing the
stock fields.

```sh
tm aliquot status 7726KW01.1 consumed --note "thawed 2026-11-04; chunked onto NGM plates"
tm aliquot status 7726KW01.1 stored   # refused: consumed is final
```

To undo a wrong `consumed`, place the vial again: a `move` of a not-in-stock tube restores `stored`
(and records where it went).

```sh
tm aliquot move 7726KW01.1 --box FR-1 --slot A1 -m "thaw was recorded on the wrong vial"
```

## 14. Inspect one tube

`tm aliquot show` answers "what is this tube, where is it and what happened to it" without opening
its box:

```sh
tm aliquot show 1226FP01.1
tm aliquot show 1226FP01.1 --json
```

`ALQ` is the full `<stock>.<seq>` id or one of the tube's aliases (an alias added with `tm alias add
aliquot …`, or an old id left by `tm stock renumber`). The table holds the aliquot row (`status`,
`quantity`, `unit`, `label`, `notes`, `created_at`), the sorted `aliases`, the owning stock's
`stock_kind` and `entity_id`, the `box_id`/`slot` and the derived `in_stock`; the `path` down the
box nesting (`Freezer-1/Shelf-2/Box:A3`) prints on its own line under the table. Below it,
`events  N` and one line per event list the tube's own trail, oldest first — creation, moves, use,
status changes — so the tube's event story no longer needs a second command. A tube that is not in
stock has no position, so `box_id`, `slot` and `path` are empty (`null` in JSON) and `in_stock` is
false.

For the rest of the picture:

```sh
tm history 1226FP01.1        # the same events plus the Dolt commits that touched the tube
tm stock show 1226FP01       # the stock kind's own fields and all its tubes
```
