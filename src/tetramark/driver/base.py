"""The :class:`Driver` interface shared by both Dolt backends.

Both drivers speak the same dialect of SQL and accept ``%s`` positional
placeholders (the PyMySQL convention).  The embedded driver cannot bind
parameters through the CLI, so it renders them as escaped SQL literals with
:func:`literal`; the server driver hands them to PyMySQL untouched.

Everything written here must work on Dolt 2.3.5.
"""

from __future__ import annotations

import abc
import math
import re
from contextlib import AbstractContextManager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Sequence

from tetramark.errors import LedgerError, UsageError

#: Positional parameters for a query.  Named parameters are deliberately not
#: supported: the embedded backend has no binder and would have to rewrite them.
Params = Sequence[Any] | None

NOTHING_TO_COMMIT = "nothing to commit"

#: Dolt errors start with these words on their own line; used to detect an
#: error in captured output even when ``dolt`` exited 0.
_ERROR_LINE = re.compile(r"(?im)^\s*(?:error|fatal|panic)\b")

#: Statements Dolt commits regardless of the surrounding transaction.  They are
#: the ones a driver may send as one batch (:meth:`Driver.execute_many`).
_DDL = re.compile(
    r"(?is)^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|#[^\n]*\n\s*)*"
    r"(?:CREATE|ALTER|DROP|RENAME|TRUNCATE|GRANT|REVOKE|COMMENT)\b"
)

__all__ = [
    "Driver",
    "Params",
    "NOTHING_TO_COMMIT",
    "check_branch_name",
    "commit_statements",
    "ensure_ascii",
    "is_ddl",
    "is_nothing_to_commit",
    "literal",
    "looks_like_error",
    "render_sql",
    "render_statement",
    "revision_database",
]


def check_branch_name(branch: str) -> str:
    """Validate a branch name before it selects a revision database.

    The name is interpolated into a backtick identifier as ``db/branch``; a
    backtick or control character would make that ambiguous, so it is rejected
    here (Dolt itself does not allow them in branch names either).
    """
    if not isinstance(branch, str) or not branch.strip():
        raise UsageError("a branch name must not be empty")
    if branch != branch.strip() or any(char in branch for char in "`\x00\n\r\t"):
        raise UsageError(f"invalid branch name {branch!r}")
    return branch


def revision_database(database: str, branch: str) -> str:
    """The quoted ``db/branch`` revision database for one session.

    This is Dolt's session-scoped checkout: reading and writing through it
    never moves the branch checked out in the repository.
    """
    check_branch_name(branch)
    return "`" + f"{database}/{branch}".replace("`", "``") + "`"


def literal(value: Any) -> str:
    """Render a Python value as a safe SQL literal.

    Strings are single-quoted and escaped (backslash, quote doubling and the
    control characters MySQL treats specially).  ``None``, booleans, numbers,
    :class:`~decimal.Decimal` and date/time objects are handled explicitly;
    anything else is a programming error.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LedgerError(f"cannot bind a non-finite float: {value!r}")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerError(f"cannot bind a non-finite Decimal: {value!r}")
        # ``format(..., "f")`` avoids scientific notation (1E+3) that Dolt's
        # parser would accept but that surprises humans reading the SQL.
        return format(value, "f")
    if isinstance(value, datetime):
        return quote(str(value.isoformat(sep=" ")))
    if isinstance(value, (date, time)):
        return quote(value.isoformat())
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    raise LedgerError(f"cannot bind a value of type {type(value).__name__}: {value!r}")


def quote(text: str) -> str:
    """Single-quote a string for MySQL/Dolt, escaping the dangerous bytes."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "''")
        .replace("\x00", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
    )
    return "'" + escaped + "'"


def render_statement(statement: str | tuple[str, Params]) -> str:
    """Render one entry of an :meth:`Driver.execute_many` batch as SQL.

    A batch entry is either a plain statement or a ``(sql, params)`` pair; both
    forms end up as one parameter-free statement, in the batch's order.
    """
    if isinstance(statement, tuple):
        sql, params = statement
        return render_sql(sql, params)
    return render_sql(statement, None)


