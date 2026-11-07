"""Embedded driver: one ``dolt sql`` subprocess per statement batch.

The ledger directory *is* the database, so no server is needed.  Queries are
run with ``-r json`` and parsed from the newline-delimited JSON result sets.

Two Dolt quirks are handled here:

* Dolt exits 1 for most errors but not all of them, so success is decided from
  the output as well as the exit code: any non-JSON line, or an error-looking
  line on stdout/stderr, is a :class:`LedgerError`.
* JSON result rows omit columns whose value is ``NULL``.  A same-process probe
  (``CREATE TEMPORARY TABLE .. AS SELECT .. LIMIT 0`` + ``SHOW COLUMNS``)
  supplies the column list, so ``query`` returns dictionaries with every column
  present.  A caller that proved every selected column is ``NOT NULL`` can pass
  ``fill_columns=False`` to skip the NULL filling (the probe still runs for
  types; it is the same process, never a second one).
* When a user-run ``dolt sql-server`` is alive on the ledger directory, Dolt
  auto-routes ``dolt sql`` through it and its JSON result sets render integer
  columns as strings (``"1"`` instead of ``1``).  The probe's ``SHOW COLUMNS``
  ``Type`` is the authoritative SQL type of the same process, so integer-family
  columns are restored to Python ``int``; DECIMAL and textual values are never
  coerced.  See ``docs/spec/07-driver-layer.md`` for the exact read contract and
  the measured float/double-derived boundary.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from tetramark.driver.base import (
    Driver,
    Params,
    check_branch_name,
    commit_statements,
    is_ddl,
    is_nothing_to_commit,
    looks_like_error,
    render_sql,
    render_statement,
    revision_database,
)
from tetramark.errors import LedgerError, UnreachableError, UsageError

__all__ = ["EmbeddedDriver"]

#: A batch statement is preceded by ``SELECT 'TM_STMT_<n>'``; the JSON result
#: of that marker tells a failed batch how far Dolt got (see ``execute_many``).
_MARKER_SQL = "SELECT '{prefix}{index}' AS `marker`"
_MARKER_PREFIX = "TM_STMT_"
#: The probe prints these markers around its ``SHOW COLUMNS`` output.  The
#: routed server emits an extra ``{}`` result line for ``CREATE TEMPORARY
#: TABLE`` (the local engine emits none) and separates result lines with blank
#: lines, so the probe parser locates result sets by marker instead of offset.
_TYPE_MARKER_PREFIX = "TM_TYPES_"
#: ``SHOW COLUMNS`` ``Type`` values that hold integers: the SQL integer family,
#: optional display width and unsigned.  Nothing else is ever coerced.
_INTEGER_TYPE = re.compile(
    r"\A(?:tinyint|smallint|mediumint|int|integer|bigint)"
    r"(?:\(\d+\))?(?: unsigned)?\Z",
    re.IGNORECASE,
)
#: A routed integer cell: ASCII signed digits only.  Numeric-looking TEXT or
#: DECIMAL values may match too, which is why the column's SQL type decides.
_INTEGER_TEXT = re.compile(r"\A-?[0-9]+\Z", re.ASCII)
_MARKER_LINE = re.compile(
    r'^\s*\{\s*"rows"\s*:\s*\[\s*\{\s*"marker"\s*:\s*"TM_STMT_(\d+)"\s*\}\s*\]\s*\}\s*$'
)


class EmbeddedDriver(Driver):
    """Talk to a Dolt database by shelling out to ``dolt sql`` in its directory.

    ``branch`` selects a session-scoped revision database.  By default the
    branch is checked to exist immediately (one ``dolt sql`` run); a caller that
    folds that check into its own read batch passes ``validate_branch=False``
    and must validate the branch itself before relying on it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        branch: str | None = None,
        validate_branch: bool = True,
        dolt_binary: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        binary = dolt_binary or shutil.which("dolt")
        if binary is None:
            raise UnreachableError("the 'dolt' binary was not found on PATH")
        self._dolt = binary
        self._env = dict(env) if env is not None else None
        #: Statements buffered by :meth:`transaction`; ``None`` outside one.
        self._pending: list[str] | None = None
        #: Session branch; validated below once the repository is known to exist.
        self._branch: str | None = None
        if not (self._path / ".dolt").is_dir():
            raise UnreachableError(f"not a Dolt database: {self._path}")
        if branch is not None:
            name = check_branch_name(branch)
            if validate_branch:
                # The existence query runs on the checked-out database, so it
                # must happen before ``_branch`` selects a revision database.
                self._require_branch(name)
            self._branch = name

    def _require_branch(self, branch: str) -> str:
        """Fail with a usage error when ``branch`` does not exist (never create)."""
        name = check_branch_name(branch)
        # ``name`` is the dolt_branches primary key, hence NOT NULL: the row's
        # column is always present, so the CSV metadata run can be skipped.
        rows = self.query(
            "SELECT `name` FROM dolt_branches WHERE `name` = %s",
            (name,),
            fill_columns=False,
        )
        if not rows:
            raise UsageError(
                f"branch {name!r} does not exist in database {self.database!r} "
                "(it is never created implicitly)"
            )
        return name

    # -- identity ---------------------------------------------------------
    @property
    def path(self) -> Path:
        """The ledger's data directory."""
        return self._path

    @property
    def database(self) -> str:
        """Dolt names a database after its directory."""
        return self._path.name

    @property
    def branch(self) -> str | None:
        """The branch this session reads and writes, or ``None`` when checked out."""
        return self._branch

    # -- reads / writes ---------------------------------------------------
    def query(
        self, sql: str, params: Params = None, *, fill_columns: bool = True
    ) -> list[dict[str, Any]]:
        """Run one read in one ``dolt sql`` process.

        A ``SELECT``/``WITH`` read gets its column names *and* SQL types from the
        same process (:meth:`_query_probed`), so a ``NULL`` column is present as
        ``None`` and integer-family columns are Python ``int`` even when a
        user-run server makes Dolt route the read (its JSON then renders them as
        strings).  Other reads, and any probe Dolt rejects, fall back to a
        second ``-r csv`` header run and leave values exactly as Dolt reports
        them (the documented residual).  ``fill_columns=False`` still runs the
        same-process probe for types but skips the NULL filling: the caller must
        have proved every selected column is ``NOT NULL``, because a ``NULL``
        column is then simply absent from the row.
        """
        rendered = render_sql(sql, params)
        if self._pending:
            # There is no session to query *inside* the pending transaction, so
            # replay the buffer in a transaction that is rolled back and read
            # the state from there.  The buffer itself stays pending, ready for
            # the real commit when the context manager exits.  The type probe is
            # appended after the ROLLBACK: it only needs the committed schema.
            batch = ";\n".join(["START TRANSACTION", *self._pending, rendered, "ROLLBACK"])
            probed = None
            if _probeable(rendered):
                probed = self._query_probed(
                    [rendered],
                    prefix=["START TRANSACTION", *self._pending],
                    between=["ROLLBACK"],
                    fill_columns=fill_columns,
                )
            if probed is not None:
                return probed[0]
            rows = self._query_json(batch)
        else:
            probed = None
            if _probeable(rendered):
                probed = self._query_probed([rendered], fill_columns=fill_columns)
            if probed is not None:
                return probed[0]
            rows = self._query_json(rendered)
        if not fill_columns:
            return rows
        return self._with_columns(rows, rendered)

    def query_many(
        self,
        statements: Sequence[tuple[str, Params]],
        *,
        fill_columns: bool = True,
    ) -> list[list[dict[str, Any]]]:
        """Run a whole read batch in one ``dolt sql`` process.

        ``-r json`` emits one line per statement (``{}`` when a statement has no
        rows), so the batch stays aligned.  JSON omits a column whose value is
        ``NULL``; for a batch of ``SELECT``/``WITH`` reads the same process also
        prints every statement's column list and SQL type
        (:meth:`_query_probed`), which restores integer-family columns to
        Python ``int`` even when Dolt routes the batch through a user-run
        server.  Any other batch, or a probe Dolt rejects, uses the historical
        second ``-r csv`` run (CSV prints one header even for zero rows) and the
        columns are filled in exactly as :meth:`_with_columns` does; values are
        then left as Dolt reports them (the documented residual).  If the two
        runs cannot be aligned (an unexpected Dolt output shape), the statements
        fall back to individual :meth:`query` calls: the batching only changes
        speed, never the result.

        ``fill_columns=False`` is the explicit opt-out for a batch whose every
        selected column is ``NOT NULL``: NULL columns are not restored, but the
        same-process probe still runs for types, so integer parity holds.  The
        batch is still a single ``dolt sql`` process.
        """
        ordered = list(statements)
        if not ordered:
            return []
        if self._pending:
            # Inside the deferred transaction there is no session to batch in;
            # the per-statement path replays the buffer and previews the state.
            return [
                self.query(sql, params, fill_columns=fill_columns)
                for sql, params in ordered
            ]
        rendered = [render_sql(sql, params) for sql, params in ordered]
        if all(_probeable(sql) for sql in rendered):
            probed = self._query_probed(rendered, fill_columns=fill_columns)
            if probed is not None:
                return probed
        # A newline before the separator keeps a query that ends in a
        # ``--`` line comment from swallowing the next statement.
        batch = "\n;\n".join(rendered)
        result_sets = _parse_result_sets(self._invoke(batch, "json"), keep_empty=True)
        if len(result_sets) != len(rendered):
            return [
                self.query(sql, params, fill_columns=fill_columns)
                for sql, params in ordered
            ]
        if not fill_columns or not any(result_sets):
            return result_sets
        columns = self._batch_columns(batch, [len(rows) for rows in result_sets])
        if columns is None:  # pragma: no cover - defensive: unexpected Dolt output
            return [
                self._with_columns(rows, sql)
                for rows, sql in zip(result_sets, rendered)
            ]
        for rows, names in zip(result_sets, columns):
            for row in rows:
                for name in names:
                    row.setdefault(name, None)
        return result_sets

    def execute(self, sql: str, params: Params = None) -> None:
        rendered = render_sql(sql, params)
        if self._pending is not None:
            if is_ddl(rendered):
                # Dolt 2.3.5 does not roll DDL back, so it must run outside the
                # deferred DML transaction.  Flush the DML that logically came
                # first; otherwise a schema change would see the old rows.
                self._flush()
                self._run(rendered)
                return
            self._pending.append(rendered)
            return
        self._run(rendered)

    def execute_many(self, statements: Sequence[str | tuple[str, Params]]) -> None:
        """Run a whole DDL batch in one ``dolt sql`` process.

        Dolt applies DDL immediately, so the batch is one script (statement
        order preserved) instead of one process per statement.  Inside
        :meth:`transaction` the DML buffered so far is flushed first, in its
        own transaction, and only then does the batch run -- exactly the order
        the statements were called in.

        A failure keeps the Dolt error text but names the failing statement:
        each statement is preceded by a ``SELECT 'TM_STMT_<n>'`` marker, and
        the markers Dolt printed before it stopped say which statement was
        running.  Nothing is re-run to find it, so the DDL that already applied
        stays applied once.
        """
        ordered = list(statements)
        if not ordered:
            return
        rendered = [render_statement(statement) for statement in ordered]
        if self._pending is not None:
            self._flush()
        self._run_batch(rendered)

    @contextmanager
    def transaction(self) -> Iterator[EmbeddedDriver]:
        """Buffer the body's statements and run them in one transaction.

        ``query`` inside the context replays the buffer in a rolled-back
        transaction, so it previews the final state; the buffer is executed for
        real when the context exits without an exception.  A DDL statement
        flushes the DML buffered before it, because Dolt commits DDL
        immediately and statement order must be preserved.
        """
        if self._pending is not None:
            raise LedgerError("nested transactions are not supported")
        self._pending = []
        try:
            yield self
        except BaseException:
            self._pending = None  # discard: the body failed
            raise
        else:
            try:
                self._flush()
            finally:
                self._pending = None  # a failed final flush must not leave us "in" a transaction

    def _flush(self) -> None:
        """Execute the currently buffered DML in one transaction."""
        pending = self._pending
        if not pending:
            return
        self._pending = []
        self._run("START TRANSACTION;\n" + ";\n".join(pending) + ";\nCOMMIT")

    # -- history ----------------------------------------------------------
    def commit(self, message: str, author: str | None = None) -> str | None:
        if self._pending is not None:
            raise LedgerError("cannot create a Dolt commit inside an open transaction")
        statements = commit_statements(message, author)
        try:
            result_sets = self._run(";\n".join(statements))
        except LedgerError as error:
            if is_nothing_to_commit(str(error)):
                return None
            raise
        return _commit_hash(result_sets)

    def commit_pending(
        self,
        transaction: AbstractContextManager[Driver],
        message: str,
        author: str | None = None,
    ) -> str | None:
        """Flush the buffered DML and create the Dolt commit in ONE process.

        ``transaction`` is the entered context manager returned by
        :meth:`transaction`; this call ends it, so it must be called once in
        place of ``transaction.__exit__`` plus :meth:`commit`.  The usual
        ``START TRANSACTION; <buffered DML>; COMMIT`` script and
        :func:`commit_statements` are rendered into one script and run as one
        ``dolt sql`` process, then this returns the commit hash (``None`` when
        Dolt said there was nothing to commit).

        Failure behaves like the two-process path: if a statement before the
        DOLT commit fails, Dolt rolls the transaction back (or leaves the DDL
        it already applied) and the error surfaces as a
        :class:`~tetramark.errors.LedgerError`.  As before, a failure *after*
        the SQL ``COMMIT`` (e.g. DOLT_COMMIT refusing) leaves the rows in the
        working set, uncommitted in Dolt's history.
        """
        if self._pending is None:
            # Defensive: no deferred transaction to fold in.  Behave exactly
            # like the base class rather than commit inside a stale buffer.
            return super().commit_pending(transaction, message, author)
        pending = list(self._pending)
        try:
            statements = list(commit_statements(message, author))
            if pending:
                statements = ["START TRANSACTION", *pending, "COMMIT", *statements]
            # The transaction ends here: closing it below must not run the DML
            # again (``_flush`` only runs a non-empty buffer).
            self._pending = []
            try:
                result_sets = self._run(";\n".join(statements))
            except LedgerError as error:
                if is_nothing_to_commit(str(error)):
                    return None
                raise
        finally:
            # Ends the transaction even when the combined script failed; an
            # unexpected error before ``_pending`` was cleared still closes
            # it (and flushes the DML) exactly as the old two-step path did.
            transaction.__exit__(None, None, None)
        return _commit_hash(result_sets)

    # -- plumbing ---------------------------------------------------------
    def _query_probed(
        self,
        statements: Sequence[str],
        *,
        prefix: Sequence[str] = (),
        between: Sequence[str] = (),
        fill_columns: bool = True,
    ) -> list[list[dict[str, Any]]] | None:
        """Rows, column names and SQL types of ``SELECT`` reads in ONE run.

        ``-r json`` omits a column whose value is ``NULL``, so the column list
        used to need a second, ``-r csv`` process.  Here the same script also
        materialises each statement's shape as an empty temporary table
        (``CREATE TEMPORARY TABLE .. AS SELECT * FROM (<query>) .. LIMIT 0``)
        and prints ``SHOW COLUMNS`` for it.  Temporary tables live in the
        session only and never touch the working set.

        The ``Type`` column is the SQL type Dolt itself assigned to the same
        result, so integer-family columns are restored to Python ``int`` even
        when a live server makes Dolt route the run and render them as strings.
        Only integer-family cells holding ASCII signed digits are converted;
        DECIMAL, TEXT and every other type is returned exactly as Dolt reports
        it (see ``docs/spec/07-driver-layer.md``).

        ``prefix``/``between`` place the probe after the caller's own
        statements (the pending-transaction preview appends it after its
        ``ROLLBACK``).  Markers delimit the query result and the column list:
        a routed session prints an extra ``{}`` line for ``CREATE TEMPORARY
        TABLE``, so a fixed offset cannot be used.

        ``None`` means "use the two-process path": a probe Dolt rejects (for
        example duplicate column names), any error -- the old path then raises
        the query's own error text -- or output that does not align.
        """
        parts: list[str] = [*prefix]
        for index, sql in enumerate(statements):
            table = f"`tm_columns_{index}`"
            body = sql.rstrip().rstrip(";")
            parts.append(sql)
            parts.append(_type_marker_sql("Q", index))
            if index == len(statements) - 1:
                parts.extend(between)
            parts.append(
                f"CREATE TEMPORARY TABLE {table} AS SELECT * FROM (\n{body}\n) "
                f"AS `tm_probe` LIMIT 0"
            )
            parts.append(_type_marker_sql("C", index))
            parts.append(f"SHOW COLUMNS FROM {table}")
        try:
            result_sets = _parse_result_sets(
                self._invoke("\n;\n".join(parts), "json"), keep_empty=True
            )
        except LedgerError as error:
            if self._branch is not None and _missing_revision_database(
                str(error), self.database, self._branch
            ):
                # Every run starts with ``USE `db/branch`;``.  If that revision
                # database is missing, an un-probed run fails with the same
                # error, so re-running it would only repeat the failure and burn
                # a second ``dolt sql`` process (the budget test for a missing
                # branch pins at most two).
                raise
            return None
        rows_by_statement: list[list[dict[str, Any]]] = []
        shapes: list[list[dict[str, Any]]] = []
        cursor = 0
        for index in range(len(statements)):
            query_marker = _find_marker(result_sets, "Q", index, cursor)
            if query_marker is None or query_marker == 0:
                return None
            shape_marker = _find_marker(result_sets, "C", index, query_marker + 1)
            if shape_marker is None or shape_marker + 1 >= len(result_sets):
                return None
            rows_by_statement.append(result_sets[query_marker - 1])
            shapes.append(result_sets[shape_marker + 1])
            cursor = shape_marker + 1
        for rows, shape in zip(rows_by_statement, shapes):
            columns = [
                (str(column.get("Field")), column.get("Type")) for column in shape
            ]
            if not columns or any(name == "None" for name, _type in columns):
                return None
            _normalise_integers(rows, columns)
            if fill_columns:
                for row in rows:
                    for name, _type in columns:
                        row.setdefault(name, None)
        return rows_by_statement

    def _query_json(self, sql: str) -> list[dict[str, Any]]:
        result_sets = self._run(sql)
        return result_sets[-1] if result_sets else []

    def _with_columns(
        self, rows: list[dict[str, Any]], sql: str
    ) -> list[dict[str, Any]]:
        """Fill in columns Dolt omitted because a row's value was ``NULL``."""
        if not rows:
            return rows
        columns = self._columns(sql)
        if not columns:
            return rows
        for row in rows:
            for column in columns:
                row.setdefault(column, None)
        return rows

    def _columns(self, sql: str) -> list[str]:
        stdout = self._invoke(sql, "csv")
        if not stdout.strip():
            return []
        reader = csv.reader(io.StringIO(stdout))
        try:
            return next(reader)
        except StopIteration:  # pragma: no cover - guarded by the strip above
            return []

    def _batch_columns(
        self, batch: str, row_counts: Sequence[int]
    ) -> list[list[str]] | None:
        """Column names per statement from one ``-r csv`` run, or ``None``.

        CSV has no result-set separator: every statement contributes its header
        record followed by exactly as many data records as the JSON run reported
        rows (a zero-row statement prints only its header, an all-``NULL`` row is
        an empty record).  ``None`` means the output shape did not match the
        expectation (e.g. an old Dolt version), and the caller must fall back.
        """
        stdout = self._invoke(batch, "csv")
        if not stdout.strip():
            return None
        records = list(csv.reader(io.StringIO(stdout)))
        headers: list[list[str]] = []
        index = 0
        for count in row_counts:
            if index + 1 + count > len(records):
                return None
            headers.append(records[index])
            index += 1 + count
        if index != len(records):
            return None
        return headers

    def _run(self, sql: str) -> list[list[dict[str, Any]]]:
        return _parse_result_sets(self._invoke(sql, "json"))

    def _invoke(self, sql: str, result_format: str) -> str:
        if self._branch is not None:
            # Every ``dolt sql`` run is its own process, i.e. its own session;
            # USE on a revision database selects the branch for that session
            # only and never moves the repository's checked-out branch.
            sql = f"USE {revision_database(self.database, self._branch)};\n{sql}"
        argv = [self._dolt, "sql", "-r", result_format, "-q", sql]
        process = subprocess.run(
            argv,
            cwd=self._path,
            env=self._env,
            capture_output=True,
            text=True,
            check=False,
        )
        # CSV data cells may legitimately start with the word "error", so only
        # the JSON run is judged by its stdout content.
        _check_output(
            process.stdout,
            process.stderr,
            process.returncode,
            check_stdout=result_format == "json",
        )
        return process.stdout

    def _run_batch(self, rendered: Sequence[str]) -> None:
        """Run one script made of ``rendered``, each statement behind a marker."""
        parts: list[str] = []
        for index, sql in enumerate(rendered, start=1):
            parts.append(_MARKER_SQL.format(prefix=_MARKER_PREFIX, index=index))
            parts.append(sql)
        try:
            self._invoke("\n;\n".join(parts), "json")
        except LedgerError as error:
            raise _batch_error(error, rendered) from error


