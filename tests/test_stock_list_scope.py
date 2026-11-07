"""``tm stock list`` defaults to active stocks; ``--all`` widens the scope.

A stock's derived ``status`` is ``empty`` when it has no aliquots, ``depleted``
when every aliquot has left stock (a tube that was never moved into a box is
*not* in stock) and ``active`` while at least one aliquot is still in stock.
The default listing prints only the ``active`` rows; ``--all`` adds the empty
and depleted ones.  ``ledger.stocks.ls()`` mirrors the default and
``ledger.stocks.ls(all_statuses=True)`` mirrors ``--all``.

The fixture is one ledger with every status present in TWO stock kinds
(``lot`` and ``bacterial``), plus a third kind (``prep``) whose stocks are all
empty or depleted.  The entity and box listings have no availability notion and
keep printing every row, so they are pinned here too.

``status`` is a derived view column, not a ``--where`` field: the usage error
for ``--where status=active`` is pinned with and without ``--all``.

Everything that opens the ledger is real Dolt and stays in the full lane; only
the help-text test is Dolt-free and carries the ``fast`` marker.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import Ledger, init_ledger
from tetramark.errors import UsageError

from conftest import FACTORY_AUTHOR, PRESETS, isolated_env

#: The three stock kinds the ``full`` preset declares: ``bacterial`` includes
#: ``chain`` (and ``base``), ``lot`` comes from ``base``; ``prep`` is the third.
FULL = PRESETS / "full"

#: Explicit datecode ids keep the table and JSON row order stable run to run.
ACTIVE_LOT = "7701AB01"
EMPTY_LOT = "7701AB02"
DEPLETED_LOT = "7701AB03"
ACTIVE_BACTERIAL = "7701AC01"
EMPTY_BACTERIAL = "7701AC02"
DEPLETED_BACTERIAL = "7701AC03"
DEPLETED_PREP = "7701AD01"
EMPTY_PREP = "7701AD02"

#: Kinds are visited in sorted order (bacterial, lot, prep) and each kind's
#: rows keep the query's ``ORDER BY id``; the default scope drops the
#: non-active rows from that same order.
ACTIVE_IDS = [ACTIVE_BACTERIAL, ACTIVE_LOT]
ALL_IDS = [
    ACTIVE_BACTERIAL,
    EMPTY_BACTERIAL,
    DEPLETED_BACTERIAL,
    ACTIVE_LOT,
    EMPTY_LOT,
    DEPLETED_LOT,
    DEPLETED_PREP,
    EMPTY_PREP,
]
LOT_IDS = [ACTIVE_LOT, EMPTY_LOT, DEPLETED_LOT]
BACTERIAL_IDS = [ACTIVE_BACTERIAL, EMPTY_BACTERIAL, DEPLETED_BACTERIAL]
PREP_IDS = [DEPLETED_PREP, EMPTY_PREP]

STOCK_COLUMNS = [
    "id",
    "kind",
    "entity_id",
    "entity_name",
    "aliquots_in_stock",
    "status",
    "notes",
]


# ---------------------------------------------------------------------------
# The one fixture: active, empty and depleted stocks across two kinds
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scope_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """The isolated Dolt environment the module's ledger and CLI share."""
    root = tmp_path_factory.mktemp("stock-scope-env")
    return {
        **isolated_env(root / "dolt-root", root / "home"),
        # Module fixtures are set up before the per-test backend fixture runs;
        # pin the backend so the ledger and the CLI always agree on it.
        "TETRAMARK_DRIVER": "server",
    }


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, scope_env: dict[str, str]) -> None:
    """Run the CLI in the same isolated Dolt environment as the ledger."""
    for key, value in scope_env.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(scope="module")
def scope_ledger(
    tmp_path_factory: pytest.TempPathFactory,
    dolt_binary: str,
    scope_env: dict[str, str],
) -> Iterator[dict[str, Any]]:
    """One ledger with every status in ``lot`` and ``bacterial``.

    Built once per worker and only read afterwards, like the other read-only
    modules in the suite.  ``prep`` has only empty/depleted stocks, which
    makes it the natural "no active rows" listing.  The returned mapping
    carries the entity ids the ``--where`` core-column test needs.
    """
    del dolt_binary  # the fixture only skips the module when dolt is missing
    root = tmp_path_factory.mktemp("stock-scope")
    ledger = init_ledger(
        root / "ledger",
        "stock-scope",
        [FULL],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=scope_env,
    )
    rows = build_rows(ledger)
    try:
        yield rows
    finally:
        ledger.close()


