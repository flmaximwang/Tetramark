"""Pin lineage/history graph reads to a few batched driver round trips.

``tm lineage`` used to run one ``dolt sql`` process per core table, per
``lineage: true`` kind, per aliquot chunk and **per stock count view for the
whole ledger**; ``tm history`` paid one per resolve step, per diff trail, per
box-name and per alias read.  The batched paths resolve first and then read
only the kind table and the visited stocks (two/three ``query_many`` batches).

These tests pin that budget by counting ``EmbeddedDriver._invoke`` calls after
the fixture rows are built, and prove the batched answer is the answer the
per-statement code path returns: ``Driver.query_many``'s default
implementation is exactly one ``query`` per statement, so running the same
operation with that implementation substituted must produce equal results,
JSON payloads and text.  Real Dolt, never a mock, like the rest of the suite.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from conftest import isolated_env
from tetramark.core.ledger import Ledger, init_ledger, open as open_ledger
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.ops.history import history, history_rows, render_history_text
from tetramark.ops.lineage import lineage, render_dot, render_text

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: the ``embedded_driver`` marker makes conftest switch them to that
#: backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver

#: A minimal status set for the custom preset's stock kind (spec 4 requires
#: one): the bundled ``tube`` set, trimmed to the four statuses these tests use.
TUBE_SET = {
    "tube": {
        "pending": {"in_stock": False, "to": ["stored", "consumed"]},
        "stored": {"in_stock": True, "to": ["consumed"]},
        "consumed": {"in_stock": False},
    }
}

#: The template ledger's schema: two ``lineage: true`` entity fields (one to an
#: entity kind, one to a stock kind), a stock kind with a count view and a box
#: kind, so the old path paid a query per lineage kind and per count view.
PRESET: dict[str, Any] = {
    "preset": "lineage-batch",
    "status_sets": TUBE_SET,
    "kinds": {
        "design": {
            "object": "entity",
            "fields": {"name": {"type": "string"}},
        },
        "variant": {
            "object": "entity",
            "fields": {
                "name": {"type": "string"},
                "from_design": {"type": "ref", "to": "design", "lineage": True},
            },
        },
        "sample": {
            "object": "entity",
            "fields": {
                "name": {"type": "string"},
                "source_culture": {
                    "type": "ref",
                    "to": "culture",
                    "lineage": True,
                },
            },
        },
        "culture": {
            "object": "stock",
            "entity_kinds": ["design", "variant"],
            "statuses": "tube",
            "fields": {"strain": {"type": "string"}},
        },
        "grid9x9": {
            "object": "box",
            "geometry": {"type": "grid", "rows": "A-I", "cols": "1-9"},
        },
    },
}


@pytest.fixture(scope="module")
def template(
    tmp_path_factory: pytest.TempPathFactory, dolt_binary: str
) -> Iterator[tuple[Ledger, dict[str, str]]]:
    """One real ledger of the template schema with the chain the tests read.

    It is created once per module (a real ``init_ledger``, no mocks): the tests
    only read it, so they can share it and stay cheap.  The rows are the
    fixture's descent chain:

        design D0 ← variant V1 (lineage ref)
        design D0 ← culture C1 (box B1, one aliquot) ← culture C2
        culture C1 ← sample S1 (lineage ref to a stock kind)
        aliquot Q1 ← moved to B2 and used, aliased ``TUBE-1``
    """
    del dolt_binary  # the fixture only skips the module when dolt is missing
    root = tmp_path_factory.mktemp("lineage-batch")
    env = isolated_env(root / "dolt-root", root / "home")
    # This module pins the embedded driver's process budget.  Module-scoped
    # fixtures are set up before the per-test backend fixture can run, so the
    # backend is forced here instead of inheriting the session default.  The
    # fixture rows are setup, not the read under test: build them through the
    # server backend, then reopen the same ledger embedded.
    preset_path = root / "lineage-batch.yaml"
    preset_path.write_text(
        yaml.safe_dump(PRESET, sort_keys=False), encoding="utf-8"
    )
    ledger = init_ledger(
        root / "template",
        "lineage-batch",
        [preset_path],
        id_prefix="77",
        author="Batch Tests <batch@tetramark.invalid>",
        env={**env, "TETRAMARK_DRIVER": "server"},
    )
    ids = build_rows(ledger)
    ledger.close()
    ledger = open_ledger(
        root / "template", env={**env, "TETRAMARK_DRIVER": "embedded"}
    )
    try:
        yield ledger, ids
    finally:
        ledger.close()


def build_rows(ledger: Ledger) -> dict[str, str]:
    """The chain both operations walk (plus the events/commits history reads)."""
    design = ledger.entities.add("design", name="D0")
    variant = ledger.entities.add(
        "variant", name="V1", values={"from_design": design["id"]}
    )
    ledger.boxes.add("grid9x9", "B1")
    ledger.boxes.add("grid9x9", "B2")
    culture = ledger.stocks.add(
        "culture",
        entity=design["id"],
        values={"strain": "DH5a"},
        aliquots=1,
        box="B1",
        slots=("A1",),
        quantity="10",
        unit="ul",
    )
    aliquot = culture["aliquots"][0]
    ledger.aliases.add("aliquot", aliquot, "TUBE-1")
    child = ledger.stocks.add(
        "culture",
        entity=variant["id"],
        source_aliquot=aliquot,
        parent=culture["id"],
    )
    sample = ledger.entities.add(
        "sample", name="S1", values={"source_culture": culture["id"]}
    )
    ledger.aliquots.move(aliquot, box="B2", slot="B2")
    ledger.aliquots.use(aliquot, "4", "ul", purpose="split")
    return {
        "design": design["id"],
        "variant": variant["id"],
        "sample": sample["id"],
        "culture": culture["id"],
        "child": child["id"],
        "aliquot": aliquot,
    }


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


def per_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``query_many`` run one ``query`` per statement.

    This is the interface's historical one-process-per-statement path: the same
    statements and the same number of ``dolt sql`` processes as
    ``Driver.query_many``.  It runs on a small thread pool because the
    statements are independent; the assertions are about the resulting rows and
    error messages, not about wall time or execution order.
    """
    def per_query(
        self: EmbeddedDriver, statements: list[tuple[str, Any]], **kwargs: Any
    ) -> list[list[dict[str, Any]]]:
        def run(statement: tuple[str, Any]) -> list[dict[str, Any]]:
            return self.query(statement[0], statement[1], **kwargs)

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(run, statements))

    monkeypatch.setattr(EmbeddedDriver, "query_many", per_query)


