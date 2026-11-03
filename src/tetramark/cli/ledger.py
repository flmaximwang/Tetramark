"""``tm init``, ``tm adopt`` and ``tm presets`` commands (spec §6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml

from tetramark.cli.options import GlobalOptions, group
from tetramark.core.ledger import (
    META_ADOPTED_FROM_COMMIT,
    AdoptPlan,
    InitPlan,
    Ledger,
    adopt_ledger,
    init_ledger,
)
from tetramark.errors import UsageError
from tetramark.schema.loader import (
    YAML_SUFFIXES,
    default_presets_dir,
    list_presets,
    load_preset,
    resolve_preset_spec,
)
from tetramark.schema.model import EffectiveSchema, Kind, ObjectType, Preset, Rule

presets_app = group("List and inspect the presets available for 'tm init --preset'.")

#: Help of the ``--preset`` option, shared by ``init`` and ``adopt``.
_PRESET_HELP = (
    "Preset name (resolved through $TETRAMARK_PRESETS_DIR, then the presets "
    "bundled with tetramark) or a path to a preset YAML file or directory; "
    "repeat to combine presets (e.g. -p base -p molbio). The preset and its "
    "includes are copied into the ledger, so it reopens without access to the "
    "source."
)


def _target_path(path: Path) -> Path:
    """The absolute ledger directory a positional PATH names (relative to cwd)."""
    target = path.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target.resolve()


def _require_preset(preset: list[str]) -> None:
    if not preset:
        raise UsageError("at least one --preset is required (e.g. --preset base)")


def init_command(
    ctx: typer.Context,
    path: Path = typer.Argument(
        ...,
        metavar="PATH",
        help=(
            "Ledger directory to create, relative to the current directory or "
            "absolute; '.dolt/' and 'ledger.yaml' end up in it. Its 'name:' in "
            "ledger.yaml defaults to the directory basename."
        ),
    ),
    preset: list[str] = typer.Option([], "--preset", "-p", help=_PRESET_HELP),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            "Ledger identity written to ledger.yaml (default: the directory "
            "basename). The path is not identity; rename or copy freely."
        ),
    ),
    id_prefix: str | None = typer.Option(
        None, "--id-prefix", help="Prefix for date-code stock ids (e.g. 12)."
    ),
    author: str | None = typer.Option(
        None, "--author", help='"Name <email>" for commits (default: git config).'
    ),
) -> None:
    """Create one standalone ledger directory (a Dolt database + ledger.yaml)."""
    options: GlobalOptions = ctx.obj
    target = _target_path(path)
    _require_preset(preset)

    result = init_ledger(
        target,
        name,
        preset,
        id_prefix=id_prefix,
        author=author,
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


def adopt_command(
    ctx: typer.Context,
    path: Path = typer.Argument(
        ...,
        metavar="PATH",
        help=(
            "Existing bare Dolt database directory to format in place: it holds "
            "'.dolt/' and no 'ledger.yaml' (the 0.4 lab databases). Its history "
            "is kept; its 'name:' in ledger.yaml defaults to the basename."
        ),
    ),
    preset: list[str] = typer.Option([], "--preset", "-p", help=_PRESET_HELP),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            "Ledger identity written to ledger.yaml (default: the directory "
            "basename). The path is not identity; rename or copy freely."
        ),
    ),
    id_prefix: str | None = typer.Option(
        None, "--id-prefix", help="Prefix for date-code stock ids (e.g. 12)."
    ),
    author: str | None = typer.Option(
        None, "--author", help='"Name <email>" for commits (default: git config).'
    ),
    force_config: bool = typer.Option(
        False,
        "--force-config",
        help=(
            "Overwrite an existing ledger.yaml left on an otherwise empty "
            "database; nothing is deleted. The database must be empty and have "
            "a clean working set."
        ),
    ),
) -> None:
    """Format an existing bare Dolt database as a ledger, in place (P9)."""
    options: GlobalOptions = ctx.obj
    target = _target_path(path)
    _require_preset(preset)

    adopted = adopt_ledger(
        target,
        name,
        preset,
        id_prefix=id_prefix,
        author=author,
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


def _presets_dir() -> Path:
    """The directory bare preset names resolve from (P8)."""
    directory = default_presets_dir()
    if directory is None:
        raise UsageError(
            "no preset directory: the presets bundled with tetramark were not "
            "found (broken installation?) and $TETRAMARK_PRESETS_DIR is unset. "
            "Set it to the directory holding <name>.yaml presets (for example "
            "the Tetramark checkout's presets/), or pass a preset path instead"
        )
    return directory


@presets_app.command("list")
def presets_list(ctx: typer.Context) -> None:
    """List the presets available to ``tm init --preset NAME``."""
    directory = _presets_dir()
    names = list_presets(directory)
    if not names:
        typer.echo(f"no presets in {directory}")
        return
    for name in names:
        typer.echo(name)


@presets_app.command("show")
def presets_show(
    ctx: typer.Context,
    name: str = typer.Argument(
        ...,
        help="Preset name (resolved like 'tm init --preset') or a path to a preset.",
    ),
) -> None:
    """Print one preset (include-resolved) as YAML."""
    text = str(name)
    path_like = "/" in text or "\\" in text or text.endswith(YAML_SUFFIXES)
    presets_dir = None
    if not path_like:
        presets_dir = _presets_dir()
    preset_file = resolve_preset_spec(name, presets_dir=presets_dir)
    preset = load_preset(str(preset_file.path))
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
    if preset.status_sets:
        payload["status_sets"] = {
            name: preset.status_sets[name].normalised()
            for name in sorted(preset.status_sets)
        }
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
    if kind.statuses is not None:
        payload["statuses"] = (
            kind.statuses
            if isinstance(kind.statuses, str)
            else kind.statuses.normalised()
        )
    payload["fields"] = {
        name: field.normalised() for name, field in kind.fields.items()
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