def render_sql(sql: str, params: Params) -> str:
    """Substitute ``%s`` placeholders with literals, leaving SQL text alone.

    The scanner understands single/double-quoted strings, backtick identifiers
    and ``--``/``#``/``/* */`` comments, so placeholders that appear inside
    those contexts are left untouched (and a literal ``%%`` becomes ``%``,
    matching PyMySQL's behaviour when parameters are supplied).
    """
    if params is None:
        return sql

    # PyMySQL collapses ``%%`` to ``%`` in the SQL text whenever parameters are
    # supplied; do the same so both backends view the statement identically.
    sql = sql.replace("%%", "%")

    values = list(params)
    out: list[str] = []
    used = 0
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]

        if char in ("'", '"'):
            end = _skip_quoted(sql, i)
            out.append(sql[i:end])
            i = end
            continue
        if char == "`":
            end = _skip_backtick(sql, i)
            out.append(sql[i:end])
            i = end
            continue
        if sql.startswith("--", i) or char == "#":
            end = sql.find("\n", i)
            end = length if end < 0 else end + 1
            out.append(sql[i:end])
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = length if end < 0 else end + 2
            out.append(sql[i:end])
            i = end
            continue

        if sql.startswith("%s", i):
            if used >= len(values):
                raise LedgerError(
                    f"too few parameters: {sql.count('%s')} placeholders, {len(values)} values"
                )
            out.append(literal(values[used]))
            used += 1
            i += 2
            continue

        out.append(char)
        i += 1

    if used != len(values):
        raise LedgerError(f"too many parameters: {used} placeholders, {len(values)} values")
    return "".join(out)


def _skip_quoted(sql: str, start: int) -> int:
    """Return the index just past the quoted string starting at ``start``."""
    quote_char = sql[start]
    i = start + 1
    length = len(sql)
    while i < length:
        char = sql[i]
        if char == "\\":
            i += 2
            continue
        if char == quote_char:
            if i + 1 < length and sql[i + 1] == quote_char:  # doubled quote
                i += 2
                continue
            return i + 1
        i += 1
    raise LedgerError("unterminated string literal in SQL")


def _skip_backtick(sql: str, start: int) -> int:
    i = start + 1
    length = len(sql)
    while i < length:
        if sql[i] == "`":
            if i + 1 < length and sql[i + 1] == "`":
                i += 2
                continue
            return i + 1
        i += 1
    raise LedgerError("unterminated quoted identifier in SQL")


def ensure_ascii(message: str) -> None:
    """Refuse commit messages Dolt may truncate mid-byte (known 2.3.x bug)."""
    try:
        message.encode("ascii")
    except UnicodeEncodeError as error:
        raise LedgerError(
            "commit messages must be ASCII; keep non-ASCII text in the row's notes "
            f"or event instead: {message!r}"
        ) from error


def commit_statements(message: str, author: str | None = None) -> list[str]:
    """The two ``CALL`` statements that make one Dolt commit."""
    ensure_ascii(message)
    args = ["'-m'", literal(message)]
    if author:
        args.append("'--author'")
        args.append(literal(author))
    return ["CALL DOLT_ADD('-A')", f"CALL DOLT_COMMIT({', '.join(args)})"]


def is_nothing_to_commit(text: str) -> bool:
    """True when Dolt reports that the working set was already clean."""
    return NOTHING_TO_COMMIT in text.lower()


def looks_like_error(text: str) -> bool:
    """True when captured output carries a Dolt error (even with exit code 0)."""
    return bool(_ERROR_LINE.search(text))


def is_ddl(sql: str) -> bool:
    """True when ``sql`` is a statement Dolt commits immediately.

    Dolt cannot roll DDL back inside a transaction, so drivers and the write
    session run these outside one; a batch of them may share a single process
    (:meth:`Driver.execute_many`).  ``CREATE``/``ALTER``/``DROP``/``RENAME``/
    ``TRUNCATE``/``GRANT``/``REVOKE``/``COMMENT`` cover every generated
    statement; a leading comment is skipped.
    """
    return bool(_DDL.match(sql))


