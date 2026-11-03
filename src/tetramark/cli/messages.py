"""The message every ``add`` -- and its interactive ``add-form`` -- prints.

``tm <obj> add-form`` prints "the same message as add", so the two commands
must not own two copies of the text.  The helpers here are the single source
of the wording; the CLI writes use them for both paths.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "aliquots_add_message",
    "box_add_message",
    "entity_add_message",
    "stock_add_message",
]


def entity_add_message(result: dict[str, Any]) -> str:
    message = f"Added entity {result['kind']} {result['id']}"
    if result.get("aliases"):
        message += " (alias: " + ", ".join(result["aliases"]) + ")"
    return message


def stock_add_message(result: dict[str, Any]) -> str:
    message = (
        f"Added stock {result['kind']} {result['id']} with "
        f"{len(result['aliquots'])} aliquot(s)"
    )
    if result["aliquots"]:
        message += ": " + ", ".join(result["aliquots"])
    return message


def aliquots_add_message(result: dict[str, Any]) -> str:
    ids = result["ids"]
    message = f"Added {len(ids)} aliquot(s) to {result['stock_id']}"
    if ids:
        message += ": " + ", ".join(ids)
    return message


def box_add_message(result: dict[str, Any]) -> str:
    return (
        f"Created box {result['name']} ({result['kind']}) "
        f"with {result['slots']} slot(s)"
    )
