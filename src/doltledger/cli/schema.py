"""``ledger schema`` commands (spec §6; plan/apply arrive with ticket 09)."""

from __future__ import annotations

import json
from typing import Any

import typer
import yaml

from doltledger.cli.options import resolve_ledger
from doltledger.schema.model import EffectiveSchema, Kind, Rule

schema_app = typer.Typer(
    help="Inspect the effective schema of a ledger.",
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
