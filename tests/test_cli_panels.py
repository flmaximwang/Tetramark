"""The root help is grouped into five workflow panels, in a fixed order.

Tetramark-43x: ``tm --help`` shows the panels Setup, Inventory, Lookup, Data
and Versioning, and every command and command group sits in the panel it
belongs to, in the order a real session uses them.

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
    "Setup": ("workspace", "server", "presets", "init", "schema", "config"),
    "Inventory": ("entity", "stock", "aliquot", "box"),
    "Lookup": ("find", "alias", "history", "lineage"),
    "Data": ("import", "export", "check"),
    "Versioning": ("branch", "remote", "backup"),
}

#: The subcommands of every root command group, in the order they are shown.
EXPECTED_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "workspace": ("init",),
    "server": ("start", "status", "stop"),
    "presets": ("list", "show"),
    "schema": ("show", "plan", "apply"),
    "config": ("get", "set"),
    "entity": ("add", "show", "ls", "edit"),
    "stock": ("add", "show", "ls", "edit", "renumber"),
    "aliquot": ("add", "move", "use", "status", "purge"),
    "box": ("add", "show", "ls", "move"),
    "alias": ("add", "rm"),
    "branch": ("ls", "create", "delete"),
    "remote": ("add", "ls", "push", "pull", "rm"),
}

#: The root commands, in the order the help lists them: the panels of
#: :data:`EXPECTED_PANELS` one after the other.
EXPECTED_ROOT_ORDER: tuple[str, ...] = tuple(
    name for commands in EXPECTED_PANELS.values() for name in commands
)

#: The panel names the CLI is allowed to use.
KNOWN_PANELS: tuple[str, ...] = tuple(EXPECTED_PANELS)


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
    """The root has exactly the twelve command groups of the epic."""
    groups = [
        name
        for name in _root_names(root)
        if getattr(_root_command(root, name), "commands", None)
    ]
    assert sorted(groups) == sorted(EXPECTED_SUBCOMMANDS)


@pytest.mark.parametrize("group_name", sorted(EXPECTED_SUBCOMMANDS))
def test_the_subcommands_of_a_group_are_in_the_expected_order(
    root: TyperGroup, group_name: str
) -> None:
    """Every group lists its subcommands in the epic's order."""
    group = _root_command(root, group_name)
    assert list(group.commands) == list(EXPECTED_SUBCOMMANDS[group_name])


def test_the_help_renders_the_panels_in_order() -> None:
    """The panels really are rendered, in the epic's order."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    positions = [result.output.find(f"─ {panel} ") for panel in EXPECTED_PANELS]
    assert all(position >= 0 for position in positions), result.output
    assert positions == sorted(positions), result.output
