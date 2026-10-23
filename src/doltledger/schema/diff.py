"""Diff an effective YAML schema against a live ledger, and apply the plan.

``ledger schema plan`` (spec §5) compares the physical model generated from
the YAML (:mod:`doltledger.schema.ddl`) with the live database introspected via
``information_schema`` plus ``ledger_meta``.  Every difference becomes one
:class:`PlanStep` tagged ``additive`` or ``destructive`` and carrying the SQL
that performs it.

The plan is ordered so that it can be executed top to bottom:

1. table and column renames (``renamed_from``);
2. drops that other objects depend on: foreign keys, checks/unique indexes,
   generated views;
3. generated columns that must be rebuilt, then removed columns and tables;
4. new core/kind/extension tables;
5. column modifications, additions and generated-column rebuilds;
6. data updates for a renamed kind (its discriminator values);
7. new checks/unique indexes and foreign keys;
8. new/replaced views.

Renames are additive: the old table/column is renamed in place and its rows
are kept.  Drops, type changes and tightenings are destructive and are refused
by :func:`apply` unless ``allow_destructive`` is true.

``apply`` executes the plan in one write session and one Dolt commit, then
refreshes ``ledger_meta`` (fingerprint, preset versions, ``ledger.yaml`` copy)
and calls the generated ``dolt_tests`` installer when ticket 08's module is
available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Sequence

from doltledger.core.ledger import (
    LEDGER_FILE,
    META_FINGERPRINT,
    META_LEDGER_YAML,
    META_PRESET_VERSIONS,
    META_SPEC_VERSION,
)
from doltledger.driver.base import Driver, literal
from doltledger.errors import LedgerError
from doltledger.schema.ddl import (
    CORE_TABLES,
    CORE_VIEWS,
    core_statements,
    field_sql_type,
    schema_statements,
    table_name,
    view_statements,
)
from doltledger.schema.model import (
    SPEC_VERSION,
    UNSET,
    EffectiveSchema,
    Field,
    FieldType,
    ObjectType,
)
from doltledger.schema.tests_gen import install_tests

if TYPE_CHECKING:  # pragma: no cover - typing only
    from doltledger.core.ledger import Ledger

__all__ = [
    "ADDITIVE",
    "DESTRUCTIVE",
    "ApplyResult",
    "PlanStep",
    "SchemaPlan",
    "apply",
    "build_plan",
    "plan",
]

ADDITIVE = "additive"
DESTRUCTIVE = "destructive"

_KIND_PREFIXES = ("entity_", "stock_", "box_")
_VIEW_RE = re.compile(r"^v_(?:entity|stock|box)_[a-z][a-z0-9_]*$")
_STRING_LITERAL_RE = re.compile(r"'((?:[^'\\]|\\.|'')*)'")
_IN_RE = re.compile(r"\bIN\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_COLUMN_REF_RE = re.compile(r"`([^`]+)`")
_GENERATED_RE = re.compile(
    r"GENERATED\s+ALWAYS\s+AS\s*\((.*)\)\s*(?:STORED|VIRTUAL)\s*,?\s*$",
    re.IGNORECASE,
)
_CREATE_COLUMN_RE = re.compile(r"^\s*`([^`]+)`\s+(.*)$")


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """One ordered step of a schema plan."""

    action: str
    tag: str
    sql: str
    object: str
    name: str
    description: str = ""

    @property
    def destructive(self) -> bool:
        return self.tag == DESTRUCTIVE

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "tag": self.tag,
            "object": self.object,
            "name": self.name,
            "description": self.description,
            "sql": self.sql,
        }


@dataclass
class SchemaPlan:
    """The complete diff between a YAML schema and a live ledger."""

    steps: list[PlanStep] = field(default_factory=list)
    ledger: str = ""
    live_fingerprint: str | None = None
    expected_fingerprint: str = ""

    # -- summaries ---------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.steps

    @property
    def meta_stale(self) -> bool:
        return self.live_fingerprint != self.expected_fingerprint

    @property
    def additive(self) -> list[PlanStep]:
        return [step for step in self.steps if step.tag == ADDITIVE]

    @property
    def destructive(self) -> list[PlanStep]:
        return [step for step in self.steps if step.tag == DESTRUCTIVE]

    @property
    def pending(self) -> bool:
        """True when apply has work to do (SQL or a ``ledger_meta`` refresh)."""
        return bool(self.steps) or self.meta_stale

    @property
    def sql(self) -> str:
        if not self.steps:
            return ""
        return ";\n".join(step.sql for step in self.steps) + ";"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger,
            "fingerprint": {
                "live": self.live_fingerprint,
                "expected": self.expected_fingerprint,
                "stale": self.meta_stale,
            },
            "empty": self.is_empty,
            "sql": self.sql,
            "steps": [step.to_dict() for step in self.steps],
            "summary": {
                "total": len(self.steps),
                "additive": len(self.additive),
                "destructive": len(self.destructive),
            },
        }


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of :func:`apply`."""

    plan: SchemaPlan
    applied: bool
    commit_hash: str | None = None

    @property
    def committed(self) -> bool:
        return self.commit_hash is not None


# ---------------------------------------------------------------------------
# Desired physical model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    """A column the YAML schema asks for."""

    name: str
    sql_type: str
    nullable: bool
    default: Any = UNSET
    generated: bool = False
    expr: str | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not UNSET and self.default is not None


@dataclass(frozen=True)
class _DesiredTable:
    name: str
    object: str
    kind: str
    columns: dict[str, _Spec]
    create_sql: str | None


@dataclass(frozen=True)
class _DesiredUnique:
    name: str
    column: str


@dataclass(frozen=True)
class _DesiredCheck:
    name: str
    column: str
    clause: str
    values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _DesiredForeignKey:
    table: str
    name: str
    columns: tuple[str, ...]
    target: str
    target_columns: tuple[str, ...]


