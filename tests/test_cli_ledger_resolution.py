"""P3/P4/P5 through the CLI: ``-l PATH``, discovery and the 0.5.0 errors.

The resolver's unit tests live in :mod:`tests.test_ledger_resolution`; this
module is the *command-line surface* contract of Tetramark-l8g.4:

* ``tm -l PATH <command>`` works from any cwd (P3);
* ``cd`` into a ledger and running with no flags works (P3);
* "no ledger found", "not a ledger", the P4 legacy-workspace guard and the P5
  nested-ledger refusal are usage errors (exit code 2 through ``main()``) whose
  message names the path/cwd and says what to do next.

The error paths are Dolt-free and marked ``fast``; the two success paths use a
real ledger through :func:`ledger_factory` and are marked ``embedded``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from tetramark.cli.app import main
from tetramark.core import ledger as ledger_module
from tetramark.errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE

WORKSPACE_FILE = "workspace.yaml"
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


def make_stub_bare_database(path: Path) -> Path:
    """A bare Dolt database: ``.dolt/noms/`` and no ``ledger.yaml``."""
    (path / ".dolt" / "noms").mkdir(parents=True)
    return path


def make_stub_config_only_dolt(path: Path) -> Path:
    """Dolt's global configuration directory: ``.dolt/`` without a database."""
    dolt = path / ".dolt"
    (dolt / "creds").mkdir(parents=True)
    (dolt / "eventsData").mkdir()
    (dolt / "config_global.json").write_text('{"user": {}}\n', encoding="utf-8")
    (dolt / "version_check.txt").write_text("1.0.0\n", encoding="utf-8")
    return path