def build_rows(ledger: Ledger) -> dict[str, Any]:
    """The fixture rows: every status in two kinds, and a kind without any."""
    item = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    thing = ledger.entities.add("everything", name="thing")
    ledger.boxes.add("grid96", "G-1")
    ledger.boxes.add("rack", "R-1")

    # lot: active (one of two tubes moved into G-1 A1), empty, depleted.
    lot_active = ledger.stocks.add(
        "lot",
        id=ACTIVE_LOT,
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=2,
    )
    ledger.aliquots.move(
        lot_active["aliquots"][0], box="G-1", slot="A1", message="moved"
    )
    ledger.stocks.add(
        "lot",
        id=EMPTY_LOT,
        entity=item["id"],
        values={"supplier": "solo"},
        aliquots=0,
    )
    ledger.stocks.add(
        "lot",
        id=DEPLETED_LOT,
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=1,
    )

    # bacterial: active, empty, depleted.
    bacterial_active = ledger.stocks.add(
        "bacterial",
        id=ACTIVE_BACTERIAL,
        entity=item["id"],
        values={"strain": "DH5a"},
        aliquots=1,
    )
    ledger.aliquots.move(
        bacterial_active["aliquots"][0], box="G-1", slot="B1", message="moved"
    )
    ledger.stocks.add(
        "bacterial",
        id=EMPTY_BACTERIAL,
        entity=item["id"],
        values={"strain": "BL21"},
        aliquots=0,
    )
    ledger.stocks.add(
        "bacterial",
        id=DEPLETED_BACTERIAL,
        entity=item["id"],
        values={"strain": "TOP10"},
        aliquots=1,
    )

    # prep: no active stock at all.
    ledger.stocks.add("prep", id=DEPLETED_PREP, entity=thing["id"], aliquots=1)
    ledger.stocks.add("prep", id=EMPTY_PREP, entity=thing["id"], aliquots=0)

    return {"ledger": ledger, "item": item["id"], "thing": thing["id"]}


