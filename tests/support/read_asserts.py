"""Assertion helper for the one-process read invariant.

`EmbeddedDriver.query` / `query_many` must never launch a second `dolt sql` process only to learn the
column names - see PR #80. Tests count process invocations through this helper instead of pinning the
literal sequence, so a future read-path change only has to edit this file.
"""
from __future__ import annotations


def assert_single_read_process(invoke_calls: list[str]) -> None:
    """The read ran in exactly one `dolt sql` process, result format json."""
    assert invoke_calls == ["json"], (
        f"expected exactly one read process (json), got {invoke_calls!r}; "
        "the read path must not launch an extra process to probe column "
        "names"
    )
