"""CLI-level contract of ``tm <obj> add-form`` (fast lane, no Dolt).

The form is interactive: without a TTY the command must refuse before it even
opens the ledger, point at ``tm <obj> add`` and exit 2.  The command is also
registered on every object group and answers ``-h`` like every other command
(the generic help walk in ``tests/test_cli_help.py`` covers it too).

The stock form's one extra human line -- the pointer at the separate aliquot
workflow -- is checked here with a fake ledger and a canned form result, so the
contract runs in the fast lane: human output gets the hint, ``--json`` and
``--dry-run`` do not, and the other forms never print it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tetramark.cli import form_commands
from tetramark.cli.app import app
from tetramark.errors import EXIT_OK, EXIT_USAGE

pytestmark = pytest.mark.fast

runner = CliRunner()

OBJECTS = ("entity", "stock", "aliquot", "box")

STOCK_RESULT = {"object": "stock", "id": "STK-1", "kind": "lot", "aliquots": []}
STOCK_MESSAGE = "Added stock lot STK-1 with 0 aliquot(s)"
STOCK_NEXT = (
    "Next: add aliquots with 'tm aliquot add-form' or "
    "'tm aliquot add STK-1 --count N --quantity Q --unit U'"
)


class _FakeLedger:
    """The interface ``run_add_form_command``/``emit_write`` touch."""

    branch = "main"

    def __init__(self, *, dry_run: bool = False, sql: str = "") -> None:
        self.last_write = SimpleNamespace(dry_run=dry_run, sql=sql)

    def close(self) -> None:
        pass


def _patched(monkeypatch: pytest.MonkeyPatch, result, *, ledger=None):
    """Replace the terminal, the ledger and the form write with canned pieces."""
    ledger = ledger or _FakeLedger()
    monkeypatch.setattr(form_commands, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(form_commands, "_make_source", lambda: object())
    monkeypatch.setattr(form_commands, "resolve_ledger", lambda ctx: ledger)
    monkeypatch.setattr(form_commands, "run_add_form", lambda *args, **kwargs: result)
    return ledger


def test_stock_form_prints_the_next_aliquot_line_after_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched(monkeypatch, STOCK_RESULT)
    result = runner.invoke(app, ["stock", "add-form"])
    assert result.exit_code == EXIT_OK
    assert result.output.splitlines() == [STOCK_MESSAGE, STOCK_NEXT]


def test_stock_form_json_has_no_next_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched(monkeypatch, STOCK_RESULT)
    result = runner.invoke(app, ["stock", "add-form", "--json"])
    assert result.exit_code == EXIT_OK
    assert "Next:" not in result.output
    assert json.loads(result.output) == STOCK_RESULT


def test_stock_form_dry_run_prints_sql_and_no_next_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _FakeLedger(dry_run=True, sql="INSERT INTO `stock` (`id`) VALUES ('STK-1');")
    _patched(monkeypatch, STOCK_RESULT, ledger=ledger)
    result = runner.invoke(app, ["stock", "add-form"])
    assert result.exit_code == EXIT_OK
    assert "INSERT INTO `stock`" in result.output
    assert "Next:" not in result.output


def test_stock_form_cancel_keeps_the_plain_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched(monkeypatch, None)
    result = runner.invoke(app, ["stock", "add-form"])
    assert result.exit_code == EXIT_OK
    assert result.output.strip() == "Cancelled; nothing written."


@pytest.mark.parametrize(
    ("object_type", "result", "message"),
    [
        ("entity", {"kind": "item", "id": "E1", "aliases": []}, "Added entity item E1"),
        (
            "aliquot",
            {"object": "aliquot", "stock_id": "S1", "ids": []},
            "Added 0 aliquot(s) to S1",
        ),
        (
            "box",
            {"object": "box", "name": "BOX-1", "kind": "grid96", "slots": 96},
            "Created box BOX-1 (grid96) with 96 slot(s)",
        ),
    ],
)
def test_other_forms_never_print_the_next_line(
    monkeypatch: pytest.MonkeyPatch, object_type: str, result: dict, message: str
) -> None:
    _patched(monkeypatch, result)
    output = runner.invoke(app, [object_type, "add-form"])
    assert output.exit_code == EXIT_OK
    assert output.output.strip() == message
    assert "Next:" not in output.output


def test_stock_add_form_help_states_the_separate_aliquot_workflow() -> None:
    result = runner.invoke(app, ["stock", "add-form", "-h"])
    assert result.exit_code == EXIT_OK
    help_text = " ".join(result.output.split())
    assert "without aliquots" in help_text
    assert "tm aliquot add-form" in help_text


@pytest.mark.parametrize("object_type", OBJECTS)
def test_add_form_refuses_without_a_tty(object_type: str) -> None:
    result = runner.invoke(app, [object_type, "add-form"])
    assert result.exit_code == EXIT_USAGE
    output = result.output + getattr(result, "stderr", "")
    assert f"'tm {object_type} add-form' needs an interactive terminal" in output
    assert f"'tm {object_type} add ...'" in output


@pytest.mark.parametrize("object_type", OBJECTS)
def test_group_help_lists_add_form(object_type: str) -> None:
    result = runner.invoke(app, [object_type, "--help"])
    assert result.exit_code == EXIT_OK
    assert "add-form" in result.output


@pytest.mark.parametrize("object_type", OBJECTS)
def test_add_form_help_succeeds_without_a_tty(object_type: str) -> None:
    short = runner.invoke(app, [object_type, "add-form", "-h"])
    long = runner.invoke(app, [object_type, "add-form", "--help"])
    assert short.exit_code == EXIT_OK
    assert short.output == long.output
    assert "interactively" in short.output.lower()
