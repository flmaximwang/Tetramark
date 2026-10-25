# Issue 001 · `tm upgrade`: move a ledger from any format version to any other

Status: open · Filed: 2026-10-25 · Depends on: version stamp in `ledger_meta` (v0.1.0)

## Problem

Tetramark will change how a ledger is stored over time: core tables, `ledger_meta` keys, generated views and
`dolt_tests` shapes. Every ledger records the format it was written in (`ledger_meta.spec_version`), plus the tool
version that wrote it (`tetramark_version`, `created_with`). Nothing reads those values yet. A newer `tm`
will happily write to an older ledger, an older `tm` to a newer one, and there is no way to bring a ledger
up to date.

## Proposal

1. **Format version is the only key.** `spec_version` is a monotonic integer, bumped only when the stored
   shape changes. Tool releases that don't change the shape need no upgrade step. `tetramark_version` is
   provenance only.
2. **Step registry.** `tetramark/upgrade/steps/` holds one module per transition `N → N+1`. Each one declares
   `from_version`, `to_version`, a description, `plan(ledger) -> [Statement]` (pure, previewable) and an
   optional `verify(ledger)`. Steps are data migrations plus DDL, written against the *old* shape.
3. **Path resolution.** `tm upgrade [--to N]` (default: the newest version the tool knows). It resolves the
   chain `current → … → N` and refuses if any link is missing. Downgrades (`N → N-1`) are supported only
   where a step ships `downgrade`; otherwise the command refuses and points to `dolt reset --hard <tag>`.
4. **One commit per step**, message `upgrade(ledger): spec N -> N+1`. After each step, `spec_version` and
   `tetramark_version` in `ledger_meta` are bumped. `--dry-run` prints the whole chain's SQL. Before the first
   step it creates the tag `pre-upgrade-spec-N` automatically. Respects `--branch`.
5. **Open-time guard.** `Ledger.open` compares `spec_version` with the tool's `SPEC_VERSION`:
   - ledger older → reads allowed with a warning; writes refused with "run `tm upgrade`";
   - ledger newer → reads and writes refused ("upgrade tm to ≥ the version in `tetramark_version`");
   - missing `spec_version` (not a tm ledger) → point to `tm init --adopt`.
6. **Non-transactional DDL.** Dolt can't roll back DDL inside a transaction. A failed step leaves the
   branch at the last good commit. `tm upgrade` reports that commit and the `dolt reset --hard` command.

## Acceptance

- Fixture ledgers per historical format version, checked in or generated, upgrade to the current version
  and pass `tm check`. A round-trip test runs per step that has a downgrade.
- `tm upgrade` on an up-to-date ledger prints "ledger is at spec N" and makes no commit.
- The guard tests cover older, newer and missing `spec_version`.
- The `docs/spec.md` section on format versions explains when to bump `SPEC_VERSION` and how to add a step.

## Out of scope

- Adopting databases that were never tm ledgers. That is `tm init --adopt`, introduced for the zsqlab10
  migration (`/Users/maxim/Documents/Lab/Dolt/migration/zsqlab10/SPEC.md`, Part A4).
- Preset version upgrades (`preset_versions`). Those stay with `tm schema plan/apply`.
