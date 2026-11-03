"""CLI-level contract of ``tm <obj> add-form`` (fast lane, no Dolt).

The form is interactive: without a TTY the command must refuse before it even
opens the ledger, point at ``tm <obj> add`` and exit 2.  The command is also
registered on every object group and answers ``-h`` like every other command
(the generic help walk in ``tests/test_cli_help.py`` covers it too).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import EXIT_OK, EXIT_USAGE

pytestmark = pytest.mark.fast

runner = CliRunner()

OBJECTS = ("entity", "stock", "aliquot", "box")


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
