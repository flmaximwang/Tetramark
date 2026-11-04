"""The spec-6 fixed aliquot status set (Tetramark-epn.2).

Covers the new model end to end: the fixed four-status table and its
transitions, the DB ``CHECK`` that replaces ``core_aliquot_status_known``, the
generated transition rule (including the ``tm check`` tolerance for a manually
migrated ledger's old-vocabulary history), creation/status/move/use behaviour,
the legacy closure that must keep loading unchanged, and plan idempotency in
both models.

The fixture ``tests/data/presets/fixed`` declares no status vocabulary, so it
loads with the fixed model.  The legacy fixtures (``base`` and the repository
presets) stay untouched and keep proving backward compatibility.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import core_statements, schema_statements
from tetramark.schema.diff import apply, plan
from tetramark.schema.loader import load_schema
from tetramark.schema.model import (
    FIXED_STATUS_SET,
    FIXED_STATUSES,
    FIXED_TRANSITIONS,
    LedgerConfig,
    SPEC_VERSION,
    StatusModel,
)
from tetramark.schema.tests_gen import generate_tests

from conftest import PRESETS, REPO_PRESETS

FIXED = PRESETS / "fixed"
BASE = PRESETS / "base"
REPO_BASE = REPO_PRESETS / "base.yaml"

#: A manual 5-to-6 migration adds exactly this constraint (the README SQL).
MIGRATION_CHECK = (
    "ALTER TABLE `aliquot` ADD CONSTRAINT `ck_aliquot_status` "
    "CHECK (`status` IN ('pending', 'stored', 'missing', 'consumed'))"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fixed_ledger(ledger_factory, name: str = "fixed"):
    return ledger_factory([FIXED], name=name, id_prefix="77")


def new_item(ledger, name: str = "alpha"):
    return ledger.entities.add("item", name=name)


def events(ledger, aliquot_id: str) -> list[dict[str, Any]]:
    return ledger.driver.query(
        "SELECT `event`, `old_status`, `new_status`, `forced`, `note` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (aliquot_id,),
    )


def installed_tests(ledger) -> dict[str, dict[str, Any]]:
    rows = ledger.driver.query("SELECT * FROM `dolt_tests`")
    return {str(row["test_name"]): row for row in rows}


def check_cli(ledger) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return CliRunner().invoke(app, ["--ledger", str(ledger.path), "check"])


def rewrite_preset_to_fixed(ledger) -> None:
    """Simulate the manual README step: replace the stored presets (files).

    The legacy fixture's preset directory is rewritten in place: the top-level
    ``status_sets:`` mapping and every kind's ``statuses:`` key disappear, so
    the ledger's own copy loads with the fixed model.  Nothing else changes
    (same kinds, same fields, same boxes), which is exactly what the manual
    5-to-6 upgrade does before remapping ``aliquot.status``.
    """
    files = sorted((ledger.path / "presets").rglob("*.yaml"))
    assert files, "the ledger has no stored preset copies"
    for path in files:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payload.pop("status_sets", None)
        for kind in (payload.get("kinds") or {}).values():
            kind.pop("statuses", None)
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Pure model / loader / DDL generation (fast lane)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_fixed_fixture_loads_the_hardcoded_set() -> None:
    schema = load_schema(
        LedgerConfig(name="fixed", presets=[str(FIXED)]), base_dir=Path.cwd()
    )
    assert schema.status_model is StatusModel.fixed
    assert schema.uses_fixed_statuses
    assert schema.status_sets == {}
    # A stock kind declares nothing; the effective set is the core one.
    assert schema.kinds["lot"].status_set() is None
    assert schema.status_set("lot") is FIXED_STATUS_SET
    assert schema.statuses_for("lot") == list(FIXED_STATUSES)
    assert schema.status_set("item") is None


@pytest.mark.fast
def test_legacy_fixture_still_loads_its_per_kind_sets() -> None:
    schema = load_schema(
        LedgerConfig(name="legacy", presets=[str(BASE)]), base_dir=Path.cwd()
    )
    assert schema.status_model is StatusModel.legacy
    assert not schema.uses_fixed_statuses
    assert set(schema.status_sets) == {"tube", "cryo"}
    assert schema.statuses_for("lot") == [
        "pending",
        "stored",
        "missing",
        "consumed",
    ]
    assert schema.kinds["lot"].status_set() is not None
    assert schema.status_set("lot") is not FIXED_STATUS_SET


@pytest.mark.fast
def test_fixed_ddl_has_the_status_check_and_no_status_known_test() -> None:
    fixed = load_schema(
        LedgerConfig(name="fixed", presets=[str(FIXED)]), base_dir=Path.cwd()
    )
    statements = schema_statements(fixed)
    aliquot_sql = next(s.sql for s in statements if s.name == "aliquot")
    assert "CONSTRAINT `ck_aliquot_status` CHECK" in aliquot_sql
    assert "`status` IN ('pending', 'stored', 'missing', 'consumed')" in aliquot_sql

    names = {test.name for test in generate_tests(fixed)}
    assert "core_status_event_allowed" in names
    # Spec 6: the DB CHECK replaces the generated vocabulary test.
    assert "core_aliquot_status_known" not in names
    transition = next(
        test for test in generate_tests(fixed)
        if test.name == "core_status_event_allowed"
    )
    # The fixed rule is global (no per-kind CASE) and only judges events whose
    # two endpoints are fixed names, so a manual migration's old-vocabulary
    # history (e.g. alive>crossed) is not checked.
    assert "CASE st." not in transition.query
    assert "e.`old_status` IN ('pending', 'stored', 'missing', 'consumed')" in (
        transition.query
    )
    assert "e.`new_status` IN ('pending', 'stored', 'missing', 'consumed')" in (
        transition.query
    )
    assert "pending>stored" in transition.query
    assert "stored>pending" not in transition.query

    # The legacy shape is untouched: per-kind sets, no DB CHECK, and the
    # generated membership test as before.
    legacy = load_schema(
        LedgerConfig(name="legacy", presets=[str(BASE)]), base_dir=Path.cwd()
    )
    legacy_sql = next(
        s.sql for s in schema_statements(legacy) if s.name == "aliquot"
    )
    assert "ck_aliquot_status" not in legacy_sql
    legacy_names = {test.name for test in generate_tests(legacy)}
    assert "core_aliquot_status_known" in legacy_names
    # Bare ``core_statements()`` (no schema) keeps the legacy shape too.
    assert all(
        "ck_aliquot_status" not in statement.sql
        for statement in core_statements()
    )


@pytest.mark.fast
def test_fixed_in_stock_views_count_only_stored() -> None:
    schema = load_schema(
        LedgerConfig(name="fixed", presets=[str(FIXED)]), base_dir=Path.cwd()
    )
    views = {statement.name: statement.sql for statement in schema_statements(schema)}
    for name in ("v_stock_status", "v_stock_lot", "v_box_slots"):
        sql = views[name]
        assert "WHEN 'lot' THEN" in sql, name
        assert "a.`status` IN ('stored')" in sql, name
        # Only ``stored`` counts as in stock in the fixed model.
        for other in ("pending", "missing", "consumed"):
            assert other not in sql, (name, other)


# ---------------------------------------------------------------------------
# Real Dolt: the CHECK, creation and the fixed transition table
# ---------------------------------------------------------------------------


def test_fixed_check_bites_and_the_four_statuses_are_accepted(ledger_factory) -> None:
    ledger = fixed_ledger(ledger_factory, name="fixed-check")
    rows = ledger.driver.query(
        "SELECT constraint_name AS name FROM information_schema.table_constraints "
        "WHERE table_schema = DATABASE() AND table_constraints.constraint_type "
        "= 'CHECK' AND table_name = 'aliquot'"
    )
    assert "ck_aliquot_status" in {str(row["name"]) for row in rows}

    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    alq = stock["aliquots"][0]
    assert ledger.aliquots.show(alq)["status"] == "pending"
    # Every fixed status is accepted by the DB and by later writes/tests.
    for status in FIXED_STATUSES:
        ledger.driver.execute(
            "UPDATE `aliquot` SET `status` = %s WHERE `id` = %s", (status, alq)
        )
    ledger.driver.commit("test: cycle the four fixed statuses")

    # A word from the old per-kind vocabularies is now a DB error.
    with pytest.raises(LedgerError):
        ledger.driver.execute(
            "UPDATE `aliquot` SET `status` = 'alive' WHERE `id` = %s", (alq,)
        )
    with pytest.raises(LedgerError):
        ledger.driver.execute(
            "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
            "VALUES ('X.9', %s, 9, 'quarantined')",
            (stock["id"],),
        )


def test_fixed_creation_status_and_transitions(ledger_factory) -> None:
    ledger = fixed_ledger(ledger_factory, name="fixed-transitions")
    item = new_item(ledger)
    # Unplaced creation: any non-in-stock fixed status, default pending.
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    alq = stock["aliquots"][0]
    assert ledger.aliquots.show(alq)["status"] == "pending"
    # The only in-stock status is stored, so an unplaced stored tube is wrong.
    with pytest.raises(LedgerError, match="must be placed in a box"):
        ledger.stocks.add(
            "lot", entity=item["id"], aliquots=1, status="stored"
        )
    # An unknown vocabulary word is refused with the fixed choices.
    with pytest.raises(UsageError, match="choose from: pending, stored, missing, consumed"):
        ledger.stocks.add("lot", entity=item["id"], aliquots=1, status="alive")

    # pending -> consumed is allowed; consumed is final without --force.
    ledger.aliquots.status(alq, "consumed")
    with pytest.raises(LedgerError, match="final status"):
        ledger.aliquots.status(alq, "stored")
    forced = ledger.aliquots.status(
        alq, "missing", force=True, note="undo a wrong consumed"
    )
    assert forced["status"] == "missing"
    last = events(ledger, alq)[-1]
    assert last["event"] == "status" and bool(last["forced"]) is True

    # stored -> pending is never allowed, forced or not (--force without a
    # position then hits the in-stock placement rule, not the transition).
    shelf = ledger.boxes.add("shelf", "SHELF")
    ledger.aliquots.move(alq, box="SHELF")
    with pytest.raises(LedgerError, match="allowed next status"):
        ledger.aliquots.status(alq, "pending")
    # An in-stock status without a position breaks the placement invariant.
    ledger.aliquots.status(alq, "missing")
    with pytest.raises(LedgerError, match="no box position"):
        ledger.aliquots.status(alq, "stored")


def test_fixed_move_and_use_follow_the_fixed_set(ledger_factory) -> None:
    ledger = fixed_ledger(ledger_factory, name="fixed-move-use")
    item = new_item(ledger)
    shelf = ledger.boxes.add("shelf", "SHELF")
    rack = ledger.boxes.add("rack", "RACK")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=1,
        box="SHELF",
        quantity=10,
        unit="ul",
    )
    alq = stock["aliquots"][0]
    assert ledger.aliquots.show(alq)["status"] == "stored"

    # Moving an in-stock tube keeps the only in-stock status.
    moved = ledger.aliquots.move(alq, box="RACK", slot="top")
    assert moved["old_status"] == "stored" and moved["status"] == "stored"

    # Leaving stock clears the position; moving it back restores stored.
    missing = ledger.aliquots.status(alq, "missing")
    assert missing["status"] == "missing"
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None}]
    restored = ledger.aliquots.move(alq, box="RACK", slot="top")
    assert restored["old_status"] == "missing"
    assert restored["status"] == "stored"

    # A partial use keeps the tube stored; zero consumes it and clears the
    # position in the same write.
    partial = ledger.aliquots.use(alq, "4", "ul")
    assert partial["status"] == "stored"
    emptied = ledger.aliquots.use(alq, "6", "ul")
    assert emptied["status"] == "consumed"
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None}]
    with pytest.raises(LedgerError, match="already consumed"):
        ledger.aliquots.use(alq, "1", "ul")


# ---------------------------------------------------------------------------
# Real Dolt: tm check, transition rule and pre-migration history
# ---------------------------------------------------------------------------


def test_fixed_check_fails_a_real_transition_and_ignores_history(
    ledger_factory,
) -> None:
    ledger = fixed_ledger(ledger_factory, name="fixed-history")
    item = new_item(ledger)
    shelf = ledger.boxes.add("shelf", "SHELF")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="SHELF"
    )
    alq = stock["aliquots"][0]
    assert ledger.check().passed

    # A genuine unforced fixed-vocabulary violation fails the generated rule.
    ledger.driver.execute(
        "INSERT INTO `aliquot_event` "
        "(`aliquot_id`, `event`, `old_status`, `new_status`) "
        "VALUES (%s, 'status', 'stored', 'pending')",
        (alq,),
    )
    ledger.driver.commit("test: illegal fixed transition")
    ledger.read_meta()
    report = ledger.check()
    assert not report.passed
    assert "core_status_event_allowed" in {
        result.name for result in report.results if result.failed
    }

    # A manually migrated ledger keeps old-vocabulary history (untouched) and
    # records `migrated: <old>` events.  Neither may fail the fixed rule.
    ledger.driver.execute(
        "DELETE FROM `aliquot_event` WHERE `aliquot_id` = %s", (alq,)
    )
    ledger.driver.execute(
        "INSERT INTO `aliquot_event` "
        "(`aliquot_id`, `event`, `old_status`, `new_status`, `note`) VALUES "
        "(%s, 'status', 'alive', 'crossed', NULL), "
        "(%s, 'status', 'alive', 'stored', 'migrated: alive')",
        (alq, alq),
    )
    ledger.driver.commit("test: migrated history")
    ledger.read_meta()
    assert ledger.check().passed
    # ... and a forced event stays exempt as before.
    ledger.driver.execute(
        "INSERT INTO `aliquot_event` "
        "(`aliquot_id`, `event`, `old_status`, `new_status`, `forced`) "
        "VALUES (%s, 'status', 'stored', 'pending', TRUE)",
        (alq,),
    )
    ledger.driver.commit("test: forced fixed transition")
    ledger.read_meta()
    assert ledger.check().passed


# ---------------------------------------------------------------------------
# Real Dolt: old ledgers and the manual spec-5 -> spec-6 upgrade
# ---------------------------------------------------------------------------


def test_spec5_legacy_ledger_opens_with_the_hint_and_stays_green(
    ledger_factory, dolt_env: dict[str, str]
) -> None:
    """The tool is newer: the ledger warns, but nothing refuses or rewrites."""
    ledger = ledger_factory([REPO_BASE], name="spec5-legacy", id_prefix="77")
    # The stored copies are the legacy ones (base v4: status_sets tube/cryo/
    # animal).  Pretend the tool that wrote the ledger was spec 5.
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '5' WHERE `key` = 'spec_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = 'oldfingerprint' "
        "WHERE `key` = 'fingerprint'"
    )
    ledger.driver.commit("test: make the ledger look like spec 5")
    ledger.close()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reopened = open_ledger(ledger.path, env=dolt_env)
    try:
        messages = [str(item.message) for item in caught]
        hint = (
            f"ledger is spec 5, tm is spec {SPEC_VERSION}: follow "
            "migration/spec/5-to-6/README.md"
        )
        assert any(hint in text for text in messages), messages
        assert any(hint in text for text in reopened.warnings)
        assert any("fingerprint does not match" in text for text in messages)

        # Old presets keep their per-kind model; normal commands still work.
        assert reopened.schema.status_model is StatusModel.legacy
        item = reopened.entities.add("item", name="legacy-item")
        reopened.stocks.add(
            "lot", entity=item["id"], values={"supplier": "acme"}
        )
        assert reopened.meta["spec_version"] == "5"

        # Old stored tests are green and the plan has no SQL steps.
        assert reopened.check().passed
        current = plan(reopened)
        assert current.steps == []
        assert current.meta_stale  # the fingerprint still names the old tool
        assert reopened.schema.fingerprint() != "oldfingerprint"

        checked = check_cli(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output

        # Every ``tm`` command that opens the ledger emits the hint (a Python
        # warning on stderr, like the fingerprint notice).
        with warnings.catch_warnings(record=True) as cli_caught:
            warnings.simplefilter("always")
            hinted = CliRunner().invoke(
                app, ["--ledger", str(reopened.path), "check"]
            )
        assert hinted.exit_code == 0, hinted.output
        assert any(
            hint.split(" to upgrade")[0] in str(warning.message)
            for warning in cli_caught
        ), [str(warning.message) for warning in cli_caught]
    finally:
        reopened.close()


def test_manual_spec5_to_spec6_upgrade_then_plan_is_clean(
    ledger_factory, dolt_env: dict[str, str]
) -> None:
    """Replace the presets, add the CHECK, run apply, plan empty, check green."""
    ledger = ledger_factory([BASE], name="manual-upgrade", id_prefix="77")
    item = ledger.entities.add("item", name="upgrade-item")
    shelf = ledger.boxes.add("shelf", "SHELF")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="SHELF"
    )
    alq = stock["aliquots"][0]
    assert ledger.schema.status_model is StatusModel.legacy
    # History a manual migration leaves untouched: the current four names plus
    # events from the old vocabulary, and one `migrated: <old>` event.
    ledger.driver.execute(
        "INSERT INTO `aliquot_event` "
        "(`aliquot_id`, `event`, `old_status`, `new_status`, `note`) VALUES "
        "(%s, 'status', 'alive', 'crossed', NULL), "
        "(%s, 'status', 'alive', 'stored', 'migrated: alive')",
        (alq, alq),
    )
    # -- the manual README steps on the live ledger: CHECK and status remap --
    ledger.driver.execute(MIGRATION_CHECK)
    ledger.driver.execute(
        "UPDATE `aliquot` SET `status` = 'stored' WHERE `status` = 'stored'"
    )
    ledger.driver.commit("test: manual 5-to-6 upgrade")
    ledger.close()

    # The other manual step is a file edit: the stored copy loses the status
    # vocabulary and locally reopens with the fixed model.
    rewrite_preset_to_fixed(ledger)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        reopened = open_ledger(ledger.path, env=dolt_env)
    try:
        assert reopened.schema.status_model is StatusModel.fixed
        # The CHECK is already the desired one: no SQL step, only the stale
        # fingerprint/preset metadata (refreshed by apply).
        current = plan(reopened)
        assert current.steps == []
        assert current.meta_stale

        result = apply(reopened)
        assert result.applied is True
        assert result.commit_hash
        clean = plan(reopened)
        assert clean.steps == []
        assert not clean.meta_stale

        # apply regenerated the fixed tests: no vocabulary test, and the
        # historical old-vocabulary events do not fail the fixed transition
        # rule.
        tests = installed_tests(reopened)
        assert "core_status_event_allowed" in tests
        assert "core_aliquot_status_known" not in tests
        assert reopened.check().passed
        checked = check_cli(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
    finally:
        reopened.close()


def test_schema_plan_adds_the_missing_fixed_check(ledger_factory) -> None:
    """A fixed ledger without the DB CHECK gets it planned, then idempotency."""
    ledger = fixed_ledger(ledger_factory, name="plan-add-check")
    ledger.driver.execute("ALTER TABLE `aliquot` DROP CHECK `ck_aliquot_status`")
    ledger.driver.commit("test: drop the fixed status check")

    current = plan(ledger)
    steps = [(step.action, step.name) for step in current.steps]
    assert ("add_constraint", "aliquot.ck_aliquot_status") in steps, steps
    assert all(step.action == "add_constraint" for step in current.steps), steps

    result = apply(ledger)
    assert result.applied is True
    names = {
        str(row["name"])
        for row in ledger.driver.query(
            "SELECT constraint_name AS name FROM "
            "information_schema.table_constraints WHERE table_schema = "
            "DATABASE() AND constraint_type = 'CHECK' AND table_name = 'aliquot'"
        )
    }
    assert "ck_aliquot_status" in names
    clean = plan(ledger)
    assert clean.steps == [] and not clean.meta_stale
    assert ledger.check().passed


def test_schema_plan_is_empty_in_both_models(ledger_factory) -> None:
    """A fresh ledger of either model has nothing to plan (spec 6)."""
    fixed = fixed_ledger(ledger_factory, name="plan-fixed")
    current = plan(fixed)
    assert current.steps == []
    assert not current.meta_stale
    assert apply(fixed).applied is False

    legacy = ledger_factory([BASE], name="plan-legacy", id_prefix="77")
    current = plan(legacy)
    assert current.steps == []
    assert not current.meta_stale
    assert apply(legacy).applied is False


@pytest.mark.fast
def test_schema_show_reports_the_effective_fixed_set() -> None:
    """``tm schema show`` shows the fixed set as each stock kind's statuses."""
    from tetramark.cli.schema import schema_payload

    schema = load_schema(
        LedgerConfig(name="fixed", presets=[str(FIXED)]), base_dir=Path.cwd()
    )
    payload = schema_payload(schema)
    assert payload["status_sets"] == {}
    lot = payload["kinds"]["lot"]
    assert set(lot["statuses"]) == set(FIXED_STATUSES)
    assert lot["statuses"]["stored"]["in_stock"] is True
    assert lot["statuses"]["consumed"]["to"] == []
    # Entity kinds have no status vocabulary at all.
    assert "statuses" not in payload["kinds"]["item"]


