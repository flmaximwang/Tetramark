"""``tm stock renumber``: legacy ids become strategy-shaped ids plus aliases (TM-06).

Everything runs against the real ``dolt`` binary (never a mock): the CLI tests
use the worker's ``dolt sql-server`` like the other CLI tests, the core
rename test runs on both drivers.  Legacy stocks (ids that do not match the
ledger's stock id strategy) can only be created by seeding them with raw SQL,
so the helpers below do exactly that inside one audited write session.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ids import DatecodeIds
from tetramark.schema.ddl import table_name

from conftest import PRESETS

BASE = PRESETS / "base"
CHAIN = PRESETS / "chain"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory


@pytest.fixture(params=["embedded", "server"], ids=["embedded", "server"])
def driver_factory(request: pytest.FixtureRequest):
    """The same real ledger through the embedded and the server driver."""
    fixture = (
        "embedded_ledger_factory"
        if request.param == "embedded"
        else "server_ledger_factory"
    )
    return request.getfixturevalue(fixture)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def latest_commit(ledger) -> dict[str, object]:
    return ledger.driver.query(
        "SELECT commit_hash, message FROM dolt_log "
        "ORDER BY date DESC, commit_order DESC LIMIT 1"
    )[0]


def base_args(ledger) -> list[str]:
    return [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def cli_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ledger,
    *args: str,
) -> tuple[int, str]:
    """Run ``tm`` through ``main()`` so the real exit code can be asserted."""
    monkeypatch.setattr(sys, "argv", ["tm", *base_args(ledger), *args])
    capsys.readouterr()
    code = 0
    try:
        main()
    except SystemExit as exit_error:
        code = int(exit_error.code or 0)
    return code, capsys.readouterr().err


def day_head(ledger) -> str:
    """``<prefix><YY><M><D>`` of the ledger's datecode stock strategy."""
    strategy = ledger.id_strategy("stock")
    assert isinstance(strategy, DatecodeIds)
    return strategy.day_prefix(date.today())


def generated_id(ledger, seq: int) -> str:
    return f"{day_head(ledger)}{seq:02d}"


def raw_write(ledger, summary: str, statements: list[tuple[str, tuple[object, ...]]]):
    """Seed raw rows (legacy data) in exactly one commit."""
    with ledger.write(summary, verb="add", object="stock") as session:
        for sql, params in statements:
            session.execute(sql, params)


def seed_legacy_stock(
    ledger,
    entity_id: str,
    stock_id: str,
    *,
    kind: str = "lot",
    aliquot_seqs: tuple[int, ...] = (1,),
    box_id: str | None = None,
    slots: tuple[str | None, ...] = (),
    alias: str | None = None,
    self_alias: bool = False,
) -> list[str]:
    """Insert one pre-strategy stock (and its tubes) straight into the tables."""
    statements: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES (%s, %s, %s)",
            (stock_id, kind, entity_id),
        )
    ]
    if kind == "lot":
        statements.append(
            (
                "INSERT INTO `stock_lot` (`id`, `supplier`) VALUES (%s, %s)",
                (stock_id, "acme"),
            )
        )
    elif kind == "bacterial":
        statements.append(
            (
                "INSERT INTO `stock_bacterial` (`id`, `strain`) VALUES (%s, %s)",
                (stock_id, "BL21(DE3)"),
            )
        )
    if alias is not None:
        statements.append(
            (
                "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
                "VALUES ('stock', %s, %s)",
                (stock_id, alias),
            )
        )
    if self_alias:
        statements.append(
            (
                "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
                "VALUES ('stock', %s, %s)",
                (stock_id, stock_id),
            )
        )

    aliquot_ids: list[str] = []
    for index, seq in enumerate(aliquot_seqs):
        aliquot = f"{stock_id}.{seq}"
        slot = slots[index] if index < len(slots) else None
        placed_box = box_id if slot is not None else None
        statements.append(
            (
                "INSERT INTO `aliquot` "
                "(`id`, `stock_id`, `seq`, `status`, `box_id`, `slot`, "
                "`quantity`, `unit`) "
                "VALUES (%s, %s, %s, 'stored', %s, %s, 100, 'ul')",
                (aliquot, stock_id, seq, placed_box, slot),
            )
        )
        statements.append(
            (
                "INSERT INTO `aliquot_event` (`aliquot_id`, `event`, `new_status`) "
                "VALUES (%s, 'create', 'stored')",
                (aliquot,),
            )
        )
        if kind == "bacterial":
            statements.append(
                (
                    "INSERT INTO `aliquot_bacterial` (`id`, `glycerol_pct`) "
                    "VALUES (%s, 15)",
                    (aliquot,),
                )
            )
        aliquot_ids.append(aliquot)
    raw_write(ledger, f"seed legacy {stock_id}", statements)
    return aliquot_ids