def _field_specs(fields: dict[str, Field]) -> dict[str, _Spec]:
    specs: dict[str, _Spec] = {}
    for name, schema_field in fields.items():
        if schema_field.type is FieldType.sequence:
            specs[name] = _Spec(
                name=name,
                sql_type="MEDIUMTEXT",
                nullable=not schema_field.required,
            )
            specs[f"{name}_sha256"] = _Spec(
                name=f"{name}_sha256",
                sql_type="CHAR(64)",
                nullable=True,
                generated=True,
                expr=f"SHA2(`{name}`, 256)",
            )
        elif schema_field.type is FieldType.computed:
            specs[name] = _Spec(
                name=name,
                sql_type=field_sql_type(schema_field),
                nullable=True,
                generated=True,
                expr=schema_field.expr,
            )
        else:
            specs[name] = _Spec(
                name=name,
                sql_type=field_sql_type(schema_field),
                nullable=not schema_field.required,
                default=schema_field.default if schema_field.has_default else UNSET,
            )
    return specs


def _desired_tables(
    schema: EffectiveSchema,
) -> dict[str, _DesiredTable]:
    tables: dict[str, _DesiredTable] = {}
    statements = {
        statement.name: statement
        for statement in schema_statements(schema)
        if statement.object == "table"
    }
    for object_type in (ObjectType.entity, ObjectType.stock, ObjectType.box):
        for kind_name in sorted(schema.kinds_for(object_type)):
            kind = schema.kinds[kind_name]
            table = table_name(object_type, kind_name)
            columns = {
                "id": _Spec("id", "VARCHAR(64)", nullable=False),
                "kind": _Spec(
                    "kind",
                    "VARCHAR(64)",
                    nullable=False,
                    default=kind_name,
                ),
                **_field_specs(kind.fields),
            }
            tables[table] = _DesiredTable(
                name=table,
                object=object_type.value,
                kind=kind_name,
                columns=columns,
                create_sql=statements[table].sql,
            )
            if object_type is ObjectType.stock and kind.aliquot_fields:
                extension = table_name("aliquot", kind_name)
                tables[extension] = _DesiredTable(
                    name=extension,
                    object="aliquot",
                    kind=kind_name,
                    columns={
                        "id": _Spec("id", "VARCHAR(64)", nullable=False),
                        **_field_specs(kind.aliquot_fields),
                    },
                    create_sql=statements[extension].sql,
                )
    return tables


def _desired_unique(fields: dict[str, Field], table: str) -> list[_DesiredUnique]:
    uniques: list[_DesiredUnique] = []
    for name, schema_field in fields.items():
        if not schema_field.unique:
            continue
        column = (
            f"{name}_sha256"
            if schema_field.type is FieldType.sequence
            else name
        )
        uniques.append(_DesiredUnique(name=f"uq_{table}_{name}", column=column))
    return uniques


def _desired_checks(
    fields: dict[str, Field], table: str
) -> list[_DesiredCheck]:
    checks: list[_DesiredCheck] = []
    for name, schema_field in fields.items():
        if schema_field.type is not FieldType.enum:
            continue
        values = tuple(schema_field.values or ())
        rendered = ", ".join(literal(value) for value in values)
        checks.append(
            _DesiredCheck(
                name=f"ck_{table}_{name}",
                column=name,
                clause=f"(`{name}` IN ({rendered}))",
                values=values,
            )
        )
    return checks


def _desired_foreign_keys(schema: EffectiveSchema) -> list[_DesiredForeignKey]:
    keys: list[_DesiredForeignKey] = []
    for statement in schema_statements(schema):
        if statement.object != "fk":
            continue
        assert statement.table is not None
        assert statement.columns is not None
        assert statement.target is not None
        assert statement.target_columns is not None
        keys.append(
            _DesiredForeignKey(
                table=statement.table,
                name=statement.name,
                columns=tuple(statement.columns),
                target=statement.target,
                target_columns=tuple(statement.target_columns),
            )
        )
    return keys


def _desired_views(schema: EffectiveSchema) -> dict[str, str]:
    views: dict[str, str] = {}
    for statement in view_statements(schema):
        head, separator, body = statement.sql.partition(" AS\n")
        views[statement.name] = body if separator else statement.sql
    return views


# ---------------------------------------------------------------------------
# Live introspection
# ---------------------------------------------------------------------------


@dataclass
class _Column:
    name: str
    sql_type: str
    nullable: bool
    default: str | None
    generated: bool = False
    generation: str | None = None


@dataclass(frozen=True)
class _Unique:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class _Check:
    name: str
    clause: str


@dataclass(frozen=True)
class _ForeignKey:
    table: str
    name: str
    columns: tuple[str, ...]
    target: str
    target_columns: tuple[str, ...]


@dataclass
class _LiveState:
    tables: dict[str, str]
    columns: dict[str, dict[str, _Column]]
    uniques: dict[str, list[_Unique]]
    checks: dict[str, list[_Check]]
    foreign_keys: list[_ForeignKey]
    views: dict[str, str]
    meta: dict[str, str]


