"""The ``tm`` console entry point survives a foreign ``PYTHONPATH``.

The console script starts in an environment the package did not choose: a
caller can point ``PYTHONPATH`` at another interpreter's ``site-packages``,
where a compiled dependency such as ``pydantic_core`` was built for a different
Python.  These tests run the real entry point in a subprocess -- the same
``from tetramark._entry import main; main()`` the generated console script
performs -- and assert it drops what it safely can, reports that in exactly one
stderr line, and fails with one actionable line (never a traceback) when a
shadow cannot be dropped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

import tetramark
from tetramark._entry import sanitize_path

pytestmark = pytest.mark.fast

PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"

#: How the generated console script invokes the package.
ENTRY_COMMAND = "from tetramark._entry import main; main()"

#: Separate stdout, stderr and exit status, like a real ``tm`` process.
RunEntry = subprocess.CompletedProcess[str]


def run_entry(
    tmp_path: Path, *args: str, pythonpath: str | None
) -> RunEntry:
    """Run the entry point in *tmp_path* with ``PYTHONPATH`` set (or removed)."""
    env = dict(os.environ)
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, "-c", ENTRY_COMMAND, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_foreign_site_packages_dir_is_dropped_in_one_stderr_line(
    tmp_path: Path,
) -> None:
    """AC2(a): the other Python's broken compiled package never gets imported."""
    foreign = tmp_path / "lib" / "python3.99" / "site-packages"
    broken = foreign / "pydantic_core"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text(
        'raise ImportError("pydantic_core from another Python")\n',
        encoding="utf-8",
    )

    result = run_entry(tmp_path, "--version", pythonpath=str(foreign))

    assert result.returncode == 0
    assert result.stdout.strip() == f"tetramark {tetramark.__version__}"
    assert result.stderr.count("\n") == 1
    line = result.stderr.strip()
    assert line.startswith(
        "tm: ignoring PYTHONPATH entries from another Python: "
    )
    assert str(foreign) in line
    assert line.endswith("(unset PYTHONPATH to silence this)")


def test_a_shadowing_import_failure_exits_2_with_one_actionable_line(
    tmp_path: Path,
) -> None:
    """AC2(b): a non-droppable shadow fails once, naming it and the fix."""
    shadow = tmp_path / "shadow"
    typer = shadow / "typer"
    typer.mkdir(parents=True)
    (typer / "__init__.py").write_text(
        'raise ImportError("shadowed typer is not usable")\n',
        encoding="utf-8",
    )

    result = run_entry(tmp_path, "--version", pythonpath=str(shadow))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert result.stderr.count("\n") == 1
    line = result.stderr.strip()
    assert line.startswith("tm: cannot start: importing typer failed (")
    assert "shadowed typer is not usable" in line
    assert str(shadow) in line
    assert "unset PYTHONPATH or remove that entry" in line


def test_a_broken_install_without_pythonpath_says_to_reinstall(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC2: with no ``PYTHONPATH`` to blame, the fix is reinstalling ``tm``."""
    from tetramark import _entry

    monkeypatch.delenv("PYTHONPATH", raising=False)

    _entry._report_import_failure(ImportError("cannot import the CLI"))

    stderr = capsys.readouterr().err
    assert stderr.count("\n") == 1
    assert stderr.strip() == (
        "tm: cannot start: importing tetramark.cli.app failed "
        "(cannot import the CLI); the installation is broken -- reinstall tm"
    )


def test_the_sanitizer_never_drops_our_own_environment(tmp_path: Path) -> None:
    """AC2(c): own ``site-packages``, ``sys.prefix`` and the checkout survive."""
    foreign = str(tmp_path / "lib" / "python3.99" / "site-packages")
    own_site_packages = sysconfig.get_path("purelib")
    own_site_packages_dir = sysconfig.get_path("platlib")
    # A foreign-version path that still lives in our own environment: dropping
    # it would break the interpreter this process is actually running.
    prefixed_foreign = str(Path(sys.prefix) / "lib" / "python3.99" / "site-packages")
    base_prefixed_foreign = str(
        Path(sys.base_prefix) / "lib" / "python3.99" / "site-packages"
    )
    same_minor = str(
        tmp_path
        / "lib"
        / f"python3.{sys.version_info.minor}"
        / "site-packages"
    )
    plain_component = str(tmp_path / "lib" / "python3.99")
    shadow = str(tmp_path / "shadow")

    already_there = [
        entry for entry in sys.path if entry in (own_site_packages, own_site_packages_dir)
    ]
    entries = [
        "",
        str(PROJECT_SRC),
        own_site_packages,
        own_site_packages_dir,
        foreign,
        prefixed_foreign,
        base_prefixed_foreign,
        same_minor,
        plain_component,
        shadow,
    ]

    kept, dropped = sanitize_path(entries)

    assert dropped == [foreign]
    assert kept == [entry for entry in entries if entry != foreign]
    for protected in (
        "",
        str(PROJECT_SRC),
        own_site_packages,
        own_site_packages_dir,
        prefixed_foreign,
        base_prefixed_foreign,
        same_minor,
        plain_component,
        shadow,
    ):
        assert protected in kept
    # ``sys.path`` in this test environment really does carry that directory.
    assert already_there


def test_sanitize_path_reports_every_foreign_entry_in_order(tmp_path: Path) -> None:
    first = str(tmp_path / "one" / "lib" / "python3.12" / "site-packages")
    second = str(tmp_path / "two" / "lib" / "python3.13" / "dist-packages")
    kept, dropped = sanitize_path([first, "keep", second])
    assert dropped == [first, second]
    assert kept == ["keep"]
