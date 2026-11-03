"""Ledger resolution (Tetramark-l8g.2): a path and a ``ledger.yaml``, never a workspace.

The oracle is the epic's settled decisions P2-P5:

* P2 -- identity is ``ledger.yaml``'s ``name:``; the path is not identity and
  the directory basename is only the Dolt database name;
* P3 -- ``-l/--ledger PATH`` wins, otherwise the upward walk from the cwd to
  the nearest directory with both ``.dolt/`` and ``ledger.yaml``; **no
  environment variable of any kind**;
* P4 -- meeting ``workspace.yaml`` before any ledger is a usage error naming
  the old workspace and telling the user to ``cd`` into the ledger directory;
* P5 -- ``refuse_nested`` rejects a new ledger inside an existing one.

Dolt-free: every fixture here is a stub directory, so this module is in the
fast lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tetramark.driver.resolution import (
    LEDGER_FILE,
    WORKSPACE_FILE,
    LedgerRef,
    is_ledger_dir,
    ledger_dir,
    ledger_name,
    refuse_nested,
    resolve_ledger,
)
from tetramark.errors import UsageError

pytestmark = pytest.mark.fast


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_ledger(path: Path, *, name: str | None = None) -> Path:
    """Write a stub standalone ledger directory (no real Dolt database)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".dolt").mkdir(exist_ok=True)
    (path / LEDGER_FILE).write_text(
        yaml.safe_dump({"name": name or path.name, "presets": ["base"]}),
        encoding="utf-8",
    )
    return path


def make_workspace(path: Path, *, default_ledger: str | None = None) -> Path:
    """Write a stub 0.4 workspace directory (``workspace.yaml`` only)."""
    path.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"host": "127.0.0.1", "port": 13308}
    if default_ledger is not None:
        payload["default_ledger"] = default_ledger
    (path / WORKSPACE_FILE).write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# P3: the explicit PATH argument
# ---------------------------------------------------------------------------


