"""Remote, backup and author configuration (ticket 13, spec §6/§1.1).

Every test drives the real ``dolt`` binary through the isolated workspace from
``conftest.py``; no Dolt behaviour is mocked.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import LEDGER_FILE, Ledger, open as open_ledger
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import LedgerError, UsageError
from tetramark.ops import remote

from conftest import FACTORY_AUTHOR, PRESETS

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_isolated(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> None:
    """Point the CLI at the same isolated Dolt/Git configuration as the tests."""
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


def add_entity(ledger: Ledger, entity_id: str, name: str) -> None:
    """Seed one entity row through the one-commit write session."""
    with ledger.write(f"add {entity_id}", verb="add", object="entity") as session:
        session.execute(
            "INSERT INTO `entity` (`id`, `kind`, `name`) VALUES (%s, %s, %s)",
            (entity_id, "item", name),
        )


def entity_rows(ledger: Ledger) -> list[dict[str, object]]:
    return ledger.driver.query(
        "SELECT `id`, `kind`, `name` FROM `entity` ORDER BY `id`"
    )


def table_names(ledger: Ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `information_schema`.`tables` "
        "WHERE `table_schema` = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


def clone_ledger(ledger: Ledger, dolt_run, name: str = "clone") -> Ledger:
    """Clone ``ledger`` from its pushed remote into the same workspace."""
    url = remote.list_remotes(ledger)[0].url
    target = ledger.workspace.ledger_path(name)
    dolt_run("clone", url, str(target))
    # Dolt clones the database, not plain files: a ledger needs its config and
    # its own preset copies (ticket 20; they are not package data and not in
    # the database). A real clone copies them next to ledger.yaml as well.
    shutil.copy(ledger.path / LEDGER_FILE, target / LEDGER_FILE)
    shutil.copytree(ledger.path / "presets", target / "presets")
    return open_ledger(ledger.workspace, name)


# ---------------------------------------------------------------------------
# Remotes
# ---------------------------------------------------------------------------


def test_remote_add_ls_remove_and_idempotency(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    remote_dir = tmp_path / "remote"

    result = remote.add_remote(ledger, "origin", remote_dir)

    assert result.dry_run is False
    assert len(result.statements) == 1
    assert "DOLT_REMOTE('add'" in result.statements[0]
    assert remote_dir.resolve().as_uri() in result.statements[0]
    assert remote.list_remotes(ledger) == [
        remote.Remote(name="origin", url=remote_dir.resolve().as_uri())
    ]

    again = remote.add_remote(ledger, "origin", remote_dir)
    assert again.statements == ()
    assert "already points to" in again.message

    removed = remote.remove_remote(ledger, "origin")
    assert "DOLT_REMOTE('remove'" in removed.statements[0]
    assert remote.list_remotes(ledger) == []


def test_remote_add_conflicting_url_is_an_error(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    remote.add_remote(ledger, "origin", tmp_path / "one")

    with pytest.raises(LedgerError, match="already points to"):
        remote.add_remote(ledger, "origin", tmp_path / "two")

    with pytest.raises(LedgerError, match="no remote named 'missing'"):
        remote.remove_remote(ledger, "missing")


def test_push_requires_a_remote(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    with pytest.raises(UsageError, match="no remote configured"):
        remote.push(ledger)


def test_push_clone_then_pull_sees_the_same_data(
    ledger_factory, dolt_run, tmp_path: Path
) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    add_entity(ledger, "e1", "one")
    remote.add_remote(ledger, "origin", tmp_path / "remote")

    pushed = remote.push(ledger)
    assert "DOLT_PUSH('origin', 'main')" in pushed.statements[0]
    assert "main" in pushed.message

    clone = clone_ledger(ledger, dolt_run, name="clone")
    assert entity_rows(clone) == [{"id": "e1", "kind": "item", "name": "one"}]
    assert table_names(clone) == table_names(ledger)

    add_entity(ledger, "e2", "two")
    remote.push(ledger)
    pulled = remote.pull(clone)

    assert "DOLT_PULL('origin', 'main')" in pulled.statements[0]
    assert entity_rows(clone) == entity_rows(ledger)
    assert entity_rows(clone) == [
        {"id": "e1", "kind": "item", "name": "one"},
        {"id": "e2", "kind": "item", "name": "two"},
    ]

    # Pulling again is harmless and reports that nothing moved.
    again = remote.pull(clone)
    assert "up-to-date" in again.message.lower()


def test_push_dry_run_writes_nothing(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    remote_dir = tmp_path / "remote"
    remote.add_remote(ledger, "origin", remote_dir)

    result = remote.push(ledger, dry_run=True)

    assert result.dry_run is True
    assert result.statements == ("CALL DOLT_PUSH('origin', 'main')",)
    # Dolt creates the remote directory when the remote is added, but the
    # dry run must not push anything into it.
    assert not any(remote_dir.iterdir())

    remote.push(ledger)
    assert any(remote_dir.iterdir())


def test_multiple_remotes_prefer_origin_unless_one_is_named(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    remote.add_remote(ledger, "mirror", tmp_path / "mirror")
    remote.add_remote(ledger, "origin", tmp_path / "origin")

    assert remote.resolve_remote(ledger).name == "origin"
    assert remote.resolve_remote(ledger, "mirror").name == "mirror"
    with pytest.raises(LedgerError, match="no remote named 'absent'"):
        remote.resolve_remote(ledger, "absent")


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def test_backup_default_directory_is_restorable(
    ledger_factory, dolt_run, dolt_conf, tmp_path: Path
) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    add_entity(ledger, "e1", "one")

    expected_dir = ledger.workspace.data_dir / "backups" / ledger.name
    result = remote.backup(ledger)

    assert result.dry_run is False
    assert any("DOLT_BACKUP('add'" in statement for statement in result.statements)
    assert any("DOLT_BACKUP('sync'" in statement for statement in result.statements)
    assert expected_dir.is_dir()
    assert remote.list_backups(ledger) == [
        remote.Backup(name=ledger.name, url=expected_dir.resolve().as_uri())
    ]
    assert remote.default_backup_dir(ledger) == expected_dir

    restored_dir = tmp_path / "restored"
    dolt_run(
        "backup",
        "restore",
        expected_dir.resolve().as_uri(),
        restored_dir.name,
    )
    restored = EmbeddedDriver(restored_dir)
    assert restored.query(
        "SELECT `id`, `kind`, `name` FROM `entity` ORDER BY `id`"
    ) == [{"id": "e1", "kind": "item", "name": "one"}]


def test_backup_explicit_directory_replaces_the_destination(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    first = tmp_path / "first"

    remote.backup(ledger, first)
    assert first.is_dir()

    # The same destination only needs a sync.
    second = remote.backup(ledger, first)
    assert second.statements == (f"CALL DOLT_BACKUP('sync', '{ledger.name}')",)

    other = tmp_path / "other"
    changed = remote.backup(ledger, other)
    assert any("DOLT_BACKUP('remove'" in statement for statement in changed.statements)
    assert any(f"'{other.resolve().as_uri()}'" in statement for statement in changed.statements)
    assert other.is_dir()
    assert remote.list_backups(ledger)[0].url == other.resolve().as_uri()


def test_backup_dry_run_writes_nothing(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    target = tmp_path / "not-made"

    result = remote.backup(ledger, target, dry_run=True)

    assert result.dry_run is True
    assert any("DOLT_BACKUP('add'" in statement for statement in result.statements)
    assert any("DOLT_BACKUP('sync'" in statement for statement in result.statements)
    assert not target.exists()
    assert remote.list_backups(ledger) == []


# ---------------------------------------------------------------------------
# Author configuration
# ---------------------------------------------------------------------------


def test_set_author_updates_yaml_meta_and_commit_author(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="authors")
    before = int(
        ledger.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")[0]["n"]
    )
    previous = ledger.author
    assert previous == FACTORY_AUTHOR

    session = ledger.set_author("New Author <new@tetramark.invalid>")

    assert session.dry_run is False
    assert session.commit_hash
    assert (ledger.path / LEDGER_FILE).read_text(encoding="utf-8").count("new@tetramark.invalid") == 1
    payload = yaml.safe_load((ledger.path / LEDGER_FILE).read_text(encoding="utf-8"))
    assert payload["author"] == "New Author <new@tetramark.invalid>"
    meta = ledger.driver.query(
        "SELECT `value` FROM `ledger_meta` WHERE `key` = 'ledger_yaml'"
    )[0]["value"]
    assert "New Author <new@tetramark.invalid>" in meta

    after = int(
        ledger.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")[0]["n"]
    )
    assert after == before + 1
    latest = ledger.driver.query(
        "SELECT `message`, `author`, `author_email` FROM `dolt_log` "
        "ORDER BY `date` DESC, `commit_order` DESC LIMIT 1"
    )[0]
    assert latest["message"] == "config(ledger): set author"
    assert latest["author"] == "New Author"
    assert latest["author_email"] == "new@tetramark.invalid"
    assert ledger.author == "New Author <new@tetramark.invalid>"
    assert open_ledger(ledger.workspace, "authors").author == "New Author <new@tetramark.invalid>"


def test_set_author_dry_run_and_validation(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="authors")
    ledger.dry_run = True
    try:
        session = ledger.set_author("Dry Run <dry@tetramark.invalid>")
    finally:
        ledger.dry_run = False

    assert session.dry_run is True
    assert session.statements and "ledger_meta" in session.statements[0]
    assert ledger.author == FACTORY_AUTHOR
    assert "dry@tetramark.invalid" not in (ledger.path / LEDGER_FILE).read_text(
        encoding="utf-8"
    )

    with pytest.raises(UsageError, match="Name <email>"):
        ledger.set_author("Only A Name")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_remote_push_and_backup_workflow(
    ledger_factory, cli_isolated, tmp_path: Path
) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="cli")
    workspace = str(ledger.workspace.data_dir)
    remote_dir = tmp_path / "cli-remote"
    prefix = ["--workspace", workspace, "--ledger", "cli"]

    added = runner.invoke(app, [*prefix, "remote", "add", "origin", str(remote_dir)])
    assert added.exit_code == 0, added.output
    assert "origin" in added.output
    assert remote_dir.resolve().as_uri() in added.output

    listed = runner.invoke(app, [*prefix, "remote", "ls"])
    assert listed.exit_code == 0, listed.output
    assert "origin" in listed.output
    assert remote_dir.resolve().as_uri() in listed.output

    pushed = runner.invoke(app, [*prefix, "remote", "push"])
    assert pushed.exit_code == 0, pushed.output
    assert "main" in pushed.output
    assert remote_dir.exists()

    empty = runner.invoke(app, [*prefix, "backup", "ls"])
    assert empty.exit_code == 0, empty.output
    assert "no backups configured" in empty.output

    backed_up = runner.invoke(app, [*prefix, "backup"])
    assert backed_up.exit_code == 0, backed_up.output
    expected_dir = ledger.workspace.data_dir / "backups" / "cli"
    assert expected_dir.is_dir()

    backups = runner.invoke(app, [*prefix, "backup", "ls"])
    assert backups.exit_code == 0, backups.output
    assert "cli" in backups.output
    assert expected_dir.resolve().as_uri() in backups.output


def test_cli_config_get_and_set_author(
    ledger_factory, cli_isolated
) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="cli-author")
    prefix = ["--workspace", str(ledger.workspace.data_dir), "--ledger", "cli-author"]

    got = runner.invoke(app, [*prefix, "config", "get", "author"])
    assert got.exit_code == 0, got.output
    assert FACTORY_AUTHOR in got.output

    new_author = "CLI Author <cli-author@tetramark.invalid>"
    updated = runner.invoke(app, [*prefix, "config", "set", "author", new_author])
    assert updated.exit_code == 0, updated.output
    assert new_author in updated.output

    got = runner.invoke(app, [*prefix, "config", "get", "author"])
    assert got.exit_code == 0, got.output
    assert new_author in got.output
    assert open_ledger(ledger.workspace, "cli-author").author == new_author

    unknown = runner.invoke(app, [*prefix, "config", "get", "colour"])
    assert unknown.exit_code != 0
    assert isinstance(unknown.exception, UsageError)


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
