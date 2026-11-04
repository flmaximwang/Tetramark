"""The marker-to-backend contract for the mixed test session."""

from __future__ import annotations

import os

import pytest

from conftest import SESSION_DEFAULT_BACKEND

pytestmark = pytest.mark.fast


def test_a_plain_ledger_test_follows_the_session_default() -> None:
    assert os.environ["TETRAMARK_DRIVER"] == SESSION_DEFAULT_BACKEND


@pytest.mark.embedded
def test_the_embedded_marker_pins_the_embedded_backend() -> None:
    assert os.environ["TETRAMARK_DRIVER"] == "embedded"


@pytest.mark.embedded_driver
def test_the_embedded_driver_marker_pins_the_embedded_backend() -> None:
    assert os.environ["TETRAMARK_DRIVER"] == "embedded"
