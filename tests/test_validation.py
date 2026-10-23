"""Validation: generated ``dolt_tests`` and ``tm check`` (ticket 08).

Every test runs against a real ``dolt`` binary.  Violations of the app-enforced
invariants are inserted with raw SQL, because the object commands of ticket 10
do not exist yet and because the point is exactly to bypass the checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tetramark.core.ledger import Ledger
from tetramark.schema.loader import load_ledger_schema, load_schema
from tetramark.schema.model import EffectiveSchema, LedgerConfig, Rule, RuleExpect
from tetramark.schema.tests_gen import (
    MAX_NESTING_DEPTH,
    generate_tests,
    install_tests,
    run_check,
)

from conftest import PRESETS

FULL = PRESETS / "full"
BASE = PRESETS / "base"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def installed_test_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_tests")[0]["n"])


def installed_test_rows(ledger: Ledger) -> list[dict[str, Any]]:
    return ledger.driver.query(
        "SELECT test_name, test_group, test_query, assertion_type, "
        "assertion_comparator, assertion_value FROM dolt_tests ORDER BY test_name"
    )


def add_everything(
    ledger: Ledger, entity_id: str = "e1", *, code: str | None = "ABC"
) -> None:
    ledger.driver.execute(
        "INSERT INTO entity (id, kind, name) VALUES (%s, 'everything', %s)",
        (entity_id, entity_id),
    )
    ledger.driver.execute(
        "INSERT INTO entity_everything (id, name, code) VALUES (%s, %s, %s)",
        (entity_id, entity_id, code),
    )


def add_item(ledger: Ledger, entity_id: str = "i1") -> None:
    ledger.driver.execute(
        "INSERT INTO entity (id, kind, name) VALUES (%s, 'item', %s)",
        (entity_id, entity_id),
    )
    ledger.driver.execute(
        "INSERT INTO entity_item (id, name) VALUES (%s, %s)", (entity_id, entity_id)
    )


def add_prep(ledger: Ledger, stock_id: str = "s1", entity_id: str = "e1") -> None:
    ledger.driver.execute(
        "INSERT INTO stock (id, kind, entity_id) VALUES (%s, 'prep', %s)",
        (stock_id, entity_id),
    )
    ledger.driver.execute("INSERT INTO stock_prep (id) VALUES (%s)", (stock_id,))


def add_box(
    ledger: Ledger,
    box_id: str = "b1",
    name: str = "B1",
    kind: str = "grid9x9",
    *,
    parent: str | None = None,
    parent_slot: str | None = None,
) -> None:
    ledger.driver.execute(
        "INSERT INTO box (id, name, kind, parent_box_id, parent_slot) "
        "VALUES (%s, %s, %s, %s, %s)",
        (box_id, name, kind, parent, parent_slot),
    )


def add_slots(ledger: Ledger, box_id: str = "b1", labels: tuple[str, ...] = ("A1",)) -> None:
    for ordinal, label in enumerate(labels, start=1):
        ledger.driver.execute(
            "INSERT INTO slot (box_id, label, ordinal) VALUES (%s, %s, %s)",
            (box_id, label, ordinal),
        )


def add_aliquot(
    ledger: Ledger,
    aliquot_id: str,
    seq: int,
    *,
    stock_id: str = "s1",
    box_id: str | None = None,
    slot: str | None = None,
    status: str = "stored",
) -> None:
    ledger.driver.execute(
        "INSERT INTO aliquot (id, stock_id, seq, box_id, slot, status) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (aliquot_id, stock_id, seq, box_id, slot, status),
    )


def failed_names(report) -> set[str]:
    return {result.name for result in report.failures}


def write_local_rules(ledger: Ledger, rules: dict[str, str]) -> Any:
    """Add a ``rules_dir`` to a ledger and reinstall its generated tests.

    ``tm init`` has no ``--rules-dir`` flag (spec §6); local rules are a
    ``ledger.yaml`` feature.  This helper mimics what ``schema apply`` will do
    in ticket 09: change the YAML, reload the schema, replace ``dolt_tests``.
    """
    directory = ledger.path / "rules"
    directory.mkdir(exist_ok=True)
    for filename, text in rules.items():
        (directory / filename).write_text(text, encoding="utf-8")

    config_path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["rules_dir"] = "rules/"
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    config, schema = load_ledger_schema(ledger.path)
    with ledger.write("refresh generated tests", verb="update", object="tests"):
        install_tests(ledger.driver, schema)
        # Keep ledger_meta truthful, as ``schema apply`` will (ticket 09).
        ledger.driver.execute(
            "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'fingerprint'",
            (schema.fingerprint(),),
        )
        ledger.driver.execute(
            "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
            (json.dumps(schema.preset_versions, sort_keys=True),),
        )
    ledger.config = config
    ledger.schema = schema
    return schema


INFO_RULE = """\
- id: LOCALINFO
  description: informational local rule (always reports the row count)
  severity: info
  sql: "SELECT COUNT(*) FROM entity_everything"
  expect: {single_value: 0}