def clear(log: list[str]) -> list[str]:
    log.clear()
    return log


# ---------------------------------------------------------------------------
# Process budgets
# ---------------------------------------------------------------------------


def test_lineage_batches_graph_owners_and_visited_counts(
    template: tuple[Ledger, dict[str, str]], invoke_log: list[str]
) -> None:
    """Graph, owners, then counts for the visited stocks only."""
    ledger, ids = template
    refs = [
        ids["variant"],  # entity root, both directions, lineage + stock counts
        "TUBE-1",  # alias resolution
        ids["culture"],  # stock root, --down walk
    ]
    for ref in refs:
        clear(invoke_log)
        result = lineage(ledger, ref)
        assert result.nodes, ref
        # One JSON batch + its CSV pass for each of up to three batches.
        assert len(invoke_log) <= 6 and len(invoke_log) % 2 == 0, (
            f"lineage {ref!r} issued {len(invoke_log)} dolt processes: {invoke_log}"
        )


def test_lineage_root_only_still_fits_the_budget(
    template: tuple[Ledger, dict[str, str]], invoke_log: list[str]
) -> None:
    """``--depth 0`` pays the graph + owner batches; counts have no stock to read."""
    ledger, ids = template
    clear(invoke_log)
    result = lineage(ledger, ids["variant"], up=False, down=True, depth=0)
    assert [node.id for node in result.nodes] == [ids["variant"]]
    assert len(invoke_log) <= 4


def test_history_batches_resolution_and_kind_diff(
    template: tuple[Ledger, dict[str, str]], invoke_log: list[str]
) -> None:
    """Resolution/events share one batch; the kind diff is one small batch."""
    ledger, ids = template
    refs = [
        ids["aliquot"],  # id resolution, events + aliquot diffs
        "TUBE-1",  # alias resolution
        "B1",  # box-name resolution, box history + aliquot box-move diffs
        ids["culture"],  # stock history (aliquot rows carry the stock id)
        ids["variant"],  # entity history (kind table diff)
    ]
    for ref in refs:
        clear(invoke_log)
        result = history(ledger, ref)
        assert result["id"], ref
        assert len(invoke_log) <= 4, (
            f"history {ref!r} issued {len(invoke_log)} dolt processes: {invoke_log}"
        )


# ---------------------------------------------------------------------------
# Output identity with the per-statement path
# ---------------------------------------------------------------------------


def test_lineage_output_matches_per_statement_reads(
    template: tuple[Ledger, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batched graph is the graph the per-query path builds."""
    ledger, ids = template
    refs = [ids["variant"], "TUBE-1", ids["culture"], ids["aliquot"]]
    expected = {ref: lineage(ledger, ref) for ref in refs}

    per_statement(monkeypatch)
    for ref in refs:
        actual = lineage(ledger, ref)
        assert actual == expected[ref], ref
        assert actual.as_payload() == expected[ref].as_payload(), ref
        assert json.dumps(actual.as_payload(), sort_keys=True) == json.dumps(
            expected[ref].as_payload(), sort_keys=True
        ), ref
        assert render_text(actual) == render_text(expected[ref]), ref
        assert render_dot(actual) == render_dot(expected[ref]), ref


def test_history_output_matches_per_statement_reads(
    template: tuple[Ledger, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batched answer is the per-query answer, JSON and text included."""
    ledger, ids = template
    refs = [ids["aliquot"], "TUBE-1", "B1", ids["culture"], ids["variant"]]
    expected = {ref: history(ledger, ref) for ref in refs}

    per_statement(monkeypatch)
    for ref in refs:
        actual = history(ledger, ref)
        assert actual == expected[ref], ref
        assert json.dumps(actual, sort_keys=True) == json.dumps(
            expected[ref], sort_keys=True
        ), ref
        assert history_rows(actual) == history_rows(expected[ref]), ref
        assert render_history_text(actual) == render_history_text(expected[ref]), ref


def test_resolution_errors_match_per_statement_reads(
    template: tuple[Ledger, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown and ambiguous refs raise the same error from both code paths."""
    ledger, ids = template
    # An alias text shared by an entity and a stock is ambiguous for both ops.
    ledger.aliases.add("entity", ids["design"], "DUP")
    ledger.aliases.add("stock", ids["culture"], "DUP")

    def messages(operation) -> list[str]:
        found = []
        for ref in ("no-such-object", "DUP"):
            with pytest.raises(Exception) as captured:  # noqa: B017 - text is the point
                operation(ledger, ref)
            found.append(str(captured.value))
        return found

    expected = messages(history) + messages(lineage)
    per_statement(monkeypatch)
    actual = messages(history) + messages(lineage)
    assert actual == expected
    assert any("unknown object" in message for message in actual)
    assert any("is ambiguous" in message for message in actual)