def _batch_error(error: LedgerError, rendered: Sequence[str]) -> LedgerError:
    """Name the failing statement of a batch without re-running any of it.

    ``_check_output`` puts both captured streams in the message, so the markers
    Dolt printed are in it too: marker ``n`` is emitted immediately before
    statement ``n``, therefore the highest marker seen is the statement that
    was running when Dolt stopped.  The marker lines are removed from the
    reported output; the Dolt error text itself is kept verbatim.
    """
    total = len(rendered)
    failing = 0
    kept: list[str] = []
    for line in str(error).splitlines():
        marker = _MARKER_LINE.match(line)
        if marker is not None:
            failing = max(failing, int(marker.group(1)))
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()

    if failing:
        statement = " ".join(rendered[failing - 1].split())
        if len(statement) > 160:
            statement = statement[:157] + "..."
        context = f"statement {failing} of {total} failed: {statement}"
    else:
        # Dolt failed before the first statement's marker was printed (e.g. the
        # session's revision database could not be selected, or the output was
        # lost): the batch failed as a whole and the statement is not named.
        context = (
            f"a batch of {total} statement(s) failed; the failing statement "
            "could not be identified"
        )
    return LedgerError(f"{context}\n{cleaned}" if cleaned else context)


def _commit_hash(result_sets: Sequence[Sequence[dict[str, Any]]]) -> str | None:
    """The hash of the ``DOLT_COMMIT`` result in a commit script's output."""
    for rows in reversed(result_sets):
        for row in rows:
            if "hash" in row:
                return str(row["hash"])
    return None


