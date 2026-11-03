"""CLI coverage for the object command groups (ticket 10)."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import open as open_ledger

from conftest import PRESETS

BASE = PRESETS / "base"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def base_args(ledger) -> list[str]:
    return [
        "--ledger",
        str(ledger.path),
    ]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def write(ledger, *args: str):
    before = commit_count(ledger)
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    assert commit_count(ledger) == before + 1
    return result


def read(ledger, *args: str):
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


def test_cli_entity_stock_aliquot_box_alias_flow(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli", id_prefix="77")

    created = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    entity_id = created["id"]
    assert created["kind"] == "item" and created["name"] == "alpha"

    shown = json.loads(read(ledger, "entity", "show", entity_id, "--json").output)
    assert shown["name"] == "alpha"
    assert shown["id"] == entity_id

    edited = json.loads(
        write(
            ledger, "entity", "edit", entity_id, "--set", "category=lab", "--json"
        ).output
    )
    assert edited["category"] == "lab"

    listed = json.loads(read(ledger, "entity", "list", "item", "--json").output)
    assert [row["id"] for row in listed] == [entity_id]
    filtered = json.loads(
        read(ledger, "entity", "list", "item", "--where", "category=lab", "--json").output
    )
    assert [row["id"] for row in filtered] == [entity_id]

    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    assert box["slots"] == 96
    shelves = json.loads(
        write(
            ledger,
            "box",
            "add",
            "shelf",
            "FREEZER",
            "--json",
        ).output
    )
    moved_box = json.loads(
        write(
            ledger,
            "box",
            "move",
            "BOX",
            "--in",
            "FREEZER",
            "--json",
        ).output
    )
    assert moved_box["parent_box_id"] == shelves["id"]
    boxes = json.loads(read(ledger, "box", "list", "--json").output)
    paths = {row["name"]: row["path"] for row in boxes}
    assert paths["BOX"] == "FREEZER/BOX"

    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity_id,
            "--aliquots",
            "2",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--slot",
            "A2",
            "--quantity",
            "100",
            "--unit",
            "ul",
            "--json",
        ).output
    )
    assert len(stock["aliquots"]) == 2
    first, second = stock["aliquots"]

    shown_stock = json.loads(
        read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert len(shown_stock["aliquots"]) == 2

    moved = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            first,
            "--box",
            "BOX",
            "--slot",
            "B1",
            "--json",
        ).output
    )
    assert moved["slot"] == "B1"

    used = json.loads(
        write(
            ledger,
            "aliquot",
            "use",
            second,
            "40",
            "ul",
            "--purpose",
            "prep",
            "--json",
        ).output
    )
    assert used["quantity"] == "60.000000"
    assert used["status"] == "stored"

    status = json.loads(
        write(ledger, "aliquot", "status", first, "missing", "--json").output
    )
    assert status["status"] == "missing"

    alias = json.loads(
        write(ledger, "alias", "add", "entity", entity_id, "E1", "--json").output
    )
    assert alias["alias"] == "E1"
    assert json.loads(read(ledger, "entity", "show", "E1", "--json").output)["id"] == entity_id
    write(ledger, "alias", "remove", "entity", "E1")

    purged = json.loads(
        write(ledger, "aliquot", "purge", second, "--json").output
    )
    assert purged["purged"] is True
    assert dirty(ledger) == []


def test_cli_alias_add_accepts_a_unique_incomplete_id(
    cli_env: None, ledger_factory
) -> None:
    """``tm alias add entity <short-id> <alias>`` works when the prefix is unique."""
    ledger = ledger_factory([BASE], name="cli-prefix", id_prefix="77")
    first = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "first",
            "--id",
            "aaaaaaaa-1111-4111-8111-111111111111",
            "--json",
        ).output
    )
    second = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "second",
            "--id",
            "aaaaaaaa-1111-4111-8111-222222222222",
            "--json",
        ).output
    )

    added = json.loads(
        write(
            ledger,
            "alias",
            "add",
            "entity",
            second["id"][:30],
            "second-old",
            "--json",
        ).output
    )
    assert added["object_id"] == second["id"]
    assert (
        json.loads(read(ledger, "entity", "show", first["id"][:26], "--json").output)["id"]
        == first["id"]
    )

    # An ambiguous prefix is a usage error (main() exits 2), names its
    # candidates and commits nothing.
    from tetramark.errors import UsageError

    before = commit_count(ledger)
    ambiguous = invoke(ledger, "alias", "add", "entity", "aaaaaaaa", "clash")
    assert isinstance(ambiguous.exception, UsageError)
    assert ambiguous.exception.exit_code == 2
    message = str(ambiguous.exception)
    assert first["id"] in message and second["id"] in message
    assert "ambiguous" in message
    assert commit_count(ledger) == before

    # An exact alias still wins over that same text as an id prefix.
    write(ledger, "alias", "add", "entity", first["id"], "aaaaaaaa")
    assert (
        json.loads(read(ledger, "entity", "show", "aaaaaaaa", "--json").output)["id"]
        == first["id"]
    )


def test_cli_show_prints_sorted_aliases_in_json_and_table(
    cli_env: None, ledger_factory
) -> None:
    """spec v2: entity/stock/box show expose the object's aliases (§6)."""
    ledger = ledger_factory([BASE], name="cli-show-aliases", id_prefix="77")

    entity = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "alpha",
            "--alias",
            "zeta",
            "--alias",
            "alpha-old",
            "--json",
        ).output
    )
    entity_id = entity["id"]
    shown_entity = json.loads(
        read(ledger, "entity", "show", entity_id, "--json").output
    )
    assert shown_entity["aliases"] == ["alpha-old", "zeta"]
    entity_table = read(ledger, "entity", "show", entity_id)
    assert "aliases" in entity_table.output
    assert "alpha-old, zeta" in entity_table.output

    stock = json.loads(
        write(ledger, "stock", "add", "lot", "--entity", entity_id, "--json").output
    )
    write(ledger, "alias", "add", "stock", stock["id"], "old-lot")
    write(ledger, "alias", "add", "stock", stock["id"], "lot-old")
    shown_stock = json.loads(
        read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert shown_stock["aliases"] == ["lot-old", "old-lot"]
    stock_table = read(ledger, "stock", "show", stock["id"])
    assert "lot-old, old-lot" in stock_table.output

    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    write(ledger, "alias", "add", "box", box["id"], "legacy-box")
    write(ledger, "alias", "add", "box", box["id"], "AAA-box")
    shown_box = json.loads(read(ledger, "box", "show", "BOX", "--json").output)
    assert shown_box["aliases"] == ["AAA-box", "legacy-box"]
    box_table = read(ledger, "box", "show", "BOX")
    assert "aliases: AAA-box, legacy-box" in box_table.output

    # No aliases: the key is present (empty) and the table row is blank.
    plain = json.loads(write(ledger, "box", "add", "grid96", "PLAIN", "--json").output)
    assert plain["slots"] == 96
    plain_box = json.loads(read(ledger, "box", "show", "PLAIN", "--json").output)
    assert plain_box["aliases"] == []
    assert "aliases:" not in read(ledger, "box", "show", "PLAIN").output


def test_cli_dry_run_prints_sql_and_writes_nothing(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-dry", id_prefix="77")
    before = commit_count(ledger)

    result = invoke(
        ledger, "--dry-run", "entity", "add", "item", "--name", "planned"
    )
    assert result.exit_code == 0, result.output
    assert "INSERT INTO `entity`" in result.output
    assert commit_count(ledger) == before
    assert dirty(ledger) == []

    as_json = invoke(
        ledger, "--dry-run", "entity", "add", "item", "--name", "planned2", "--json"
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["dry_run"] is True
    assert "INSERT INTO `entity`" in payload["sql"]
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_no_commit_executes_without_committing(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-nocommit", id_prefix="77")
    before = commit_count(ledger)
    result = invoke(
        ledger, "--no-commit", "entity", "add", "item", "--name", "uncommitted"
    )
    assert result.exit_code == 0, result.output
    assert commit_count(ledger) == before
    assert dirty(ledger)


def test_cli_business_error_exits_1(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="cli-business", id_prefix="77")
    assert invoke(ledger, "box", "add", "grid96", "DUP").exit_code == 0
    duplicate = invoke(ledger, "box", "add", "grid96", "DUP")
    assert duplicate.exit_code == 1


def test_cli_global_json_flag_applies_to_writes_and_reads(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-global-json", id_prefix="77")
    created = json.loads(
        invoke(ledger, "--json", "entity", "add", "item", "--name", "gamma").output
    )
    assert created["name"] == "gamma"
    listed = json.loads(invoke(ledger, "--json", "entity", "list", "item").output)
    assert [row["id"] for row in listed] == [created["id"]]


# ---------------------------------------------------------------------------
# Branch support (TM-03)
# ---------------------------------------------------------------------------


def test_cli_global_branch_selects_the_session_branch(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    before = commit_count(ledger)

    result = invoke(
        ledger, "-b", "feature", "entity", "add", "item", "--name", "branch-item"
    )
    assert result.exit_code == 0, result.output

    # Nothing changed on the checked-out branch; the write and commit are on
    # the session branch only.
    assert ledger.entities.ls() == []
    assert commit_count(ledger) == before
    branch = open_ledger(ledger.path, branch="feature")
    try:
        assert [row["name"] for row in branch.entities.ls()] == ["branch-item"]
        assert ledger.driver.query("SELECT active_branch() AS `b`") == [
            {"b": "main"}
        ]
    finally:
        branch.close()


def test_cli_branch_dry_run_names_the_target_branch(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch-dry", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    before = commit_count(ledger)

    result = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "entity",
        "add",
        "item",
        "--name",
        "planned",
    )
    assert result.exit_code == 0, result.output
    assert "-- target branch: feature" in result.output
    assert "INSERT INTO `entity`" in result.output
    assert commit_count(ledger) == before

    as_json = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "entity",
        "add",
        "item",
        "--name",
        "planned2",
        "--json",
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["branch"] == "feature"
    assert "INSERT INTO `entity`" in payload["sql"]


def test_cli_branch_list_create_and_delete_roundtrip(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch-cmd", id_prefix="77")

    listed = invoke(ledger, "branch", "list")
    assert listed.exit_code == 0, listed.output
    assert "main" in listed.output and "* main" in listed.output

    created = invoke(ledger, "branch", "create", "feature", "--from", "main")
    assert created.exit_code == 0, created.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "list").output)
    ]
    assert {"main", "feature"} <= set(names)

    deleted = invoke(ledger, "branch", "delete", "feature")
    assert deleted.exit_code == 0, deleted.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "list").output)
    ]
    assert "feature" not in names

    # An unmerged branch is refused by Dolt, then deletable once merged.
    assert invoke(ledger, "branch", "create", "unmerged").exit_code == 0
    branch = open_ledger(ledger.path, branch="unmerged")
    try:
        branch.entities.add("item", name="unmerged-row")
    finally:
        branch.close()
    refused = invoke(ledger, "branch", "delete", "unmerged")
    assert refused.exit_code == 1
    assert "not fully merged" in str(refused.exception)

    ledger.driver.execute("CALL DOLT_MERGE(%s)", ("unmerged",))
    assert invoke(ledger, "branch", "delete", "unmerged").exit_code == 0


def test_remote_push_b_still_names_the_remote_branch(
    cli_env: None, ledger_factory, tmp_path
) -> None:
    """Q-08: the subcommand's -b wins over the global -b (different meanings)."""
    ledger = ledger_factory([BASE], name="cli-remote-b", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    added = invoke(ledger, "remote", "add", "origin", str(tmp_path / "remote"))
    assert added.exit_code == 0, added.output

    pushed = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "remote",
        "push",
        "-b",
        "remote-only",
        "origin",
    )
    assert pushed.exit_code == 0, pushed.output
    assert "DOLT_PUSH('origin', 'remote-only')" in pushed.output

    # Without the subcommand option the session branch is pushed.
    session = invoke(
        ledger, "-b", "feature", "--dry-run", "remote", "push", "origin"
    )
    assert session.exit_code == 0, session.output
    assert "DOLT_PUSH('origin', 'feature')" in session.output


def test_cli_unknown_field_exits_usage(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([BASE], name="cli-usage", id_prefix="77")
    args = [
        "tm",
        *base_args(ledger),
        "entity",
        "add",
        "item",
        "--name",
        "probe",
        "--set",
        "nope=1",
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    assert "valid fields" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Status CLI: creation status, transition rules and --force (spec 4)
# ---------------------------------------------------------------------------


def test_cli_box_name_and_grid_rules(cli_env: None, ledger_factory) -> None:
    """Acceptance through the CLI: unsafe names and nesting in a grid box."""
    from tetramark.errors import LedgerError, UsageError

    ledger = ledger_factory([BASE], name="cli-box-rules", id_prefix="77")
    write(ledger, "box", "add", "grid96", "G")
    write(ledger, "box", "add", "rack", "R")

    for name, match in (
        ("slots", "core view"),
        ("x" * 59, "58"),
        ("g", "differs only in case"),
    ):
        refused = invoke(ledger, "box", "add", "rack", name)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert match in str(refused.exception)

    # Grid boxes hold tubes only, so a box cannot be added or moved into one.
    added_inside = invoke(ledger, "box", "add", "rack", "INNER", "--in", "G", "--slot", "A1")
    assert isinstance(added_inside.exception, LedgerError)
    assert added_inside.exception.exit_code == 1
    assert "grid boxes hold tubes only" in str(added_inside.exception)
    moved_inside = invoke(ledger, "box", "move", "R", "--in", "G", "--slot", "A1")
    assert isinstance(moved_inside.exception, LedgerError)
    assert "grid boxes hold tubes only" in str(moved_inside.exception)


def test_cli_status_creation_transition_and_force(cli_env: None, ledger_factory) -> None:
    from tetramark.errors import UsageError

    ledger = ledger_factory([BASE], name="cli-status", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "a", "--json").output
    )
    # Creation accepts any status of the stock kind's set.
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--status",
            "consumed",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    # An unforced illegal change is a business error (exit 1 via main()).
    from tetramark.errors import LedgerError

    refused = invoke(ledger, "aliquot", "status", alq, "stored")
    assert isinstance(refused.exception, LedgerError)
    assert refused.exception.exit_code == 1
    assert "final status" in str(refused.exception)

    # --force without --note is a usage error (main() exits 2).
    usage = invoke(ledger, "aliquot", "status", alq, "stored", "--force")
    assert isinstance(usage.exception, UsageError)
    assert usage.exception.exit_code == 2
    assert "--force requires --note" in str(usage.exception)

    # A forced change into a status that is not in stock is allowed and
    # recorded (the position is already absent).
    forced = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            alq,
            "missing",
            "--force",
            "--note",
            "found in the back of the shelf",
            "--json",
        ).output
    )
    assert forced["forced"] is True
    assert forced["status"] == "missing"

    # An in-stock status needs a position: refused with the move hint.
    placed = invoke(ledger, "aliquot", "status", alq, "stored")
    assert isinstance(placed.exception, LedgerError)
    assert placed.exception.exit_code == 1
    assert "tm aliquot move" in str(placed.exception)

    # Placing the tube restores the in-stock status through the move command.
    box = json.loads(
        write(ledger, "box", "add", "grid96", "BOX", "--json").output
    )
    moved = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            alq,
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    assert moved["status"] == "stored"
    assert moved["old_status"] == "missing"
    assert moved["slot"] == "A1"
    row = ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]
    assert row == {"status": "stored", "box_id": box["id"], "slot": "A1"}


