"""Which questions ``tm <obj> add-form`` asks, and in which order.

A :class:`FormPlan` is built from the ledger's effective schema and is pure
Python (no database access), so the whole question list can be tested in the
Dolt-free fast lane.  The plan answers three questions:

* which object kinds a form can create (``kinds`` -- for an **aliquot** that is
  the list of *stock* kinds, because an aliquot inherits its stock's kind);
* the numbered kind step that always comes first (spec: "Step 1 is always a
  numbered kind list built from the ledger schema");
* the field steps of one chosen kind, in **schema order** (``steps``).

The step list is deliberately data-only: :mod:`tetramark.form.state` does the
validation and navigation, :mod:`tetramark.form.tui` renders it.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from tetramark.schema.model import (
    UNSET,
    EffectiveSchema,
    Field,
    FieldType,
    GeometryType,
    Kind,
)

__all__ = [
    "FACT_LABEL_HEAD",
    "FACT_LABEL_LIMIT",
    "FACT_LABEL_TAIL",
    "OBJECT_TYPES",
    "FormPlan",
    "KindChoice",
    "Step",
    "box_kind_facts",
    "build_kind_step",
    "build_plan",
    "default_text",
    "field_step",
]

#: A fact label list with at most this many labels is shown in full.
FACT_LABEL_LIMIT = 12
#: Labels kept at the head of a longer list.
FACT_LABEL_HEAD = 8
#: Labels kept at the tail of a longer list.
FACT_LABEL_TAIL = 2

#: The four core objects a form can create.
OBJECT_TYPES: tuple[str, ...] = ("entity", "stock", "aliquot", "box")

#: The object kind a form's first step lists.  An aliquot has no kind of its
#: own (it is a generic tube), so its kind list is the stock kinds: that is what
#: decides the status set and narrows the stock choice.
KIND_OBJECT: dict[str, str] = {
    "entity": "entity",
    "stock": "stock",
    "aliquot": "stock",
    "box": "box",
}


@dataclass(frozen=True)
class KindChoice:
    """One entry of the numbered kind list."""

    name: str
    description: str | None = None
    #: Structured ``(label, value)`` facts shown under the description (box
    #: kind lists only; the other kind lists keep the empty default).
    facts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Step:
    """One question of the form.

    ``type`` is either a :class:`~tetramark.schema.model.FieldType` value
    (``string``, ``text``, ``int``, ``decimal``, ``bool``, ``date``,
    ``datetime``, ``enum``, ``sequence``, ``ref``) or one of the form-only
    types ``kind``, ``count``, ``box``, ``slots`` and ``aliases``.
    """

    key: str
    label: str
    type: str
    description: str | None = None
    required: bool = False
    default: str | None = None
    values: tuple[str, ...] = ()
    pattern: str | None = None
    long: bool = False
    multi: bool = False
    target: str | None = None
    allowed_kinds: tuple[str, ...] = ()
    choices: tuple[KindChoice, ...] = ()
    #: Minimum accepted value of a ``count`` step (0 for stock aliquots, 1 for
    #: aliquots added to an existing stock).
    minimum: int = 0
    #: Short hint shown under the question (``?`` syntax and the like).
    help: str | None = None


def default_text(field: Field) -> str | None:
    """The schema default of ``field`` as the text a form step prefills."""
    if not field.has_default or field.default is UNSET or field.default is None:
        return None
    value = field.default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return str(value)


def field_step(name: str, field: Field) -> Step:
    """One schema field as a form step (computed fields are filtered out)."""
    return Step(
        key=name,
        label=name,
        type=field.type.value,
        description=field.description,
        required=field.required,
        default=default_text(field),
        values=tuple(field.values or ()),
        pattern=field.pattern,
        long=field.long,
        target=field.to,
    )


def _label_list(labels: list[str]) -> str:
    """A bound label list: all of them, or first 8 ``…`` last 2, then ``(N)``."""
    total = len(labels)
    if total <= FACT_LABEL_LIMIT:
        shown = labels
    else:
        shown = [*labels[:FACT_LABEL_HEAD], "…", *labels[-FACT_LABEL_TAIL:]]
    return f"{' '.join(shown)} ({total})"


def box_kind_facts(kind: Kind) -> tuple[tuple[str, str], ...]:
    """One box kind's structured facts as ordered ``(label, value)`` rows.

    Pure: every value comes from the parsed effective schema (the kind and its
    geometry), never from the database.  The capacity row follows the same
    formula as ``tm box list``: ``len(geometry.labels) * kind.slot_capacity``
    for slotted kinds, ``geometry.capacity`` for slotless ones.
    """
    geometry = kind.geometry
    if geometry is None:
        return ()
    if geometry.is_slotted:
        slots = len(geometry.labels)
        capacity_fact = (
            f"{slots * kind.slot_capacity} aliquots "
            f"({slots} slots × {kind.slot_capacity} per slot)"
        )
    elif geometry.capacity is not None:
        capacity_fact = f"{geometry.capacity} (aliquots + child boxes)"
    else:
        capacity_fact = "unlimited"
    if geometry.type is GeometryType.grid:
        rows = geometry.row_labels
        columns = geometry.col_labels
        labels = geometry.labels
        return (
            ("geometry", f"grid, {len(rows)} rows × {len(columns)} columns"),
            ("rows", _label_list(rows)),
            ("columns", _label_list(columns)),
            ("slots", f"{labels[0]} … {labels[-1]} ({len(labels)})"),
            ("capacity", capacity_fact),
            ("holds boxes", "no"),
        )
    if geometry.type is GeometryType.named:
        labels = geometry.labels
        return (
            ("geometry", f"named, {len(labels)} slots"),
            ("slots", _label_list(labels)),
            ("capacity", capacity_fact),
            ("holds boxes", "yes, one per free slot"),
        )
    return (
        ("geometry", "slotless"),
        ("capacity", capacity_fact),
        ("holds boxes", "yes"),
    )


def _kind_choices(schema: EffectiveSchema, object_type: str) -> tuple[KindChoice, ...]:
    kind_object = KIND_OBJECT[object_type]
    kinds = schema.kinds_for(kind_object)
    return tuple(
        KindChoice(
            name=name,
            description=kinds[name].description,
            facts=box_kind_facts(kinds[name]) if object_type == "box" else (),
        )
        for name in sorted(kinds)
    )


def build_kind_step(schema: EffectiveSchema, object_type: str) -> Step:
    """The numbered kind list every form starts with."""
    choices = _kind_choices(schema, object_type)
    if object_type == "aliquot":
        description = "The stock kind the aliquots belong to."
    else:
        description = f"The {KIND_OBJECT[object_type]} kind to create."
    return Step(
        key="kind",
        label="kind",
        type="kind",
        description=description,
        required=True,
        choices=choices,
        help="Enter accepts the only kind; otherwise pick a number or name.",
    )


def _schema_steps(kind: Kind) -> list[Step]:
    """The kind's own fields in schema order (computed fields are skipped)."""
    steps: list[Step] = []
    for name, field in kind.fields.items():
        if field.type is FieldType.computed:
            continue
        steps.append(field_step(name, field))
    return steps


