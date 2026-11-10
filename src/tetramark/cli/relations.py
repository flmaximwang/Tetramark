"""``tm parent`` / ``tm child``: the relation between two objects (spec 6.7).

Both commands are thin presentation layers over :mod:`tetramark.core.relations`
(``ledger.relations``).  The parent form treats its first positional as the
child; the child form treats its first positional as the parent and speaks the
same relation in the inverse wording.  A bare call reads; ``--set`` and
``--clear`` are the only writes, and for these two commands both ``--dry-run``
and ``--no-commit`` preview the intended SQL and write nothing (spec 6.7 (i)).
"""

from __future__ import annotations

import typer

from tetramark.cli.options import json_output, resolve_ledger
from tetramark.cli.output import emit_payload, emit_write
from tetramark.errors import UsageError

__all__ = ["child_command", "parent_command"]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _descriptor_text(descriptor: dict[str, object]) -> str:
    return f"{descriptor['object']} {descriptor['id']}"


def _line(entry: dict[str, object], *, side: str) -> str:
    """One relation line: ``<field>  <object> <kind> <id> [slot S]``."""
    descriptor = entry[side]  # type: ignore[index]
    assert isinstance(descriptor, dict)
    text = (
        f"{entry['via']}  {descriptor['object']} "
        f"{descriptor.get('kind') or '-'} {descriptor['id']}"
    )
    slot = entry.get("slot")
    if slot is not None:
        text += f" slot {slot}"
    return text


def _previous_text(result: dict[str, object]) -> str:
    previous = result.get("previous")
    if previous is None:
        return "unset"
    text = _descriptor_text(previous)  # type: ignore[arg-type]
    previous_slot = result.get("previous_slot")
    if previous_slot is not None:
        text += f" slot {previous_slot}"
    return text


def _set_message(result: dict[str, object]) -> str:
    text = (
        f"Set {result['via']} of {_descriptor_text(result['child'])} "  # type: ignore[arg-type]
        f"to {_descriptor_text(result['parent'])}"  # type: ignore[arg-type]
    )
    slot = result.get("slot")
    if slot is not None:
        text += f" slot {slot}"
    return f"{text} (was {_previous_text(result)})"


def _clear_message(result: dict[str, object]) -> str:
    return (
        f"Cleared {result['via']} of "
        f"{_descriptor_text(result['child'])} (was {_previous_text(result)})"  # type: ignore[arg-type]
    )


def _emit_relation_list(
    result: dict[str, object], *, side: str, as_json: bool
) -> None:
    if as_json:
        emit_payload(result, fmt="json")
        return
    for entry in result["relations"]:  # type: ignore[index]
        typer.echo(_line(entry, side=side))


