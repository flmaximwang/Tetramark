"""Embedded driver: one ``dolt sql`` subprocess per statement batch.

The ledger directory *is* the database, so no server is needed.  Queries are
run with ``-r json`` and parsed from the newline-delimited JSON result sets.

Two Dolt quirks are handled here:

* Dolt exits 1 for most errors but not all of them, so success is decided from
  the output as well as the exit code: any non-JSON line, or an error-looking
  line on stdout/stderr, is a :class:`LedgerError`.
* JSON result rows omit columns whose value is ``NULL``.  A second run with
  ``-r csv`` supplies the column list (the CSV header is printed even for zero
  rows), so ``query`` always returns dictionaries with every column present.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tetramark.driver.base import (
    Driver,
    Params,
    check_branch_name,
    commit_statements,
    is_nothing_to_commit,
    looks_like_error,
    render_sql,
    revision_database,
)
from tetramark.errors import LedgerError, UnreachableError, UsageError

__all__ = ["EmbeddedDriver"]

#: Statements Dolt commits regardless of the surrounding transaction.
_DDL = re.compile(
    r"(?is)^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|#[^\n]*\n\s*)*"
    r"(?:CREATE|ALTER|DROP|RENAME|TRUNCATE|GRANT|REVOKE|COMMENT)\b"
)


class EmbeddedDriver(Driver):
    """Talk to a Dolt database by shelling out to ``dolt sql`` in its directory."""

    def __init__(
        self,
        path: str | Path,
        *,
        branch: str | None = None,
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
            self._branch = self._require_branch(branch)

    def _require_branch(self, branch: str) -> str:
        """Fail with a usage error when ``branch`` does not exist (never create)."""
        name = check_branch_name(branch)
        rows = self.query(
            "SELECT `name` FROM dolt_branches WHERE `name` = %s", (name,)
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
    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        rendered = render_sql(sql, params)
        if self._pending:
            # There is no session to query *inside* the pending transaction, so
            # replay the buffer in a transaction that is rolled back and read
            # the state from there.  The buffer itself stays pending, ready for
            # the real commit when the context manager exits.
            batch = ";\n".join(["START TRANSACTION", *self._pending, rendered, "ROLLBACK"])
            # Column names do not depend on the pending data, so ask for the
            # header with the bare query instead of replaying the buffer.
            return self._with_columns(self._query_json(batch), rendered)
        return self._with_columns(self._query_json(rendered), rendered)

    def query_many(
        self, statements: Sequence[tuple[str, Params]]
    ) -> list[list[dict[str, Any]]]:
        """Run a whole read batch in one ``dolt sql`` process (plus one CSV run).

        ``-r json`` emits one line per statement (``{}`` when a statement has no
        rows), so the batch stays aligned.  JSON omits a column whose value is
        ``NULL``, so one ``-r csv`` run then supplies every statement's column
        list (CSV prints one header even for zero rows) and the columns are
        filled in exactly as :meth:`_with_columns` does.  If the two runs cannot
        be aligned (an unexpected Dolt output shape), the statements fall back to
        individual :meth:`query` calls: the batching only changes speed, never
        the result.
        """
        ordered = list(statements)
        if not ordered:
            return []
        if self._pending:
            # Inside the deferred transaction there is no session to batch in;
            # the per-statement path replays the buffer and previews the state.
            return [self.query(sql, params) for sql, params in ordered]
        rendered = [render_sql(sql, params) for sql, params in ordered]
        batch = ";\n".join(rendered)
        result_sets = _parse_result_sets(self._invoke(batch, "json"), keep_empty=True)
        if len(result_sets) != len(rendered):
            return [self.query(sql, params) for sql, params in ordered]
        if not any(result_sets):
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
            if _DDL.match(rendered):
                # Dolt 2.3.5 does not roll DDL back, so it must run outside the
                # deferred DML transaction.  Flush the DML that logically came
                # first; otherwise a schema change would see the old rows.
                self._flush()
                self._run(rendered)
                return
            self._pending.append(rendered)
            return
        self._run(rendered)

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
        for rows in reversed(result_sets):
            for row in rows:
                if "hash" in row:
                    return str(row["hash"])
        return None

    # -- plumbing ---------------------------------------------------------
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
