# 25 · Status sets per stock kind, transition rules, in-stock views (spec 4)
Depends on: 23 · Design: `docs/preset-parts.md` §4 · Issue: git-bug `0a9acec`

Goal: statuses belong to stock kinds, not ledgers. Each status says whether it counts as in stock and
which statuses it may change to. A stock's status is read from its aliquots: how many are in stock out
of how many in total.

## Decisions (settled, do not re-open)

- New top-level preset key `status_sets: {<set>: {<status>: {in_stock: bool, to: [<status>…]}}}`.
  Allowed in presets and ledger `kinds/` files. Set names are global, duplicates are a load error, and
  every `to:` target must exist in the same set. A status without `to:` is final.
- Every **stock** kind requires `statuses:` (a set name or an inline set; missing → load error). Entity
  and box kinds must not have it. An aliquot's allowed statuses are its stock kind's set.
- `ledger.yaml` loses `statuses:`. The `--statuses` option of `tm init` and `DEFAULT_STATUSES`
  (`core/ledger.py`) go away. Reading a ledger whose `ledger.yaml` still has `statuses:` is an error
  pointing to `migration/spec/3-to-4`.
- `base` defines the sets `tube`, `cryo` and `animal` exactly as in design §4. Every existing stock kind
  (base `lot`, library, molbio v2 kinds) gets `statuses: tube`, and molbio `cell_vial` gets `cryo`.
  These are content changes to preset files: bump their `version:` and add the matching
  `migration/presets/<name>/N-to-N+1` steps. Each step only edits the ledger's preset copy, because no
  table changes.
- **Creation** accepts any status in the set (add, stock add with aliquots, import). **Changes** must
  follow `to:`. `tm aliquot status ALQ NEW [--force --note TEXT]`: `--force` requires `--note` and writes
  `aliquot_event.forced = TRUE` (new `BOOLEAN NOT NULL DEFAULT FALSE` column). Every status-changing path
  goes through one check in `core/objects.py`, including CSV import of an existing aliquot if import
  can update.
- The global `aliquot.status` CHECK is dropped. Generated `tm check` rules replace it:
  (1) every aliquot's status is in its stock kind's set; (2) every unforced `status` event's
  `old_status → new_status` is allowed. Generate the SQL from the schema (a VALUES list or CASE over
  stock kinds); no per-ledger hand-writing.
- Views: replace ticket 23's `aliquots_stored`, `quantity_stored` and five-value `status` in every
  `v_stock_<kind>`, `v_stock_status` and `v_box_slots` with `aliquots_in_stock`, `aliquots_total`,
  `quantity_in_stock` + `unit` (NULL on mixed units) and `status` ∈ {`active`, `depleted`, `empty`}.
  Statuses that count as in stock come from the schema when the views are generated.
- `SPEC_VERSION = 4`. Migration `migration/spec/3-to-4/` (ticket 21's helper): add `forced`, drop the
  status CHECK, regenerate views and rules. Remove `statuses:` from `ledger.yaml` and from
  `ledger_meta.ledger_yaml` only when it equals `[pending, stored, missing, consumed]`; otherwise refuse
  and explain the conversion in the README. Also refuse (listing rows) if any aliquot's status isn't in
  its stock kind's set.

## Files

- `src/tetramark/schema/{model.py,loader.py,ddl.py,diff.py}`, `src/tetramark/core/{ledger.py,objects.py,events.py}`,
  `src/tetramark/cli/{aliquot.py,ledger.py,schema.py}`, `src/tetramark/ops/{io_csv.py,history.py,render.py}` (as needed)
- `presets/*.yaml` (`statuses:` on every stock kind, the three sets in `base`, version bumps)
- `migration/spec/3-to-4/`, `migration/presets/<each changed preset>/N-to-N+1/`
- tests (new: status-set loading errors, transition allow/deny/force, check rules, views, 3-to-4)
- `docs/spec.md` (status vocabulary, aliquot table, views, `tm aliquot status`), `skills/tetramark/SKILL.md`, `README.md`

## Constraints

- Ticket 23's view columns are delivered first; this ticket renames them. Don't keep the old names as
  aliases.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers.

## Verify

- A stock kind without `statuses:` fails to load; so does a `to:` pointing outside its set and a
  duplicate set name.
- `tube`: stored → consumed works; consumed → stored is refused; the same with `--force --note x` works
  and the event has `forced = TRUE`; `--force` without `--note` is a usage error.
- Creating an aliquot directly as `consumed` works.
- An event inserted by hand that breaks a rule shows up as FAIL in `tm check`.
- `v_stock_<kind>`: 2 `stored` (1 ml, 0.5 ml) + 1 `consumed` → `aliquots_in_stock=2, aliquots_total=3,
  quantity_in_stock=1.5, unit='ml', status='active'`; all consumed → `depleted`; none → `empty`.
- `spec/3-to-4` on a spec-3 fixture with the default list drops the key, adds `forced` and passes
  `tm check`. A custom list refuses with nothing written.
