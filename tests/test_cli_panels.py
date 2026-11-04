"""The root help is grouped into five workflow panels, in a fixed order.

Tetramark-43x: ``tm --help`` shows the panels Setup, Inventory, Lookup, Data
and Versioning, and every command and command group sits in the panel it
belongs to, in the order a real session uses them.

0.5.1 spells every subcommand out in full: ``ls`` is ``list`` and ``rm`` is
``remove``, with no compatibility aliases.  The abbreviations of 0.5.0 are
unknown commands now, so they exit 2.

The expectations below are hardcoded on purpose -- they are the oracle.  A
reordered, re-panelled or new root command must fail loudly instead of
drifting silently, so the test never derives an expectation from the CLI
itself.  Dolt-free: the whole module runs in the fast lane.
"""

from __future__ import annotations

import pytest
import typer.main
from typer.core import TyperCommand, TyperGroup
from typer.testing import CliRunner

from tetramark.cli.app import (
    PANEL_DATA,
    PANEL_INVENTORY,
    PANEL_LOOKUP,
    PANEL_SETUP,
    PANEL_VERSIONING,
    ROOT_COMMAND_ORDER,
    ROOT_PANELS,
    app,
)

pytestmark = pytest.mark.fast

#: The root help, as the epic defines it: panel name -> its commands, in the
#: order they are shown.
EXPECTED_PANELS: dict[str, tuple[str, ...]] = {
    "Setup": ("presets", "init", "adopt", "schema", "config", "status"),
    "Inventory": ("entity", "stock", "aliquot", "box"),
    "Lookup": ("find", "alias", "history", "lineage"),
    "Data": ("import", "export", "check"),
    "Versioning": ("branch", "remote", "backup"),
}

#: The subcommands of every root command group, in the order they are shown.
EXPECTED_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "presets": ("list", "show"),
    "schema": ("show", "plan", "apply"),
    "config": ("get", "set"),
    "entity": ("add", "add-form", "show", "list", "edit", "rename"),
    "stock": ("add", "add-form", "show", "list", "edit", "renumber"),
    "aliquot": ("add", "add-form", "show", "move", "use", "status", "purge"),
    "box": ("add", "add-form", "show", "list", "move", "rename"),
    "alias": ("add", "remove"),
    "branch": ("list", "create", "delete"),
    "remote": ("add", "list", "push", "pull", "remove"),
}

#: The root commands, in the order the help lists them: the panels of
#: :data:`EXPECTED_PANELS` one after the other.
EXPECTED_ROOT_ORDER: tuple[str, ...] = tuple(
    name for commands in EXPECTED_PANELS.values() for name in commands
)

#: The panel names the CLI is allowed to use.
KNOWN_PANELS: tuple[str, ...] = tuple(EXPECTED_PANELS)

#: Abbreviated subcommand names 0.5.1 removed: every command is spelled out.
FORBIDDEN_SUBCOMMAND_NAMES: frozenset[str] = frozenset({"ls", "rm"})

#: A subcommand shorter than this is an abbreviation, not a word.
MIN_SUBCOMMAND_LENGTH = 3

#: The abbreviated 0.5.0 spellings and the argv that used to reach them.
OLD_ABBREVIATED_SPELLINGS: tuple[tuple[str, ...], ...] = (
    ("entity", "ls"),
    ("stock", "ls"),
    ("box", "ls"),
    ("branch", "ls"),
    ("remote", "ls"),
    ("alias", "rm"),
    ("remote", "rm"),
    ("backup", "--ls"),
)


@pytest.fixture(scope="module")
def root() -> TyperGroup:
    """The real root command group, built the way ``tm`` builds it."""
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    return group


def _root_names(group: TyperGroup) -> list[str]:
    """The root command names, in the order the help lists them."""
    ctx = group.make_context("tm", [], resilient_parsing=True)
    return list(group.list_commands(ctx))


def _root_command(group: TyperGroup, name: str) -> TyperCommand | TyperGroup:
    """The root command (or command group) called ``name``."""
    ctx = group.make_context("tm", [], resilient_parsing=True)
    command = group.get_command(ctx, name)
    assert command is not None, f"tm has no root command {name!r}"
    return command


def _panel_of(group: TyperGroup, name: str) -> str:
    """The panel of the root command ``name``; it must be a real panel name.

    An unset panel is a falsy ``DefaultPlaceholder`` in Typer, not ``None``,
    so the type is what tells "no panel" apart from a panel name.
    """
    panel = getattr(_root_command(group, name), "rich_help_panel", None)
    assert isinstance(panel, str), f"tm {name} has no rich_help_panel ({panel!r})"
    return panel