def _entity_steps(kind: Kind) -> list[Step]:
    steps: list[Step] = []
    if "name" not in kind.fields:
        steps.append(
            Step(
                key="name",
                label="name",
                type="string",
                description="Human-readable name used in lists and searches.",
            )
        )
    steps.extend(_schema_steps(kind))
    steps.append(
        Step(
            key="aliases",
            label="aliases",
            type="aliases",
            multi=True,
            description=(
                "Alternative handles for the entity (one per line or "
                "comma-separated; an empty line ends the list)."
            ),
        )
    )
    steps.append(
        Step(
            key="notes",
            label="notes",
            type="text",
            long=True,
            description="Free-text notes about the entity.",
        )
    )
    return steps


def _stock_steps(kind: Kind) -> list[Step]:
    steps: list[Step] = [
        Step(
            key="entity",
            label="entity",
            type="ref",
            target="entity",
            allowed_kinds=tuple(kind.entity_kinds or ()),
            required=True,
            description="The entity this stock is a batch of.",
            help="Type an id or alias (its name is shown back), or ?term to search.",
        )
    ]
    steps.extend(_schema_steps(kind))
    steps.append(
        Step(
            key="aliquots",
            label="aliquots",
            type="count",
            default="1",
            minimum=0,
            description="How many aliquots to create (0 for none).",
        )
    )
    steps.append(
        Step(
            key="box",
            label="box",
            type="box",
            description="Box to place the aliquots in (optional).",
            help="Type a box name, or ? to list the boxes.",
        )
    )
    steps.append(
        Step(
            key="slots",
            label="slots",
            type="slots",
            multi=True,
            description=(
                "Slot per aliquot, in order (one per line or comma-separated; "
                "an empty line ends the list)."
            ),
            help="Enter accepts the next free slots; ? shows the free slots.",
        )
    )
    return steps