def make_stub_workspace(path: Path, *, default_ledger: str | None = None) -> Path:
    """A 0.4 workspace directory: ``workspace.yaml`` and nothing else."""
    path.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"host": "127.0.0.1", "port": 13308}
    if default_ledger is not None:
        payload["default_ledger"] = default_ledger
    (path / WORKSPACE_FILE).write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def run_cli(
    args: list[str | Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run the real console entry point and return (exit code, stdout, stderr).

    ``main()`` is the ``tm`` console script; unlike a bare ``CliRunner(app)``
    it maps :class:`~tetramark.errors.LedgerError` to the documented process
    exit code (2 for a usage error), so the exit code is asserted here too.
    """
    monkeypatch.setattr(sys, "argv", ["tm", *[str(argument) for argument in args]])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return excinfo.value.code, captured.out, captured.err


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# P3: success paths -- explicit path and the upward walk
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_dash_l_path_works_from_a_foreign_cwd(
    cli_env: None,
    ledger_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tm -l <ledger> ...`` needs no ledger anywhere above the cwd."""
    ledger = ledger_factory(name="cli-foreign")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    code, out, err = run_cli(
        ["-l", ledger.path, "config", "get", "author"], monkeypatch, capsys
    )

    assert code == EXIT_OK, err
    assert err == ""
    assert out.strip() == ledger.author


@pytest.mark.embedded
def test_inside_a_ledger_needs_no_flags(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Standing in a ledger (or below it) is enough: no ``-l`` and no workspace."""
    ledger = ledger_factory(name="cli-inside")
    nested = ledger.path / "docs" / "figures"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    code, out, err = run_cli(["config", "get", "author"], monkeypatch, capsys)

    assert code == EXIT_OK, err
    assert out.strip() == ledger.author


# ---------------------------------------------------------------------------
# P3/P4: errors are usage errors (exit 2) with actionable messages
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_no_ledger_found_exits_2_and_names_the_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    code, out, err = run_cli(["config", "get", "author"], monkeypatch, capsys)

    assert code == EXIT_USAGE == 2
    assert "no ledger found from" in err
    assert str(empty.resolve()) in err
    assert "--ledger PATH" in err
    assert "cd into a ledger directory" in err


@pytest.mark.fast
def test_explicit_missing_path_exits_2_and_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "not-there"

    code, out, err = run_cli(["-l", missing, "config", "get"], monkeypatch, capsys)

    assert code == EXIT_USAGE
    assert str(missing) in err
    assert "does not exist" in err


@pytest.mark.fast
def test_a_plain_directory_exits_2_and_explains_what_a_ledger_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    code, out, err = run_cli(["-l", plain, "config", "get"], monkeypatch, capsys)

    assert code == EXIT_USAGE
    assert "is not a ledger" in err
    assert str(plain) in err
    assert "tm init" in err


@pytest.mark.fast
def test_an_unformatted_database_exits_2_with_the_adopt_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare 0.4 Dolt database (``.dolt/``, no ``ledger.yaml``) is not a ledger."""
    bare = make_stub_bare_database(tmp_path / "zsqlab_05")

    code, out, err = run_cli(["-l", bare, "config", "get"], monkeypatch, capsys)

    assert code == EXIT_USAGE
    assert "is not a ledger" in err
    assert "no ledger.yaml" in err
    assert "tm adopt" in err


@pytest.mark.fast
def test_a_config_only_dolt_home_is_walked_past(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dolt's global ``~/.dolt`` is no ledger: the error says "no ledger found"."""
    home = make_stub_config_only_dolt(tmp_path / "home")
    monkeypatch.chdir(home)

    code, out, err = run_cli(["config", "get", "author"], monkeypatch, capsys)

    assert code == EXIT_USAGE == 2
    assert "no ledger found from" in err
    assert str(home.resolve()) in err
    assert "tm adopt" not in err


@pytest.mark.fast
def test_a_config_only_dolt_home_is_walked_past_from_a_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory below Dolt's global config dir finds no ledger either."""
    home = make_stub_config_only_dolt(tmp_path / "home")
    child = home / "projects" / "notes"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    code, out, err = run_cli(["config", "get", "author"], monkeypatch, capsys)

    assert code == EXIT_USAGE == 2
    assert "no ledger found from" in err
    assert str(child.resolve()) in err
    assert "tm adopt" not in err


@pytest.mark.fast
def test_an_explicit_config_only_dolt_says_it_is_the_global_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tm -l ~/.dolt`` names Dolt's config directory -- without an adopt hint."""
    home = make_stub_config_only_dolt(tmp_path / "home")

    code, out, err = run_cli(["-l", home, "config", "get"], monkeypatch, capsys)

    assert code == EXIT_USAGE == 2
    assert "is not a ledger" in err
    assert "global configuration directory" in err
    assert str(home.resolve()) in err
    assert "tm adopt" not in err
    assert "tm init" in err


@pytest.mark.fast
def test_adopt_refuses_a_config_only_dolt_before_any_dolt_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tm adopt ~/.dolt`` never runs ``dolt`` against the global config."""
    home = make_stub_config_only_dolt(tmp_path / "home")
    before = sorted(entry.name for entry in (home / ".dolt").iterdir())

    def _no_driver(*args: object, **kwargs: object) -> object:
        raise AssertionError("dolt must not be run against a config-only .dolt/")

    monkeypatch.setattr(ledger_module, "driver_for", _no_driver)

    code, out, err = run_cli(
        ["adopt", home, "-p", "base", "--author", "Tester <t@example.invalid>"],
        monkeypatch,
        capsys,
    )

    assert code == EXIT_ERROR
    assert "global configuration directory" in err
    assert "tm adopt" not in err
    # Nothing was written into the config directory.
    assert sorted(entry.name for entry in (home / ".dolt").iterdir()) == before
    assert not (home / LEDGER_FILE).exists()


@pytest.mark.fast
def test_the_workspace_guard_exits_2_and_suggests_cd_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P4: standing in a 0.4 workspace names it and points at the ledger."""
    workspace = make_stub_workspace(tmp_path / "Dolt", default_ledger="zsqlab_05")
    ledger = make_stub_ledger(workspace / "zsqlab_05")
    monkeypatch.chdir(workspace)

    code, out, err = run_cli(["config", "get", "author"], monkeypatch, capsys)

    assert code == EXIT_USAGE
    assert "0.4 workspace 'Dolt'" in err
    assert "workspace.yaml" in err  # the one place the removed file is named
    assert f"cd {ledger.resolve()}" in err
    assert f"tm --ledger {ledger.resolve()}" in err


@pytest.mark.fast
def test_the_workspace_guard_also_covers_an_explicit_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = make_stub_workspace(tmp_path / "Dolt")
    make_stub_ledger(workspace / "zsqlab_05")

    code, out, err = run_cli(
        ["-l", workspace, "config", "get", "author"], monkeypatch, capsys
    )

    assert code == EXIT_USAGE
    assert "0.4 workspace 'Dolt'" in err
    assert str(workspace.resolve()) in err


@pytest.mark.fast
def test_nested_init_exits_2_naming_the_enclosing_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P5: ``tm init`` inside an existing ledger is a usage error, before any write."""
    outer = make_stub_ledger(tmp_path / "lab", name="lab-identity")
    inside = outer / "child"
    inside.mkdir()
    monkeypatch.chdir(inside)

    code, out, err = run_cli(
        ["init", "newledger", "--preset", "base"], monkeypatch, capsys
    )

    assert code == EXIT_USAGE
    assert "Ledgers do not nest" in err
    assert "'lab-identity'" in err
    assert str(outer.resolve()) in err
    assert not (inside / "newledger").exists()