def _introspect(driver: Driver) -> _LiveState:
    """Read tables, columns, constraints, views and ``ledger_meta``."""
    table_rows = driver.query(
        "SELECT table_name AS name, table_type AS kind "
        "FROM information_schema.tables WHERE table_schema = DATABASE()"
    )
    tables = {str(row["name"]): str(row["kind"]) for row in table_rows}

    column_rows = driver.query(
        "SELECT table_name AS tbl, column_name AS name, "
        "column_type AS ctype, is_nullable AS nullable, column_default AS def "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position"
    )
    columns: dict[str, dict[str, _Column]] = {}
    for row in column_rows:
        table = str(row["tbl"])
        if tables.get(table) != "BASE TABLE":
            continue
        name = str(row["name"])
        default = row.get("def")
        columns.setdefault(table, {})[name] = _Column(
            name=name,
            sql_type=str(row["ctype"]),
            nullable=str(row["nullable"]).upper() == "YES",
            default=None if default is None else str(default),
        )

    unique_rows = driver.query(
        "SELECT tc.table_name AS tbl, tc.constraint_name AS name, "
        "kcu.column_name AS col, kcu.ordinal_position AS pos "
        "FROM information_schema.table_constraints AS tc "
        "JOIN information_schema.key_column_usage AS kcu "
        "  ON kcu.constraint_schema = tc.constraint_schema "
        " AND kcu.table_name = tc.table_name "
        " AND kcu.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = DATABASE() AND tc.constraint_type = 'UNIQUE' "
        "ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position"
    )
    unique_parts: dict[tuple[str, str], list[str]] = {}
    for row in unique_rows:
        key = (str(row["tbl"]), str(row["name"]))
        unique_parts.setdefault(key, []).append(str(row["col"]))
    uniques: dict[str, list[_Unique]] = {}
    for (table, name), cols in unique_parts.items():
        uniques.setdefault(table, []).append(_Unique(name, tuple(cols)))

    check_rows = driver.query(
        "SELECT tc.table_name AS tbl, tc.constraint_name AS name, "
        "cc.check_clause AS clause "
        "FROM information_schema.table_constraints AS tc "
        "JOIN information_schema.check_constraints AS cc "
        "  ON cc.constraint_schema = tc.constraint_schema "
        " AND cc.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = DATABASE() AND tc.constraint_type = 'CHECK' "
        "ORDER BY tc.table_name, tc.constraint_name"
    )
    checks: dict[str, list[_Check]] = {}
    for row in check_rows:
        checks.setdefault(str(row["tbl"]), []).append(
            _Check(str(row["name"]), str(row["clause"]))
        )

    fk_rows = driver.query(
        "SELECT tc.table_name AS tbl, tc.constraint_name AS name, "
        "kcu.column_name AS col, kcu.ordinal_position AS pos, "
        "kcu.referenced_table_name AS ref_tbl, "
        "kcu.referenced_column_name AS ref_col "
        "FROM information_schema.table_constraints AS tc "
        "JOIN information_schema.key_column_usage AS kcu "
        "  ON kcu.constraint_schema = tc.constraint_schema "
        " AND kcu.table_name = tc.table_name "
        " AND kcu.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = DATABASE() AND tc.constraint_type = 'FOREIGN KEY' "
        "ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position"
    )
    fk_parts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in fk_rows:
        key = (str(row["tbl"]), str(row["name"]))
        entry = fk_parts.setdefault(
            key,
            {
                "columns": [],
                "target": str(row["ref_tbl"]),
                "target_columns": [],
            },
        )
        entry["columns"].append(str(row["col"]))
        entry["target_columns"].append(str(row["ref_col"]))
    foreign_keys = [
        _ForeignKey(
            table=table,
            name=name,
            columns=tuple(entry["columns"]),
            target=entry["target"],
            target_columns=tuple(entry["target_columns"]),
        )
        for (table, name), entry in fk_parts.items()
    ]

    view_rows = driver.query(
        "SELECT table_name AS name, view_definition AS definition "
        "FROM information_schema.views WHERE table_schema = DATABASE()"
    )
    views = {
        str(row["name"]): str(row["definition"] or "") for row in view_rows
    }

    # Generated columns are not visible in information_schema on 2.3.5;
    # SHOW CREATE TABLE carries the expression and generated column names.
    for table in columns:
        if table in CORE_TABLES:
            # Core tables have no generated columns.
            continue
        rows = driver.query(f"SHOW CREATE TABLE `{table.replace('`', '``')}`")
        if not rows:
            continue
        create_sql = str(rows[0].get("Create Table") or "")
        for name, expression in _parse_generated_columns(create_sql).items():
            column = columns[table].get(name)
            if column is None:  # pragma: no cover - inconsistent metadata
                continue
            columns[table][name] = replace(
                column, generated=True, generation=expression
            )

    try:
        meta_rows = driver.query(
            "SELECT `key` AS k, `value` AS v FROM `ledger_meta`"
        )
    except LedgerError:  # a database that was never initialised
        meta = {}
    else:
        meta = {str(row["k"]): str(row["v"]) for row in meta_rows}

    return _LiveState(
        tables=tables,
        columns=columns,
        uniques=uniques,
        checks=checks,
        foreign_keys=foreign_keys,
        views=views,
        meta=meta,
    )


