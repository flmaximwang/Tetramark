# 14 · Skills, migration rulebook, README
Depends on: 07, 08, 09, 11, 12  · Spec: §10

- `skills/tetramark/SKILL.md` (+ `references/commands.md`, `references/recipes.md`): frontmatter `name`,
  `description` starting "Use when …" incl. Chinese triggers (台账, 样品, 盒, 冻存, 库存, 管, 分装). Body: concepts,
  decision table (which command for which intent), recipes (register a new plasmid + glycerol stock in a box, use part
  of a pellet, move a box, find where X is, audit), guardrails (no raw SQL writes, never edit dolt_tests, always
  `--dry-run` first for bulk). Progressive disclosure: SKILL.md ≤150 lines.
- `skills/tetramark-schema/SKILL.md` (+ references): authoring presets/kinds/fields/geometry/rules, plan/apply,
  destructive changes, examples from `library` and `molbio`.
- `docs/migration.md`: rulebook per spec §10 with a worked mapping for zsqlab08 (tables in the old README) — doc only.
- `README.md`: install (`uv tool install .`), quickstart (workspace init → server start → init with molbio → add →
  box show → check), links.

Verify every command in the docs actually runs (add `tests/test_docs_commands.py` that executes the quickstart).
