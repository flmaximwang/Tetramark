"""R2/P9: ``tm init PATH`` creates a ledger and ``tm adopt PATH`` formats one.

The 0.5.0 setup commands are path-based and standalone (the epic's R2, P5, P9):

* ``tm init PATH`` creates ``.dolt/`` and ``ledger.yaml`` **in PATH** (relative
  to the cwd or absolute); ``name:`` defaults to the directory basename and
  ``--name`` overrides it;
* ``tm adopt PATH`` formats an existing bare Dolt database (``.dolt/`` and no
  ``ledger.yaml``) in place, keeping its history -- the path for the 0.4 lab
  databases;
* an existing ledger, a bare database or a non-empty directory is refused by
  ``init`` with a hint that fits, and a bare database points at ``adopt``;
* neither command creates a ledger inside an existing ledger (P5).

The refusal and dry-run tests are Dolt-free (they are marked ``fast``); the
tests that really create/adopt a database use the embedded driver and are
marked ``embedded``.  ``tm check`` on an adopted database is asserted through
the real console entry point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import (
    AdoptPlan,
    adopt_ledger,
    open as open_ledger,
)
from tetramark.errors import EXIT_ERROR, EXIT_USAGE

from conftest import FACTORY_AUTHOR

LEDGER_FILE = "ledger.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_stub_ledger(path: Path, *, name: str | None = None) -> Path:
    """A ledger directory for resolution only (no real Dolt database)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".dolt").mkdir(exist_ok=True)
    (path / LEDGER_FILE).write_text(
        yaml.safe_dump({"name": name or path.name, "presets": ["base"]}),
        encoding="utf-8",
    )
    return path