@pytest.mark.fast
def test_fixed_model_transitions_are_the_spec_table() -> None:
    """Pin the table from docs/spec/01-concepts.md §1.1."""
    assert FIXED_STATUSES == ("pending", "stored", "missing", "consumed")
    assert FIXED_TRANSITIONS == {
        "pending": ("stored", "missing", "consumed"),
        "stored": ("missing", "consumed"),
        "missing": ("stored", "consumed"),
        "consumed": (),
    }
    assert FIXED_STATUS_SET.in_stock_names == ["stored"]
    assert FIXED_STATUS_SET.final_names == ["consumed"]
    assert FIXED_STATUS_SET.allows("pending", "stored")
    assert not FIXED_STATUS_SET.allows("stored", "pending")
    assert not FIXED_STATUS_SET.allows("consumed", "stored")


def test_legacy_ledger_stored_tests_are_not_rederived(ledger_factory) -> None:
    """``tm check`` runs the ledger's installed tests, not the tool's model.

    A spec-5 ledger (legacy presets) keeps its ``core_aliquot_status_known``
    test and passes it after the tool moves to spec 6; the tool never
    re-derives the vocabulary behind the ledger's back.
    """
    ledger = ledger_factory([BASE], name="legacy-tests")
    rows = installed_tests(ledger)
    assert "core_aliquot_status_known" in rows
    assert "core_status_event_allowed" in rows
    # The spec-5 stored copy says per-kind sets; a four-name tube status is
    # valid under it and the installed rule passes.
    item = ledger.entities.add("item", name="legacy-tested")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    shelf = ledger.boxes.add("shelf", "SHELF")
    ledger.aliquots.move(stock["aliquots"][0], box="SHELF")
    assert ledger.check().passed