class Driver(abc.ABC):
    """Uniform access to one Dolt database.

    Writes are grouped with :meth:`transaction` and published with
    :meth:`commit`.  A Dolt commit must happen *after* the SQL transaction has
    closed; drivers raise :class:`LedgerError` otherwise.

    Dolt 2.3.5 does not roll DDL back inside a transaction, so transactions are
    for DML only.
    """

    @property
    @abc.abstractmethod
    def database(self) -> str:
        """Name of the Dolt database this driver talks to."""

    @abc.abstractmethod
    def query(
        self, sql: str, params: Params = None, *, fill_columns: bool = True
    ) -> list[dict[str, Any]]:
        """Run a read-only statement and return rows as dictionaries.

        ``fill_columns`` is part of the contract: with the default ``True``
        every column key is present in every row, even when its value is
        ``NULL``.  A caller that has established -- from the schema it just
        loaded -- that every selected column is ``NOT NULL`` may pass ``False``
        to opt out of that guarantee; a backend can then skip the extra
        metadata read it needs to discover NULL columns.  With ``False`` a
        ``NULL`` value is simply absent from the row, so the caller must not
        rely on the key being there.
        """

    def query_many(
        self,
        statements: Sequence[tuple[str, Params]],
        *,
        fill_columns: bool = True,
    ) -> list[list[dict[str, Any]]]:
        """Run several independent read-only statements, one result each.

        The results are returned in statement order; a statement that matches no
        rows yields an empty list (never a missing entry), so callers can index
        the batch by position.  A backend may run the whole batch in one round
        trip; this default implementation simply runs :meth:`query` per
        statement.  The statements must be independent (a later statement cannot
        read an earlier one's result) and must not write.

        ``fill_columns`` is forwarded to every statement's :meth:`query`; the
        default ``True`` keeps the all-columns-present contract.  Pass ``False``
        only when every selected column of every statement is ``NOT NULL`` (see
        :meth:`query`): the batch then never needs the NULL-column metadata read.
        """
        return [
            self.query(sql, params, fill_columns=fill_columns)
            for sql, params in statements
        ]

    @abc.abstractmethod
    def execute(self, sql: str, params: Params = None) -> None:
        """Run a writing statement.  ``DML`` only inside :meth:`transaction`."""

    def execute_many(self, statements: Sequence[str | tuple[str, Params]]) -> None:
        """Run several writing statements in order, as one logical batch.

        Each entry is a statement or a ``(sql, params)`` pair, exactly like one
        :meth:`execute` call.  The batch keeps its order, and a backend may run
        the whole batch in one round trip; this default implementation simply
        calls :meth:`execute` per statement, which is always correct.

        The batch is meant for statements Dolt applies immediately (DDL, see
        :func:`is_ddl`): a backend that defers DML in :meth:`transaction` must
        flush it first rather than fold it into the batch.  When a statement
        fails, the error names the statement that failed without re-running the
        ones that already ran.
        """
        for statement in statements:
            if isinstance(statement, tuple):
                self.execute(statement[0], statement[1])
            else:
                self.execute(statement)

    @abc.abstractmethod
    def transaction(self) -> AbstractContextManager[Driver]:
        """Context manager grouping writes; rolls back when the body raises."""

    @abc.abstractmethod
    def commit(self, message: str, author: str | None = None) -> str | None:
        """Create one Dolt commit; return its hash, or ``None`` if nothing changed."""

    def commit_pending(
        self,
        transaction: AbstractContextManager[Driver],
        message: str,
        author: str | None = None,
    ) -> str | None:
        """End the entered write transaction and create the Dolt commit.

        ``transaction`` is the already-entered context manager returned by
        :meth:`transaction`; this call closes it exactly once, so a
        :class:`~tetramark.core.ledger.WriteSession` uses this instead of
        ``transaction.__exit__`` followed by :meth:`commit`.  The default
        implementation is exactly that pair (two round trips); a backend that
        defers its writes may flush them and commit in one round trip as long
        as the contract is kept:

        * the transaction is closed on success *and* on failure;
        * the return value is the same as :meth:`commit` would return (the
          commit hash, or ``None`` when there was nothing to commit);
        * a failure still surfaces as :class:`~tetramark.errors.LedgerError`.
        """
        transaction.__exit__(None, None, None)
        return self.commit(message, author)