def _emit_clear(
    ledger: object, result: dict[str, object], *, as_json: bool
) -> None:
    if result.get("via") is None:  # nothing was set: read-only no-op
        if as_json:
            emit_payload(result, fmt="json")
        else:
            typer.echo("no parent relations")
        return
    emit_write(ledger, result, _clear_message(result), as_json=as_json)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def parent_command(
    ctx: typer.Context,
    ref_a: str = typer.Argument(
        ..., help="The child object: full id, exact alias, box name or unique id prefix."
    ),
    ref_b: str | None = typer.Argument(
        None, help="The parent object, in the same reference forms."
    ),
    set_relation: bool = typer.Option(
        False,
        "--set",
        help=(
            "Make REF_B the parent of REF_A (two refs required; one write lock, "
            "one Dolt commit)."
        ),
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help=(
            "Remove REF_A's parent relation (with REF_B: only when REF_B is the "
            "current parent)."
        ),
    ),
    via: str | None = typer.Option(
        None,
        "--via",
        metavar="FIELD",
        help=(
            "Relation field: parent_stock, source_aliquot, parent_box or a kind's "
            "bare ref field name (e.g. dam, sire, parental)."
        ),
    ),
    slot: str | None = typer.Option(
        None,
        "--slot",
        "-s",
        help="Slot in a slotted box parent (parent_box only, with --set).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the relation(s) or the change as JSON."
    ),
) -> None:
    """Read or change a parent relation: tm parent A [B] [--set|--clear].

    One ref reads every relation in which A is the child; two refs read the
    relation between A and B (exit 1 when there is none); two refs with --set
    makes B the parent of A; `--clear` removes A's parent relation.  Refs
    resolve by full id, exact alias, box name or unique id prefix; an
    ambiguous ref is an error.
    """
    as_json = json_output(ctx, as_json)
    if set_relation and clear:
        raise UsageError("--set and --clear cannot be combined")
    if slot is not None and not set_relation:
        raise UsageError("--slot is only valid with --set")
    ledger = resolve_ledger(ctx)
    try:
        if set_relation:
            if ref_b is None:
                raise UsageError(
                    "--set requires two refs: tm parent CHILD PARENT --set"
                )
            result = ledger.relations.set_parent(
                ref_a, ref_b, via=via, slot=slot
            )
            emit_write(ledger, result, _set_message(result), as_json=as_json)
            return
        if clear:
            if ref_b is None:
                result = ledger.relations.clear_parent(ref_a, via=via)
            else:
                result = ledger.relations.clear_relation(ref_a, ref_b, via=via)
            _emit_clear(ledger, result, as_json=as_json)
            return
        if ref_b is None:
            result = ledger.relations.get_parent(ref_a, via=via)
            if not as_json and not result["relations"]:
                typer.echo("no parent relations")
                return
            _emit_relation_list(result, side="parent", as_json=as_json)
            return
        result = ledger.relations.get_relation(ref_a, ref_b, via=via)
        _emit_relation_list(result, side="parent", as_json=as_json)
    finally:
        ledger.close()


def child_command(
    ctx: typer.Context,
    ref_b: str = typer.Argument(
        ..., help="The parent object: full id, exact alias, box name or unique id prefix."
    ),
    ref_a: str | None = typer.Argument(
        None, help="The child object, in the same reference forms."
    ),
    set_relation: bool = typer.Option(
        False,
        "--set",
        help=(
            "Make REF_A the child of REF_B (two refs required; the same write as "
            "tm parent REF_A REF_B --set)."
        ),
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        hidden=True,
        help="Not available on the child form; use tm parent CHILD --clear.",
    ),
    via: str | None = typer.Option(
        None,
        "--via",
        metavar="FIELD",
        help=(
            "Relation field: parent_stock, source_aliquot, parent_box or a kind's "
            "bare ref field name (e.g. dam, sire, parental)."
        ),
    ),
    slot: str | None = typer.Option(
        None,
        "--slot",
        "-s",
        help="Slot in a slotted box parent (parent_box only, with --set).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the relation(s) or the change as JSON."
    ),
) -> None:
    """Read a parent relation from the parent side: tm child B [A] [--set].

    One ref lists every relation in which B is the parent; two refs report the
    relation between B (the parent) and A (the child), exit 1 when there is
    none; two refs with --set make A the child of B (the same write as
    `tm parent A B --set`).  `--clear` is not available here: use
    `tm parent CHILD --clear` (there is no "clear all children").
    """
    as_json = json_output(ctx, as_json)
    if clear:
        if ref_a is not None:
            raise UsageError(
                "tm child does not clear a parent relation: "
                f"use tm parent {ref_a} --clear"
            )
        raise UsageError(
            "tm child does not clear a parent relation: "
            "use tm parent <CHILD> --clear"
        )
    if slot is not None and not set_relation:
        raise UsageError("--slot is only valid with --set")
    ledger = resolve_ledger(ctx)
    try:
        if set_relation:
            if ref_a is None:
                raise UsageError(
                    "--set requires two refs: tm child PARENT CHILD --set"
                )
            result = ledger.relations.set_parent(
                ref_a, ref_b, via=via, slot=slot
            )
            emit_write(ledger, result, _set_message(result), as_json=as_json)
            return
        if ref_a is None:
            result = ledger.relations.get_children(ref_b, via=via)
            if not as_json and not result["relations"]:
                typer.echo("no child relations")
                return
            _emit_relation_list(result, side="child", as_json=as_json)
            return
        result = ledger.relations.get_relation(ref_a, ref_b, via=via)
        _emit_relation_list(result, side="child", as_json=as_json)
    finally:
        ledger.close()