def seed_legacy_child(
    ledger,
    entity_id: str,
    stock_id: str,
    *,
    parent_stock_id: str,
    source_aliquot_id: str,
    kind: str = "lot",
) -> None:
    extra: list[tuple[str, tuple[object, ...]]] = []
    if kind == "bacterial":
        extra.append(
            (
                f"INSERT INTO `{table_name('stock', kind)}` (`id`, `strain`) "
                "VALUES (%s, %s)",
                (stock_id, "DH5alpha"),
            )
        )
    else:
        extra.append(
            (
                f"INSERT INTO `{table_name('stock', kind)}` (`id`) VALUES (%s)",
                (stock_id,),
            )
        )
    raw_write(
        ledger,
        f"seed legacy child {stock_id}",
        [
            (
                "INSERT INTO `stock` "
                "(`id`, `kind`, `entity_id`, `parent_stock_id`, `source_aliquot_id`) "
                "VALUES (%s, %s, %s, %s, %s)",
                (stock_id, kind, entity_id, parent_stock_id, source_aliquot_id),
            ),
            *extra,
        ],
    )


def aliases(ledger, object_type: str | None = None) -> dict[str, str]:
    where = "" if object_type is None else " WHERE `object_type` = %s"
    params = () if object_type is None else (object_type,)
    rows = ledger.driver.query(
        "SELECT `object_type`, `alias`, `object_id` FROM `alias`" + where,
        params,
    )
    return {
        f"{row['object_type']}:{row['alias']}": str(row["object_id"]) for row in rows
    }


# ---------------------------------------------------------------------------
# Single rename through the CLI
# ---------------------------------------------------------------------------


