"""Install the built wheel and prove an agent can rely on the installed ``tm``.

This is the end-to-end acceptance test for the release: it builds the source
distribution, then the wheel *from* that sdist (``uv build`` does both in one
command, so a directory left out of the sdist cannot silently disappear from
the wheel), installs the wheel into a throwaway virtual environment from the
local uv cache, and runs the installed ``tm`` from a fake HOME.

Why this is **not** in the fast lane: it shells out to ``uv build`` and
``uv pip install`` and creates a venv -- tens of seconds, not the seconds the
fast lane budgets.  ``@pytest.mark.timeout(180)`` keeps a wedged uv process
from stalling the full suite; the default 60 s is too tight for a cold build
plus an offline install, and every run of the full lane pays for this fixture
once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ("tetramark", "tetramark-schema")
VERSION_RE = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)

# The build plus the offline install does not fit the suite's 60 s per-test
# timeout; everything after the module fixture is a sub-second CLI run.
pytestmark = pytest.mark.timeout(180)


@dataclass(frozen=True)
class Installed:
    """The built artefacts and the throwaway install the tests exercise."""

    work: Path
    home: Path
    venv: Path
    tm: Path
    version: str
    wheel: Path
    sdist: Path


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def checked(
    argv: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = run(argv, cwd=cwd, env=env)
    assert result.returncode == 0, (
        f"{' '.join(argv)} failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def package_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def make_fake_home(work: Path) -> Path:
    """A HOME whose only ``.dolt/`` is Dolt's own global configuration directory.

    That shape is what the ledger-discovery bug mistook for a bare database
    (``~/.dolt`` with ``creds/``, ``eventsData/`` and a config file); ``tm``
    must walk past it and say "no ledger found", never offer ``tm adopt``.
    """
    home = work / "home"
    dolt = home / ".dolt"
    (dolt / "creds").mkdir(parents=True)
    (dolt / "eventsData").mkdir(parents=True)
    (dolt / "config_global.json").write_text("{}\n", encoding="utf-8")
    (dolt / "version_check.txt").write_text("", encoding="utf-8")
    return home


def installed_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment an installed ``tm`` runs in.

    ``PYTHONPATH`` and ``TETRAMARK_SKILLS_DIR`` are unset: the whole point is
    that the wheel's own package data is what the installer finds.  The test
    harness exports ``TETRAMARK_DRIVER=server`` for the suite; the throwaway
    venv has no ``pymysql`` (a dev-only dependency), so pin the product's own
    default backend instead.
    """
    env = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "TETRAMARK_SKILLS_DIR",
        "TETRAMARK_PRESETS_DIR",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["DOLT_ROOT_PATH"] = str(home)
    env["TETRAMARK_DRIVER"] = "embedded"
    if extra:
        env.update(extra)
    return env


def assert_wheel_contents(wheel: Path, version: str) -> None:
    """AC6: the wheel ships presets, both skills with their references, and tm."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        presets = [name for name in names if name.startswith("tetramark/presets/")]
        assert presets, "the wheel ships no tetramark/presets/"
        assert any(name.endswith(".yaml") for name in presets)
        for skill in SKILL_NAMES:
            assert f"tetramark/skills/{skill}/SKILL.md" in names
            references = f"tetramark/skills/{skill}/references/"
            assert any(name.startswith(references) for name in names), (
                f"no references/ under tetramark/skills/{skill}/"
            )

        dist_info = f"tetramark-{version}.dist-info"
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        assert "tm = tetramark._entry:main" in entry_points

        metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
        metadata_version = re.search(r"^Version:\s*(\S+)\s*$", metadata, re.MULTILINE)
        assert metadata_version and metadata_version.group(1) == version

        for skill in SKILL_NAMES:
            body = archive.read(f"tetramark/skills/{skill}/SKILL.md").decode("utf-8")
            stamped = VERSION_RE.search(body)
            assert stamped and stamped.group(1) == version, (
                f"tetramark/skills/{skill}/SKILL.md does not carry version {version}"
            )


def assert_sdist_contents(sdist: Path, version: str) -> None:
    """The wheel is built from this sdist, so it must carry the package data too."""
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    root = f"tetramark-{version}"
    for skill in SKILL_NAMES:
        assert f"{root}/skills/{skill}/SKILL.md" in names
    assert any(
        name.startswith(f"{root}/presets/") and name.endswith(".yaml") for name in names
    )


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """Build the sdist + wheel once, install the wheel once, then share both."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; the canonical test command is `uv run pytest`")

    work = tmp_path_factory.mktemp("installed-wheel")
    dist = work / "dist"
    checked([uv, "build", "--out-dir", str(dist)], cwd=ROOT)

    version = package_version()
    wheel = dist / f"tetramark-{version}-py3-none-any.whl"
    sdist = dist / f"tetramark-{version}.tar.gz"
    built = sorted(
        path.name for path in dist.iterdir() if path.is_file() and not path.name.startswith(".")
    )
    assert built == sorted([wheel.name, sdist.name]), (
        f"uv build produced {built}, expected exactly the versioned wheel and sdist"
    )

    assert_wheel_contents(wheel, version)
    assert_sdist_contents(sdist, version)

    venv = work / "venv"
    checked([uv, "venv", str(venv), "--python", sys.executable], cwd=ROOT)
    checked(
        [uv, "pip", "install", "--offline", "--python", str(venv), str(wheel)],
        cwd=ROOT,
    )

    return Installed(
        work=work,
        home=make_fake_home(work),
        venv=venv,
        tm=venv / "bin" / "tm",
        version=version,
        wheel=wheel,
        sdist=sdist,
    )


def test_the_installed_tm_reports_the_built_version(installed: Installed) -> None:
    result = run(
        [str(installed.tm), "--version"],
        cwd=installed.home,
        env=installed_env(installed.home),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"tetramark {installed.version}\n"
    # A wheel install has no metadata drift to warn about.
    assert result.stderr == ""


def test_the_installed_tm_says_no_ledger_found_from_the_fake_home(
    installed: Installed,
) -> None:
    """AC1 through the installed entry point: walk past ``~/.dolt``, never suggest adopt."""
    result = run(
        [str(installed.tm), "status"],
        cwd=installed.home,
        env=installed_env(installed.home),
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    output = result.stdout + result.stderr
    assert "no ledger found" in output
    assert "tm adopt" not in output


def test_the_installed_tm_finds_the_packaged_skill(installed: Installed) -> None:
    result = run(
        [str(installed.tm), "skill", "path", "--json"],
        cwd=installed.home,
        env=installed_env(installed.home),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "tetramark"
    assert payload["source"] == "package"
    skill_dir = Path(payload["path"])
    assert "site-packages" in skill_dir.parts
    assert (skill_dir / "SKILL.md").is_file()


def test_a_poisoned_pythonpath_yields_one_actionable_line(installed: Installed) -> None:
    """AC2: a shadowing entry that cannot be dropped fails cleanly, not with a traceback."""
    shadow = installed.work / "shadow"
    (shadow / "typer").mkdir(parents=True)
    (shadow / "typer" / "__init__.py").write_text(
        "raise ImportError('shadowed by test_installed_wheel')\n", encoding="utf-8"
    )
    result = run(
        [str(installed.tm), "--version"],
        cwd=installed.home,
        env=installed_env(installed.home, {"PYTHONPATH": str(shadow)}),
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, result.stderr
    assert str(shadow) in lines[0]
    assert "PYTHONPATH" in lines[0]