def invoke(ledger, *args: str):
    """Run the CLI against one ledger."""
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def run_json(ledger, *args: str) -> list[dict]:
    """Run a list command with ``--json`` and return the payload."""
    result = invoke(ledger, *args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def ids(rows: list[dict]) -> list[str]:
    """The ids of the listed rows, in listing order."""
    return [str(row["id"]) for row in rows]


def table_ids(output: str) -> list[str]:
    """The first cell (the id) of every data line of a default table."""
    lines = output.splitlines()
    assert lines, "the table printed nothing"
    assert lines[0].split()[0] == "id", output
    return [line.split()[0] for line in lines[1:]]


def run_csv(ledger, *args: str) -> list[dict[str, str]]:
    """Run a list command with the global ``--format csv`` and parse it."""
    result = invoke(ledger, "--format", "csv", *args)
    assert result.exit_code == 0, result.output
    return list(csv.DictReader(io.StringIO(result.output)))


# ---------------------------------------------------------------------------
# Default scope: table, JSON and CSV all show only the active rows
# ---------------------------------------------------------------------------


def test_default_scope_table_json_and_csv_show_only_active_stocks(
    cli_env: None, scope_ledger
) -> None:
    ledger = scope_ledger["ledger"]

    table = invoke(ledger, "stock", "list")
    assert table.exit_code == 0, table.output
    assert table_ids(table.output) == ACTIVE_IDS
    assert table.output.splitlines()[0].split() == STOCK_COLUMNS

    default_rows = run_json(ledger, "stock", "list")
    assert ids(default_rows) == ACTIVE_IDS
    assert {row["status"] for row in default_rows} == {"active"}
    assert all(row["aliquots_in_stock"] > 0 for row in default_rows)

    csv_rows = run_csv(ledger, "stock", "list")
    assert ids(csv_rows) == ACTIVE_IDS
    assert {row["status"] for row in csv_rows} == {"active"}
    assert list(csv_rows[0]) == STOCK_COLUMNS


# ---------------------------------------------------------------------------
# --all: every status, and the row count is the whole stock table
# ---------------------------------------------------------------------------


def test_all_shows_every_stock_and_the_row_count_matches_the_table(
    cli_env: None, scope_ledger
) -> None:
    ledger = scope_ledger["ledger"]

    table = invoke(ledger, "stock", "list", "--all")
    assert table.exit_code == 0, table.output
    assert table_ids(table.output) == ALL_IDS

    rows = run_json(ledger, "stock", "list", "--all")
    assert ids(rows) == ALL_IDS
    assert {row["status"] for row in rows} == {"active", "empty", "depleted"}
    total = ledger.driver.query("SELECT COUNT(*) AS n FROM `stock`")[0]["n"]
    assert int(total) == len(rows) == len(ALL_IDS) == 8

    # Each row carries the status its aliquots derive.
    status = {row["id"]: row["status"] for row in rows}
    assert status == {
        ACTIVE_BACTERIAL: "active",
        EMPTY_BACTERIAL: "empty",
        DEPLETED_BACTERIAL: "depleted",
        ACTIVE_LOT: "active",
        EMPTY_LOT: "empty",
        DEPLETED_LOT: "depleted",
        DEPLETED_PREP: "depleted",
        EMPTY_PREP: "empty",
    }
    in_stock = {row["id"]: row["aliquots_in_stock"] for row in rows}
    assert in_stock[ACTIVE_BACTERIAL] == 1
    assert in_stock[ACTIVE_LOT] == 1
    assert all(
        in_stock[stock_id] == 0
        for stock_id in (
            EMPTY_BACTERIAL,
            DEPLETED_BACTERIAL,
            EMPTY_LOT,
            DEPLETED_LOT,
            DEPLETED_PREP,
            EMPTY_PREP,
        )
    )


# ---------------------------------------------------------------------------
# The KIND positional argument, with and without --all
# ---------------------------------------------------------------------------


def test_kind_argument_with_default_and_all(cli_env: None, scope_ledger) -> None:
    ledger = scope_ledger["ledger"]

    lot_table = invoke(ledger, "stock", "list", "lot")
    assert lot_table.exit_code == 0, lot_table.output
    assert table_ids(lot_table.output) == [ACTIVE_LOT]

    lot_all = invoke(ledger, "stock", "list", "lot", "--all")
    assert lot_all.exit_code == 0, lot_all.output
    assert table_ids(lot_all.output) == LOT_IDS

    assert ids(run_json(ledger, "stock", "list", "bacterial")) == [
        ACTIVE_BACTERIAL
    ]
    assert ids(run_json(ledger, "stock", "list", "bacterial", "--all")) == (
        BACTERIAL_IDS
    )


# ---------------------------------------------------------------------------
# --all combined with --where, --json and the global --format csv
# ---------------------------------------------------------------------------


def test_all_combined_with_where_json_and_csv(cli_env: None, scope_ledger) -> None:
    ledger = scope_ledger["ledger"]

    # A kind field: only ``lot`` declares ``supplier``; the depleted row with
    # the same supplier comes back with --all, and only the active one without.
    assert ids(
        run_json(ledger, "stock", "list", "--all", "--where", "supplier=acme")
    ) == [ACTIVE_LOT, DEPLETED_LOT]
    assert ids(
        run_json(ledger, "stock", "list", "--where", "supplier=acme")
    ) == [ACTIVE_LOT]

    # A core column: every row that belongs to the item entity, all statuses.
    entity_filter = f"entity_id={scope_ledger['item']}"
    assert ids(
        run_json(ledger, "stock", "list", "--all", "--where", entity_filter)
    ) == [
        ACTIVE_BACTERIAL,
        EMPTY_BACTERIAL,
        DEPLETED_BACTERIAL,
        ACTIVE_LOT,
        EMPTY_LOT,
        DEPLETED_LOT,
    ]

    # The global --format csv honours --all and --where like the table does.
    assert ids(run_csv(ledger, "stock", "list", "--all")) == ALL_IDS
    assert ids(
        run_csv(ledger, "stock", "list", "--all", "--where", "supplier=acme")
    ) == [ACTIVE_LOT, DEPLETED_LOT]
    assert ids(
        run_csv(ledger, "stock", "list", "--all", "--where", "strain=DH5a")
    ) == [ACTIVE_BACTERIAL]


# ---------------------------------------------------------------------------
# status is not a --where field, with or without --all
# ---------------------------------------------------------------------------


def test_where_status_is_still_an_unknown_field(cli_env: None, scope_ledger) -> None:
    ledger = scope_ledger["ledger"]

    for args in (
        ("stock", "list", "--where", "status=active"),
        ("stock", "list", "--all", "--where", "status=active"),
    ):
        result = invoke(ledger, *args)
        # The CLI translates a UsageError to exit 2 (the same convention as
        # tests/test_where_empty.py: CliRunner surfaces the exception).
        assert isinstance(result.exception, UsageError)
        assert result.exception.exit_code == 2
        message = str(result.exception)
        assert "unknown field" in message
        assert "status" in message

    # The Python facade reports the same usage error before any row is read,
    # in both scopes.
    for all_statuses in (False, True):
        with pytest.raises(UsageError, match="unknown field.*status"):
            ledger.stocks.ls(where=["status=active"], all_statuses=all_statuses)


# ---------------------------------------------------------------------------
# An empty active set is the existing empty output, not an error
# ---------------------------------------------------------------------------


def test_empty_active_set_exits_zero_with_the_existing_empty_output(
    cli_env: None, scope_ledger
) -> None:
    ledger = scope_ledger["ledger"]

    # ``prep`` has stocks, but none is active: the default listing is empty.
    table = invoke(ledger, "stock", "list", "prep")
    assert table.exit_code == 0, table.output
    assert table.output.strip() == "(none)"

    csv_rows = run_csv(ledger, "stock", "list", "prep")
    assert csv_rows == []
    csv_text = invoke(ledger, "--format", "csv", "stock", "list", "prep")
    assert csv_text.exit_code == 0, csv_text.output
    assert csv_text.output.splitlines() == [",".join(STOCK_COLUMNS)]

    json_rows = run_json(ledger, "stock", "list", "prep")
    assert json_rows == []
    assert invoke(ledger, "stock", "list", "prep", "--json").output.strip() == "[]"

    # The rows exist: --all prints the depleted and empty prep stocks.
    assert ids(run_json(ledger, "stock", "list", "prep", "--all")) == PREP_IDS


# ---------------------------------------------------------------------------
# The Python facade mirrors both scopes
# ---------------------------------------------------------------------------


def test_python_facade_matches_the_cli_scopes(cli_env: None, scope_ledger) -> None:
    ledger = scope_ledger["ledger"]

    default_ids = ids(run_json(ledger, "stock", "list"))
    all_ids = ids(run_json(ledger, "stock", "list", "--all"))

    assert [row["id"] for row in ledger.stocks.ls()] == default_ids
    assert [row["id"] for row in ledger.stocks.ls(all_statuses=True)] == all_ids
    assert default_ids == ACTIVE_IDS
    assert all_ids == ALL_IDS

    # The kind positional forwards the same scope switch.
    assert [row["id"] for row in ledger.stocks.ls("lot")] == [ACTIVE_LOT]
    assert [
        row["id"] for row in ledger.stocks.ls("lot", all_statuses=True)
    ] == LOT_IDS


# ---------------------------------------------------------------------------
# entity list and box list are unchanged: every row, no availability notion
# ---------------------------------------------------------------------------


def test_entity_and_box_lists_are_unchanged(cli_env: None, scope_ledger) -> None:
    ledger = scope_ledger["ledger"]

    # Both entities print, including the one whose only stocks are empty or
    # depleted.  Neither listing filters on stock availability.
    entity_rows = run_json(ledger, "entity", "list")
    assert {row["id"] for row in entity_rows} == {
        scope_ledger["item"],
        scope_ledger["thing"],
    }

    # Every box prints, occupied or not.
    box_rows = run_json(ledger, "box", "list")
    assert {row["name"] for row in box_rows} == {"G-1", "R-1"}

    # The stock-scope switch was not added to either command.
    for path in (("entity", "list"), ("box", "list")):
        rejected = invoke(ledger, *path, "--all")
        assert rejected.exit_code == 2, rejected.output


# ---------------------------------------------------------------------------
# Dolt-free: the CLI help documents the new default and the flag
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_stock_list_help_documents_the_active_default_and_all() -> None:
    result = CliRunner().invoke(app, ["stock", "list", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "--all" in text
    assert "only active stocks" in text
    assert "Every stock, including empty and depleted ones." in text

    # entity list and box list gain no such option.
    for path in (("entity", "list"), ("box", "list")):
        other = CliRunner().invoke(app, [*path, "--help"])
        assert other.exit_code == 0, other.output
        assert "--all" not in other.output