def run_cli(
    args: list[str | Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run the real console entry point and return (exit code, stdout, stderr).

    ``main()`` is the ``tm`` console script: it maps ``LedgerError`` to its
    documented exit code (1 for a business error, 2 for a usage error), so the
    exit code is asserted here too.
    """
    monkeypatch.setattr(sys, "argv", ["tm", *[str(argument) for argument in args]])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return excinfo.value.code, captured.out, captured.err


def dolt_query(path: Path, dolt_run, sql: str) -> list[dict[str, object]]:
    """Rows of one read-only ``dolt sql -r json`` query in ``path``."""
    output = dolt_run("sql", "-r", "json", "-q", sql, cwd=path).stdout
    return list(json.loads(output).get("rows", []))


def head_hash(path: Path, dolt_run) -> str:
    rows = dolt_query(path, dolt_run, "SELECT DOLT_HASHOF('HEAD') AS h")
    return str(rows[0]["h"])


def object_names(path: Path, dolt_run) -> list[str]:
    """Every table and view currently in the database."""
    return [
        str(row["name"])
        for row in dolt_query(
            path,
            dolt_run,
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name",
        )
    ]


@pytest.fixture
def no_preset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare preset names resolve from the bundled checkout presets (P8)."""
    monkeypatch.delenv("TETRAMARK_PRESETS_DIR", raising=False)


runner = CliRunner()


# ---------------------------------------------------------------------------
# CLI shape: a PATH argument and --name, and a top-level adopt command
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_init_takes_a_path_and_a_name_option() -> None:
    """R2: ``tm init PATH [--name]`` -- PATH is the ledger directory."""
    result = runner.invoke(app, ["init", "-h"])
    assert result.exit_code == 0, result.output
    compact = " ".join(result.output.split())
    assert "Usage: tm init [OPTIONS]" in compact
    assert "--name" in result.output
    assert "directory basename" in compact
    # The 0.4 flag is gone: adoption has its own command now.
    assert "--adopt" not in result.output


@pytest.mark.fast
def test_adopt_is_a_top_level_setup_command() -> None:
    """P9: ``tm adopt PATH`` is its own command, next to ``tm init``."""
    result = runner.invoke(app, ["adopt", "-h"])
    assert result.exit_code == 0, result.output
    compact = " ".join(result.output.split())
    assert "Usage: tm adopt [OPTIONS]" in compact
    assert "--preset" in result.output
    assert "--force-config" in result.output
    assert "in place" in compact
    # ``--workspace`` and the old ``init --adopt`` spelling are both gone.
    fold = result.output.lower()
    assert "workspace" not in fold
    assert "--adopt" not in result.output


@pytest.mark.fast
def test_init_no_longer_accepts_the_old_adopt_flag() -> None:
    result = runner.invoke(app, ["init", "target", "--adopt", "-p", "base"])
    assert result.exit_code == EXIT_USAGE
    assert "no such option" in result.output.lower()


@pytest.mark.fast
def test_force_config_belongs_to_adopt_only() -> None:
    result = runner.invoke(app, ["init", "target", "--force-config", "-p", "base"])
    assert result.exit_code == EXIT_USAGE
    assert "no such option" in result.output.lower()


@pytest.mark.fast
def test_both_commands_require_a_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_cli(
        ["--dry-run", "init", tmp_path / "one"], monkeypatch, capsys
    )
    assert code == EXIT_USAGE
    assert "at least one --preset" in err

    code, out, err = run_cli(
        ["--dry-run", "adopt", tmp_path / "two"], monkeypatch, capsys
    )
    assert code == EXIT_USAGE
    assert "at least one --preset" in err


# ---------------------------------------------------------------------------
# init: PATH, --name, dry run (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_init_dry_run_uses_the_path_and_the_basename(
    tmp_path: Path, no_preset_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH is relative to the cwd; ``name:`` defaults to its basename."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    target = Path("labs") / "lab42"

    result = runner.invoke(
        app,
        [
            "--dry-run",
            "init",
            str(target),
            "-p",
            "base",
            "--author",
            FACTORY_AUTHOR,
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Would create ledger lab42 at {work.resolve() / target}" in result.output
    assert "CREATE TABLE `ledger_meta`" in result.output
    assert "presets : base" in result.output
    assert not (work / target).exists()


@pytest.mark.fast
def test_init_dry_run_name_option_overrides_the_basename(
    tmp_path: Path, no_preset_env: None
) -> None:
    target = tmp_path / "directory-name"

    result = runner.invoke(
        app,
        [
            "--dry-run",
            "init",
            str(target),
            "--name",
            "lab-custom",
            "-p",
            "base",
            "--author",
            FACTORY_AUTHOR,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Would create ledger lab-custom at" in result.output
    assert not target.exists()


# ---------------------------------------------------------------------------
# init: existing paths get a hint that fits (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_init_refuses_a_bare_database_and_points_at_adopt(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P9: ``.dolt/`` without ``ledger.yaml`` is adopted, never re-initialised."""
    bare = tmp_path / "zsqlab_05"
    (bare / ".dolt").mkdir(parents=True)

    code, out, err = run_cli(
        ["init", bare, "-p", "base", "--author", FACTORY_AUTHOR], monkeypatch, capsys
    )

    assert code == EXIT_ERROR
    assert "bare Dolt database" in err
    assert f"tm adopt {bare.resolve()}" in err
    assert not (bare / LEDGER_FILE).exists()


@pytest.mark.fast
def test_init_refuses_an_existing_ledger(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = make_stub_ledger(tmp_path / "lab", name="lab-identity")

    code, out, err = run_cli(
        ["init", ledger, "-p", "base", "--author", FACTORY_AUTHOR],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_ERROR
    assert "already the ledger 'lab-identity'" in err
    assert "tm --ledger" in err
    # Nothing was re-created over the existing one.
    assert yaml.safe_load((ledger / LEDGER_FILE).read_text())["name"] == "lab-identity"


@pytest.mark.fast
def test_init_refuses_a_non_empty_non_ledger_directory(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("do not clobber me", encoding="utf-8")

    code, out, err = run_cli(
        ["init", occupied, "-p", "base", "--author", FACTORY_AUTHOR],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_ERROR
    assert "not an empty directory" in err
    assert (occupied / "notes.txt").read_text(encoding="utf-8") == "do not clobber me"


# ---------------------------------------------------------------------------
# adopt: refusals that need no connection (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_adopt_refuses_a_path_without_a_database(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "not-a-database"

    code, out, err = run_cli(
        ["adopt", missing, "-p", "base", "--author", FACTORY_AUTHOR],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_ERROR
    assert "no Dolt database" in err
    assert "tm init" in err


@pytest.mark.fast
def test_adopt_refuses_an_already_formatted_ledger(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = make_stub_ledger(tmp_path / "lab", name="lab-identity")

    code, out, err = run_cli(
        ["adopt", ledger, "-p", "base", "--author", FACTORY_AUTHOR],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_ERROR
    assert "the ledger 'lab-identity'" in err
    assert "nothing to adopt" in err
    assert "--force-config" in err


# ---------------------------------------------------------------------------
# P5: no nested ledgers, through the CLI (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_nested_init_is_refused_before_anything_is_written(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outer = make_stub_ledger(tmp_path / "lab", name="lab-identity")
    inside = outer / "child"
    inside.mkdir()

    code, out, err = run_cli(
        ["init", inside / "nested", "-p", "base"], monkeypatch, capsys
    )

    assert code == EXIT_USAGE
    assert "Ledgers do not nest" in err
    assert "'lab-identity'" in err
    assert not (inside / "nested").exists()


@pytest.mark.fast
def test_nested_adopt_is_refused(
    tmp_path: Path,
    no_preset_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outer = make_stub_ledger(tmp_path / "lab", name="lab-identity")
    bare = outer / "bare"
    (bare / ".dolt").mkdir(parents=True)

    code, out, err = run_cli(
        ["adopt", bare, "-p", "base", "--author", FACTORY_AUTHOR],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_USAGE
    assert "Ledgers do not nest" in err
    assert "'lab-identity'" in err
    assert not (bare / LEDGER_FILE).exists()


# ---------------------------------------------------------------------------
# Real init and adopt (embedded driver, isolated Dolt/Git environment)
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_cli_init_creates_the_ledger_directory_at_path(
    tmp_path: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 end to end: ``tm init <relative PATH>`` creates PATH as the ledger."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    result = runner.invoke(
        app,
        ["init", str(Path("labs") / "lab42"), "-p", "base", "--author", FACTORY_AUTHOR],
        env=dolt_env,
    )

    assert result.exit_code == 0, result.output
    target = work / "labs" / "lab42"
    assert (target / ".dolt").is_dir()
    assert (target / LEDGER_FILE).is_file()
    assert f"Created ledger lab42 at {target.resolve()}" in result.output

    ledger = open_ledger(target, env=dolt_env)
    assert ledger.name == "lab42"
    assert ledger.author == FACTORY_AUTHOR
    assert ledger.config.presets == ["base"]
    assert (target / "presets" / "base.yaml").is_file()


@pytest.mark.embedded
def test_cli_init_name_option_sets_the_identity_not_the_directory(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    target = tmp_path / "directory-name"

    result = runner.invoke(
        app,
        ["init", str(target), "--name", "lab-custom", "-p", "base", "--author", FACTORY_AUTHOR],
        env=dolt_env,
    )

    assert result.exit_code == 0, result.output
    ledger = open_ledger(target, env=dolt_env)
    assert ledger.name == "lab-custom"
    assert ledger.config.name == "lab-custom"
    assert yaml.safe_load((target / LEDGER_FILE).read_text())["name"] == "lab-custom"
    # The path (not the identity) names the Dolt database.
    assert ledger.driver.database == "directory-name"


@pytest.mark.embedded
def test_cli_init_accepts_an_existing_empty_directory(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """``mkdir lab && tm init lab`` is a valid standalone setup."""
    target = tmp_path / "pre-made"
    target.mkdir()

    result = runner.invoke(
        app,
        ["init", str(target), "-p", "base", "--author", FACTORY_AUTHOR],
        env=dolt_env,
    )

    assert result.exit_code == 0, result.output
    assert open_ledger(target, env=dolt_env).name == "pre-made"


@pytest.mark.embedded
def test_cli_adopt_formats_a_bare_database_in_place_and_check_passes(
    tmp_path: Path, dolt_env: dict[str, str], dolt_run, make_ledger
) -> None:
    """P9: the 0.4-lab path -- ``dolt init`` then ``tm adopt`` in place."""
    bare = make_ledger(tmp_path / "zsqlab_05")
    history_before = len(
        dolt_run("log", "--oneline", cwd=bare).stdout.strip().splitlines()
    )
    head_before = head_hash(bare, dolt_run)

    result = runner.invoke(
        app,
        ["adopt", str(bare), "-p", "base", "--author", FACTORY_AUTHOR],
        env=dolt_env,
    )

    assert result.exit_code == 0, result.output
    assert "Adopted ledger zsqlab_05 at" in result.output
    assert (bare / LEDGER_FILE).is_file()
    assert (bare / "presets" / "base.yaml").is_file()

    ledger = open_ledger(bare, env=dolt_env)
    assert ledger.name == "zsqlab_05"
    assert ledger.author == FACTORY_AUTHOR
    # The history is kept and the adoption is one commit on top of it.
    history_after = dolt_run("log", "--oneline", cwd=bare).stdout.strip().splitlines()
    assert len(history_after) == history_before + 1
    assert ledger.meta["adopted_from_commit"] == head_before
    commit_message = dolt_query(
        bare, dolt_run, "SELECT message FROM dolt_log ORDER BY date DESC LIMIT 1"
    )[0]["message"]
    assert commit_message == "adopt(ledger): zsqlab_05"

    check = runner.invoke(app, ["-l", str(bare), "check"], env=dolt_env)
    assert check.exit_code == 0, check.output
    assert "OK:" in check.output


@pytest.mark.embedded
def test_adopt_dry_run_reports_and_writes_nothing(
    tmp_path: Path, dolt_env: dict[str, str], dolt_run, make_ledger
) -> None:
    bare = make_ledger(tmp_path / "legacy")
    head_before = head_hash(bare, dolt_run)
    objects_before = object_names(bare, dolt_run)

    plan = adopt_ledger(
        bare,
        None,
        ["base"],
        author=FACTORY_AUTHOR,
        dry_run=True,
        env=dolt_env,
    )

    assert isinstance(plan, AdoptPlan)
    assert plan.name == "legacy"
    assert plan.adopted_from_commit == head_before
    assert not (bare / LEDGER_FILE).exists()
    assert not (bare / "presets").exists()
    assert head_hash(bare, dolt_run) == head_before
    assert object_names(bare, dolt_run) == objects_before == []


@pytest.mark.embedded
def test_adopt_core_defaults_the_name_to_the_directory_basename(
    tmp_path: Path, dolt_env: dict[str, str], make_ledger
) -> None:
    bare = make_ledger(tmp_path / "zsqlab_08")

    ledger = adopt_ledger(bare, None, ["base"], author=FACTORY_AUTHOR, env=dolt_env)

    assert ledger.name == "zsqlab_08"
    assert ledger.path == bare.resolve()
    assert (bare / LEDGER_FILE).is_file()
