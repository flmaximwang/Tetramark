# 08 · Validation → dolt_tests & `ledger check`
Depends on: 06  · Spec: §4, §3 (app-enforced list)

- `schema/tests_gen.py`: produce `dolt_tests` rows (test_name, test_group, test_query, assertion_type,
  assertion_comparator, assertion_value — check the exact Dolt 2.3.5 `dolt_tests` columns by inspecting a repo)
  for: core invariants (capacity/exclusivity, acyclic nesting via bounded-depth query, stored-in-slotted-box ⇒ slot,
  slotless capacity), per-kind patterns (use `REGEXP` in the test query; verify Dolt support), entity_kinds
  restriction, custom rules (`:info` group suffix for info severity). Each test has a detail query for `-v`.
- Install on init (hook into 06) and replace wholesale on schema apply (exposed function for 09).
- `ledger check [-v] [--group G] [--format]` via `dolt_test_run()`; exit 1 only on non-info FAIL.

Tests: each invariant has a passing and a failing fixture (violations created with raw SQL); info rules never
change exit code; `-v` lists offending ids.
