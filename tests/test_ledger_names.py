"""Ledger names may contain dots (TM-02 / SPEC A2), on both drivers.

Everything runs against the real ``dolt`` binary.  The server case follows the
harness in ``tests/test_driver_contract.py`` (``server_workspace``): a real
``dolt sql-server`` on a random free port.  ``dolt sql-server`` discovers the
databases present when it starts but does not pick up one created later, so the
ledger is initialised (``init_ledger`` always writes through the embedded
driver) before the server starts — exactly how a workspace's server is started
after its ledgers exist.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tetramark.core.ledger import init_ledger
from tetramark.core.ledger import open as open_ledger
from tetramark.driver.server import ServerDriver
from tetramark.driver.workspace import Workspace
from tetramark.errors import UsageError
from tetramark.ops import remote
from tetramark.ops.io_csv import export_csv

from conftest import FACTORY_AUTHOR, PRESETS

#: The Part B database shape plus two other dotted forms.
VALID_NAMES = [
    "zsqlab_10_x-y_2026.06.25",
    "lab.2",
    "a.b_c-d9",
]

#: ``..`` anywhere, a trailing ``.``, a leading ``.``, ``/`` and blanks.
INVALID_NAMES = ["a..b", "a.", ".a", "a/b", "a b", "a..", "..", "."]

DOTTED = "zsqlab_10_x-y_2026.06.25"


def _exercise(ledger, tmp_path: Path) -> None:
    """Write, export and back up through the opened ledger's driver."""
    record = ledger.entities.add("item", name="Dotted ledger entity")
    rows = ledger.driver.query(
        "SELECT `id`, `kind`, `name` FROM `entity` ORDER BY `id`"
    )
    assert rows == [
        {"id": record["id"], "kind": "item", "name": "Dotted ledger entity"}
    ]

    export_dir = tmp_path / "exports"
    export_csv(ledger, "all", export_dir)
    assert (export_dir / "item.csv").is_file()

    result = remote.backup(ledger)
    expected_dir = ledger.workspace.data_dir / "backups" / ledger.name
    assert result.dry_run is False
    assert remote.default_backup_dir(ledger) == expected_dir
    assert expected_dir.is_dir()
    assert remote.list_backups(ledger) == [
        remote.Backup(name=ledger.name.replace(".", "_"), url=expected_dir.resolve().as_uri())
    ]


@pytest.fixture
def workspace(tmp_path: Path, dolt_env: dict[str, str]) -> Workspace:
    return Workspace.create(tmp_path / "data", env=dolt_env)


@pytest.fixture
def server_workspace(
    tmp_path: Path,
    dolt_env: dict[str, str],
    dolt_binary: str,
    port: int,
) -> Iterator[Workspace]:
    """A workspace holding one dotted ledger and a running server."""
    for key, value in (
        ("user.name", "Tetramark Tests"),
        ("user.email", "tests@tetramark.invalid"),
    ):
        subprocess.run(
            [dolt_binary, "config", "--global", "--add", key, value],
            env=dolt_env,
            capture_output=True,
            check=True,
        )
    workspace = Workspace.create(
        tmp_path / "data", port=port, default_ledger=DOTTED, env=dolt_env
    )
    ledger = init_ledger(
        workspace, DOTTED, [PRESETS / "base"], id_prefix="77", author=FACTORY_AUTHOR
    )
    ledger.close()
    workspace.start(daemon=True)
    try:
        yield workspace
    finally:
        workspace.stop()


@pytest.mark.parametrize("name", VALID_NAMES)
def test_dotted_name_end_to_end_embedded(
    workspace: Workspace, tmp_path: Path, name: str
) -> None:
    ledger = init_ledger(
        workspace, name, [PRESETS / "base"], id_prefix="77", author=FACTORY_AUTHOR
    )
    try:
        assert ledger.name == name
        assert ledger.database == name
        assert workspace.ledger_path(name).is_dir()
        assert name in workspace.list_ledgers()
        latest = ledger.driver.query(
            "SELECT `message` FROM `dolt_log` "
            "ORDER BY `date` DESC, `commit_order` DESC LIMIT 1"
        )[0]
        assert latest["message"] == f"init(ledger): {name}"
        _exercise(ledger, tmp_path)
    finally:
        ledger.close()

    # The ledger reopens from its dotted directory name and config.
    reopened = open_ledger(workspace, name)
    try:
        assert reopened.config.name == name
    finally:
        reopened.close()


@pytest.mark.server
def test_dotted_name_end_to_end_on_server(
    server_workspace: Workspace, tmp_path: Path
) -> None:
    workspace = server_workspace
    assert DOTTED in workspace.list_ledgers()

    ledger = open_ledger(workspace, DOTTED)
    try:
        assert isinstance(ledger.driver, ServerDriver)
        assert ledger.database == DOTTED
        _exercise(ledger, tmp_path)
    finally:
        ledger.close()


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_init_rejects_invalid_names_without_touching_disk(
    workspace: Workspace, name: str
) -> None:
    with pytest.raises(UsageError, match="invalid ledger name"):
        init_ledger(workspace, name, [PRESETS / "base"], author=FACTORY_AUTHOR)
    # Only workspace.yaml exists: no ledger directory (and no ../escape) was made.
    assert {entry.name for entry in workspace.data_dir.iterdir()} == {"workspace.yaml"}


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_open_rejects_invalid_names(workspace: Workspace, name: str) -> None:
    with pytest.raises(UsageError, match="invalid ledger name"):
        open_ledger(workspace, name)