def test_cli_aliquot_status_clears_position(cli_env: None, ledger_factory) -> None:
    """Acceptance through the CLI: consumed clears, the event keeps the old one."""
    ledger = ledger_factory([BASE], name="cli-clear", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "a", "--json").output
    )
    box = json.loads(
        write(ledger, "box", "add", "grid96", "G", "--json").output
    )
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--box",
            "G",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    changed = json.loads(
        write(ledger, "aliquot", "status", alq, "consumed", "--json").output
    )
    assert changed["status"] == "consumed"
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None}]
    event = ledger.driver.query(
        "SELECT `event`, `from_box_id`, `from_slot`, `to_box_id`, `to_slot` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (alq,),
    )[-1]
    assert event == {
        "event": "status",
        "from_box_id": box["id"],
        "from_slot": "A1",
        "to_box_id": None,
        "to_slot": None,
    }

    # ``consumed`` is final: a forced change to ``missing`` (not in stock) is
    # recorded without a position...
    forced = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            alq,
            "missing",
            "--force",
            "--note",
            "tube found",
            "--json",
        ).output
    )
    assert forced["status"] == "missing"
    # ...and restoring ``stored`` (a legal missing -> stored transition) needs
    # the tube placed again.
    refused = invoke(ledger, "aliquot", "status", alq, "stored")
    assert refused.exit_code == 1
    assert "tm aliquot move" in str(refused.exception)
    restored = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            alq,
            "--box",
            "G",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    assert restored["status"] == "stored"


