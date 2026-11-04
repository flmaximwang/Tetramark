"""Both drivers name the failing statement of an ``execute_many`` batch.

The ``Driver.execute_many`` contract promises that a failed batch reports
``statement N of M failed: <sql>`` without re-running the statements that
already ran.  The embedded driver proved it; the server driver must match.
"""

from __future__ import annotations

import pytest

pymysql = pytest.importorskip("pymysql")

from tetramark.driver import EmbeddedDriver
from tetramark.driver.server import ServerDriver
from tetramark.errors import LedgerError


@pytest.mark.parametrize("backend", ["embedded", "server"])
def test_execute_many_names_the_failing_statement(
    dolt_repo, dolt_env, backend: str
) -> None:
    if backend == "server":
        env = {
            **dolt_env,
            "TETRAMARK_DRIVER": "server",
            "TETRAMARK_SERVER_CACHE": "0",
        }
        driver = ServerDriver(dolt_repo, env=env)
    else:
        driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    try:
        driver.execute("CREATE TABLE batch_failure (id BIGINT PRIMARY KEY)")
        with pytest.raises(LedgerError) as error:
            driver.execute_many(
                [
                    "INSERT INTO batch_failure (id) VALUES (1)",
                    "INSERT INTO batch_failure (id) VALUES (1)",
                ]
            )
        message = str(error.value)
        assert "statement 2 of 2 failed:" in message, message
        assert "INSERT INTO batch_failure (id) VALUES (1)" in message, message
    finally:
        close = getattr(driver, "close", None)
        if callable(close):
            close()
