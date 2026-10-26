"""``tm schema`` commands: show, plan and apply (spec §5, §6)."""

from __future__ import annotations

import json
from typing import Any

import typer
import yaml

from tetramark.cli.options import echo_dry_run_sql, resolve_ledger, target_branch
from tetramark.errors import LedgerError
from tetramark.schema.diff import (
    SchemaPlan,
    apply as apply_schema_plan,
    plan as schema_plan,
)
from tetramark.schema.model import EffectiveSchema, Kind, Rule

schema_app = typer.Typer(
    help="Inspect, plan and apply the effective schema of a ledger.",
    no_args_is_help=True,
)


@schema_app.command("show")
def show_schema(
    ctx: typer.Context,
    kind: str | None = typer.Argument(
        None, help="Show one kind instead of the whole schema."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of YAML."),
) -> None:
    """Print the effective schema (kinds, rules, statuses, fingerprint)."""
    ledger = resolve_ledger(ctx)
    try:
        payload = schema_payload(ledger.schema, kind=kind)
    finally:
        ledger.close()

    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        typer.echo(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), nl=False
        )


@schema_app.command("plan")
def plan_schema(
    ctx: typer.Context,
    as_json: bool = typer.Option(
        False, "--json", help="Print the plan as JSON instead of text."
    ),
) -> None:
    """Diff the YAML schema against the live database (read-only)."""
    ledger = resolve_ledger(ctx)
    try:
        current = schema_plan(ledger)
    finally:
        ledger.close()

    if as_json:
        typer.echo(
            json.dumps(current.to_dict(), indent=2, ensure_ascii=False)
        )
    else:
        echo_plan(current)


@schema_app.command("apply")
def apply_schema(
    ctx: typer.Context,
    allow_destructive: bool = typer.Option(
        False,
        "--allow-destructive",
        help="Apply drop/rename/type-change/tightening steps.",
    ),
) -> None:
    """Execute the schema plan in one transaction and one Dolt commit."""
    ledger = resolve_ledger(ctx)
    try:
        current = schema_plan(ledger)
        if current.destructive and not allow_destructive:
            echo_plan(current)
            raise LedgerError(
                f"refusing to apply {len(current.destructive)} destructive "
                "step(s) without --allow-destructive"
            )

        result = apply_schema_plan(
            ledger,
            allow_destructive=allow_destructive,
            schema_plan=current,
        )
        if ledger.dry_run:
            typer.echo(f"Would apply {len(current.steps)} step(s)")
            if current.sql:
                echo_dry_run_sql(ledger, current.sql)
            else:
                typer.echo(f"-- target branch: {target_branch(ledger)}")
            return
        if not result.applied:
            typer.echo("schema is up to date")
            return
        commit = result.commit_hash or "(not committed)"
        typer.echo(f"Applied {len(current.steps)} step(s); commit {commit}")
    finally:
        ledger.close()


def echo_plan(current: SchemaPlan) -> None:
    """Human-readable plan: one tagged block per step, SQL included."""
    if current.is_empty:
        typer.echo("schema is up to date")
        if current.meta_stale:
            typer.echo(
                "ledger_meta fingerprint is stale; 'tm schema apply' "
                "will refresh it"
            )
        return

    if current.ledger:
        typer.echo(f"schema plan for ledger {current.ledger!r}")
    for index, step in enumerate(current.steps, start=1):
        typer.echo(f"{index:>3}. [{step.tag}] {step.action} {step.name}")
        if step.description:
            typer.echo(f"     {step.description}")
        for line in step.sql.splitlines():
            typer.echo(f"     {line}")
    typer.echo(
        f"{len(current.steps)} step(s): {len(current.additive)} additive, "
        f"{len(current.destructive)} destructive"
    )
    if current.meta_stale:
        typer.echo("ledger_meta fingerprint will be refreshed on apply")


def schema_payload(schema: EffectiveSchema, *, kind: str | None = None) -> dict[str, Any]:
    """A stable rendering of the effective schema, for YAML/JSON output."""
    if kind is not None:
        return {"kind": kind, **_kind_payload(schema.kind(kind))}

    return {
        "fingerprint": schema.fingerprint(),
        "statuses": list(schema.statuses),
        "preset_versions": dict(sorted(schema.preset_versions.items())),
        "kinds": {
            name: _kind_payload(schema.kinds[name]) for name in sorted(schema.kinds)
        },
        "rules": [_rule_payload(rule) for rule in sorted(schema.rules, key=lambda r: r.id)],
    }


def _kind_payload(kind: Kind) -> dict[str, Any]:
    payload: dict[str, Any] = {"object": kind.object.value}
    if kind.description:
        payload["description"] = kind.description
    if kind.entity_kinds is not None:
        payload["entity_kinds"] = list(kind.entity_kinds)
    payload["fields"] = {
        field_name: field.normalised() for field_name, field in kind.fields.items()
    }
    if kind.aliquot_fields:
        payload["aliquot_fields"] = {
            field_name: field.normalised()
            for field_name, field in kind.aliquot_fields.items()
        }
    if kind.geometry is not None:
        payload["geometry"] = kind.geometry.normalised()
    if kind.object.value == "box":
        payload["slot_capacity"] = kind.slot_capacity
    return payload


def _rule_payload(rule: Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "severity": rule.severity.value,
        "description": rule.description,
        "expect": rule.expect.normalised(),
        "sql": rule.sql,
    }
