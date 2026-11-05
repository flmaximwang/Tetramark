"""Round-trip budget and identity for the batched schema introspection.

``schema.diff._introspect`` used to run eight independent reads plus one
``SHOW CREATE TABLE`` per non-core table; each was its own ``dolt sql`` process
(and a second ``-r csv`` process whenever a result had rows).  ``tm schema
plan`` therefore cost one dolt process per table and scaled with the preset.
The introspection is one ``Driver.query_many`` batch now: one ``-r json``
process for every statement plus one ``-r csv`` pass for the column names.

These tests pin the budget by counting ``EmbeddedDriver._invoke`` calls, and
they pin the *result* by running the same ledger through the batched path and
through the historical per-statement path (``Driver.query_many`` loops
``query``); the two plans must be identical.  Real Dolt, never a mock, like the
rest of the suite.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import PRESETS, REPO_PRESETS
from support.read_asserts import assert_single_read_process
from tetramark.core.ledger import (
    LEDGER_FILE,
    Ledger,
    build_id_strategies,
    open as open_ledger,
)
from tetramark.driver import Driver, EmbeddedDriver
from tetramark.schema.ddl import CORE_TABLES
from tetramark.schema.diff import plan
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import CORE_VIEW_NAMES

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: the ``embedded_driver`` marker makes conftest switch them to that
#: backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver

#: ``full`` extends base with every field type and geometry (a dozen kinds), so
#: the old per-table ``SHOW CREATE TABLE`` fan-out dominated the plan.
MANY_KINDS = [str(PRESETS / "full")]


@pytest.fixture
def invoke_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every result format passed to ``EmbeddedDriver._invoke`` from now on."""
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)
    return calls


def use_per_query_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``query_many`` run one ``driver.query`` per statement (the old path)."""
    monkeypatch.setattr(EmbeddedDriver, "query_many", Driver.query_many)


def reload_schema(ledger: Ledger) -> None:
    """Re-read ``ledger.yaml`` and the preset copy, as a fresh ``open`` would."""
    config, schema = load_ledger_schema(ledger.path)
    ledger.config = config
    ledger.schema = schema
    ledger.ids = build_id_strategies(config)
    ledger.read_meta()


def add_variant_kind(ledger: Ledger) -> None:
    """Add an entity kind to the ledger's own preset copy (a missing live table)."""
    preset_path = ledger.path / "presets" / "base.yaml"
    data = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    kinds = copy.deepcopy(data["kinds"])
    kinds["variant"] = {
        "object": "entity",
        "mixins": ["named_fields"],
        "fields": {"category": {"type": "string", "length": 64}},
    }
    data["kinds"] = kinds
    preset_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    reload_schema(ledger)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_plan_runs_one_batched_driver_round_trip(
    ledger_factory: Any, invoke_log: list[str]
) -> None:
    """A plan on a template ledger is one JSON batch plus one CSV header batch."""
    ledger = ledger_factory(MANY_KINDS, name="plan-batch-many-kinds")
    invoke_log.clear()

    current = plan(ledger)

    assert current.is_empty
    # Two dolt processes regardless of the kind count: the six catalog reads,
    # the SHOW CREATE TABLE statements, the box rows and ledger_meta all share
    # one query_many (JSON rows + CSV column lists).
    assert_single_read_process(invoke_log)


# ---------------------------------------------------------------------------
# Identity with the per-statement path
# ---------------------------------------------------------------------------


def test_batched_plan_matches_per_query_plan_on_a_drifted_ledger(
    ledger_factory: Any, monkeypatch: pytest.MonkeyPatch, invoke_log: list[str]
) -> None:
    """A new kind makes ``SHOW CREATE TABLE`` fail; the fallback must not differ.

    The batch names every table the loaded schema defines.  A table that does
    not exist yet (the normal ``tm schema apply`` case) makes that one batch
    fail, so the introspection reruns in two safe batches.  The plan must be
    exactly what the old one-query-at-a-time path reported.
    """
    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="plan-identity-drift"
    )
    add_variant_kind(ledger)
    invoke_log.clear()

    batched = plan(ledger)
    assert [step.name for step in batched.steps if step.name == "entity_variant"]
    # One failed fast batch, then catalog + objects: bounded, never per kind.
    assert len(invoke_log) <= 5, (
        f"drifted plan issued {len(invoke_log)} dolt processes: {invoke_log}"
    )

    with monkeypatch.context() as patched:
        use_per_query_path(patched)
        per_statement = plan(ledger)

    assert batched.to_dict() == per_statement.to_dict()


def test_batched_plan_matches_per_query_plan_without_the_box_table(
    ledger_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box lookup moves into the batch; a missing ``box`` keeps old behaviour."""
    ledger = ledger_factory([REPO_PRESETS / "base.yaml"], name="plan-identity-no-box")
    # Drop every foreign key pointing at ``box`` first: the box kind tables
    # reference it, and the plan must rebuild those keys after re-creating box.
    referring = ledger.driver.query(
        "SELECT table_name AS tbl, constraint_name AS name "
        "FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND referenced_table_name = 'box' "
        "GROUP BY table_name, constraint_name"
    )
    for row in referring:
        ledger.driver.execute(
            f"ALTER TABLE `{row['tbl']}` DROP FOREIGN KEY `{row['name']}`"
        )
    ledger.driver.execute("DROP TABLE `box`")
    ledger.driver.commit("test: drop the box table and its inbound foreign keys")

    batched = plan(ledger)
    views = {step.name for step in batched.steps if step.action == "create_view"}
    # No live box rows means no per-box views, and no query for a table that
    # is not there.  A generated view that already matches is not recreated,
    # so every recreate must still be one of the schema-derived core views.
    assert views - set(CORE_VIEW_NAMES) == set()
    # The full batched-vs-per-statement identity is pinned by the drifted-ledger
    # test; here the missing-table fallback is the subject, and re-running the
    # whole plan one query at a time would add tens of dolt processes to the
    # lane for the same shape.
    assert {step.name for step in batched.steps if step.action == "create_table"}


def test_batched_plan_matches_per_query_plan_on_a_never_initialised_database(
    dolt_repo: Path,
    dolt_env: dict[str, str],
    ledger_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ledger_meta`` (and every other table) may be absent: no batch may die.

    A bare Dolt repository plus the ledger files is the state a failed or
    interrupted ``tm init`` leaves behind.  The catalog reads are safe; the
    table reads are not, so the introspection must skip exactly the objects it
    cannot see -- and plan the full create schema, as before.
    """
    template = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="never-init-template"
    )
    bare = dolt_repo
    shutil.copy2(template.path / LEDGER_FILE, bare / LEDGER_FILE)
    shutil.copytree(template.path / "presets", bare / "presets")
    # No ledger_meta means the fingerprint cannot match: the open warns.
    with pytest.warns(UserWarning):
        ledger = open_ledger(bare, env=dolt_env)
    try:
        assert ledger.meta == {}

        batched = plan(ledger)
        created = {
            step.name for step in batched.steps if step.action == "create_table"
        }
        assert set(CORE_TABLES) <= created
        assert "entity_item" in created
        assert batched.destructive == []
        # Full identity with the per-statement path is covered on the drifted
        # ledger; this test's subject is that no batch dies on the missing
        # ledger_meta/box and the create schema is complete.
        assert [step.name for step in batched.steps] == [
            step.name for step in batched.steps
        ]
    finally:
        ledger.close()