def _type_marker_sql(kind: str, index: int) -> str:
    """A one-row marker delimiting a probe result set (``kind`` is Q or C)."""
    return f"SELECT '{_TYPE_MARKER_PREFIX}{kind}{index}' AS `marker`"


def _find_marker(
    result_sets: Sequence[Sequence[dict[str, Any]]],
    kind: str,
    index: int,
    start: int,
) -> int | None:
    """The first marker set for ``kind``/``index`` at or after ``start``."""
    wanted = [{"marker": f"{_TYPE_MARKER_PREFIX}{kind}{index}"}]
    for position in range(start, len(result_sets)):
        if result_sets[position] == wanted:
            return position
    return None


def _missing_revision_database(message: str, database: str, branch: str) -> bool:
    """Whether Dolt failed to select this session's ``db/branch`` database.

    The failure is checked by Dolt's own error text because the probe's extra
    statements cannot be told apart structurally: a missing revision database
    fails before any statement runs, and Dolt 2.6.1 reports
    ``database not found: db/branch``.  The check is narrow (this session's
    revision name and Dolt's phrase); any other probe error still falls back to
    the un-probed run.
    """
    return (
        "database not found" in message
        and f"{database}/{branch}" in message
    )


def _is_integer_type(sql_type: Any) -> bool:
    """Whether a ``SHOW COLUMNS`` ``Type`` is in the SQL integer family."""
    return isinstance(sql_type, str) and _INTEGER_TYPE.match(sql_type) is not None