def test_explicit_path_wins_over_the_upward_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-l PATH`` is authoritative: the cwd ledger is not consulted."""
    cwd_ledger = make_ledger(tmp_path / "cwd-ledger")
    other = make_ledger(tmp_path / "other-ledger", name="identity-from-yaml")
    monkeypatch.chdir(cwd_ledger)

    ref = resolve_ledger(other)

    assert ref == LedgerRef(path=other.resolve(), name="identity-from-yaml")


def test_explicit_path_accepts_a_ledger_yaml_file(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path / "lab")
    assert ledger_dir(ledger / LEDGER_FILE) == ledger.resolve()


def test_explicit_missing_path_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="does not exist"):
        ledger_dir(tmp_path / "nowhere")


def test_explicit_plain_directory_is_not_a_ledger(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UsageError, match="not a ledger"):
        ledger_dir(empty)


# ---------------------------------------------------------------------------
# P3: the upward walk
# ---------------------------------------------------------------------------


def test_upward_search_finds_the_nearest_ledger(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path / "repo" / "ledger")
    deep = ledger / "docs" / "figures"
    deep.mkdir(parents=True)

    assert ledger_dir(cwd=deep) == ledger.resolve()


def test_upward_search_prefers_the_nearest_of_two_ledgers(tmp_path: Path) -> None:
    outer = make_ledger(tmp_path / "data")
    inner = make_ledger(outer / "nested", name="nested")
    deep = inner / "sub"
    deep.mkdir()

    # Even though ``outer`` is itself a ledger, the nearest one wins.  (P5
    # refuses creating this layout; resolution still reads it deterministically.)
    assert ledger_dir(cwd=deep) == inner.resolve()


def test_default_cwd_is_the_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = make_ledger(tmp_path / "lab")
    deep = ledger / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    assert ledger_dir() == ledger.resolve()


def test_no_ledger_found_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as error:
        ledger_dir(cwd=tmp_path)
    message = str(error.value)
    assert "no ledger found" in message
    assert "--ledger PATH" in message


# ---------------------------------------------------------------------------
# P4: the legacy workspace guard
# ---------------------------------------------------------------------------


def test_workspace_yaml_before_any_ledger_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_workspace(tmp_path / "Dolt", default_ledger="zsqlab_05")
    make_ledger(workspace / "zsqlab_05")
    monkeypatch.chdir(workspace)

    with pytest.raises(UsageError) as error:
        ledger_dir()

    message = str(error.value)
    assert "0.4 workspace 'Dolt'" in message
    assert str(workspace) in message
    assert "cd into the ledger directory" in message
    # The named ledgers are suggested as the place to cd into.
    assert str((workspace / "zsqlab_05").resolve()) in message
    assert "workspace.yaml" in message


def test_workspace_guard_does_not_shadow_a_ledger_below_it(tmp_path: Path) -> None:
    """A ledger found before the workspace marker still wins (P3 order)."""
    workspace = make_workspace(tmp_path / "Dolt")
    ledger = make_ledger(workspace / "zsqlab_10")
    deep = ledger / "docs"
    deep.mkdir()

    assert ledger_dir(cwd=deep) == ledger.resolve()


def test_explicit_workspace_directory_gets_the_same_guard(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "Dolt")
    with pytest.raises(UsageError, match="0.4 workspace 'Dolt'"):
        ledger_dir(workspace)


# ---------------------------------------------------------------------------
# P3(d): .dolt/ without ledger.yaml is not a ledger
# ---------------------------------------------------------------------------


def test_dolt_without_ledger_yaml_is_not_a_ledger(tmp_path: Path) -> None:
    bare = tmp_path / "zsqlab_05"
    (bare / ".dolt").mkdir(parents=True)

    with pytest.raises(UsageError) as error:
        ledger_dir(bare)
    message = str(error.value)
    assert "is not a ledger" in message
    assert "no ledger.yaml" in message
    assert "tm adopt" in message


def test_bare_database_stops_the_upward_walk(tmp_path: Path) -> None:
    """Standing in an unformatted database reports it instead of walking past."""
    bare = tmp_path / "zsqlab_05"
    (bare / ".dolt").mkdir(parents=True)
    inner = bare / "sub"
    inner.mkdir()

    with pytest.raises(UsageError, match="is not a ledger"):
        ledger_dir(cwd=inner)


# ---------------------------------------------------------------------------
# P2: identity comes from ledger.yaml
# ---------------------------------------------------------------------------


def test_identity_is_the_ledger_yaml_name_not_the_directory(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path / "renamed-directory", name="zsqlab_10_x")
    assert ledger_name(ledger) == "zsqlab_10_x"
    assert resolve_ledger(ledger).name == "zsqlab_10_x"
    assert resolve_ledger(ledger).path == ledger.resolve()


def test_ledger_yaml_without_a_name_is_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "broken"
    (ledger / ".dolt").mkdir(parents=True)
    (ledger / LEDGER_FILE).write_text("presets: [base]\n", encoding="utf-8")

    with pytest.raises(UsageError, match="'name' must be a non-empty string"):
        resolve_ledger(ledger)


def test_ledger_yaml_that_is_not_a_mapping_is_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "broken"
    (ledger / ".dolt").mkdir(parents=True)
    (ledger / LEDGER_FILE).write_text("- a list\n", encoding="utf-8")

    with pytest.raises(UsageError, match="expected a mapping"):
        resolve_ledger(ledger)


def test_is_ledger_dir_needs_both_halves(tmp_path: Path) -> None:
    assert not is_ledger_dir(tmp_path)
    complete = make_ledger(tmp_path / "complete")
    assert is_ledger_dir(complete)
    (complete / LEDGER_FILE).unlink()
    assert not is_ledger_dir(complete)


# ---------------------------------------------------------------------------
# No environment variable of any kind (P3, REV)
# ---------------------------------------------------------------------------


def test_no_environment_variable_selects_a_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = make_ledger(tmp_path / "lab")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    workspace = make_workspace(tmp_path / "Dolt")

    monkeypatch.setenv("TETRAMARK_LEDGER", str(ledger))
    monkeypatch.setenv("TETRAMARK_WORKSPACE", str(workspace))
    monkeypatch.chdir(elsewhere)

    with pytest.raises(UsageError, match="no ledger found"):
        ledger_dir()


# ---------------------------------------------------------------------------
# P5: no nested ledgers
# ---------------------------------------------------------------------------


def test_refuse_nested_rejects_a_directory_inside_a_ledger(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path / "lab", name="lab-identity")
    target = ledger / "sub" / "new"

    with pytest.raises(UsageError) as error:
        refuse_nested(target)

    message = str(error.value)
    assert "refusing to create a ledger" in message
    assert "'lab-identity'" in message
    assert "do not nest" in message


def test_refuse_nested_allows_a_sibling(tmp_path: Path) -> None:
    make_ledger(tmp_path / "first")
    sibling = tmp_path / "second" / "nested"
    assert refuse_nested(sibling) == sibling.resolve()


def test_refuse_nested_allows_a_ledger_directory_itself(tmp_path: Path) -> None:
    """Re-initialising a directory is not "nesting"; init's own check owns that."""
    existing = make_ledger(tmp_path / "lab")
    assert refuse_nested(existing) == existing.resolve()


def test_refuse_nested_ignores_a_workspace_marker(tmp_path: Path) -> None:
    """A 0.4 workspace is not a ledger: adopting a bare database inside it is fine."""
    workspace = make_workspace(tmp_path / "Dolt")
    bare = workspace / "zsqlab_05"
    (bare / ".dolt").mkdir(parents=True)

    assert refuse_nested(bare) == bare.resolve()