def _parse_generated_columns(create_sql: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in create_sql.splitlines():
        match = _CREATE_COLUMN_RE.match(line)
        if match is None:
            continue
        generated = _GENERATED_RE.search(match.group(2))
        if generated is not None:
            found[match.group(1)] = generated.group(1).strip()
    return found


# ---------------------------------------------------------------------------
# SQL/value normalisation
# ---------------------------------------------------------------------------


def _norm_type(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


#: Dolt rewrites ``CAST(x AS T)`` as ``convert(x, t)`` in ``SHOW CREATE TABLE``.
_CAST_RE = re.compile(
    r"cast\((.+?)as(decimal\([0-9]+,[0-9]+\)|signed|unsigned|char\([0-9]+\)|date|datetime)\)"
)


def _norm_expr(expr: str) -> str:
    """Canonical form for comparison against Dolt's rewritten expressions."""
    text = expr.replace("`", "").replace("_utf8mb4", "").lower()
    text = re.sub(r"\s+", "", text)
    text = _CAST_RE.sub(r"convert(\1,\2)", text)
    while text.startswith("(") and text.endswith(")") and _balanced(text):
        text = text[1:-1]
    return text


def _balanced(text: str) -> bool:
    """True when the first character's parenthesis closes at the last one."""
    depth = 0
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_string and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index == len(text) - 1
        index += 1
    return False


def _norm_clause(clause: str) -> str:
    return _norm_expr(clause)


def _default_equal(expected: Any, live: str | None, sql_type: str = "") -> bool:
    """Compare a YAML default with Dolt's ``column_default`` representation."""
    if expected is UNSET or expected is None:
        return live is None
    if live is None:
        return False
    if isinstance(expected, bool):
        return str(live).strip().lower() in (
            ("1", "true") if expected else ("0", "false")
        )
    if isinstance(expected, (int, float, Decimal)):
        try:
            return Decimal(live) == Decimal(str(expected))
        except InvalidOperation:  # pragma: no cover - defensive
            return False

    normalized = _norm_type(sql_type)
    if normalized == "date":
        if isinstance(expected, datetime):
            expected = expected.date()
        return str(live).strip().startswith(str(expected)[:10])
    if normalized == "datetime":
        if isinstance(expected, datetime):
            parsed = expected
        else:
            try:
                parsed = datetime.fromisoformat(str(expected))
            except ValueError:
                return str(live) == str(expected)
        return str(live).strip() == parsed.isoformat(sep=" ")
    return str(live) == str(expected)


def _check_values(clause: str) -> list[str] | None:
    match = _IN_RE.search(clause)
    if match is None:
        return None
    values = []
    for literal_match in _STRING_LITERAL_RE.finditer(match.group(1)):
        raw = literal_match.group(1)
        values.append(
            raw.replace("''", "'")
            .replace("\\'", "'")
            .replace("\\\\", "\\")
            .replace("\\n", "\n")
            .replace("\\r", "\r")
        )
    return values


def _clause_has_column(clause: str, column: str) -> bool:
    return f"`{column}`" in clause


def _expr_columns(expr: str) -> set[str]:
    return set(_COLUMN_REF_RE.findall(expr))


def _translate_expr(expr: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return expr

    def rename(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"`{mapping.get(name, name)}`"

    return _COLUMN_REF_RE.sub(rename, expr)


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


class _Buckets:
    """Plan steps grouped by execution phase."""

    def __init__(self) -> None:
        self.renames: list[PlanStep] = []
        self.fk_drops: list[PlanStep] = []
        self.constraint_drops: list[PlanStep] = []
        self.view_drops: list[PlanStep] = []
        self.generated_drops: list[PlanStep] = []
        self.column_drops: list[PlanStep] = []
        self.table_drops: list[PlanStep] = []
        self.core_creates: list[PlanStep] = []
        self.table_creates: list[PlanStep] = []
        self.column_modifies: list[PlanStep] = []
        self.column_adds: list[PlanStep] = []
        self.generated_adds: list[PlanStep] = []
        self.updates: list[PlanStep] = []
        self.constraint_adds: list[PlanStep] = []
        self.fk_adds: list[PlanStep] = []
        self.view_creates: list[PlanStep] = []

    def ordered(self) -> list[PlanStep]:
        return [
            *self.renames,
            *self.fk_drops,
            *self.constraint_drops,
            *self.view_drops,
            *self.generated_drops,
            *self.column_drops,
            *self.table_drops,
            *self.core_creates,
            *self.table_creates,
            *self.column_modifies,
            *self.column_adds,
            *self.generated_adds,
            *self.updates,
            *self.constraint_adds,
            *self.fk_adds,
            *self.view_creates,
        ]


def _step(
    action: str,
    tag: str,
    sql: str,
    *,
    object: str,
    name: str,
    description: str = "",
) -> PlanStep:
    return PlanStep(
        action=action,
        tag=tag,
        sql=sql,
        object=object,
        name=name,
        description=description,
    )


def _ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def build_plan(
    driver: Driver,
    schema: EffectiveSchema,
    *,
    ledger_name: str = "",
) -> SchemaPlan:
    """Compare ``schema`` with the live database behind ``driver``."""
    desired = _desired_tables(schema)
    desired_fks = _desired_foreign_keys(schema)
    desired_views = _desired_views(schema)

    live = _introspect(driver)
    plan = SchemaPlan(
        ledger=ledger_name,
        live_fingerprint=live.meta.get(META_FINGERPRINT),
        expected_fingerprint=schema.fingerprint(),
    )
    buckets = _Buckets()

    table_renames, renamed_kinds = _plan_renames(
        buckets, schema, desired, live
    )
    column_renames = _plan_column_renames(
        buckets, schema, desired, live, table_renames
    )
    state = _translate_live(live, table_renames, column_renames)

    created: set[str] = set()
    dropped_tables: set[str] = set()

    # -- core tables -------------------------------------------------------
    core_sql = {
        statement.name: statement.sql
        for statement in core_statements(schema.statuses)
        if statement.object == "table"
    }
    for table in CORE_TABLES:
        if table in state.columns:
            continue
        buckets.core_creates.append(
            _step(
                "create_table",
                ADDITIVE,
                core_sql[table],
                object="table",
                name=table,
                description="create missing core table",
            )
        )
        created.add(table)

    # -- kind / extension tables ------------------------------------------
    for name, table in desired.items():
        if name in state.columns:
            continue
        assert table.create_sql is not None
        buckets.table_creates.append(
            _step(
                "create_table",
                ADDITIVE,
                table.create_sql,
                object="table",
                name=name,
                description=f"new {table.object} kind {table.kind!r}",
            )
        )
        created.add(name)

    extras = _extra_tables(state, desired)
    for name in sorted(extras, key=lambda item: (not item.startswith("aliquot_"), item)):
        if name in created:  # pragma: no cover - defensive
            continue
        buckets.table_drops.append(
            _step(
                "drop_table",
                DESTRUCTIVE,
                f"DROP TABLE {_ident(name)}",
                object="table",
                name=name,
                description="kind/extension no longer in the YAML schema",
            )
        )
        dropped_tables.add(name)

    # -- columns -----------------------------------------------------------
    dropped_columns: dict[str, set[str]] = {}
    readded_generated: dict[str, set[str]] = {}
    for name, table in desired.items():
        if name not in state.columns:
            continue
        dropped, readded = _diff_columns(
            buckets,
            name,
            table.columns,
            state.columns.get(name, {}),
            created=name in created,
        )
        dropped_columns[name] = dropped
        readded_generated[name] = readded

    # -- checks and unique indexes ----------------------------------------
    for name, table in desired.items():
        if name in created:
            continue
        kind = schema.kinds.get(table.kind)
        if kind is None:  # pragma: no cover - desired tables come from kinds
            continue
        fields = (
            kind.aliquot_fields
            if table.object == "aliquot"
            else kind.fields
        )
        checks = _desired_checks(fields, name)
        if table.object != "aliquot":
            checks.insert(
                0,
                _DesiredCheck(
                    name=f"ck_{name}_kind",
                    column="kind",
                    clause=f"({_ident('kind')} = {literal(table.kind)})",
                ),
            )
        _diff_constraints(
            buckets,
            name,
            desired_uniques=_desired_unique(fields, name),
            desired_checks=checks,
            live=state,
            dropped=dropped_columns.get(name, set()),
            readded=readded_generated.get(name, set()),
        )

    if "aliquot" in state.columns and "aliquot" not in created:
        _diff_constraints(
            buckets,
            "aliquot",
            desired_uniques=[],
            desired_checks=[
                _DesiredCheck(
                    name="ck_aliquot_status",
                    column="status",
                    clause=(
                        "(`status` IN ("
                        + ", ".join(literal(value) for value in schema.statuses)
                        + "))"
                    ),
                    values=tuple(schema.statuses),
                )
            ],
            live=state,
            dropped=set(),
            readded=set(),
            drop_extras=False,
        )

    # -- renamed kinds keep their identities, not their discriminator ------
    for core, old_kind, new_kind, _old_table, new_table in renamed_kinds:
        buckets.updates.append(
            _step(
                "update_data",
                ADDITIVE,
                f"UPDATE {_ident(new_table)} SET {_ident('kind')} = "
                f"{literal(new_kind)} WHERE {_ident('kind')} = {literal(old_kind)}",
                object="data",
                name=new_table,
                description=f"rename kind values {old_kind!r} to {new_kind!r}",
            )
        )
        buckets.updates.append(
            _step(
                "update_data",
                ADDITIVE,
                f"UPDATE {_ident(core)} SET {_ident('kind')} = "
                f"{literal(new_kind)} WHERE {_ident('kind')} = {literal(old_kind)}",
                object="data",
                name=core,
                description=f"rename core kind values {old_kind!r} to {new_kind!r}",
            )
        )

    # -- foreign keys ------------------------------------------------------
    _diff_foreign_keys(
        buckets,
        desired_fks,
        state,
        desired_tables=desired,
        created=created,
        dropped_tables=dropped_tables,
        renamed_kinds=renamed_kinds,
    )

    # -- views -------------------------------------------------------------
    _diff_views(
        buckets,
        desired_views,
        state.views,
    )

    plan.steps = buckets.ordered()
    return plan


def plan(ledger: "Ledger", schema: EffectiveSchema | None = None) -> SchemaPlan:
    """The plan for an opened ledger (defaults to its loaded YAML schema)."""
    return build_plan(
        ledger.driver,
        schema or ledger.schema,
        ledger_name=ledger.name,
    )


# -- renames ---------------------------------------------------------------


def _plan_renames(
    buckets: _Buckets,
    schema: EffectiveSchema,
    desired: dict[str, _DesiredTable],
    live: _LiveState,
) -> tuple[dict[str, str], list[tuple[str, str, str, str, str]]]:
    """Plan ``ALTER TABLE ... RENAME TO`` for kinds that declare a rename."""
    table_renames: dict[str, str] = {}
    renamed_kinds: list[tuple[str, str, str, str, str]] = []

    for kind_name, kind in schema.kinds.items():
        old_kind = kind.renamed_from
        if not old_kind:
            continue
        old_table = table_name(kind.object, old_kind)
        new_table = table_name(kind.object, kind_name)
        if (
            old_table in live.columns
            and new_table not in live.columns
            and new_table in desired
        ):
            buckets.renames.append(
                _step(
                    "rename_table",
                    ADDITIVE,
                    f"ALTER TABLE {_ident(old_table)} RENAME TO {_ident(new_table)}",
                    object="table",
                    name=new_table,
                    description=f"rename kind {old_kind!r} to {kind_name!r}",
                )
            )
            table_renames[old_table] = new_table
            renamed_kinds.append(
                (kind.object.value, old_kind, kind_name, old_table, new_table)
            )

        if kind.object is not ObjectType.stock or not kind.aliquot_fields:
            continue
        old_extension = table_name("aliquot", old_kind)
        new_extension = table_name("aliquot", kind_name)
        if (
            old_extension in live.columns
            and new_extension not in live.columns
            and new_extension in desired
        ):
            buckets.renames.append(
                _step(
                    "rename_table",
                    ADDITIVE,
                    f"ALTER TABLE {_ident(old_extension)} "
                    f"RENAME TO {_ident(new_extension)}",
                    object="table",
                    name=new_extension,
                    description=(
                        f"rename aliquot extension of kind "
                        f"{old_kind!r} to {kind_name!r}"
                    ),
                )
            )
            table_renames[old_extension] = new_extension

    return table_renames, renamed_kinds


def _plan_column_renames(
    buckets: _Buckets,
    schema: EffectiveSchema,
    desired: dict[str, _DesiredTable],
    live: _LiveState,
    table_renames: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Plan ``ALTER TABLE ... RENAME COLUMN`` from ``renamed_from``."""
    final_to_live = {new: old for old, new in table_renames.items()}
    renames: dict[str, dict[str, str]] = {}

    for kind_name, kind in schema.kinds.items():
        scopes: list[tuple[str, dict[str, Field]]] = [
            (table_name(kind.object, kind_name), kind.fields)
        ]
        if kind.object is ObjectType.stock and kind.aliquot_fields:
            scopes.append(
                (table_name("aliquot", kind_name), kind.aliquot_fields)
            )
        for table, fields in scopes:
            if table not in desired:
                continue
            source = final_to_live.get(table, table)
            live_columns = live.columns.get(source, {})
            if not live_columns:
                continue
            mapping: dict[str, str] = {}
            for field_name, schema_field in fields.items():
                old_name = schema_field.renamed_from
                if not old_name:
                    continue
                if old_name not in live_columns or field_name in live_columns:
                    continue
                buckets.renames.append(
                    _step(
                        "rename_column",
                        ADDITIVE,
                        f"ALTER TABLE {_ident(table)} RENAME COLUMN "
                        f"{_ident(old_name)} TO {_ident(field_name)}",
                        object="column",
                        name=f"{table}.{field_name}",
                        description=f"rename field {old_name!r} to {field_name!r}",
                    )
                )
                mapping[old_name] = field_name
                if schema_field.type is FieldType.sequence:
                    old_sha = f"{old_name}_sha256"
                    new_sha = f"{field_name}_sha256"
                    if old_sha in live_columns and new_sha not in live_columns:
                        buckets.renames.append(
                            _step(
                                "rename_column",
                                ADDITIVE,
                                f"ALTER TABLE {_ident(table)} RENAME COLUMN "
                                f"{_ident(old_sha)} TO {_ident(new_sha)}",
                                object="column",
                                name=f"{table}.{new_sha}",
                                description=(
                                    "rename the generated sequence hash column"
                                ),
                            )
                        )
                        mapping[old_sha] = new_sha
            if mapping:
                renames[table] = mapping
    return renames


def _translate_live(
    live: _LiveState,
    table_renames: dict[str, str],
    column_renames: dict[str, dict[str, str]],
) -> _LiveState:
    """Rewrite live metadata as if the planned renames had already run."""
    tables = {
        table_renames.get(name, name): kind
        for name, kind in live.tables.items()
    }
    columns: dict[str, dict[str, _Column]] = {}
    for table, table_columns in live.columns.items():
        final = table_renames.get(table, table)
        mapping = column_renames.get(final, {})
        translated: dict[str, _Column] = {}
        for name, column in table_columns.items():
            new_name = mapping.get(name, name)
            generation = column.generation
            if generation is not None:
                generation = _translate_expr(generation, mapping)
            translated[new_name] = replace(
                column,
                name=new_name,
                generation=generation,
            )
        columns[final] = translated

    uniques: dict[str, list[_Unique]] = {}
    for table, table_uniques in live.uniques.items():
        final = table_renames.get(table, table)
        mapping = column_renames.get(final, {})
        uniques[final] = [
            _Unique(
                unique.name,
                tuple(mapping.get(column, column) for column in unique.columns),
            )
            for unique in table_uniques
        ]

    checks: dict[str, list[_Check]] = {}
    for table, table_checks in live.checks.items():
        final = table_renames.get(table, table)
        mapping = column_renames.get(final, {})
        checks[final] = [
            _Check(check.name, _translate_expr(check.clause, mapping))
            for check in table_checks
        ]

    foreign_keys: list[_ForeignKey] = []
    for key in live.foreign_keys:
        table = table_renames.get(key.table, key.table)
        mapping = column_renames.get(table, {})
        target = table_renames.get(key.target, key.target)
        target_mapping = column_renames.get(target, {})
        foreign_keys.append(
            _ForeignKey(
                table=table,
                name=key.name,
                columns=tuple(mapping.get(col, col) for col in key.columns),
                target=target,
                target_columns=tuple(
                    target_mapping.get(col, col) for col in key.target_columns
                ),
            )
        )

    return _LiveState(
        tables=tables,
        columns=columns,
        uniques=uniques,
        checks=checks,
        foreign_keys=foreign_keys,
        views=dict(live.views),
        meta=dict(live.meta),
    )


def _extra_tables(
    live: _LiveState, desired: dict[str, _DesiredTable]
) -> list[str]:
    extras: list[str] = []
    for table in live.columns:
        if table in desired or table in CORE_TABLES:
            continue
        if table.startswith(_KIND_PREFIXES):
            extras.append(table)
        elif table.startswith("aliquot_"):
            kind = table[len("aliquot_") :]
            if f"stock_{kind}" in live.tables or f"stock_{kind}" in live.columns:
                extras.append(table)
    return extras


# -- columns ---------------------------------------------------------------


def _definition(spec: _Spec) -> str:
    column = _ident(spec.name)
    if spec.generated:
        return (
            f"{column} {spec.sql_type} GENERATED ALWAYS AS "
            f"({spec.expr}) STORED"
        )
    sql = f"{column} {spec.sql_type}"
    if not spec.nullable:
        sql += " NOT NULL"
    if spec.has_default:
        sql += f" DEFAULT {literal(spec.default)}"
    return sql


def _diff_columns(
    buckets: _Buckets,
    table: str,
    desired: dict[str, _Spec],
    live: dict[str, _Column],
    *,
    created: bool,
) -> tuple[set[str], set[str]]:
    """Plan column changes for one table.

    Returns ``(dropped_columns, rebuilt_generated_columns)`` for the
    constraint and foreign-key passes.
    """
    if created:
        return set(), set()

    live_base = {name: col for name, col in live.items() if not col.generated}
    live_generated = {name: col for name, col in live.items() if col.generated}
    desired_base = {name: col for name, col in desired.items() if not col.generated}
    desired_generated = {
        name: col for name, col in desired.items() if col.generated
    }

    dropped_base: set[str] = set()
    changed_type: set[str] = set()

    for name, spec in desired_base.items():
        if name == "id":
            continue
        current = live_base.get(name)
        if current is None:
            tag = (
                DESTRUCTIVE
                if not spec.nullable and not spec.has_default
                else ADDITIVE
            )
            buckets.column_adds.append(
                _step(
                    "add_column",
                    tag,
                    f"ALTER TABLE {_ident(table)} ADD COLUMN {_definition(spec)}",
                    object="column",
                    name=f"{table}.{name}",
                    description="new field" if spec.nullable else "new required field",
                )
            )
            continue

        type_changed = _norm_type(current.sql_type) != _norm_type(spec.sql_type)
        tightened = current.nullable and not spec.nullable
        relaxed = not current.nullable and spec.nullable
        default_changed = not _default_equal(
            spec.default, current.default, spec.sql_type
        )
        if not (type_changed or tightened or relaxed or default_changed):
            continue
        tag = DESTRUCTIVE if (type_changed or tightened) else ADDITIVE
        buckets.column_modifies.append(
            _step(
                "modify_column",
                tag,
                f"ALTER TABLE {_ident(table)} MODIFY COLUMN {_definition(spec)}",
                object="column",
                name=f"{table}.{name}",
                description=_modify_description(
                    type_changed=type_changed,
                    tightened=tightened,
                    relaxed=relaxed,
                    default_changed=default_changed,
                ),
            )
        )
        if type_changed:
            changed_type.add(name)

    for name in live_base:
        if name in desired_base or name == "id":
            continue
        buckets.column_drops.append(
            _step(
                "drop_column",
                DESTRUCTIVE,
                f"ALTER TABLE {_ident(table)} DROP COLUMN {_ident(name)}",
                object="column",
                name=f"{table}.{name}",
                description="field no longer in the YAML schema",
            )
        )
        dropped_base.add(name)

    rebuilt: set[str] = set()
    for name, current in live_generated.items():
        spec = desired_generated.get(name)
        if spec is None:
            buckets.generated_drops.append(_generated_drop(table, name))
            continue
        type_changed = _norm_type(current.sql_type) != _norm_type(spec.sql_type)
        expr_changed = _norm_expr(current.generation or "") != _norm_expr(
            spec.expr or ""
        )
        depends_on_changed = bool(
            _expr_columns(current.generation or "")
            & (dropped_base | changed_type)
        )
        if not (type_changed or expr_changed or depends_on_changed):
            continue
        tag = DESTRUCTIVE if type_changed else ADDITIVE
        buckets.generated_drops.append(
            _generated_drop(table, name, tag=tag)
        )
        buckets.generated_adds.append(
            _step(
                "add_generated_column",
                tag,
                f"ALTER TABLE {_ident(table)} "
                f"ADD COLUMN {_definition(spec)}",
                object="column",
                name=f"{table}.{name}",
                description="rebuild generated column",
            )
        )
        rebuilt.add(name)

    for name, spec in desired_generated.items():
        if name in live_generated:
            continue
        buckets.generated_adds.append(
            _step(
                "add_generated_column",
                ADDITIVE,
                f"ALTER TABLE {_ident(table)} ADD COLUMN {_definition(spec)}",
                object="column",
                name=f"{table}.{name}",
                description="new generated column",
            )
        )

    return dropped_base | rebuilt, rebuilt


def _generated_drop(
    table: str, name: str, *, tag: str = ADDITIVE
) -> PlanStep:
    return _step(
        "drop_generated_column",
        tag,
        f"ALTER TABLE {_ident(table)} DROP COLUMN {_ident(name)}",
        object="column",
        name=f"{table}.{name}",
        description="drop generated column before rebuilding it",
    )


def _modify_description(
    *,
    type_changed: bool,
    tightened: bool,
    relaxed: bool,
    default_changed: bool,
) -> str:
    reasons: list[str] = []
    if type_changed:
        reasons.append("type change")
    if tightened:
        reasons.append("tighten NOT NULL")
    if relaxed:
        reasons.append("relax NOT NULL")
    if default_changed:
        reasons.append("default change")
    return ", ".join(reasons)


# -- constraints -----------------------------------------------------------


def _diff_constraints(
    buckets: _Buckets,
    table: str,
    *,
    desired_uniques: Sequence[_DesiredUnique],
    desired_checks: Sequence[_DesiredCheck],
    live: _LiveState,
    dropped: set[str],
    readded: set[str],
    drop_extras: bool = True,
) -> None:
    live_uniques = live.uniques.get(table, [])
    by_columns = {unique.columns: unique for unique in live_uniques}
    wanted = {unique.column for unique in desired_uniques}
    for unique in desired_uniques:
        key = (unique.column,)
        current = by_columns.get(key)
        if current is not None and unique.column not in readded:
            continue
        if unique.column in dropped:
            continue
        buckets.constraint_adds.append(
            _step(
                "add_constraint",
                DESTRUCTIVE,
                f"ALTER TABLE {_ident(table)} ADD CONSTRAINT "
                f"{_ident(unique.name)} UNIQUE ({_ident(unique.column)})",
                object="constraint",
                name=f"{table}.{unique.name}",
                description="new UNIQUE constraint",
            )
        )

    if drop_extras:
        for unique in live_uniques:
            if any(column in dropped for column in unique.columns):
                continue
            if any(column in wanted for column in unique.columns):
                continue
            buckets.constraint_drops.append(
                _step(
                    "drop_constraint",
                    ADDITIVE,
                    f"ALTER TABLE {_ident(table)} "
                    f"DROP INDEX {_ident(unique.name)}",
                    object="constraint",
                    name=f"{table}.{unique.name}",
                    description="UNIQUE constraint no longer requested",
                )
            )
    live_checks = list(live.checks.get(table, []))
    used: set[str] = set()
    for check in desired_checks:
        if check.column in dropped:
            continue
        current = _find_check(live_checks, check, used)
        if current is None:
            buckets.constraint_adds.append(_check_add(table, check, ADDITIVE))
            continue
        used.add(current.name)
        if check.values is not None:
            actual = _check_values(current.clause)
            if actual is not None and set(actual) == set(check.values):
                continue
            tag = (
                ADDITIVE
                if actual is not None and set(actual) <= set(check.values)
                else DESTRUCTIVE
            )
            buckets.constraint_drops.append(_check_drop(table, current.name))
            buckets.constraint_adds.append(_check_add(table, check, tag))
        else:
            if _norm_clause(current.clause) == _norm_clause(check.clause):
                continue
            buckets.constraint_drops.append(_check_drop(table, current.name))
            buckets.constraint_adds.append(_check_add(table, check, ADDITIVE))

    for current in live_checks:
        if current.name in used:
            continue
        if not drop_extras or not current.name.startswith("ck_"):
            continue
        column = _check_column(current.clause)
        if column is None or column in dropped:
            continue
        buckets.constraint_drops.append(_check_drop(table, current.name))


def _check_add(
    table: str, check: _DesiredCheck, tag: str
) -> PlanStep:
    return _step(
        "add_constraint",
        tag,
        f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(check.name)} "
        f"CHECK {check.clause}",
        object="constraint",
        name=f"{table}.{check.name}",
        description="enum/discriminator CHECK constraint",
    )


def _check_drop(table: str, name: str) -> PlanStep:
    return _step(
        "drop_constraint",
        ADDITIVE,
        f"ALTER TABLE {_ident(table)} DROP CHECK {_ident(name)}",
        object="constraint",
        name=f"{table}.{name}",
        description="replace CHECK constraint",
    )


def _find_check(
    checks: Sequence[_Check],
    wanted: _DesiredCheck,
    used: set[str],
) -> _Check | None:
    for check in checks:
        if check.name in used or not check.name.startswith("ck_"):
            continue
        if not _clause_has_column(check.clause, wanted.column):
            continue
        has_in = _check_values(check.clause) is not None
        if wanted.values is not None and has_in:
            return check
        if wanted.values is None and not has_in:
            return check
    return None


def _check_column(clause: str) -> str | None:
    columns = _COLUMN_REF_RE.findall(clause)
    return columns[0] if columns else None


# -- foreign keys ----------------------------------------------------------


def _diff_foreign_keys(
    buckets: _Buckets,
    desired: Sequence[_DesiredForeignKey],
    live: _LiveState,
    *,
    desired_tables: dict[str, _DesiredTable],
    created: set[str],
    dropped_tables: set[str],
    renamed_kinds: Sequence[tuple[str, str, str, str, str]],
) -> None:
    desired_keys = {
        (key.table, key.columns, key.target, key.target_columns)
        for key in desired
    }
    live_keys = {
        (key.table, key.columns, key.target, key.target_columns)
        for key in live.foreign_keys
    }
    live_by_name = {(key.table, key.name): key for key in live.foreign_keys}
    dropped_names: set[tuple[str, str]] = set()

    # A renamed kind must update both the parent and the child discriminator
    # values, so its (id, kind) foreign key has to be dropped and re-added.
    forced: set[tuple[str, str, tuple[str, ...], str, tuple[str, ...]]] = set()
    for core, _old, _new, _old_table, new_table in renamed_kinds:
        for key in live.foreign_keys:
            if (
                key.table == new_table
                and key.columns == ("id", "kind")
                and key.target == core
            ):
                forced.add(
                    (key.table, key.columns, key.target, key.target_columns)
                )

    def managed(table: str) -> bool:
        return (
            table in CORE_TABLES
            or table in desired_tables
            or table in created
        )

    for key in live.foreign_keys:
        if key.table in dropped_tables or not managed(key.table):
            continue
        semantic = (key.table, key.columns, key.target, key.target_columns)
        if semantic in desired_keys and semantic not in forced:
            continue
        if (key.table, key.name) in dropped_names:
            continue
        buckets.fk_drops.append(_fk_drop_step(key))
        dropped_names.add((key.table, key.name))

    for key in desired:
        semantic = (key.table, key.columns, key.target, key.target_columns)
        if semantic in live_keys and semantic not in forced:
            continue
        current = live_by_name.get((key.table, key.name))
        if current is not None and (key.table, key.name) not in dropped_names:
            # Same constraint name, different definition: drop before add.
            buckets.fk_drops.append(_fk_drop_step(current))
            dropped_names.add((key.table, key.name))
        buckets.fk_adds.append(
            _step(
                "add_foreign_key",
                ADDITIVE,
                f"ALTER TABLE {_ident(key.table)} ADD CONSTRAINT "
                f"{_ident(key.name)} FOREIGN KEY "
                f"({', '.join(_ident(column) for column in key.columns)}) "
                f"REFERENCES {_ident(key.target)} "
                f"({', '.join(_ident(column) for column in key.target_columns)})",
                object="foreign key",
                name=f"{key.table}.{key.name}",
                description=f"references {key.target}",
            )
        )


def _fk_drop_step(key: _ForeignKey) -> PlanStep:
    return _step(
        "drop_foreign_key",
        ADDITIVE,
        f"ALTER TABLE {_ident(key.table)} DROP FOREIGN KEY {_ident(key.name)}",
        object="foreign key",
        name=f"{key.table}.{key.name}",
        description="foreign key no longer in the YAML schema",
    )


# -- views -----------------------------------------------------------------


def _diff_views(
    buckets: _Buckets,
    desired: dict[str, str],
    live: dict[str, str],
) -> None:
    for name, definition in live.items():
        if not _managed_view(name):
            continue
        wanted = desired.get(name)
        if wanted is None:
            buckets.view_drops.append(_view_drop(name))
        elif wanted != definition:
            buckets.view_drops.append(_view_drop(name))
            buckets.view_creates.append(_view_create(name, wanted))

    for name, definition in desired.items():
        if name in live:
            continue
        buckets.view_creates.append(_view_create(name, definition))


def _managed_view(name: str) -> bool:
    return name in CORE_VIEWS or bool(_VIEW_RE.match(name))


def _view_drop(name: str) -> PlanStep:
    return _step(
        "drop_view",
        ADDITIVE,
        f"DROP VIEW {_ident(name)}",
        object="view",
        name=name,
        description="generated view is replaced",
    )


def _view_create(name: str, body: str) -> PlanStep:
    return _step(
        "create_view",
        ADDITIVE,
        f"CREATE VIEW {_ident(name)} AS\n{body}",
        object="view",
        name=name,
        description="generated convenience view",
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply(
    ledger: "Ledger",
    *,
    allow_destructive: bool = False,
    schema_plan: SchemaPlan | None = None,
    dry_run: bool | None = None,
) -> ApplyResult:
    """Execute a schema plan in one write session and one Dolt commit.

    Destructive steps are refused with :class:`~doltledger.errors.LedgerError`
    unless ``allow_destructive`` is true (the CLI maps that to exit code 1).
    """
    current = schema_plan or build_plan(
        ledger.driver, ledger.schema, ledger_name=ledger.name
    )
    if current.destructive and not allow_destructive:
        details = "\n".join(
            f"  - {step.action} {step.name}: {step.description}"
            for step in current.destructive
        )
        raise LedgerError(
            f"refusing to apply {len(current.destructive)} destructive "
            f"step(s) without --allow-destructive:\n{details}"
        )

    if not current.steps and not current.meta_stale:
        return ApplyResult(plan=current, applied=False)

    if dry_run is None:
        dry_run = ledger.dry_run
    if dry_run:
        return ApplyResult(plan=current, applied=False)

    summary = (
        f"{len(current.steps)} step(s)"
        if current.steps
        else "refresh ledger_meta"
    )
    with ledger.write(summary, verb="apply", object="schema") as session:
        for step in current.steps:
            session.execute(step.sql)
        _refresh_meta(session, ledger)
        _install_generated_tests(ledger)
    commit_hash = session.commit_hash

    ledger.read_meta()
    return ApplyResult(
        plan=current, applied=True, commit_hash=commit_hash
    )


def _refresh_meta(session: Any, ledger: "Ledger") -> None:
    """Write the fingerprint/preset versions/ledger.yaml copy after apply."""
    values = _meta_values(ledger)
    for key, value in values.items():
        session.execute(
            "INSERT INTO `ledger_meta` (`key`, `value`) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
            (key, value),
        )


def _meta_values(ledger: "Ledger") -> dict[str, str]:
    config_path = ledger.path / LEDGER_FILE
    ledger_yaml = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    )
    return {
        META_SPEC_VERSION: str(SPEC_VERSION),
        META_FINGERPRINT: ledger.schema.fingerprint(),
        META_PRESET_VERSIONS: json.dumps(
            ledger.schema.preset_versions, sort_keys=True, ensure_ascii=False
        ),
        META_LEDGER_YAML: ledger_yaml,
    }


def _install_generated_tests(ledger: "Ledger") -> None:
    """Replace generated ``dolt_tests`` wholesale (spec §4) for ``schema apply``."""
    install_tests(ledger.driver, ledger.schema)