def test_cli_aliquot_move_status_option(cli_env: None, ledger_factory) -> None:
    from tetramark.errors import LedgerError

    from conftest import REPO_PRESETS

    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml", REPO_PRESETS / "c_elegans.yaml"],
        name="cli-move-status",
        id_prefix="77",
    )
    strain = json.loads(
        write(ledger, "entity", "add", "worm_strain", "--name", "N2", "--json").output
    )
    write(ledger, "box", "add", "grid8x12", "G")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "worm_culture",
            "--entity",
            strain["id"],
            "--aliquots",
            "1",
            "--status",
            "contaminated",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    # Two in-stock statuses: the bare move lists them and asks for --status.
    ambiguous = invoke(ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1")
    assert isinstance(ambiguous.exception, LedgerError)
    assert "several in-stock statuses" in str(ambiguous.exception)
    assert "growing" in str(ambiguous.exception)
    assert "starved" in str(ambiguous.exception)

    # --status must name an in-stock status of the set.
    bad = invoke(
        ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1",
        "--status", "contaminated",
    )
    assert bad.exception.exit_code == 2
    assert "not in stock" in str(bad.exception)
    unknown = invoke(
        ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1",
        "--status", "nope",
    )
    assert unknown.exception.exit_code == 2
    assert "unknown status" in str(unknown.exception)

    restored = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            alq,
            "--box",
            "G",
            "--slot",
            "A1",
            "--status",
            "growing",
            "--json",
        ).output
    )
    assert restored["old_status"] == "contaminated"
    assert restored["status"] == "growing"