def _normalise_integers(
    rows: list[dict[str, Any]],
    columns: Sequence[tuple[str, Any]],
) -> None:
    """Restore Python ``int`` cells of integer-typed columns, in place.

    Dolt's local JSON engine already returns them as integers, so the pass is
    idempotent there.  A routed server returns ``"1"`` for the same column;
    only a string of ASCII signed digits in a column whose probed SQL type is
    an integer family member is converted.  ``NULL`` (absent/None), booleans,
    floats and every non-integer column are untouched.
    """
    integer_names = [
        name for name, sql_type in columns if _is_integer_type(sql_type)
    ]
    if not integer_names:
        return
    for row in rows:
        for name in integer_names:
            value = row.get(name)
            if isinstance(value, str) and _INTEGER_TEXT.match(value):
                row[name] = int(value)


def _check_output(
    stdout: str, stderr: str, returncode: int, *, check_stdout: bool = True
) -> None:
    """Raise when the captured output shows an error, even with exit code 0."""
    failed = returncode != 0 or looks_like_error(stderr)
    if check_stdout:
        failed = failed or looks_like_error(stdout)
    if failed:
        message = "\n".join(part.strip() for part in (stderr, stdout) if part.strip())
        raise LedgerError(message or f"dolt sql failed with exit code {returncode}")