def test_panel_constants_agree_with_the_expected_names() -> None:
    """The CLI and the oracle must spell every panel the same way."""
    assert PANEL_SETUP == "Setup"
    assert PANEL_INVENTORY == "Inventory"
    assert PANEL_LOOKUP == "Lookup"
    assert PANEL_DATA == "Data"
    assert PANEL_VERSIONING == "Versioning"
    assert ROOT_PANELS == ("Setup", "Inventory", "Lookup", "Data", "Versioning")


def test_root_command_order_constant_agrees_with_the_expected_order() -> None:
    """The CLI's own order constant must agree with the oracle."""
    assert ROOT_COMMAND_ORDER == EXPECTED_ROOT_ORDER


def test_the_root_has_exactly_the_expected_commands(root: TyperGroup) -> None:
    """A new root command -- panelled or not -- has to be added on purpose."""
    assert sorted(_root_names(root)) == sorted(EXPECTED_ROOT_ORDER)


def test_the_root_lists_the_expected_commands_in_order(root: TyperGroup) -> None:
    """The root help lists the commands panel by panel, in the epic's order."""
    assert _root_names(root) == list(EXPECTED_ROOT_ORDER)


def test_every_root_command_has_a_known_panel(root: TyperGroup) -> None:
    """No root command may fall out of the panels (the default 'Commands')."""
    for name in _root_names(root):
        panel = _panel_of(root, name)
        assert panel in KNOWN_PANELS, f"tm {name} is in the unknown panel {panel!r}"


def test_the_root_shows_the_expected_panels_in_order(root: TyperGroup) -> None:
    """The panels are Setup, Inventory, Lookup, Data, Versioning -- no more."""
    panels: list[str] = []
    for name in _root_names(root):
        panel = _panel_of(root, name)
        if panel not in panels:
            panels.append(panel)
    assert panels == list(EXPECTED_PANELS)


def test_the_commands_inside_each_panel_are_in_the_expected_order(
    root: TyperGroup,
) -> None:
    """Each panel lists its own commands in the epic's order."""
    found: dict[str, list[str]] = {panel: [] for panel in EXPECTED_PANELS}
    for name in _root_names(root):
        found[_panel_of(root, name)].append(name)
    assert found == {
        panel: list(commands) for panel, commands in EXPECTED_PANELS.items()
    }


def test_the_root_has_exactly_the_expected_groups(root: TyperGroup) -> None:
    """The root has exactly the ten command groups of the 0.5.0 tree."""
    groups = [
        name
        for name in _root_names(root)
        if getattr(_root_command(root, name), "commands", None)
    ]
    assert sorted(groups) == sorted(EXPECTED_SUBCOMMANDS)


def test_the_root_has_no_workspace_or_server_commands(root: TyperGroup) -> None:
    """0.5.0 removed ``tm workspace`` (P3) and the server group (P14)."""
    names = set(_root_names(root))
    assert "workspace" not in names
    assert "server" not in names


def test_the_rendered_help_has_no_workspace_content() -> None:
    """The root help never mentions the removed workspace layer."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "workspace" not in result.output.lower()
    assert "─ Server " not in result.output


@pytest.mark.parametrize("group_name", sorted(EXPECTED_SUBCOMMANDS))
def test_the_subcommands_of_a_group_are_in_the_expected_order(
    root: TyperGroup, group_name: str
) -> None:
    """Every group lists its subcommands in the epic's order."""
    group = _root_command(root, group_name)
    assert list(group.commands) == list(EXPECTED_SUBCOMMANDS[group_name])


def test_no_group_exposes_an_abbreviated_subcommand(root: TyperGroup) -> None:
    """Every subcommand is spelled out in full: no ``ls``/``rm``, no stub."""
    for group_name in EXPECTED_SUBCOMMANDS:
        group = _root_command(root, group_name)
        abbreviated = [
            name
            for name in group.commands
            if name in FORBIDDEN_SUBCOMMAND_NAMES
            or len(name) < MIN_SUBCOMMAND_LENGTH
        ]
        assert not abbreviated, f"tm {group_name} exposes {abbreviated}"


@pytest.mark.parametrize(
    "argv", OLD_ABBREVIATED_SPELLINGS, ids=lambda argv: " ".join(argv)
)
def test_the_old_abbreviated_spellings_are_unknown_commands(
    argv: tuple[str, ...],
) -> None:
    """The 0.5.1 rename is a hard break: every old spelling exits 2."""
    result = CliRunner().invoke(app, list(argv))
    assert result.exit_code == 2, result.output


def test_the_help_renders_the_panels_in_order() -> None:
    """The panels really are rendered, in the epic's order."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    positions = [result.output.find(f"─ {panel} ") for panel in EXPECTED_PANELS]
    assert all(position >= 0 for position in positions), result.output
    assert positions == sorted(positions), result.output
