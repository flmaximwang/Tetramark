"""Ledger names may contain dots (TM-02 / SPEC A2).

Everything runs against the real ``dolt`` binary through the embedded driver
(``dolt sql`` in the ledger directory).  A ledger is one self-contained
directory, so the dotted name is the directory basename *and* the identity in
``ledger.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tetramark.core.ledger import init_ledger
from tetramark.core.ledger import open as open_ledger
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

    # A dotted ledger name still works as a Dolt backup name: dots are
    # sanitized, and the destination must be given explicitly (P10).
    backup_dir = tmp_path / "backups" / ledger.name
    result = remote.backup(ledger, backup_dir)
    assert result.dry_run is False
    assert backup_dir.is_dir()
    assert remote.list_backups(ledger) == [
        remote.Backup(
            name=ledger.name.replace(".", "_"),
            url=backup_dir.resolve().as_uri(),
        )
    ]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_dotted_name_end_to_end_embedded(
    tmp_path: Path, dolt_env: dict[str, str], name: str
) -> None:
    target = tmp_path / "data" / name
    ledger = init_ledger(
        target,
        name,
        [PRESETS / "base"],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=dolt_env,
    )
    try:
        assert ledger.name == name
        assert ledger.database == name
        assert target.is_dir()
        latest = ledger.driver.query(
            "SELECT `message` FROM `dolt_log` "
            "ORDER BY `date` DESC, `commit_order` DESC LIMIT 1"
        )[0]
        assert latest["message"] == f"init(ledger): {name}"
        _exercise(ledger, tmp_path)
    finally:
        ledger.close()

    # The ledger reopens from its dotted directory name and config.
    reopened = open_ledger(target, env=dolt_env)
    try:
        assert reopened.config.name == name
        assert reopened.path == target.resolve()
    finally:
        reopened.close()


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_init_rejects_invalid_names_without_touching_disk(
    tmp_path: Path, dolt_env: dict[str, str], name: str
) -> None:
    data = tmp_path / "data"
    with pytest.raises(UsageError, match="invalid ledger name"):
        init_ledger(
            data / name, name, [PRESETS / "base"], author=FACTORY_AUTHOR, env=dolt_env
        )
    # No ledger directory (and no ../escape) was made.
    assert not data.exists() or list(data.iterdir()) == []
