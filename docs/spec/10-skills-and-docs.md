# 10. Skills & docs

- `skills/tetramark` — operating manual: concepts, find/query recipes, add/move/use/status flows, check,
  history, what not to do (raw SQL writes, hand-editing dolt_tests). Chinese trigger words in description
  (台账, 样品, 盒, 冻存, 库存, 管).
- `skills/tetramark-schema` — writing presets/kinds/rules, schema plan/apply, destructive changes.
- [Migrating a legacy ledger](../guides/migrating-a-legacy-ledger.md) — rulebook for moving an old zsqlab DB: map old tables → kinds, keep old UIDs as
  explicit IDs (or aliases when they don't fit), `box/row/col` → box name + slot label, `status` 三态 →
  the fixed four-status set (`pending`/`stored`/`missing`/`consumed`; see
  `migration/spec/5-to-6/README.md`), pellet aliquots → aliquots with quantity, export old tables to CSV → `tm import`,
  checklist (row counts per kind, `tm check` all PASS, spot-check `box show` vs old grid views).
- [Upgrading 0.4.2 → 0.5.0](../guides/upgrading-0.4-to-0.5.md) — the workspace removal in practice:
  `cd` into the ledger directory, the P4 stale-`workspace.yaml` pointer, author and backup changes.
- [Upgrading 0.5.0 → 0.5.1](../guides/upgrading-0.5.0-to-0.5.1.md) — the full-word subcommands: `ls` → `list`,
  `rm` → `remove`, `tm backup --ls` → `tm backup --list`. A hard break, CLI-only, no ledger step.
- [Upgrading 0.5.1 → 0.6.0](../guides/upgrading-0.5.1-to-0.6.0.md) — the interactive `tm <obj> add-form`
  commands and the `long: true` preset key: CLI/preset metadata only, no ledger or migration step.
- [Releasing](../guides/releasing.md) — the tag is not a step: `.github/workflows/release.yml` tags the
  merge commit and publishes the release when a version bump lands on `main`. The files that carry
  the version, including both skill frontmatters (checked by
  [`scripts/release_check.py`](../../scripts/release_check.py)), the
  [notes file](../releases/README.md), backfilling a forgotten tag, and what the workflow refuses.
- [Test harness](../guides/test-harness.md) — embedded-only cost model, session ledger templates, the
  fast/full lanes and the wall-time budget enforced by [`scripts/test-lanes.sh`](../../scripts/test-lanes.sh).

## Skill versioning

A shipped skill promises "this text describes the CLI you are running", so it carries that promise
as data. The version **is** the Tetramark package version in `pyproject.toml`; there is no separate
skill revision and no `rev:` field. Both `skills/tetramark/SKILL.md` and
`skills/tetramark-schema/SKILL.md` declare it in their frontmatter, together with the spec chapter
the text mirrors and the date it was stamped:

```yaml
version: "0.8.26"      # == [project] version; re-stamped by every release bump
spec: "06-cli"         # tetramark-schema says "02-schema-language"
updated: 2026-11-05    # ISO date of the release commit; a date, never compared with today
```

The version is quoted because YAML would read an unquoted `0.9` as a float. The first body line is
the same claim in visible form, so an agent that pasted a stale copy knows it before its first
command fails:

    > Tetramark 0.8.26 skill — if `tm --version` differs, use the copy at `tm skill path`.

`scripts/release_check.py` reads the field from the frontmatter only (a skill body may contain
`version:` examples) with the regex `^version:\s*"?([^"\s]+)"?\s*$`, and refuses to tag when either
skill is missing a version or disagrees with `pyproject.toml`; the same guard runs in the fast lane.
`src/tetramark/skills.py` is the single place that resolves a skill copy
(`$TETRAMARK_SKILLS_DIR`, then the checkout's `skills/`, then installed package data) and classifies
it against `tm --version`.

Documentation entry points: [the spec](../spec/README.md) (this document), [guides](../guides/),
[records](../records/) of deliveries and real migrations, [release notes](../releases/) per tag, and open
[design issues](../issues/) for planned work.