"""

FAIL_RULE = """\
- id: LOCALFAIL
  description: failing local rule
  severity: fail
  sql: "SELECT COUNT(*) FROM entity_everything"
  expect: {single_value: 0}
"""


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> None:
    """Point the process's Dolt/Git config at tmp paths (CLI uses ``os.environ``)."""
    for key in (
        "DOLT_ROOT_PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.setenv(key, dolt_env[key])
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Installation on init
# ---------------------------------------------------------------------------


def test_init_installs_every_generated_test(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="installed")
    definitions = generate_tests(ledger.schema)
    rows = installed_test_rows(ledger)

    assert installed_test_count(ledger) == len(definitions)
    assert {row["test_name"] for row in rows} == {d.name for d in definitions}
    by_name = {row["test_name"]: row for row in rows}
    for definition in definitions:
        row = by_name[definition.name]
        assert row["test_group"] == definition.group
        assert row["test_query"] == definition.query
        assert row["assertion_type"] == definition.assertion_type
        assert row["assertion_comparator"] == definition.comparator
        assert row["assertion_value"] == str(definition.value)

    groups = {row["test_group"] for row in rows}
    assert "core" in groups
    assert "schema:everything" in groups
    assert "rules:preset" in groups
    assert "rules:preset:info" in groups
    # The install is part of the init commit: no dirty working set.
    assert ledger.driver.query("SELECT * FROM dolt_status") == []


def test_fresh_ledger_passes_and_core_groups_are_present(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="clean")
    report = ledger.check()

    assert report.passed
    assert report.count == len(generate_tests(ledger.schema))
    assert report.failures == []
    core = ledger.check(group="core")
    assert core.count == len(
        [d for d in generate_tests(ledger.schema) if d.group == "core"]
    )
    assert {result.group for result in core.results} == {"core"}


def test_group_filter_uses_group_prefixes(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="groups")
    definitions = generate_tests(ledger.schema)

    schema = ledger.check(group="schema")
    assert schema.count == len([d for d in definitions if d.group.startswith("schema")])
    rules = ledger.check(group="rules")
    assert rules.count == len([d for d in definitions if d.group.startswith("rules")])
    plasmid = ledger.check(group="schema:prep")
    assert {result.group for result in plasmid.results} == {"schema:prep"}
    empty = ledger.check(group="core:missing")
    assert empty.count == 0


def test_install_tests_replaces_the_whole_table(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="replaced")
    full_count = installed_test_count(ledger)

    base_schema = load_schema(LedgerConfig(name="base", presets=[str(BASE)]))
    with ledger.write("replace generated tests", verb="update", object="tests"):
        install_tests(ledger.driver, base_schema)

    rows = installed_test_rows(ledger)
    assert installed_test_count(ledger) == len(generate_tests(base_schema)) < full_count
    assert "rule_CH01" not in {row["test_name"] for row in rows}
    assert "schema_everything_pattern_code" not in {
        row["test_name"] for row in rows
    }


# ---------------------------------------------------------------------------
# Core invariants: one failing fixture each
# ---------------------------------------------------------------------------


def test_slot_capacity_violation_fails_with_details(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="capacity")
    add_everything(ledger)
    add_prep(ledger)
    add_box(ledger)  # grid9x9 has slot_capacity 2
    add_slots(ledger, labels=("A1",))
    for number in range(1, 4):
        add_aliquot(ledger, f"s1.{number}", number, box_id="b1", slot="A1")

    quiet = ledger.check(group="core")
    assert "core_slot_capacity" in failed_names(quiet)
    assert quiet.passed is False

    verbose = ledger.check(group="core", verbose=True)
    detail = next(r for r in verbose.results if r.name == "core_slot_capacity")
    assert detail.detail_rows
    assert detail.detail_rows[0]["box"] == "B1"
    assert detail.detail_rows[0]["slot"] == "A1"
    assert detail.detail_rows[0]["aliquots"] == 3
    # Without -v no detail query is run.
    plain = next(r for r in quiet.results if r.name == "core_slot_capacity")
    assert plain.detail_rows == []


def test_slot_exclusivity_violation_fails(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="exclusive")
    add_everything(ledger)
    add_prep(ledger)
    add_box(ledger)
    add_slots(ledger, labels=("A1",))
    add_aliquot(ledger, "s1.1", 1, box_id="b1", slot="A1")
    add_box(ledger, "b2", "B2", "grid9x9", parent="b1", parent_slot="A1")

    report = ledger.check(group="core", verbose=True)
    assert "core_slot_exclusive" in failed_names(report)
    detail = next(r for r in report.results if r.name == "core_slot_exclusive")
    assert detail.detail_rows[0]["box"] == "B1"
    assert detail.detail_rows[0]["slot"] == "A1"

    # A slot holds at most one child box as well.
    add_box(ledger, "b3", "B3", "grid9x9", parent="b1", parent_slot="A1")
    crowded = ledger.check(group="core", verbose=True)
    assert "core_slot_single_child_box" in failed_names(crowded)
    children = next(
        r for r in crowded.results if r.name == "core_slot_single_child_box"
    )
    assert children.detail_rows[0]["box"] == "B1"
    assert children.detail_rows[0]["slot"] == "A1"


def test_box_nesting_cycle_fails(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="cycle")
    add_box(ledger, "b1", "B1", "grid9x9")
    add_box(ledger, "b2", "B2", "grid9x9", parent="b1", parent_slot=None)
    ledger.driver.execute(
        "UPDATE box SET parent_box_id = 'b2' WHERE id = 'b1'"
    )

    report = ledger.check(group="core", verbose=True)
    assert "core_box_nesting_acyclic" in failed_names(report)
    detail = next(r for r in report.results if r.name == "core_box_nesting_acyclic")
    assert {row["box"] for row in detail.detail_rows} == {"B1", "B2"}


def test_bounded_acyclic_query_ignores_deep_non_cycles(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="deep")
    # A chain longer than the bound is not a cycle and must keep passing.
    depth = MAX_NESTING_DEPTH + 3
    for index in range(depth):
        parent = f"b{index - 1}" if index else None
        add_box(
            ledger,
            f"b{index}",
            f"B{index}",
            "grid9x9",
            parent=parent,
            parent_slot=None,
        )
    report = ledger.check(group="core")
    assert report.passed


def test_stored_aliquot_in_slotted_box_requires_a_slot(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="stored")
    add_everything(ledger)
    add_prep(ledger)
    add_box(ledger)
    add_aliquot(ledger, "s1.1", 1, box_id="b1", slot=None, status="stored")

    report = ledger.check(group="core", verbose=True)
    assert "core_stored_aliquot_slot" in failed_names(report)
    detail = next(r for r in report.results if r.name == "core_stored_aliquot_slot")
    assert detail.detail_rows[0]["aliquot_id"] == "s1.1"


def test_slotless_capacity_counts_aliquots_and_child_boxes(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="slotless")
    assert ledger.schema.kinds["slotlessbox"].geometry.capacity == 5

    add_everything(ledger)
    add_prep(ledger)
    add_box(ledger, "b1", "B1", "slotlessbox")
    for number in range(1, 4):
        add_aliquot(ledger, f"s1.{number}", number, box_id="b1", slot=None)
    for index in range(2):
        add_box(
            ledger,
            f"c{index}",
            f"C{index}",
            "grid9x9",
            parent="b1",
            parent_slot=None,
        )
    # Three aliquots + two child boxes fit in capacity 5.
    assert ledger.check(group="core").passed

    add_box(ledger, "c2", "C2", "grid9x9", parent="b1", parent_slot=None)
    report = ledger.check(group="core", verbose=True)
    assert "core_slotless_capacity_slotlessbox" in failed_names(report)
    detail = next(
        r for r in report.results if r.name == "core_slotless_capacity_slotlessbox"
    )
    assert detail.detail_rows[0]["aliquots"] == 3
    assert detail.detail_rows[0]["child_boxes"] == 3


# ---------------------------------------------------------------------------
# Per-kind schema tests
# ---------------------------------------------------------------------------


def test_pattern_test_accepts_valid_and_rejects_invalid_values(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="pattern")
    add_everything(ledger, "e1", code="ABC")
    assert ledger.check(group="schema:everything").passed

    ledger.driver.execute(
        "UPDATE entity_everything SET code = 'abc' WHERE id = 'e1'"
    )
    report = ledger.check(group="schema:everything", verbose=True)
    assert "schema_everything_pattern_code" in failed_names(report)
    detail = next(
        r for r in report.results if r.name == "schema_everything_pattern_code"
    )
    assert detail.detail_rows[0]["id"] == "e1"
    assert detail.detail_rows[0]["value"] == "abc"


def test_entity_kinds_restriction_is_enforced(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="kinds")
    # bacterial accepts item and plasmid.
    add_item(ledger, "i1")
    ledger.driver.execute(
        "INSERT INTO stock (id, kind, entity_id) VALUES ('s1', 'bacterial', 'i1')"
    )
    ledger.driver.execute(
        "INSERT INTO stock_bacterial (id, strain) VALUES ('s1', 'K12')"
    )
    assert ledger.check(group="schema:bacterial").passed

    # 'everything' is not in bacterial's entity_kinds; the discriminator alone
    # allows a raw insert, so the generated test must catch it.
    add_everything(ledger, "e1")
    ledger.driver.execute(
        "INSERT INTO stock (id, kind, entity_id) VALUES ('s2', 'bacterial', 'e1')"
    )
    ledger.driver.execute(
        "INSERT INTO stock_bacterial (id, strain) VALUES ('s2', 'K12')"
    )

    report = ledger.check(group="schema:bacterial", verbose=True)
    assert "schema_bacterial_entity_kinds" in failed_names(report)
    detail = next(
        r for r in report.results if r.name == "schema_bacterial_entity_kinds"
    )
    assert detail.detail_rows[0]["stock_id"] == "s2"
    assert detail.detail_rows[0]["entity_kind"] == "everything"


# ---------------------------------------------------------------------------
# Rules and informational failures
# ---------------------------------------------------------------------------


def test_rule_expectations_map_to_dolt_assertions_and_groups() -> None:
    schema = EffectiveSchema(
        rules=[
            Rule(
                id="ONE",
                description="one value",
                sql="SELECT 1",
                expect=RuleExpect(single_value=0),
            ),
            Rule(
                id="EXACT",
                description="exactly no rows",
                sql="SELECT 1",
                expect=RuleExpect(rows=0),
            ),
            Rule(
                id="AT_MOST",
                description="at most two rows",
                severity="info",
                sql="SELECT 1",
                expect=RuleExpect(rows_at_most=2),
            ),
        ],
        rule_sources={"ONE": "preset", "EXACT": "local", "AT_MOST": "local"},
    )
    tests = {test.name: test for test in generate_tests(schema)}
    assert (
        tests["rule_ONE"].assertion_type,
        tests["rule_ONE"].comparator,
        tests["rule_ONE"].value,
        tests["rule_ONE"].group,
    ) == ("expected_single_value", "==", 0, "rules:preset")
    assert (
        tests["rule_EXACT"].assertion_type,
        tests["rule_EXACT"].comparator,
        tests["rule_EXACT"].value,
        tests["rule_EXACT"].group,
    ) == ("expected_rows", "==", 0, "rules:local")
    assert (
        tests["rule_AT_MOST"].assertion_type,
        tests["rule_AT_MOST"].comparator,
        tests["rule_AT_MOST"].value,
        tests["rule_AT_MOST"].group,
    ) == ("expected_rows", "<=", 2, "rules:local:info")


def test_empty_entity_kinds_forbids_the_stock_kind() -> None:
    from tetramark.schema.model import Kind

    schema = EffectiveSchema(
        kinds={"orphan": Kind(object="stock", entity_kinds=[])},
    )
    tests = {test.name: test for test in generate_tests(schema)}
    test = tests["schema_orphan_entity_kinds"]
    assert "NOT IN" not in test.query
    assert "'orphan'" in test.query


def test_rule_groups_distinguish_preset_and_local_and_info(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="rule-groups")
    schema = write_local_rules(ledger, {"local.yaml": INFO_RULE + FAIL_RULE})

    assert schema.rule_sources["CH01"] == "preset"
    assert schema.rule_sources["LOCALINFO"] == "local"
    rows = {row["test_name"]: row["test_group"] for row in installed_test_rows(ledger)}
    assert rows["rule_CH01"] == "rules:preset"
    assert rows["rule_CH02"] == "rules:preset:info"
    assert rows["rule_LOCALINFO"] == "rules:local:info"
    assert rows["rule_LOCALFAIL"] == "rules:local"
    # Reinstall replaced only generated state; it did not duplicate rows.
    assert len(rows) == len(generate_tests(schema))


def test_info_rule_is_reported_but_never_blocks(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="info")
    add_everything(ledger)  # makes the local info rule report 1 row
    write_local_rules(ledger, {"local.yaml": INFO_RULE})

    report = ledger.check()
    assert report.passed is True
    assert [result.name for result in report.info_failures] == ["rule_LOCALINFO"]
    assert report.blocking_failures == []
    # The test really did fail; it is only demoted by its group suffix.
    info = report.info_failures[0]
    assert info.status.upper() == "FAIL"
    assert info.is_info


def test_local_fail_rule_blocks(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="local-fail")
    add_everything(ledger)
    write_local_rules(ledger, {"local.yaml": FAIL_RULE})

    report = ledger.check()
    assert report.passed is False
    assert [result.name for result in report.blocking_failures] == ["rule_LOCALFAIL"]
    assert report.blocking_failures[0].group == "rules:local"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_check_exit_codes_formats_and_verbose(ledger_factory, cli_env) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    ledger = ledger_factory([FULL], name="cli-check")
    base = [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        "cli-check",
        "check",
    ]
    runner = CliRunner()

    ok = runner.invoke(app, base)
    assert ok.exit_code == 0, ok.output
    assert "OK:" in ok.output

    as_json = runner.invoke(app, [*base, "--json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["ok"] is True
    assert payload["count"] == len(generate_tests(ledger.schema))

    core = runner.invoke(app, [*base, "--group", "core", "--json"])
    assert core.exit_code == 0, core.output
    core_payload = json.loads(core.output)
    assert {test["group"] for test in core_payload["tests"]} == {"core"}

    csv_output = runner.invoke(app, [*base, "--format", "csv"])
    assert csv_output.exit_code == 0, csv_output.output
    assert csv_output.output.splitlines()[0] == "status,group,name,message"

    add_everything(ledger, "e1", code="ABC")
    ledger.driver.execute("UPDATE entity_everything SET code = 'nope' WHERE id = 'e1'")
    broken = runner.invoke(app, [*base, "-v"])
    assert broken.exit_code == 1, broken.output
    assert "FAILED:" in broken.output
    assert "schema_everything_pattern_code" in broken.output
    assert "e1" in broken.output  # -v prints the offending id

    broken_csv = runner.invoke(app, [*base, "--format", "csv", "-v"])
    assert broken_csv.exit_code == 1, broken_csv.output
    assert broken_csv.output.splitlines()[0] == "status,group,name,message,details"
    assert "e1" in broken_csv.output


def test_cli_info_failure_does_not_change_the_exit_code(ledger_factory, cli_env) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    ledger = ledger_factory([FULL], name="cli-info")
    add_everything(ledger)
    write_local_rules(ledger, {"local.yaml": INFO_RULE})

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            "cli-info",
            "check",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["info_failed"] == 1


def test_check_python_api_matches_the_generated_tests(ledger_factory) -> None:
    """``Ledger.check`` reports one result per installed test and stays read-only."""
    ledger = ledger_factory([FULL], name="api")
    report = ledger.check(group="core")
    assert report.count == len(
        [d for d in generate_tests(ledger.schema) if d.group == "core"]
    )
    assert isinstance(report.as_dict(), dict)
    assert ledger.driver.query("SELECT * FROM dolt_status") == []
