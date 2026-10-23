"""Server driver: PyMySQL over the local ``dolt sql-server``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from doltledger.driver.base import (
    Driver,
    Params,
    commit_statements,
    is_nothing_to_commit,
)
from doltledger.errors import LedgerError, UnreachableError

__all__ = ["ServerDriver"]

#: MySQL client error codes that mean "the server went away", not "bad SQL".
_UNREACHABLE_CODES = {2002, 2003, 2005, 2006, 2013}


def _error_text(error: pymysql.err.MySQLError) -> str:
    """PyMySQL stringifies errors as ``(code, 'message')``; keep the message."""
    if len(error.args) >= 2:
        return str(error.args[1])
    return str(error)


def _raise_mysql_error(error: pymysql.err.MySQLError) -> None:
    code = error.args[0] if error.args else None
    if code in _UNREACHABLE_CODES:
        raise UnreachableError(_error_text(error)) from error
    raise LedgerError(_error_text(error)) from error


class ServerDriver(Driver):
    """Talk to a database served by ``dolt sql-server``.

    The connection uses ``127.0.0.1`` without TLS and stays in manual-commit
    mode so :meth:`transaction` can roll back.
    """

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        *,
        user: str = "root",
        password: str = "",
        connect_timeout: int = 5,
    ) -> None:
        self._database = database
        self._in_transaction = False
        try:
            self._conn = pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                autocommit=False,
                charset="utf8mb4",
                connect_timeout=connect_timeout,
            )
        except OSError as error:  # refused / timed out before the handshake
            raise UnreachableError(
                f"cannot connect to the Dolt server at {host}:{port}: {error}"
            ) from error
        except pymysql.err.MySQLError as error:
            _raise_mysql_error(error)

        try:
            quoted = database.replace("`", "``")
            with self._conn.cursor() as cursor:
                cursor.execute(f"USE `{quoted}`")
        except pymysql.err.MySQLError as error:
            self.close()
            _raise_mysql_error(error)

    # -- identity ---------------------------------------------------------
    @property
    def database(self) -> str:
        return self._database

    # -- reads / writes ---------------------------------------------------
    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        try:
            with self._conn.cursor(DictCursor) as cursor:
                cursor.execute(sql, tuple(params) if params is not None else None)
                return list(cursor.fetchall())
        except pymysql.err.MySQLError as error:
            _raise_mysql_error(error)
            raise AssertionError("unreachable")  # for type checkers

    def execute(self, sql: str, params: Params = None) -> None:
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, tuple(params) if params is not None else None)
        except pymysql.err.MySQLError as error:
            _raise_mysql_error(error)

    @contextmanager
    def transaction(self) -> Iterator[ServerDriver]:
        if self._in_transaction:
            raise LedgerError("nested transactions are not supported")
        self._in_transaction = True
        try:
            self.execute("START TRANSACTION")
            try:
                yield self
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        finally:
            self._in_transaction = False

    # -- history ----------------------------------------------------------
    def commit(self, message: str, author: str | None = None) -> str | None:
        if self._in_transaction:
            raise LedgerError("cannot create a Dolt commit inside an open transaction")
        statements = commit_statements(message, author)
        try:
            # DOLT_ADD stages the *working set*; flush any implicit transaction
            # first so writes issued outside transaction() are included.
            self._conn.commit()
            with self._conn.cursor(DictCursor) as cursor:
                cursor.execute(statements[0])
                cursor.fetchall()  # drain CALL results before the next statement
                cursor.execute(statements[1])
                row = cursor.fetchone()
        except pymysql.err.MySQLError as error:
            self._conn.rollback()
            message_text = _error_text(error)
            if is_nothing_to_commit(message_text):
                return None
            _raise_mysql_error(error)
            return None
        else:
            # Close the implicit transaction so following reads see the commit.
            self._conn.commit()
            return str(row["hash"]) if row and row.get("hash") else None

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ServerDriver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
