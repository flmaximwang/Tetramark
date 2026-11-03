"""R3: ``tm status`` for a standalone ledger (Tetramark-l8g.9).

``tm status`` is the 0.5.0 ledger overview: path, ``ledger.yaml`` name, branch,
``spec_version``, presets, author, the write-lock holder (P11) and portability
warnings (P13).  There is no ledger list and no workspace/server status.

The JSON shape is an API: it is fixed, documented in
:mod:`tetramark.cli.status` and asserted here key by key and type by type.  The
fast-lane tests cover the renderer and the CLI's own errors; the embedded tests
run against real temporary ledgers created by the session template factory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.cli.status import (
    LOCK_KEYS,
    STATUS_KEYS,
    WARNING_KEYS,
    render_human,
    status_command,
)
from tetramark.core.writelock import WriteLock, lock_path
from tetramark.errors import EXIT_USAGE
from tetramark.schema.model import SPEC_VERSION

from conftest import FACTORY_AUTHOR

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sample_status(**updates: object) -> dict[str, object]:
    """A complete status payload with the documented shape."""
    payload: dict[str, object] = {
        "path": "/tmp/lab/zsqlab_10",
        "name": "zsqlab_10",
        "branch": "main",
        "spec_version": SPEC_VERSION,
        "presets": ["molbio"],
        "author": "Wang Fanlin <wang@example.invalid>",
        "lock": {
            "held": False,
            "pid": None,
            "command": None,
            "since": None,
            "host": None,
        },
        "portability_warnings": [],
    }
    payload.update(updates)
    return payload


def field(output: str, label: str) -> str:
    """The value of one ``label<space>value`` human-output line."""
    for line in output.splitlines():
        if line.startswith(f"{label} "):
            return line[len(label) :].strip()
    raise AssertionError(f"no {label!r} line in output:\n{output}")


def run_cli(
    args: list[str | Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run the console entry point and return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["tm", *[str(argument) for argument in args]])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return excinfo.value.code, captured.out, captured.err


def invoke_status(*args: str):
    """Run ``tm status`` through the Typer app and return the result."""
    return runner.invoke(app, [*args, "status"])


# ---------------------------------------------------------------------------
# The fixed shape (fast lane: no Dolt)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_json_key_sets_are_the_documented_ones() -> None:
    """The constants are the API: tests compare them verbatim."""
    assert STATUS_KEYS == (
        "path",
        "name",
        "branch",
        "spec_version",
        "presets",
        "author",
        "lock",
        "portability_warnings",
    )
    assert LOCK_KEYS == ("held", "pid", "command", "since", "host")
    assert WARNING_KEYS == ("kind", "name", "url", "path")

    payload = sample_status()
    assert set(payload) == set(STATUS_KEYS)
    assert set(payload["lock"]) == set(LOCK_KEYS)


@pytest.mark.fast
def test_status_is_a_read_command_and_never_takes_the_write_lock() -> None:
    """P11: only the lock *probe* runs; the command is not a write command."""
    from tetramark.cli.options import WRITE_COMMAND_MARKER

    assert getattr(status_command, WRITE_COMMAND_MARKER, False) is False


@pytest.mark.fast
def test_human_output_prints_every_field() -> None:
    """One aligned line per field, no ledger list, no workspace wording."""
    output = render_human(sample_status())

    assert field(output, "path") == "/tmp/lab/zsqlab_10"
    assert field(output, "name") == "zsqlab_10"
    assert field(output, "branch") == "main"
    assert field(output, "spec_version") == str(SPEC_VERSION)
    assert field(output, "presets") == "molbio"
    assert field(output, "author") == "Wang Fanlin <wang@example.invalid>"
    assert field(output, "write lock") == "free"
    assert field(output, "portability") == "no warnings"
    assert "workspace" not in output.lower()
    assert "server" not in output.lower()


@pytest.mark.fast
def test_human_output_renders_a_held_lock() -> None:
    """A busy lock names the writer instead of saying 'free'."""
    payload = sample_status(
        lock={
            "held": True,
            "pid": 4711,
            "command": "tm stock add",
            "since": "2026-11-03T05:00:00Z",
            "host": "lab.example",
        }
    )
    text = field(render_human(payload), "write lock")
    assert text == (
        "pid 4711 (tm stock add) since 2026-11-03T05:00:00Z on lab.example"
    )


@pytest.mark.fast
def test_human_output_lists_each_portability_warning() -> None:
    """Every warning is one indented line naming kind, name and URL."""
    warnings = [
        {
            "kind": "remote",
            "name": "origin",
            "url": "file:///old/remote",
            "path": "/old/remote",
        },
        {
            "kind": "backup",
            "name": "pre-spec4",
            "url": "file:///old/backups/zsqlab_10",
            "path": "/old/backups/zsqlab_10",
        },
    ]
    output = render_human(sample_status(portability_warnings=warnings))

    assert field(output, "portability") == "2 warning(s):"
    assert "remote 'origin': file:///old/remote" in output
    assert "backup 'pre-spec4': file:///old/backups/zsqlab_10" in output


@pytest.mark.fast
def test_human_output_of_missing_values_is_readable() -> None:
    """A ledger with no identity-adjacent values still renders without crashing."""
    payload = sample_status(
        branch=None, spec_version=None, presets=[], author=None
    )
    output = render_human(payload)
    assert field(output, "branch") == "(unknown)"
    assert field(output, "spec_version") == "(unknown)"
    assert field(output, "presets") == "(none)"
    assert field(output, "author") == "(none)"


# ---------------------------------------------------------------------------
# CLI errors (fast lane: the resolver fails before any Dolt process)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_missing_ledger_is_a_usage_error_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = run_cli(
        ["--ledger", tmp_path / "nowhere", "status"], monkeypatch, capsys
    )
    assert code == EXIT_USAGE
    assert "no ledger at" in err


@pytest.mark.fast
def test_not_a_ledger_directory_is_a_usage_error_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _out, err = run_cli(["--ledger", empty, "status"], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert "not a ledger" in err


@pytest.mark.fast
def test_csv_output_is_refused_before_the_ledger_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``tm status`` has exactly two shapes: human and --json."""
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _out, err = run_cli(
        ["--format", "csv", "--ledger", empty, "status"], monkeypatch, capsys
    )
    assert code == EXIT_USAGE
    assert "no CSV output" in err


