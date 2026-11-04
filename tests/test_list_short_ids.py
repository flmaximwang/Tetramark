"""Short uuid ids in the default list tables (Tetramark-vq1).

``tm entity list`` and ``tm stock list`` show a uuid id as its shortest unique
prefix of at least 8 characters, git style.  ``show``, JSON and CSV always keep
the full 36-character id.  The helper tests are Dolt-free (fast lane); the CLI
tests run against a real ledger.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.cli.output import emit_read, is_uuid4, short_uuid_ids

from conftest import PRESETS

BASE = PRESETS / "base"

#: Two uuid ids whose first 8 characters collide; they differ at character 10
#: (character 9 is the shared hyphen).
UUID_A = "abcdef12-1111-4111-8111-111111111111"
UUID_B = "abcdef12-2222-4222-8222-222222222222"
#: Shares a prefix with UUID_A up to character 10, so both must grow to 11.
UUID_C = "abcdef12-1333-4333-8333-333333333333"
#: No collision: keeps the bare 8-character prefix.
UUID_D = "12345678-9abc-4def-8123-456789abcdef"


# ---------------------------------------------------------------------------
# Helper: uuid detection
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize("value", [UUID_A, UUID_A.upper()])
def test_is_uuid4_accepts_canonical_uuid_ids(value: str) -> None:
    assert is_uuid4(value)


@pytest.mark.fast
@pytest.mark.parametrize(
    "value",
    [
        "0826FP01",  # datecode
        "7700001",  # sequence
        "0826FP01.1",  # aliquot
        "not-a-uuid",
        "",
        "abcdef12111141118111111111111111",  # 32 hex chars, no hyphens
        "abcdef12-1111-4111-8111",  # truncated
        "abcdef12-1111-4111-8111-11111111111g",  # non-hex
        None,
        7,
    ],
)
def test_is_uuid4_rejects_everything_else(value: object) -> None:
    assert not is_uuid4(value)


# ---------------------------------------------------------------------------
# Helper: collision-safe short prefixes
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_short_uuid_ids_uses_eight_chars_and_leaves_other_ids_alone() -> None:
    values = [UUID_D, "0826FP01", "0826FP01.1", None]
    assert short_uuid_ids(values) == [UUID_D[:8], "0826FP01", "0826FP01.1", None]


@pytest.mark.fast
def test_short_uuid_ids_extends_colliding_prefixes() -> None:
    assert short_uuid_ids([UUID_A, UUID_B]) == [UUID_A[:10], UUID_B[:10]]
    assert short_uuid_ids([UUID_A, UUID_C]) == [UUID_A[:11], UUID_C[:11]]


@pytest.mark.fast
def test_short_uuid_ids_extends_only_the_colliding_rows() -> None:
    assert short_uuid_ids([UUID_A, UUID_B, UUID_C, UUID_D]) == [
        UUID_A[:11],
        UUID_B[:10],
        UUID_C[:11],
        UUID_D[:8],
    ]


@pytest.mark.fast
def test_short_uuid_ids_is_deterministic() -> None:
    values = [UUID_A, UUID_B, UUID_C, UUID_D]
    assert short_uuid_ids(values) == short_uuid_ids(values)


@pytest.mark.fast
def test_short_uuid_ids_collision_check_is_case_insensitive() -> None:
    folded = UUID_A.upper()
    # Two ids that differ only in case never become distinguishable prefixes.
    assert short_uuid_ids([UUID_A, folded]) == [UUID_A, folded]


@pytest.mark.fast
def test_short_uuid_ids_keeps_empty_and_none_columns() -> None:
    assert short_uuid_ids([]) == []
    assert short_uuid_ids([None, None]) == [None, None]


@pytest.mark.fast
def test_emit_read_shortens_every_named_table_column(capsys) -> None:
    """The stock table names both ``id`` and ``entity_id``; the table uses both."""
    rows = [{"id": UUID_A, "entity_id": UUID_D, "kind": "lot"}]
    emit_read(
        rows,
        fmt="table",
        columns=["id", "kind", "entity_id"],
        short_uuid_columns=("id", "entity_id"),
    )
    out = capsys.readouterr().out
    assert UUID_A[:8] in out and UUID_A not in out
    assert UUID_D[:8] in out and UUID_D not in out


# ---------------------------------------------------------------------------
# CLI: real Dolt
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def base_args(ledger) -> list[str]:
    return ["--ledger", str(ledger.path)]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def _row_tokens(output: str, row_label: str) -> list[str]:
    """Split the table line that contains ``row_label`` into cells."""
    lines = [line for line in output.splitlines() if row_label in line]
    assert len(lines) == 1, output
    return lines[0].split()


def test_list_tables_shorten_uuid_ids_but_show_and_machine_formats_keep_them(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-short-ids")

    created = json.loads(
        invoke(ledger, "--json", "entity", "add", "item", "--name", "alpha").output
    )
    entity_id = created["id"]
    assert is_uuid4(entity_id)

    entity_table = invoke(ledger, "entity", "list")
    assert entity_table.exit_code == 0, entity_table.output
    assert entity_id[:8] in _row_tokens(entity_table.output, "alpha")
    assert entity_id not in entity_table.output

    shown = invoke(ledger, "entity", "show", entity_id)
    assert shown.exit_code == 0, shown.output
    assert entity_id in shown.output

    listed_json = json.loads(invoke(ledger, "--json", "entity", "list").output)
    assert [row["id"] for row in listed_json] == [entity_id]

    listed_csv = invoke(ledger, "--format", "csv", "entity", "list")
    assert listed_csv.exit_code == 0, listed_csv.output
    assert f"{entity_id},item,alpha" in listed_csv.output.splitlines()

    stock = json.loads(
        invoke(
            ledger, "--json", "stock", "add", "lot", "--entity", entity_id
        ).output
    )
    stock_id = stock["id"]
    assert not is_uuid4(stock_id)  # datecode ids are never shortened

    stock_table = invoke(ledger, "stock", "list")
    assert stock_table.exit_code == 0, stock_table.output
    assert entity_id[:8] in _row_tokens(stock_table.output, stock_id)
    assert entity_id not in stock_table.output
    assert stock_id in stock_table.output

    stock_json = json.loads(invoke(ledger, "--json", "stock", "list").output)
    assert stock_json[0]["entity_id"] == entity_id

    stock_csv = invoke(ledger, "--format", "csv", "stock", "list")
    assert stock_csv.exit_code == 0, stock_csv.output
    assert f"{stock_id},lot,{entity_id},alpha,0,depleted" in (
        stock_csv.output.splitlines()
    )


def test_list_table_widens_colliding_uuid_prefixes(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-short-collision")
    for name, uuid_id in (("collide-a", UUID_A), ("collide-b", UUID_C), ("solo", UUID_D)):
        added = invoke(
            ledger, "entity", "add", "item", "--name", name, "--id", uuid_id
        )
        assert added.exit_code == 0, added.output

    table = invoke(ledger, "entity", "list")
    assert table.exit_code == 0, table.output
    assert _row_tokens(table.output, "collide-a")[0] == UUID_A[:11]
    assert _row_tokens(table.output, "collide-b")[0] == UUID_C[:11]
    assert _row_tokens(table.output, "solo")[0] == UUID_D[:8]

    # Machine formats keep the full ids even when prefixes collide.
    listed = json.loads(invoke(ledger, "--json", "entity", "list").output)
    assert {row["id"] for row in listed} == {UUID_A, UUID_C, UUID_D}