def test_cli_single_rename_rewrites_every_reference(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="renum-single", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    box = ledger.boxes.add("grid96", "BOX")

    old = "230410-1"
    alq_old = seed_legacy_stock(
        ledger,
        entity["id"],
        old,
        aliquot_seqs=(1, 2),
        box_id=box["id"],
        slots=("A1", None),
        alias="LEGACY-1",
    )
    seed_legacy_child(
        ledger,
        entity["id"],
        "CHILD-1",
        parent_stock_id=old,
        source_aliquot_id=alq_old[1],
    )
    ledger.aliases.add("aliquot", alq_old[0], "TUBE-A")

    before = commit_count(ledger)
    result = invoke(ledger, "stock", "renumber", old, "--auto", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    new = payload["renames"][0]["new"]
    assert new == generated_id(ledger, 1)
    assert payload["mapping"] == {
        old: new,
        alq_old[0]: f"{new}.1",
        alq_old[1]: f"{new}.2",
    }
    assert payload["commit"]

    # Exactly one commit, with the documented message.
    assert commit_count(ledger) == before + 1
    committed = latest_commit(ledger)
    assert committed["message"] == f"renumber(stock): {old} -> {new}"
    assert str(committed["commit_hash"]) == payload["commit"]
    assert dirty(ledger) == []

    # The stock row, its kind row and all tubes moved to the new id.
    stocks = [row["id"] for row in ledger.driver.query("SELECT `id` FROM `stock`")]
    assert old not in stocks
    assert new in stocks
    assert ledger.driver.query(
        "SELECT `id`, `supplier` FROM `stock_lot` WHERE `id` = %s", (new,)
    ) == [{"id": new, "supplier": "acme"}]
    aliquots = ledger.driver.query(
        "SELECT `id`, `stock_id`, `seq`, `box_id`, `slot` FROM `aliquot` "
        "WHERE `stock_id` = %s ORDER BY `seq`",
        (new,),
    )
    assert [row["id"] for row in aliquots] == [f"{new}.1", f"{new}.2"]
    assert all(row["stock_id"] == new for row in aliquots)
    assert aliquots[0]["box_id"] == box["id"] and aliquots[0]["slot"] == "A1"
    assert aliquots[1]["box_id"] is None and aliquots[1]["slot"] is None

    # Events, parent/source references and existing aliases follow the object.
    events = ledger.driver.query(
        "SELECT `aliquot_id` FROM `aliquot_event` ORDER BY `id`"
    )
    assert [row["aliquot_id"] for row in events] == [f"{new}.1", f"{new}.2"]
    child = ledger.driver.query(
        "SELECT `parent_stock_id`, `source_aliquot_id` FROM `stock` "
        "WHERE `id` = 'CHILD-1'"
    )[0]
    assert child == {"parent_stock_id": new, "source_aliquot_id": f"{new}.2"}
    found = aliases(ledger)
    assert found[f"stock:{old}"] == new
    assert found[f"aliquot:{alq_old[0]}"] == f"{new}.1"
    assert found[f"aliquot:{alq_old[1]}"] == f"{new}.2"
    assert found["stock:LEGACY-1"] == new
    assert found["aliquot:TUBE-A"] == f"{new}.1"

    # Read side: old handles resolve, the kind fields still load, check passes.
    old_find = json.loads(invoke(ledger, "find", old, "--json").output)
    assert [row["id"] for row in old_find] == [new]
    tube_find = json.loads(invoke(ledger, "find", alq_old[0], "--json").output)
    assert [row["id"] for row in tube_find] == [f"{new}.1"]
    new_find = json.loads(invoke(ledger, "--json", "find", new).output)
    assert [row["id"] for row in new_find] == [new]
    shown = json.loads(
        invoke(ledger, "stock", "show", new, "--json").output
    )
    assert shown["supplier"] == "acme"
    assert [row["id"] for row in shown["aliquots"]] == [f"{new}.1", f"{new}.2"]

    history = json.loads(invoke(ledger, "--json", "history", new).output)
    assert history["id"] == new
    assert [event["aliquot_id"] for event in history["events"]] == [
        f"{new}.1",
        f"{new}.2",
    ]
    assert any(
        commit["message"] == f"renumber(stock): {old} -> {new}"
        for commit in history["commits"]
    )

    checked = invoke(ledger, "check")
    assert checked.exit_code == 0, checked.output
    assert json.loads(invoke(ledger, "check", "--json").output)["ok"] is True

    # Counts are unchanged.
    assert int(
        ledger.driver.query("SELECT COUNT(*) AS n FROM `aliquot`")[0]["n"]
    ) == 2
    assert int(
        ledger.driver.query("SELECT COUNT(*) AS n FROM `aliquot_event`")[0]["n"]
    ) == 2


# ---------------------------------------------------------------------------
# Batch renames
# ---------------------------------------------------------------------------


def test_cli_batch_map_is_one_commit(cli_env: None, ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([BASE], name="renum-map", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    first = seed_legacy_stock(ledger, entity["id"], "230410-1", aliquot_seqs=(1, 2))
    second = seed_legacy_stock(ledger, entity["id"], "4a334ac2")
    third = seed_legacy_stock(ledger, entity["id"], "1026.10.12.01-Capto1")
    seed_legacy_child(
        ledger,
        entity["id"],
        "CHILD-MAP",
        parent_stock_id="230410-1",
        source_aliquot_id=first[1],
    )

    targets = [generated_id(ledger, 1), generated_id(ledger, 2), generated_id(ledger, 3)]
    mapping_file = tmp_path / "renames.tsv"
    mapping_file.write_text(
        "# legacy ids\n"
        f"230410-1\t{targets[0]}\n"
        "\n"
        f"4a334ac2,{targets[1]}\n"
        f"1026.10.12.01-Capto1\t{targets[2]}\n",
        encoding="utf-8",
    )

    before = commit_count(ledger)
    result = invoke(ledger, "stock", "renumber", "--map", str(mapping_file), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [rename["new"] for rename in payload["renames"]] == targets
    assert payload["mapping"]["230410-1"] == targets[0]
    assert payload["mapping"][first[1]] == f"{targets[0]}.2"
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"] == "renumber(stock): 3 stock(s)"
    assert dirty(ledger) == []

    ids = {row["id"] for row in ledger.driver.query("SELECT `id` FROM `stock`")}
    assert ids == set(targets) | {"CHILD-MAP"}
    child = ledger.driver.query(
        "SELECT `parent_stock_id`, `source_aliquot_id` FROM `stock` "
        "WHERE `id` = 'CHILD-MAP'"
    )[0]
    assert child == {
        "parent_stock_id": targets[0],
        "source_aliquot_id": f"{targets[0]}.2",
    }
    for old, new in zip(
        ["230410-1", "4a334ac2", "1026.10.12.01-Capto1"], targets, strict=True
    ):
        assert old not in ids
        assert aliases(ledger)[f"stock:{old}"] == new


def test_cli_batch_auto_generates_sequential_ids(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="renum-auto", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    seed_legacy_stock(ledger, entity["id"], "230410-1", aliquot_seqs=(1, 2, 3))
    seed_legacy_stock(ledger, entity["id"], "gPFD-2026.07.08.01", alias="gPFD")
    seed_legacy_stock(ledger, entity["id"], "1026.10.12.01-Capto1")

    before = commit_count(ledger)
    result = invoke(
        ledger,
        "stock",
        "renumber",
        "230410-1",
        "gPFD",
        "1026.10.12.01-Capto1",
        "--auto",
        "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    targets = [generated_id(ledger, 1), generated_id(ledger, 2), generated_id(ledger, 3)]
    assert [rename["new"] for rename in payload["renames"]] == targets
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"] == "renumber(stock): 3 stock(s)"

    # The multi-aliquot stock and the alias-ref stock both moved completely.
    tubes = ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `stock_id` = %s ORDER BY `seq`",
        (targets[0],),
    )
    assert [row["id"] for row in tubes] == [f"{targets[0]}.{n}" for n in (1, 2, 3)]
    assert aliases(ledger)["stock:gPFD"] == targets[1]
    assert aliases(ledger)["stock:gPFD-2026.07.08.01"] == targets[1]


def test_cli_map_with_auto_fills_in_missing_ids(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="renum-map-auto", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    seed_legacy_stock(ledger, entity["id"], "230410-1")
    seed_legacy_stock(ledger, entity["id"], "4a334ac2")

    mapping_file = tmp_path / "old-ids.txt"
    mapping_file.write_text("230410-1\n# comment\n4a334ac2\n", encoding="utf-8")

    before = commit_count(ledger)
    result = invoke(
        ledger,
        "stock",
        "renumber",
        "--map",
        str(mapping_file),
        "--auto",
        "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [rename["new"] for rename in payload["renames"]] == [
        generated_id(ledger, 1),
        generated_id(ledger, 2),
    ]
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"] == "renumber(stock): 2 stock(s)"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_cli_dry_run_prints_mapping_and_sql_and_writes_nothing(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="renum-dry", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    old = "230410-1"
    tubes = seed_legacy_stock(ledger, entity["id"], old, aliquot_seqs=(1, 2))

    before = commit_count(ledger)
    result = invoke(ledger, "--dry-run", "stock", "renumber", old, "--auto")
    assert result.exit_code == 0, result.output
    output = result.output
    assert f"stock   {old} -> " in output
    assert f"aliquot {tubes[0]} -> " in output
    assert "SET FOREIGN_KEY_CHECKS = 0;" in output
    assert "SET FOREIGN_KEY_CHECKS = 1;" in output
    assert "UPDATE `stock` SET `id` = " in output
    assert "INSERT INTO `alias`" in output

    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert [row["id"] for row in ledger.driver.query("SELECT `id` FROM `stock`")] == [
        old
    ]
    assert [row["id"] for row in ledger.driver.query("SELECT `id` FROM `aliquot`")] == tubes
    assert aliases(ledger) == {}

    as_json = invoke(
        ledger, "--dry-run", "stock", "renumber", old, "--auto", "--json"
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["dry_run"] is True
    assert payload["result"]["renames"][0]["new"] == generated_id(ledger, 1)
    assert "UPDATE `stock`" in payload["sql"]
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_cli_refuses_taken_or_invalid_new_ids(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([BASE], name="renum-refuse-id", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    taken = ledger.stocks.add("lot", entity=entity["id"], id=generated_id(ledger, 1))
    aliased = ledger.stocks.add("lot", entity=entity["id"], id=generated_id(ledger, 2))
    ledger.aliases.add("stock", aliased["id"], generated_id(ledger, 3))
    box = ledger.boxes.add("grid96", "BOX")
    old = "230410-1"
    seed_legacy_stock(ledger, entity["id"], old, aliquot_seqs=(1, 2))

    before = commit_count(ledger)

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", old, "--id", taken["id"]
    )
    assert code == 1
    assert "already exists" in err

    code, err = cli_exit(
        monkeypatch,
        capsys,
        ledger,
        "stock",
        "renumber",
        old,
        "--id",
        generated_id(ledger, 3),
    )
    assert code == 1
    assert "already an alias of stock" in err

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", old, "--id", "NOT-AN-ID"
    )
    assert code == 1
    assert "malformed datecode id" in err

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", "nope-999", "--auto"
    )
    assert code == 1
    assert "unknown stock" in err

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", entity["id"], "--auto"
    )
    assert code == 2
    assert "not a stock" in err

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", box["id"], "--auto"
    )
    assert code == 2
    assert "is a box" in err

    # Nothing was written by any refusal.
    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    stock_ids = {
        row["id"] for row in ledger.driver.query("SELECT `id` FROM `stock`")
    }
    assert stock_ids == {
        generated_id(ledger, 1),
        generated_id(ledger, 2),
        old,
    }
    aliquot_ids = {
        row["id"] for row in ledger.driver.query("SELECT `id` FROM `aliquot`")
    }
    assert aliquot_ids == {
        f"{old}.1",
        f"{old}.2",
        f"{generated_id(ledger, 1)}.1",
        f"{generated_id(ledger, 2)}.1",
    }


def test_cli_new_id_must_differ_from_the_old_one(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([BASE], name="renum-refuse-same", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    stock = ledger.stocks.add("lot", entity=entity["id"], id=generated_id(ledger, 1))

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", stock["id"], "--id", stock["id"]
    )
    assert code == 2
    assert "must differ from the old id" in err
    assert dirty(ledger) == []


def test_cli_refuses_bad_invocations(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ledger = ledger_factory([BASE], name="renum-refuse-args", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    old = "230410-1"
    seed_legacy_stock(ledger, entity["id"], old, alias="LEGACY-1")
    other = "230410-2"
    seed_legacy_stock(ledger, entity["id"], other)
    before = commit_count(ledger)

    code, err = cli_exit(monkeypatch, capsys, ledger, "stock", "renumber", old)
    assert code == 2
    assert "nothing to renumber" in err

    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", old, other, "--id", "X"
    )
    assert code == 2
    assert "--id takes exactly one REF" in err

    code, err = cli_exit(
        monkeypatch,
        capsys,
        ledger,
        "stock",
        "renumber",
        old,
        "--id",
        generated_id(ledger, 1),
        "--auto",
    )
    assert code == 2
    assert "--id cannot be combined" in err

    mapping_file = tmp_path / "renames.tsv"
    mapping_file.write_text(f"{old}\t{generated_id(ledger, 1)}\n", encoding="utf-8")
    code, err = cli_exit(
        monkeypatch,
        capsys,
        ledger,
        "stock",
        "renumber",
        other,
        "--map",
        str(mapping_file),
    )
    assert code == 2
    assert "cannot be combined" in err

    code, err = cli_exit(
        monkeypatch,
        capsys,
        ledger,
        "stock",
        "renumber",
        old,
        "--id",
        generated_id(ledger, 1),
        "--map",
        str(mapping_file),
    )
    assert code == 2
    assert "--id cannot be combined" in err

    # The same stock named twice (id and alias) is refused before any write.
    duplicate = tmp_path / "duplicate.tsv"
    duplicate.write_text(
        f"{old}\t{generated_id(ledger, 1)}\n"
        f"LEGACY-1\t{generated_id(ledger, 2)}\n",
        encoding="utf-8",
    )
    code, err = cli_exit(
        monkeypatch, capsys, ledger, "stock", "renumber", "--map", str(duplicate)
    )
    assert code == 2
    assert "appears more than once" in err

    assert commit_count(ledger) == before
    assert dirty(ledger) == []

    # A map line without a new id needs --auto.
    ledger2 = ledger_factory([BASE], name="renum-refuse-map", id_prefix="77")
    entity2 = ledger2.entities.add("item", name="beta")
    seed_legacy_stock(ledger2, entity2["id"], "230410-9")
    bare = tmp_path / "bare.tsv"
    bare.write_text("230410-9\n", encoding="utf-8")
    code, err = cli_exit(
        monkeypatch, capsys, ledger2, "stock", "renumber", "--map", str(bare)
    )
    assert code == 2
    assert "missing the new id" in err
    assert dirty(ledger2) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_cli_renumbers_a_stock_without_aliquots(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="renum-empty", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    old = "230410-1"
    seed_legacy_stock(ledger, entity["id"], old, aliquot_seqs=())
    target = generated_id(ledger, 1)

    before = commit_count(ledger)
    result = invoke(ledger, "stock", "renumber", old, "--id", target, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["renames"][0]["aliquots"] == []
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"] == f"renumber(stock): {old} -> {target}"
    assert aliases(ledger) == {f"stock:{old}": target}
    assert ledger.stocks.show(target)["aliquots"] == []
    assert dirty(ledger) == []


def test_core_renumber_on_both_drivers(driver_factory) -> None:
    ledger = driver_factory([CHAIN], id_prefix="77")
    entity = ledger.entities.add("item", name="beta")
    old = "4a334ac2"
    tubes = seed_legacy_stock(
        ledger,
        entity["id"],
        old,
        kind="bacterial",
        aliquot_seqs=(1, 2),
        alias="LEGACY-BACT",
        self_alias=True,
    )
    seed_legacy_child(
        ledger,
        entity["id"],
        "CHILD-BACT",
        parent_stock_id=old,
        source_aliquot_id=tubes[1],
        kind="bacterial",
    )

    before = commit_count(ledger)
    result = ledger.stocks.renumber([old], auto=True)
    new = result["renames"][0]["new"]
    assert result["commit"]
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"] == f"renumber(stock): {old} -> {new}"
    assert dirty(ledger) == []

    # Kind table and aliquot extension rows follow the primary keys.
    assert [
        row["id"]
        for row in ledger.driver.query("SELECT `id` FROM `stock_bacterial` ORDER BY `id`")
    ] == sorted([new, "CHILD-BACT"])
    extensions = ledger.driver.query(
        "SELECT `id`, `glycerol_pct` FROM `aliquot_bacterial` ORDER BY `id`"
    )
    assert [row["id"] for row in extensions] == [f"{new}.1", f"{new}.2"]
    assert [str(row["glycerol_pct"]) for row in extensions] == ["15.000000", "15.000000"]

    # The self-referential alias is rewritten, not duplicated.
    self_rows = ledger.driver.query(
        "SELECT `object_id` FROM `alias` WHERE `object_type` = 'stock' AND `alias` = %s",
        (old,),
    )
    assert [row["object_id"] for row in self_rows] == [new]
    assert aliases(ledger)["stock:LEGACY-BACT"] == new

    # The FK checks switch is a session variable: it must be restored.
    assert int(ledger.driver.query("SELECT @@FOREIGN_KEY_CHECKS AS fk")[0]["fk"]) == 1

    # Read-side and validation keep working on the renamed object.
    assert [row["id"] for row in ledger.find(old)] == [new]
    assert [row["id"] for row in ledger.find(tubes[0])] == [f"{new}.1"]
    shown = ledger.stocks.show(new)
    assert shown["strain"] == "BL21(DE3)"
    assert [row["id"] for row in shown["aliquots"]] == [f"{new}.1", f"{new}.2"]
    assert ledger.check().passed