@pytest.mark.fast
def test_status_help_is_available_with_dash_h() -> None:
    short = runner.invoke(app, ["status", "-h"])
    long = runner.invoke(app, ["status", "--help"])
    assert short.exit_code == 0, short.output
    assert short.output == long.output
    assert "Usage:" in short.output
    assert "--json" in short.output


# ---------------------------------------------------------------------------
# Real ledger (embedded lane: real Dolt)
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_status_human_reports_a_real_standalone_ledger(ledger_factory) -> None:
    ledger = ledger_factory(name="status-human")

    result = invoke_status("--ledger", str(ledger.path))

    assert result.exit_code == 0, result.output
    output = result.output
    assert field(output, "path") == str(ledger.path)
    assert field(output, "name") == "status-human"
    assert field(output, "branch") == "main"
    assert field(output, "spec_version") == str(SPEC_VERSION)
    assert field(output, "presets") == "base"
    assert field(output, "author") == FACTORY_AUTHOR
    assert field(output, "write lock") == "free"
    assert field(output, "portability") == "no warnings"
    # A read command probes the lock but never creates it (P11).
    assert not lock_path(ledger.path).exists()


@pytest.mark.embedded
def test_status_json_has_the_fixed_shape(ledger_factory) -> None:
    ledger = ledger_factory(name="status-json")

    # The command's own ``--json``.
    result = runner.invoke(app, ["--ledger", str(ledger.path), "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == set(STATUS_KEYS)
    assert payload["path"] == str(ledger.path)
    assert payload["name"] == "status-json"
    assert payload["branch"] == "main"
    assert payload["spec_version"] == SPEC_VERSION
    assert isinstance(payload["spec_version"], int)
    assert payload["presets"] == ["base"]
    assert payload["author"] == FACTORY_AUTHOR
    assert payload["lock"] == {
        "held": False,
        "pid": None,
        "command": None,
        "since": None,
        "host": None,
    }
    assert payload["portability_warnings"] == []

    # The global --json flags select the same shape.
    global_json = runner.invoke(app, ["--json", "--ledger", str(ledger.path), "status"])
    assert json.loads(global_json.output) == payload

    # A session branch (``-b``) is what status reports.
    branch = runner.invoke(
        app, ["--ledger", str(ledger.path), "-b", "main", "status", "--json"]
    )
    assert json.loads(branch.output)["branch"] == "main"


@pytest.mark.embedded
def test_status_reports_a_busy_write_lock(ledger_factory) -> None:
    """A held P11 lock is named in both outputs; status itself never waits."""
    ledger = ledger_factory(name="status-busy")
    lock = WriteLock(ledger.path, command="tm stock add")
    lock.acquire()
    try:
        human = invoke_status("--ledger", str(ledger.path))
        assert human.exit_code == 0, human.output
        lock_text = field(human.output, "write lock")
        assert lock_text != "free"
        assert f"pid {os.getpid()}" in lock_text
        assert "tm stock add" in lock_text

        payload = json.loads(
            invoke_status("--ledger", str(ledger.path), "--json").output
        )
        assert payload["lock"]["held"] is True
        assert payload["lock"]["pid"] == os.getpid()
        assert payload["lock"]["command"] == "tm stock add"
        assert isinstance(payload["lock"]["since"], str)
        assert isinstance(payload["lock"]["host"], str)
    finally:
        lock.release()

    # Released: the same ledger reads as free again.
    payload = json.loads(invoke_status("--ledger", str(ledger.path), "--json").output)
    assert payload["lock"]["held"] is False
    assert payload["lock"]["pid"] is None


@pytest.mark.embedded
def test_status_reports_an_absolute_file_url_as_a_portability_warning(
    ledger_factory, tmp_path: Path
) -> None:
    """The .10 rule's findings are surfaced by status (P13)."""
    ledger = ledger_factory(name="status-portable")
    state_path = ledger.path / ".dolt" / "repo_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    backup_url = f"file://{tmp_path / 'old-backup'}"
    state.setdefault("backups", {})["old"] = {"name": "old", "url": backup_url}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    human = invoke_status("--ledger", str(ledger.path))
    assert human.exit_code == 0, human.output
    assert field(human.output, "portability") == "1 warning(s):"
    assert backup_url in human.output

    payload = json.loads(invoke_status("--ledger", str(ledger.path), "--json").output)
    assert payload["portability_warnings"] == [
        {
            "kind": "backup",
            "name": "old",
            "url": backup_url,
            "path": str(tmp_path / "old-backup"),
        }
    ]
    assert set(payload["portability_warnings"][0]) == set(WARNING_KEYS)
