"""``tm init`` and ``tm presets`` commands (spec §6)."""

from __future__ import annotations

from typing import Any

import typer
import yaml

from tetramark.cli.options import GlobalOptions
from tetramark.cli.workspace import resolve_workspace
from tetramark.core.ledger import (
    META_ADOPTED_FROM_COMMIT,
    AdoptPlan,
    InitPlan,
    Ledger,
    adopt_ledger,
    init_ledger,
)
from tetramark.errors import UsageError
from tetramark.schema.loader import list_bundled_presets, load_preset
from tetramark.schema.model import EffectiveSchema, Kind, ObjectType, Preset, Rule

presets_app = typer.Typer(
    help="List and inspect the presets shipped with Tetramark.",
    no_args_is_help=True,
)


def init_command(
    ctx: typer.Context,
    name: str = typer.Argument(
        ...,
        help=(
            "Ledger (Dolt database) name: letters, digits, '_', '-' and '.' "
            "(no '/', '..' or trailing '.')."
        ),
    ),
    preset: list[str] = typer.Option(
        [],
        "--preset",
        "-p",
        help="Preset name or path; repeat to combine presets (e.g. -p base -p molbio).",
    ),
    id_prefix: str | None = typer.Option(
        None, "--id-prefix", help="Prefix for date-code stock ids (e.g. 12)."
    ),
    author: str | None = typer.Option(
        None, "--author", help='"Name <email>" for commits (default: workspace/git).'
    ),
    status: list[str] = typer.Option(
        [],
        "--status",
        help="Aliquot status vocabulary (repeat to override the default four).",
    ),
    adopt: bool = typer.Option(
        False,
        "--adopt",
        help=(
            "Format a Dolt database that already exists in the workspace instead "
            "of creating one: same schema as init, existing history kept. The "
            "database must be empty (no tables, views or dolt_tests rows), have "
            "a clean working set and no ledger.yaml (or pass --force-config)."
        ),
    ),
    force_config: bool = typer.Option(
        False,
        "--force-config",
        help="With --adopt: overwrite an existing ledger.yaml; nothing is deleted.",
    ),
) -> None:
    """Create a ledger in the workspace, or format an existing one with --adopt."""
    options: GlobalOptions = ctx.obj
    workspace = resolve_workspace(ctx)
    if not preset:
        raise UsageError("at least one --preset is required (e.g. --preset base)")
    if force_config and not adopt:
        raise UsageError("--force-config only applies to --adopt")

    if adopt:
        adopted = adopt_ledger(
            workspace,
            name,
            preset,
            id_prefix=id_prefix,
            author=author,
            statuses=status or None,
            force_config=force_config,
            branch=options.branch,
            dry_run=options.dry_run,
        )
        if isinstance(adopted, AdoptPlan):
            typer.echo(
                f"Would adopt ledger {adopted.name} at {adopted.path} "
                f"(branch {adopted.branch}, HEAD {adopted.adopted_from_commit})"
            )
            typer.echo(f"  presets : {', '.join(adopted.config.presets)}")
            typer.echo(f"  schema  : {_describe_schema(adopted.schema)}")
            typer.echo("  history : kept; nothing is deleted")
            typer.echo("")
            typer.echo(adopted.sql)
            return
        _print_adopted(adopted)
        return

    result = init_ledger(
        workspace,
        name,
        preset,
        id_prefix=id_prefix,
        author=author,
        statuses=status or None,
        dry_run=options.dry_run,
    )
    if isinstance(result, InitPlan):
        typer.echo(f"Would create ledger {result.name} at {result.path}")
        typer.echo(f"  presets : {', '.join(result.config.presets)}")
        typer.echo(f"  schema  : {_describe_schema(result.schema)}")
        typer.echo("")
        typer.echo(result.sql)
        return

    _print_created(result)


def _print_adopted(ledger: Ledger) -> None:
    typer.echo(f"Adopted ledger {ledger.name} at {ledger.path}")
    typer.echo(f"  presets    : {', '.join(ledger.config.presets)}")
    typer.echo(f"  schema     : {_describe_schema(ledger.schema)}")
    typer.echo(f"  fingerprint: {ledger.fingerprint}")
    typer.echo(f"  author     : {ledger.author or '(from git config)'}")
    typer.echo(f"  branch     : {ledger.branch or '(checked out)'}")
    adopted_from = ledger.meta.get(META_ADOPTED_FROM_COMMIT)
    if adopted_from:
        typer.echo(f"  adopted    : {adopted_from}")


def _print_created(ledger: Ledger) -> None:
    typer.echo(f"Created ledger {ledger.name} at {ledger.path}")
    typer.echo(f"  presets    : {', '.join(ledger.config.presets)}")
    typer.echo(f"  schema     : {_describe_schema(ledger.schema)}")
    typer.echo(f"  fingerprint: {ledger.fingerprint}")
    typer.echo(f"  author     : {ledger.author or '(from git config)'}")


def _describe_schema(schema: EffectiveSchema) -> str:
    kinds = schema.kinds
    parts = [
        f"{len(kinds)} kinds ({len(schema.entity_kinds)} entity, "
        f"{len(schema.stock_kinds)} stock, {len(schema.box_kinds)} box)"
    ]
    if schema.rules:
        parts.append(f"{len(schema.rules)} rules")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


@presets_app.command("list")
def list_presets() -> None:
    """List the presets bundled with this installation."""
    names = list_bundled_presets()
    if not names:
        typer.echo("no bundled presets")
        return
    for name in names:
        typer.echo(name)


@presets_app.command("show")
def show_preset(
    name: str = typer.Argument(..., help="Bundled preset name or a path to a preset."),
) -> None:
    """Print one preset (include-resolved) as YAML."""
    preset = load_preset(name)
    typer.echo(
        yaml.safe_dump(
            preset_payload(preset), sort_keys=False, allow_unicode=True
        ),
        nl=False,
    )


def preset_payload(preset: Preset) -> dict[str, Any]:
    """A stable YAML-able rendering of a preset document."""
    payload: dict[str, Any] = {}
    if preset.preset:
        payload["preset"] = preset.preset
    payload["version"] = preset.version
    if preset.description:
        payload["description"] = preset.description
    if preset.mixins:
        payload["mixins"] = {
            mixin: {name: field.normalised() for name, field in fields.items()}
            for mixin, fields in preset.mixins.items()
        }
    if preset.kinds:
        payload["kinds"] = {
            name: kind_payload(preset.kinds[name]) for name in sorted(preset.kinds)
        }
    if preset.rules:
        payload["rules"] = [rule_payload(rule) for rule in preset.rules]
    return payload


def kind_payload(kind: Kind) -> dict[str, Any]:
    payload: dict[str, Any] = {"object": kind.object.value}
    if kind.description:
        payload["description"] = kind.description
    if kind.entity_kinds is not None:
        payload["entity_kinds"] = kind.entity_kinds
    payload["fields"] = {
        name: field.normalised() for name, field in kind.fields.items()
    }
    if kind.aliquot_fields:
        payload["aliquot_fields"] = {
            name: field.normalised() for name, field in kind.aliquot_fields.items()
        }
    if kind.geometry is not None:
        payload["geometry"] = kind.geometry.normalised()
    if kind.object is ObjectType.box:
        payload["slot_capacity"] = kind.slot_capacity
    if kind.renamed_from:
        payload["renamed_from"] = kind.renamed_from
    return payload


def rule_payload(rule: Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "description": rule.description,
        "severity": rule.severity.value,
        "sql": rule.sql,
        "expect": rule.expect.normalised(),
    }
