"""``tm --version`` warns once on stderr when installed metadata drifts.

An editable install follows its checkout: the installed metadata version stays
behind after a bump until the next ``uv sync``.  ``tm --version`` keeps its one
stdout line and adds one stderr warning naming both versions, the checkout and
the pinned reinstall command, so an agent can act on it.  No network is
involved: :func:`tetramark._entry.installed_version_drift` reads only
``importlib.metadata`` and the distribution's ``direct_url.json``.
"""

from __future__ import annotations

from importlib import metadata

import pytest
from typer.testing import CliRunner

import tetramark
import tetramark._entry as entry
import tetramark.cli.app as cli_app

pytestmark = pytest.mark.fast

runner = CliRunner()

#: What ``tetramark._entry`` reports for the editable checkout in the warning.
EDITABLE_DIR = "/checkout/tetramark"

#: The pinned command the warning tells the user to run.
PINNED_REINSTALL = (
    "uv tool install --reinstall "
    f'"git+https://github.com/flmaximwang/Tetramark@v{tetramark.__version__}"'
)


class _FakeDistribution:
    """The two ``importlib.metadata`` attributes drift detection reads."""

    def __init__(self, version: str, direct_url: str | None) -> None:
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url


def _patch_distribution(
    monkeypatch: pytest.MonkeyPatch,
    distribution: _FakeDistribution | metadata.PackageNotFoundError,
) -> None:
    def fake_distribution(name: str) -> _FakeDistribution:
        assert name == "tetramark"
        if isinstance(distribution, metadata.PackageNotFoundError):
            raise distribution
        return distribution

    monkeypatch.setattr(metadata, "distribution", fake_distribution)


def test_drift_prints_one_stdout_line_and_one_stderr_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: both versions, the editable dir and the pinned command, one line."""
    monkeypatch.setattr(
        cli_app, "installed_version_drift", lambda: ("9.9.9", EDITABLE_DIR)
    )

    result = runner.invoke(cli_app.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"tetramark {tetramark.__version__}\n"
    assert result.stderr.count("\n") == 1
    warning = result.stderr.strip()
    assert warning.startswith(
        "warning: installed metadata says tetramark 9.9.9 "
        f"but the code is {tetramark.__version__}"
    )
    assert f": editable install of {EDITABLE_DIR}, it follows that checkout" in warning
    assert PINNED_REINSTALL in warning


def test_matching_versions_print_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_app,
        "installed_version_drift",
        lambda: (tetramark.__version__, EDITABLE_DIR),
    )

    result = runner.invoke(cli_app.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"tetramark {tetramark.__version__}\n"
    assert result.stderr == ""


def test_missing_metadata_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare source checkout has no installed metadata to disagree with."""
    monkeypatch.setattr(cli_app, "installed_version_drift", lambda: None)

    result = runner.invoke(cli_app.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"tetramark {tetramark.__version__}\n"
    assert result.stderr == ""


def test_drift_on_a_regular_install_omits_the_editable_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The editable sentence appears only when ``direct_url.json`` says editable."""
    monkeypatch.setattr(
        cli_app, "installed_version_drift", lambda: ("9.9.9", None)
    )

    result = runner.invoke(cli_app.app, ["--version"])

    assert result.exit_code == 0
    warning = result.stderr.strip()
    assert "installed metadata says tetramark 9.9.9" in warning
    assert "editable install of" not in warning
    assert PINNED_REINSTALL in warning


def test_installed_version_drift_reads_an_editable_direct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_distribution(
        monkeypatch,
        _FakeDistribution(
            "0.1.0",
            '{"dir_info": {"editable": true}, "url": "file:///checkout/tetramark"}',
        ),
    )

    assert entry.installed_version_drift() == ("0.1.0", "/checkout/tetramark")


def test_installed_version_drift_has_no_dir_for_a_wheel_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_distribution(
        monkeypatch,
        _FakeDistribution(
            "0.1.0",
            '{"dir_info": {"editable": false}, "url": "file:///checkout/tetramark"}',
        ),
    )

    assert entry.installed_version_drift() == ("0.1.0", None)


def test_installed_version_drift_tolerates_a_missing_direct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_distribution(monkeypatch, _FakeDistribution("0.1.0", None))

    assert entry.installed_version_drift() == ("0.1.0", None)


def test_installed_version_drift_is_none_without_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PackageNotFoundError`` means there is nothing to compare, not an error."""
    _patch_distribution(monkeypatch, metadata.PackageNotFoundError("tetramark"))

    assert entry.installed_version_drift() is None