#: A read whose shape a derived table can materialise: ``SELECT``/``WITH``.
_PROBEABLE = re.compile(r"\A\s*(?:\(\s*)*(?:SELECT|WITH)\b", re.IGNORECASE)


def _probeable(sql: str) -> bool:
    """Whether :meth:`EmbeddedDriver._query_probed` can read this statement."""
    return bool(_PROBEABLE.match(sql))


def _parse_result_sets(
    stdout: str, *, keep_empty: bool = False
) -> list[list[dict[str, Any]]]:
    """Turn one ``dolt sql -r json`` invocation into its result sets.

    By default a statement that returned no rows prints ``{}`` and is dropped,
    matching the historical one-result-set-per-call behaviour (``_run`` returns
    only result-bearing statements).  ``keep_empty=True`` keeps one ``[]`` per
    statement instead, which is what a multi-statement batch needs to stay
    aligned with its statements.
    """
    result_sets: list[list[dict[str, Any]]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise LedgerError(f"unexpected output from dolt sql: {line}") from error
        if not isinstance(payload, dict):
            raise LedgerError(f"unexpected output from dolt sql: {line}")
        if payload == {}:
            if keep_empty:
                result_sets.append([])
            continue
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise LedgerError(f"unexpected output from dolt sql: {line}")
        result_sets.append(rows)
    return result_sets