def _aliquot_steps(kind_name: str) -> list[Step]:
    return [
        Step(
            key="stock",
            label="stock",
            type="ref",
            target="stock",
            allowed_kinds=(kind_name,),
            required=True,
            description="The stock to add aliquots to.",
            help="Type an id or alias (its name is shown back), or ?term to search.",
        ),
        Step(
            key="count",
            label="count",
            type="count",
            default="1",
            minimum=1,
            description="How many aliquots to add.",
        ),
        Step(
            key="box",
            label="box",
            type="box",
            description="Box to place the aliquots in (optional).",
            help="Type a box name, or ? to list the boxes.",
        ),
        Step(
            key="slots",
            label="slots",
            type="slots",
            multi=True,
            description=(
                "Slot per aliquot, in order (one per line or comma-separated; "
                "an empty line ends the list)."
            ),
            help="Enter accepts the next free slots; ? shows the free slots.",
        ),
    ]


def _box_steps(kind: Kind) -> list[Step]:
    steps: list[Step] = [
        Step(
            key="name",
            label="name",
            type="string",
            required=True,
            description="Unique human-readable box name (e.g. WFL-08-01).",
        )
    ]
    # The name is the box NAME argument, not a kind field: ``add_box`` refuses
    # ``--set name=...``, so a kind's own ``name`` field is not a second
    # question.
    steps.extend(step for step in _schema_steps(kind) if step.key != "name")
    steps.extend(
        [
            Step(
                key="parent",
                label="parent",
                type="box",
                description="Parent box to nest this box in (optional; never a grid).",
                help="Type a box name, or ? to list the boxes.",
            ),
            Step(
                key="slot",
                label="slot",
                type="slot",
                description="Slot in the parent box (slotted parents only).",
                help="? shows the free slots of the parent.",
            ),
            Step(
                key="notes",
                label="notes",
                type="text",
                long=True,
                description="Free-text notes about the box.",
            ),
        ]
    )
    return steps


def _steps_for(kind: Kind) -> list[Step]:
    if kind.object.value == "entity":
        return _entity_steps(kind)
    if kind.object.value == "stock":
        return _stock_steps(kind)
    if kind.object.value == "aliquot":  # pragma: no cover - aliquots have no kinds
        return _aliquot_steps(kind)
    return _box_steps(kind)


@dataclass(frozen=True)
class FormPlan:
    """The question plan of one ``add-form`` command."""

    object_type: str
    schema: EffectiveSchema
    kinds: tuple[KindChoice, ...]

    def kind_step(self) -> Step:
        return build_kind_step(self.schema, self.object_type)

    def steps(self, kind_name: str) -> tuple[Step, ...]:
        """The steps of one chosen kind (the kind step itself excluded)."""
        if self.object_type == "aliquot":
            # An aliquot has no kind row of its own: its kind is its stock's.
            return tuple(_aliquot_steps(kind_name))
        return tuple(_steps_for(self.schema.kind(kind_name)))


def build_plan(schema: EffectiveSchema, object_type: str) -> FormPlan:
    """Build the form plan for one core object."""
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"unknown form object {object_type!r}")
    return FormPlan(
        object_type=object_type,
        schema=schema,
        kinds=_kind_choices(schema, object_type),
    )
