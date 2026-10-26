"""Generate the physical DDL of a ledger from an :class:`EffectiveSchema`.

Everything in this module is pure: it turns the resolved YAML schema into an
ordered list of :class:`Statement` objects and never talks to Dolt.  ``ledger
init`` and ``tm schema apply`` execute the list; ``tm schema plan``
diffs it (ticket 09).

Generation order
----------------
1. core tables (``ledger_meta``, ``entity``, ``stock``, ``box``, ``slot``,
   ``aliquot``, ``aliquot_event``, ``alias``);
2. core foreign keys;
3. kind tables (``entity_<kind>``, ``stock_<kind>``, ``box_<kind>``) and
   aliquot extension tables (``aliquot_<stockkind>``);
4. kind foreign keys;
5. views.

Foreign keys are emitted as separate ``ALTER TABLE ... ADD CONSTRAINT``
statements, after *all* tables exist.  This is the only way to express the
mutual references in the core model (``stock`` → ``aliquot`` → ``stock``),
lets a kind field reference a kind generated later, and gives ticket 09 a
one-statement-per-object unit to diff and rebuild.

Not in the DDL (enforced by the app and by generated ``dolt_tests``, spec §3):
slot capacity, aliquot-vs-child-box exclusivity, box nesting acyclicity,
``entity_kinds`` restrictions, ``pattern``, and status/slot consistency.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tetramark.driver.base import literal
from tetramark.schema.model import (
    DEFAULT_STATUSES,
    EffectiveSchema,
    Field,
    FieldType,
    Geometry,
    GeometryType,
    Kind,
    ObjectType,
    SchemaError,
    SqlType,
)

__all__ = [
    "CORE_COLUMNS",
    "CORE_TABLES",
    "CORE_VIEWS",
    "Statement",
    "core_statements",
    "field_sql_type",
    "kind_statements",
    "schema_statements",
    "slot_rows",
    "table_name",
    "to_sql",
    "view_statements",
]

#: Core tables, in creation order (foreign keys are added afterwards).
CORE_TABLES: tuple[str, ...] = (
    "ledger_meta",
    "entity",
    "stock",
    "box",
    "slot",
    "aliquot",
    "aliquot_event",
    "alias",
)

#: Views that exist for every ledger, independent of the selected kinds.
CORE_VIEWS: tuple[str, ...] = (
    "v_box_slots",
    "v_aliquot_location",
    "v_stock_status",
)

#: Core columns of the three extensible objects, in display order.  The
#: ``v_<object>_<kind>`` views project these and then the kind's own fields.
CORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "entity": ("id", "kind", "name", "created_at", "notes"),
    "stock": (
        "id",
        "kind",
        "entity_id",
        "parent_stock_id",
        "source_aliquot_id",
        "created_at",
        "notes",
    ),
    "box": (
        "id",
        "kind",
        "name",
        "parent_box_id",
        "parent_slot",
        "created_at",
        "notes",
    ),
}

_EVENT_VALUES: tuple[str, ...] = ("create", "use", "move", "status", "edit")

#: Status values the derived stock status (``v_stock_status``) is defined in
#: terms of (spec §3): a custom vocabulary may add values, but the derivation
#: rules still speak about ``stored``, ``pending``, ``missing`` and
#: ``consumed``.
_ID_TYPE = "VARCHAR(64)"
_KIND_TYPE = "VARCHAR(64)"


# ---------------------------------------------------------------------------
# Statement model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """One generated SQL statement.

    ``object`` is the kind of database object the statement creates —
    ``table``, ``fk`` or ``view`` — and ``name`` is its name (table, constraint
    or view).  ``table`` names the owning table for constraints, so a diff can
    regroup statements per table without parsing SQL.  ``columns``,
    ``target`` and ``target_columns`` are filled for foreign keys so
    :mod:`tetramark.schema.diff` can compare them semantically instead of
    re-parsing the SQL.
    """

    sql: str
    object: str
    name: str
    table: str | None = None
    columns: tuple[str, ...] | None = None
    target: str | None = None
    target_columns: tuple[str, ...] | None = None


def to_sql(statements: Iterable[Statement]) -> str:
    """Render statements as one semicolon-terminated SQL script."""
    rendered = [f"{statement.sql};" for statement in statements]
    return "\n\n".join(rendered) + ("\n" if rendered else "")


# ---------------------------------------------------------------------------
# Small SQL helpers
# ---------------------------------------------------------------------------


def _ident(name: str) -> str:
    """Backtick-quote an identifier.

    Schema identifiers are restricted to ``[a-z][a-z0-9_]*`` by the loader, so
    doubling backticks is belt-and-braces; quoting also keeps core column names
    such as ``key`` and ``status`` out of trouble.
    """
    return "`" + name.replace("`", "``") + "`"


def _name(*parts: str) -> str:
    return "_".join(parts)


def _fk(table: str, *columns: str) -> str:
    return _name("fk", table, *columns)


def _uq(table: str, *columns: str) -> str:
    return _name("uq", table, *columns)


def _ck(table: str, *columns: str) -> str:
    return _name("ck", table, *columns)


def _pk(table: str) -> str:
    return _name("pk", table)


def table_name(object_type: ObjectType | str, kind: str) -> str:
    """Physical table of a kind: ``<object>_<kind>``."""
    obj = object_type.value if isinstance(object_type, ObjectType) else object_type
    return f"{obj}_{kind}"


def _sql_type(sql_type: SqlType | None, field: Field) -> str:
    if sql_type is SqlType.string:
        return f"VARCHAR({field.string_length})"
    if sql_type is SqlType.text:
        return "MEDIUMTEXT"
    if sql_type is SqlType.int:
        return "BIGINT"
    if sql_type is SqlType.decimal:
        return f"DECIMAL({field.decimal_precision},{field.decimal_scale})"
    if sql_type is SqlType.bool:
        return "TINYINT(1)"
    if sql_type is SqlType.date:
        return "DATE"
    if sql_type is SqlType.datetime:
        return "DATETIME"
    raise SchemaError(  # pragma: no cover - Field validation guarantees sql_type
        f"computed field has no supported sql_type: {sql_type!r}"
    )


def _base_field_type(field: Field) -> str:
    t = field.type
    if t is FieldType.string:
        return f"VARCHAR({field.string_length})"
    if t is FieldType.text:
        return "MEDIUMTEXT"
    if t is FieldType.int:
        return "BIGINT"
    if t is FieldType.decimal:
        return f"DECIMAL({field.decimal_precision},{field.decimal_scale})"
    if t is FieldType.bool:
        return "TINYINT(1)"
    if t is FieldType.date:
        return "DATE"
    if t is FieldType.datetime:
        return "DATETIME"
    if t is FieldType.enum:
        # A generous fixed width keeps "add an enum value" additive even when
        # the new value is longer than every existing one.
        return "VARCHAR(255)"
    if t is FieldType.sequence:
        return "MEDIUMTEXT"
    if t is FieldType.ref:
        return _ID_TYPE
    raise SchemaError(f"unsupported field type {t!r}")  # pragma: no cover


def field_sql_type(field: Field) -> str:
    """The SQL type of a field's column, including computed columns.

    Public so the schema diff (ticket 09) can build ``ADD``/``MODIFY COLUMN``
    statements from the same rules as table creation.
    """
    if field.type is FieldType.computed:
        return _sql_type(field.sql_type, field)
    return _base_field_type(field)


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------


def core_statements(
    statuses: Sequence[str] = DEFAULT_STATUSES,
) -> list[Statement]:
    """DDL for the eight core tables and their foreign keys."""
    statements: list[Statement] = [
        _create_ledger_meta(),
        _create_entity(),
        _create_stock(),
        _create_box(),
        _create_slot(),
        _create_aliquot(statuses),
        _create_aliquot_event(),
        _create_alias(),
    ]
    statements.extend(_core_foreign_keys())
    return statements


def _create_ledger_meta() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('ledger_meta')} (\n"
            f"  {_ident('key')} VARCHAR(64) NOT NULL,\n"
            f"  {_ident('value')} MEDIUMTEXT,\n"
            f"  CONSTRAINT {_ident(_pk('ledger_meta'))} PRIMARY KEY ({_ident('key')})\n"
            ")"
        ),
        object="table",
        name="ledger_meta",
    )


def _create_entity() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('entity')} (\n"
            f"  {_ident('id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('kind')} {_KIND_TYPE} NOT NULL,\n"
            f"  {_ident('name')} VARCHAR(255),\n"
            f"  {_ident('created_at')} DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            f"  {_ident('notes')} MEDIUMTEXT,\n"
            f"  CONSTRAINT {_ident(_pk('entity'))} PRIMARY KEY ({_ident('id')}),\n"
            f"  CONSTRAINT {_ident(_uq('entity', 'id', 'kind'))} "
            f"UNIQUE ({_ident('id')}, {_ident('kind')})\n"
            ")"
        ),
        object="table",
        name="entity",
    )


def _create_stock() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('stock')} (\n"
            f"  {_ident('id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('kind')} {_KIND_TYPE} NOT NULL,\n"
            f"  {_ident('entity_id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('parent_stock_id')} {_ID_TYPE},\n"
            f"  {_ident('source_aliquot_id')} {_ID_TYPE},\n"
            f"  {_ident('created_at')} DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            f"  {_ident('notes')} MEDIUMTEXT,\n"
            f"  CONSTRAINT {_ident(_pk('stock'))} PRIMARY KEY ({_ident('id')}),\n"
            f"  CONSTRAINT {_ident(_uq('stock', 'id', 'kind'))} "
            f"UNIQUE ({_ident('id')}, {_ident('kind')})\n"
            ")"
        ),
        object="table",
        name="stock",
    )


def _create_box() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('box')} (\n"
            f"  {_ident('id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('name')} VARCHAR(255) NOT NULL,\n"
            f"  {_ident('kind')} {_KIND_TYPE} NOT NULL,\n"
            f"  {_ident('parent_box_id')} {_ID_TYPE},\n"
            f"  {_ident('parent_slot')} VARCHAR(64),\n"
            f"  {_ident('created_at')} DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            f"  {_ident('notes')} MEDIUMTEXT,\n"
            f"  CONSTRAINT {_ident(_pk('box'))} PRIMARY KEY ({_ident('id')}),\n"
            f"  CONSTRAINT {_ident(_uq('box', 'name'))} UNIQUE ({_ident('name')}),\n"
            f"  CONSTRAINT {_ident(_uq('box', 'id', 'kind'))} "
            f"UNIQUE ({_ident('id')}, {_ident('kind')})\n"
            ")"
        ),
        object="table",
        name="box",
    )


def _create_slot() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('slot')} (\n"
            f"  {_ident('box_id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('label')} VARCHAR(64) NOT NULL,\n"
            f"  {_ident('row_label')} VARCHAR(64),\n"
            f"  {_ident('col_label')} VARCHAR(64),\n"
            f"  {_ident('ordinal')} INT NOT NULL,\n"
            f"  CONSTRAINT {_ident(_pk('slot'))} "
            f"PRIMARY KEY ({_ident('box_id')}, {_ident('label')})\n"
            ")"
        ),
        object="table",
        name="slot",
    )


def _create_aliquot(statuses: Sequence[str]) -> Statement:
    vocabulary = ", ".join(literal(status) for status in statuses)
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('aliquot')} (\n"
            f"  {_ident('id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('stock_id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('seq')} INT NOT NULL,\n"
            f"  {_ident('box_id')} {_ID_TYPE},\n"
            f"  {_ident('slot')} VARCHAR(64),\n"
            f"  {_ident('status')} VARCHAR(32) NOT NULL,\n"
            f"  {_ident('quantity')} DECIMAL(18,6),\n"
            f"  {_ident('unit')} VARCHAR(32),\n"
            f"  {_ident('label')} VARCHAR(255),\n"
            f"  {_ident('created_at')} DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            f"  {_ident('notes')} MEDIUMTEXT,\n"
            f"  CONSTRAINT {_ident(_pk('aliquot'))} PRIMARY KEY ({_ident('id')}),\n"
            f"  CONSTRAINT {_ident(_uq('aliquot', 'stock_id', 'seq'))} "
            f"UNIQUE ({_ident('stock_id')}, {_ident('seq')}),\n"
            f"  CONSTRAINT {_ident(_ck('aliquot', 'status'))} "
            f"CHECK ({_ident('status')} IN ({vocabulary}))\n"
            ")"
        ),
        object="table",
        name="aliquot",
    )


def _create_aliquot_event() -> Statement:
    events = ", ".join(literal(value) for value in _EVENT_VALUES)
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('aliquot_event')} (\n"
            f"  {_ident('id')} BIGINT NOT NULL AUTO_INCREMENT,\n"
            f"  {_ident('aliquot_id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('event')} ENUM({events}) NOT NULL,\n"
            f"  {_ident('amount')} DECIMAL(18,6),\n"
            f"  {_ident('unit')} VARCHAR(32),\n"
            f"  {_ident('from_box_id')} {_ID_TYPE},\n"
            f"  {_ident('from_slot')} VARCHAR(64),\n"
            f"  {_ident('to_box_id')} {_ID_TYPE},\n"
            f"  {_ident('to_slot')} VARCHAR(64),\n"
            f"  {_ident('old_status')} VARCHAR(32),\n"
            f"  {_ident('new_status')} VARCHAR(32),\n"
            f"  {_ident('actor')} VARCHAR(255),\n"
            f"  {_ident('purpose')} MEDIUMTEXT,\n"
            f"  {_ident('note')} MEDIUMTEXT,\n"
            f"  {_ident('at')} DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            f"  CONSTRAINT {_ident(_pk('aliquot_event'))} PRIMARY KEY ({_ident('id')})\n"
            ")"
        ),
        object="table",
        name="aliquot_event",
    )


def _create_alias() -> Statement:
    return Statement(
        sql=(
            f"CREATE TABLE {_ident('alias')} (\n"
            f"  {_ident('object_type')} VARCHAR(16) NOT NULL,\n"
            f"  {_ident('object_id')} {_ID_TYPE} NOT NULL,\n"
            f"  {_ident('alias')} VARCHAR(255) NOT NULL,\n"
            f"  CONSTRAINT {_ident(_pk('alias'))} "
            f"PRIMARY KEY ({_ident('object_type')}, {_ident('alias')})\n"
            ")"
        ),
        object="table",
        name="alias",
    )


def _core_foreign_keys() -> list[Statement]:
    """Core FKs, after every core table exists (``stock`` ↔ ``aliquot``)."""
    specs = [
        ("stock", ("entity_id",), "entity", ("id",)),
        ("stock", ("parent_stock_id",), "stock", ("id",)),
        ("stock", ("source_aliquot_id",), "aliquot", ("id",)),
        ("box", ("parent_box_id",), "box", ("id",)),
        ("slot", ("box_id",), "box", ("id",)),
        ("aliquot", ("stock_id",), "stock", ("id",)),
        ("aliquot", ("box_id", "slot"), "slot", ("box_id", "label")),
        ("aliquot_event", ("aliquot_id",), "aliquot", ("id",)),
    ]
    return [_fk_statement(*spec) for spec in specs]


def _fk_statement(
    table: str,
    columns: Sequence[str],
    target: str,
    target_columns: Sequence[str],
) -> Statement:
    name = _fk(table, *columns)
    column_sql = ", ".join(_ident(column) for column in columns)
    target_sql = ", ".join(_ident(column) for column in target_columns)
    return Statement(
        sql=(
            f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(name)} "
            f"FOREIGN KEY ({column_sql}) REFERENCES {_ident(target)} ({target_sql})"
        ),
        object="fk",
        name=name,
        table=table,
        columns=tuple(columns),
        target=target,
        target_columns=tuple(target_columns),
    )


# ---------------------------------------------------------------------------
# Kind tables and aliquot extensions
# ---------------------------------------------------------------------------


def kind_statements(schema: EffectiveSchema) -> list[Statement]:
    """Kind tables, aliquot extension tables, and their foreign keys."""
    tables: list[Statement] = []
    foreign_keys: list[Statement] = []

    for object_type in (ObjectType.entity, ObjectType.stock, ObjectType.box):
        for kind_name in sorted(schema.kinds_for(object_type)):
            kind = schema.kinds[kind_name]
            tables.append(_create_kind_table(kind_name, kind))
            if kind.object is ObjectType.stock and kind.aliquot_fields:
                tables.append(_create_aliquot_extension(kind_name, kind))
            foreign_keys.extend(_kind_foreign_keys(schema, kind_name, kind))

    return [*tables, *foreign_keys]


def _create_kind_table(kind_name: str, kind: Kind) -> Statement:
    table = table_name(kind.object, kind_name)
    columns: list[str] = [
        f"  {_ident('id')} {_ID_TYPE} NOT NULL",
    ]
    constraints: list[str] = [
        f"  CONSTRAINT {_ident(_pk(table))} PRIMARY KEY ({_ident('id')})",
        f"  CONSTRAINT {_ident(_ck(table, 'kind'))} "
        f"CHECK ({_ident('kind')} = {literal(kind_name)})",
    ]

    for field_name, schema_field in kind.fields.items():
        if schema_field.type is FieldType.sequence:
            sha_name = f"{field_name}_sha256"
            if sha_name in kind.fields:
                raise SchemaError(
                    f"field {field_name!r} generates {sha_name!r}, "
                    "which is also a declared field",
                    path=f"kinds.{kind_name}.fields.{field_name}",
                )
        columns.append(_column_definition(field_name, schema_field))
        constraints.extend(_field_constraints(table, field_name, schema_field))

    # The discriminator defaults to the kind so raw inserts do not repeat it.
    columns.insert(
        1,
        f"  {_ident('kind')} {_KIND_TYPE} NOT NULL DEFAULT {literal(kind_name)}",
    )
    body = ",\n".join([*columns, *constraints])
    return Statement(
        sql=f"CREATE TABLE {_ident(table)} (\n{body}\n)",
        object="table",
        name=table,
    )


def _create_aliquot_extension(kind_name: str, kind: Kind) -> Statement:
    table = table_name("aliquot", kind_name)
    columns = [f"  {_ident('id')} {_ID_TYPE} NOT NULL"]
    constraints = [
        f"  CONSTRAINT {_ident(_pk(table))} PRIMARY KEY ({_ident('id')})"
    ]
    for field_name, schema_field in kind.aliquot_fields.items():
        if schema_field.type is FieldType.sequence:
            sha_name = f"{field_name}_sha256"
            if sha_name in kind.aliquot_fields:
                raise SchemaError(
                    f"field {field_name!r} generates {sha_name!r}, "
                    "which is also a declared field",
                    path=f"kinds.{kind_name}.aliquot_fields.{field_name}",
                )
        columns.append(_column_definition(field_name, schema_field))
        constraints.extend(_field_constraints(table, field_name, schema_field))
    body = ",\n".join([*columns, *constraints])
    return Statement(
        sql=f"CREATE TABLE {_ident(table)} (\n{body}\n)",
        object="table",
        name=table,
    )


def _column_definition(name: str, schema_field: Field) -> str:
    """One column (or source + generated sha column for ``sequence``)."""
    column = _ident(name)
    if schema_field.type is FieldType.sequence:
        null = " NOT NULL" if schema_field.required else ""
        return (
            f"  {column} MEDIUMTEXT{null},\n"
            f"  {_ident(name + '_sha256')} CHAR(64) "
            f"GENERATED ALWAYS AS (SHA2({column}, 256)) STORED"
        )
    if schema_field.type is FieldType.computed:
        return (
            f"  {column} {_sql_type(schema_field.sql_type, schema_field)} "
            f"GENERATED ALWAYS AS ({schema_field.expr}) STORED"
        )

    pieces = [f"  {column} {_base_field_type(schema_field)}"]
    if schema_field.required:
        pieces.append("NOT NULL")
    if schema_field.has_default:
        pieces.append(f"DEFAULT {literal(schema_field.default)}")
    return " ".join(pieces)


def _field_constraints(
    table: str, name: str, schema_field: Field
) -> list[str]:
    """Table-level UNIQUE and CHECK constraints for one field."""
    constraints: list[str] = []
    column = _ident(name)

    if schema_field.type is FieldType.enum:
        values = ", ".join(literal(value) for value in schema_field.values or [])
        constraints.append(
            f"  CONSTRAINT {_ident(_ck(table, name))} "
            f"CHECK ({column} IN ({values}))"
        )

    if schema_field.unique:
        unique_column = (
            _ident(name + "_sha256")
            if schema_field.type is FieldType.sequence
            else column
        )
        constraints.append(
            f"  CONSTRAINT {_ident(_uq(table, name))} UNIQUE ({unique_column})"
        )
    return constraints


def _kind_foreign_keys(
    schema: EffectiveSchema, kind_name: str, kind: Kind
) -> list[Statement]:
    table = table_name(kind.object, kind_name)
    statements = [
        _fk_statement(table, ("id", "kind"), kind.object.value, ("id", "kind"))
    ]

    scopes: list[tuple[str, dict[str, Field]]] = [(table, kind.fields)]
    if kind.object is ObjectType.stock and kind.aliquot_fields:
        extension = table_name("aliquot", kind_name)
        statements.append(
            _fk_statement(extension, ("id",), "aliquot", ("id",))
        )
        scopes.append((extension, kind.aliquot_fields))

    for owner, fields in scopes:
        for field_name in sorted(fields):
            schema_field = fields[field_name]
            if schema_field.type is not FieldType.ref:
                continue
            target = schema_field.to
            assert target is not None  # Field validation guarantees this
            if target in schema.kinds:
                target_table = table_name(schema.kinds[target].object, target)
            else:  # loader restricts core targets to entity/stock/aliquot/box
                target_table = target
            statements.append(
                _fk_statement(owner, (field_name,), target_table, ("id",))
            )
    return statements


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _alias_list_sql(id_expr: str, object_type: str) -> str:
    """Correlated scalar subquery aggregating one object's aliases.

    The alias rows live only in the ``alias`` table, so every generated view
    that represents an object exposes them through this subquery rather than a
    join (a join would multiply the object's rows).  ``GROUP_CONCAT`` with an
    explicit ``ORDER BY`` and ``';'`` separator keeps the value deterministic;
    the subquery is NULL when the object has no aliases.

    The column is called ``alias_list`` and never ``aliases``: kinds may
    already have a domain field named ``aliases`` (the bundled ``construct``
    kind does, meaning alternative construct names), and two view columns with
    the same name are invalid DDL.
    """
    return (
        f"(SELECT GROUP_CONCAT({_ident('alias')} ORDER BY {_ident('alias')} "
        f"SEPARATOR ';') FROM {_ident('alias')} "
        f"WHERE {_ident('object_type')} = {literal(object_type)} "
        f"AND {_ident('object_id')} = {id_expr})"
    )


def _occupant_alias_list_sql() -> str:
    """The ``occupant_alias_list`` select item of ``v_box_slots``.

    The occupant is an aliquot (its own aliases are the tube labels a renumber
    turns old aliquot ids into) or a nested box; an empty slot has no aliases.
    """
    aliquot_id = f"a.{_ident('id')}"
    child_id = f"c.{_ident('id')}"
    aliquot_aliases = _alias_list_sql(aliquot_id, "aliquot")
    child_aliases = _alias_list_sql(child_id, "box")
    return (
        "  CASE\n"
        f"    WHEN {aliquot_id} IS NOT NULL THEN {aliquot_aliases}\n"
        f"    WHEN {child_id} IS NOT NULL THEN {child_aliases}\n"
        "    ELSE NULL\n"
        f"  END AS {_ident('occupant_alias_list')}"
    )


def view_statements(schema: EffectiveSchema) -> list[Statement]:
    """``v_box_slots``, ``v_aliquot_location``, ``v_stock_status`` and one
    ``v_<object>_<kind>`` per kind.

    Every view aggregates its object's ``alias`` rows into a final
    ``alias_list`` column (``occupant_alias_list`` for ``v_box_slots``).  The
    name avoids the domain field ``aliases`` a kind may define.
    """
    statements = [
        _view_box_slots(),
        _view_aliquot_location(),
        _view_stock_status(),
    ]
    for object_type in (ObjectType.entity, ObjectType.stock, ObjectType.box):
        core = object_type.value
        for kind_name in sorted(schema.kinds_for(object_type)):
            statements.append(_view_kind(core, kind_name, schema.kinds[kind_name]))
    return statements


def _view_box_slots() -> Statement:
    sql = (
        f"CREATE VIEW {_ident('v_box_slots')} AS\n"
        "SELECT\n"
        f"  b.{_ident('id')} AS {_ident('box_id')},\n"
        f"  b.{_ident('name')} AS {_ident('box_name')},\n"
        f"  b.{_ident('kind')} AS {_ident('box_kind')},\n"
        f"  s.{_ident('label')} AS {_ident('slot')},\n"
        f"  s.{_ident('row_label')} AS {_ident('row_label')},\n"
        f"  s.{_ident('col_label')} AS {_ident('col_label')},\n"
        f"  s.{_ident('ordinal')} AS {_ident('ordinal')},\n"
        "  CASE\n"
        f"    WHEN a.{_ident('id')} IS NOT NULL THEN 'aliquot'\n"
        f"    WHEN c.{_ident('id')} IS NOT NULL THEN 'box'\n"
        "    ELSE NULL\n"
        f"  END AS {_ident('occupant_type')},\n"
        f"  COALESCE(a.{_ident('id')}, c.{_ident('id')}) AS {_ident('occupant_id')},\n"
        f"  a.{_ident('id')} AS {_ident('aliquot_id')},\n"
        f"  a.{_ident('stock_id')} AS {_ident('stock_id')},\n"
        f"  st.{_ident('entity_id')} AS {_ident('entity_id')},\n"
        f"  e.{_ident('kind')} AS {_ident('entity_kind')},\n"
        f"  a.{_ident('status')} AS {_ident('status')},\n"
        f"  c.{_ident('id')} AS {_ident('child_box_id')},\n"
        f"  c.{_ident('name')} AS {_ident('child_box_name')},\n"
        f"{_occupant_alias_list_sql()}\n"
        f"FROM {_ident('slot')} s\n"
        f"JOIN {_ident('box')} b ON b.{_ident('id')} = s.{_ident('box_id')}\n"
        f"LEFT JOIN {_ident('aliquot')} a "
        f"ON a.{_ident('box_id')} = s.{_ident('box_id')} "
        f"AND a.{_ident('slot')} = s.{_ident('label')}\n"
        f"LEFT JOIN {_ident('box')} c "
        f"ON c.{_ident('parent_box_id')} = s.{_ident('box_id')} "
        f"AND c.{_ident('parent_slot')} = s.{_ident('label')}\n"
        f"LEFT JOIN {_ident('stock')} st ON st.{_ident('id')} = a.{_ident('stock_id')}\n"
        f"LEFT JOIN {_ident('entity')} e ON e.{_ident('id')} = st.{_ident('entity_id')}"
    )
    return Statement(sql=sql, object="view", name="v_box_slots")


def _view_aliquot_location() -> Statement:
    """Aliquots with their full nested box path.

    Dolt 2.3.5 supports a recursive CTE inside a view (verified by the tests),
    so no fixed-depth join fallback is needed.  ``CAST`` pins the path column
    type for ``UNION ALL``; 1024 characters is generous for six nesting levels.
    ``alias_list`` carries the aliquot's aliases (old tube ids after a renumber
    are aliases of the aliquot).
    """
    aliquot_aliases = _alias_list_sql(f"a.{_ident('id')}", "aliquot")
    sql = (
        f"CREATE VIEW {_ident('v_aliquot_location')} AS\n"
        f"WITH RECURSIVE {_ident('box_path')} AS (\n"
        "  SELECT\n"
        f"    b.{_ident('id')} AS {_ident('id')},\n"
        f"    b.{_ident('name')} AS {_ident('name')},\n"
        f"    b.{_ident('parent_box_id')} AS {_ident('parent_box_id')},\n"
        f"    CAST(b.{_ident('name')} AS CHAR(1024)) AS {_ident('box_path')},\n"
        "    0 AS " + _ident("depth") + "\n"
        f"  FROM {_ident('box')} b\n"
        f"  WHERE b.{_ident('parent_box_id')} IS NULL\n"
        "  UNION ALL\n"
        "  SELECT\n"
        f"    b.{_ident('id')} AS {_ident('id')},\n"
        f"    b.{_ident('name')} AS {_ident('name')},\n"
        f"    b.{_ident('parent_box_id')} AS {_ident('parent_box_id')},\n"
        f"    CONCAT(p.{_ident('box_path')}, '/', b.{_ident('name')}) "
        f"AS {_ident('box_path')},\n"
        f"    p.{_ident('depth')} + 1 AS {_ident('depth')}\n"
        f"  FROM {_ident('box')} b\n"
        f"  JOIN {_ident('box_path')} p ON b.{_ident('parent_box_id')} = p.{_ident('id')}\n"
        ")\n"
        "SELECT\n"
        f"  a.{_ident('id')} AS {_ident('aliquot_id')},\n"
        f"  a.{_ident('stock_id')} AS {_ident('stock_id')},\n"
        f"  a.{_ident('seq')} AS {_ident('seq')},\n"
        f"  a.{_ident('box_id')} AS {_ident('box_id')},\n"
        f"  a.{_ident('slot')} AS {_ident('slot')},\n"
        f"  a.{_ident('status')} AS {_ident('status')},\n"
        f"  a.{_ident('quantity')} AS {_ident('quantity')},\n"
        f"  a.{_ident('unit')} AS {_ident('unit')},\n"
        f"  a.{_ident('label')} AS {_ident('label')},\n"
        f"  p.{_ident('box_path')} AS {_ident('box_path')},\n"
        f"  CASE WHEN p.{_ident('box_path')} IS NULL THEN NULL\n"
        f"       ELSE CONCAT(p.{_ident('box_path')}, ':', a.{_ident('slot')})\n"
        f"  END AS {_ident('location')},\n"
        f"  {aliquot_aliases} AS {_ident('alias_list')}\n"
        f"FROM {_ident('aliquot')} a\n"
        f"LEFT JOIN {_ident('box_path')} p ON p.{_ident('id')} = a.{_ident('box_id')}"
    )
    return Statement(sql=sql, object="view", name="v_aliquot_location")


def _view_stock_status() -> Statement:
    """Stock status derived from aliquot statuses (spec §3).

    One row per stock.  ``total_quantity`` sums the aliquot quantities and
    ``unit`` is that unit when every aliquotted quantity shares one; a stock
    whose aliquots mix units therefore has a sum but a NULL unit.
    ``alias_list`` carries the stock's aliases (e.g. the legacy id a
    ``tm stock renumber`` replaced).
    """
    def count_where(condition: str) -> str:
        return f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)"

    stored = count_where(f"a.{_ident('status')} = {literal('stored')}")
    pending = count_where(f"a.{_ident('status')} = {literal('pending')}")
    missing = count_where(f"a.{_ident('status')} = {literal('missing')}")
    consumed = count_where(f"a.{_ident('status')} = {literal('consumed')}")
    # Rows that are neither missing nor consumed (so 'missing' requires that
    # every remaining aliquot is missing or consumed).
    not_missing_consumed = count_where(
        f"a.{_ident('status')} NOT IN ({literal('missing')}, {literal('consumed')})"
    )
    stock_aliases = _alias_list_sql(f"s.{_ident('id')}", "stock")
    sql = (
        f"CREATE VIEW {_ident('v_stock_status')} AS\n"
        "SELECT\n"
        f"  s.{_ident('id')} AS {_ident('stock_id')},\n"
        f"  s.{_ident('kind')} AS {_ident('kind')},\n"
        f"  s.{_ident('entity_id')} AS {_ident('entity_id')},\n"
        "  CASE\n"
        f"    WHEN COUNT(a.{_ident('id')}) = 0 THEN 'empty'\n"
        f"    WHEN {stored} > 0 THEN 'active'\n"
        f"    WHEN {pending} > 0 THEN 'pending'\n"
        f"    WHEN {missing} > 0 AND {not_missing_consumed} = 0 THEN 'missing'\n"
        f"    WHEN SUM(CASE WHEN a.{_ident('status')} <> {literal('consumed')} "
        "THEN 1 ELSE 0 END) = 0 THEN 'depleted'\n"
        "    ELSE 'pending'\n"
        f"  END AS {_ident('status')},\n"
        f"  COUNT(a.{_ident('id')}) AS {_ident('aliquot_count')},\n"
        f"  {stored} AS {_ident('stored_count')},\n"
        f"  {pending} AS {_ident('pending_count')},\n"
        f"  {missing} AS {_ident('missing_count')},\n"
        f"  {consumed} AS {_ident('consumed_count')},\n"
        f"  SUM(a.{_ident('quantity')}) AS {_ident('total_quantity')},\n"
        f"  CASE WHEN COUNT(DISTINCT a.{_ident('unit')}) = 1 "
        f"THEN MAX(a.{_ident('unit')}) ELSE NULL END AS {_ident('unit')},\n"
        f"  {stock_aliases} AS {_ident('alias_list')}\n"
        f"FROM {_ident('stock')} s\n"
        f"LEFT JOIN {_ident('aliquot')} a ON a.{_ident('stock_id')} = s.{_ident('id')}\n"
        f"GROUP BY s.{_ident('id')}, s.{_ident('kind')}, s.{_ident('entity_id')}"
    )
    return Statement(sql=sql, object="view", name="v_stock_status")


def _view_kind(core: str, kind_name: str, kind: Kind) -> Statement:
    """``v_<object>_<kind>``: the core row joined with its kind table.

    When a kind field reuses a core column name (``name`` is the common case:
    it exists on ``entity`` and in the ``named_fields`` mixin), the kind's
    column is projected and the core one is omitted — the kind table is the
    domain-specific definition.

    The final ``alias_list`` column aggregates the object's alias rows.  It is
    deliberately not called ``aliases``: a kind field may already be named
    ``aliases`` (the bundled ``construct`` kind is), and a view cannot expose
    two columns with one name.
    """
    table = table_name(core, kind_name)
    core_columns = CORE_COLUMNS[core]
    kind_fields = list(kind.fields)
    if "alias_list" in kind_fields:
        # ``alias_list`` is a generated view column (spec §3).  Reserved here
        # rather than in the loader so every entry point that generates DDL
        # rejects the collision the same way.
        raise SchemaError(
            f"kind {kind_name!r} defines a field named 'alias_list', which "
            "collides with the generated view column of the same name"
        )
    overridden = {name for name in kind_fields if name in core_columns}

    select: list[str] = []
    for column in core_columns:
        if column in overridden:
            continue
        select.append(f"  c.{_ident(column)} AS {_ident(column)}")
    for column in kind_fields:
        select.append(f"  k.{_ident(column)} AS {_ident(column)}")
    core_id = f"c.{_ident('id')}"
    select.append(f"  {_alias_list_sql(core_id, core)} AS {_ident('alias_list')}")

    sql = (
        f"CREATE VIEW {_ident(f'v_{core}_{kind_name}')} AS\n"
        "SELECT\n"
        + ",\n".join(select)
        + f"\nFROM {_ident(table)} k\n"
        f"JOIN {_ident(core)} c ON c.{_ident('id')} = k.{_ident('id')}"
    )
    return Statement(sql=sql, object="view", name=f"v_{core}_{kind_name}")


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def slot_rows(box_id: str, geometry: Geometry) -> list[dict[str, Any]]:
    """Rows for ``slot`` when a box is created (spec §2.2).

    ``grid`` materialises rows × cols in row-major order, ``named`` the
    declared labels, and ``slotless`` no rows.  Keys match the ``slot``
    columns, ready for an ``INSERT`` in ticket 10.
    """
    if geometry.type is GeometryType.slotless:
        return []

    rows: list[dict[str, Any]] = []
    if geometry.type is GeometryType.named:
        for ordinal, label in enumerate(geometry.labels, start=1):
            rows.append(
                {
                    "box_id": box_id,
                    "label": label,
                    "row_label": None,
                    "col_label": None,
                    "ordinal": ordinal,
                }
            )
        return rows

    ordinal = 0
    row_labels = geometry.row_labels
    col_labels = geometry.col_labels
    for row_index, row in enumerate(row_labels, start=1):
        for col_index, col in enumerate(col_labels, start=1):
            ordinal += 1
            rows.append(
                {
                    "box_id": box_id,
                    "label": geometry.label.format(
                        row=row,
                        col=col,
                        row_index=row_index,
                        col_index=col_index,
                        ordinal=ordinal,
                    ),
                    "row_label": row,
                    "col_label": col,
                    "ordinal": ordinal,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Everything
# ---------------------------------------------------------------------------


def schema_statements(schema: EffectiveSchema) -> list[Statement]:
    """The complete DDL of one ledger in dependency order."""
    return [
        *core_statements(schema.statuses),
        *kind_statements(schema),
        *view_statements(schema),
    ]
