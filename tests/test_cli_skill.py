"""``tm skill path`` / ``tm skill version``: ledger-free skill stamp checks.

Tetramark-ytf.2.  ``src/tetramark/skills.py`` resolves a skill copy and reads
its frontmatter ``version:``; these tests pin the CLI contract:

* ``tm skill path`` prints the bare directory of the copy and, with ``--json``,
  the source (``env`` / ``checkout`` / ``package``); a name no root holds exits
  1 with the exact three-clause message;
* ``tm skill version`` prints ``skill <v> (<path>); tm <v>; match|mismatch``
  (or the fixed JSON shape), exits 0 and warns on stderr for a stale (``older``
  / ``newer``), versionless, or non-comparable copy;
* both accept the global ``--json`` and neither opens a ledger.

Every test is Dolt-free and belongs to the fast lane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetramark import __version__, skills
from tetramark.cli.app import app

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
TETRAMARK_SKILL = SKILLS_ROOT / "tetramark"

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_skills_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution starts at the checkout unless a test sets the override."""
    monkeypatch.delenv(skills.SKILLS_DIR_ENV, raising=False)


def write_skill(directory: Path, version: str | None, *, name: str = "tetramark") -> Path:
    """A minimal skill copy; ``version=None`` leaves the frontmatter unversioned."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}"]
    if version is not None:
        lines.append(f'version: "{version}"')
    lines += ["---", "", "# Test skill", ""]
    path = directory / skills.SKILL_FILENAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# tm skill path
# ---------------------------------------------------------------------------


def test_path_prints_the_checkout_directory() -> None:
    result = runner.invoke(app, ["skill", "path"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{TETRAMARK_SKILL.resolve()}\n"
    assert result.stderr == ""


def test_path_json_reports_the_checkout_source() -> None:
    result = runner.invoke(app, ["skill", "path", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "name": "tetramark",
        "path": str(TETRAMARK_SKILL.resolve()),
        "source": skills.SOURCE_CHECKOUT,
    }
    assert result.stderr == ""


def test_path_prefers_the_environment_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    write_skill(root / "tetramark", "0.4.1")
    monkeypatch.setenv(skills.SKILLS_DIR_ENV, str(root))

    result = runner.invoke(app, ["skill", "path", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "name": "tetramark",
        "path": str((root / "tetramark").resolve()),
        "source": skills.SOURCE_ENV,
    }


def test_path_resolves_the_schema_skill() -> None:
    result = runner.invoke(app, ["skill", "path", "tetramark-schema"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{(SKILLS_ROOT / 'tetramark-schema').resolve()}\n"


def test_path_not_found_is_exact() -> None:
    result = runner.invoke(app, ["skill", "path", "does-not-exist"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Error: no tetramark skill named 'does-not-exist' found: "
        "$TETRAMARK_SKILLS_DIR is unset, "
        f"{SKILLS_ROOT / 'does-not-exist'} does not exist, "
        "and this installation ships no skills"
    )


def test_path_not_found_adapts_to_a_set_environment_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv(skills.SKILLS_DIR_ENV, str(root))

    result = runner.invoke(app, ["skill", "path", "does-not-exist"])
    assert result.exit_code == 1, result.output
    assert result.stderr.strip() == (
        "Error: no tetramark skill named 'does-not-exist' found: "
        f"$TETRAMARK_SKILLS_DIR ({root.resolve()}) does not contain "
        "'does-not-exist', "
        f"{SKILLS_ROOT / 'does-not-exist'} does not exist, "
        "and this installation ships no skills"
    )


# ---------------------------------------------------------------------------
# tm skill version: the checkout copy
# ---------------------------------------------------------------------------


def test_version_human_matches_the_checkout() -> None:
    result = runner.invoke(app, ["skill", "version"])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"skill {__version__} ({TETRAMARK_SKILL / 'SKILL.md'}); "
        f"tm {__version__}; match\n"
    )
    assert result.stderr == ""


def test_version_json_matches_the_checkout() -> None:
    result = runner.invoke(app, ["skill", "version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "name": "tetramark",
        "skill_version": __version__,
        "skill_path": str(TETRAMARK_SKILL / "SKILL.md"),
        "tm_version": __version__,
        "match": True,
    }
    assert result.stderr == ""


def test_version_accepts_the_global_json_flag() -> None:
    result = runner.invoke(app, ["--json", "skill", "version"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["skill_version"] == __version__
    assert payload["tm_version"] == __version__
    assert payload["match"] is True
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# tm skill version --file: stale, versionless and non-comparable copies
# ---------------------------------------------------------------------------


def test_version_stale_copy_warns_older_and_exits_zero(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "stale", "0.4.1")
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"skill 0.4.1 ({path.resolve()}); tm {__version__}; mismatch\n"
    )
    assert result.stderr.strip() == (
        f"warning: skill 0.4.1 describes an older CLI than tm {__version__}; "
        "use the copy at $(tm skill path) instead"
    )


def test_version_stale_copy_json_has_no_match(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "stale", "0.4.1")
    result = runner.invoke(app, ["skill", "version", "--file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "tetramark"
    assert payload["skill_version"] == "0.4.1"
    assert payload["skill_path"] == str(path.resolve())
    assert payload["tm_version"] == __version__
    assert payload["match"] is False
    assert "older CLI" in result.stderr


def test_version_newer_copy_warns_newer(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "newer", "99.0.0")
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "describes a newer CLI than" in result.stderr


def test_version_accepts_the_skill_directory(tmp_path: Path) -> None:
    directory = tmp_path / "copy"
    path = write_skill(directory, "0.4.1")
    result = runner.invoke(app, ["skill", "version", "--file", str(directory)])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"skill 0.4.1 ({path.resolve()}); tm {__version__}; mismatch\n"
    )


def test_versionless_copy_warns_and_still_exits_zero(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "versionless", None)
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"skill ? ({path.resolve()}); tm {__version__}; mismatch\n"
    assert result.stderr.strip() == (
        f"warning: {path.resolve()} carries no version; it predates versioned "
        "skills (tetramark < 0.8.25)"
    )


def test_versionless_copy_json_reports_null_and_no_match(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "versionless", None)
    result = runner.invoke(app, ["skill", "version", "--file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["skill_version"] is None
    assert payload["match"] is False
    assert "predates versioned skills" in result.stderr


def test_non_comparable_version_warns(tmp_path: Path) -> None:
    path = write_skill(tmp_path / "odd", "devel")
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert result.stderr.strip() == (
        f"warning: skill version 'devel' is not comparable with tm {__version__}"
    )
    assert result.stdout.endswith("mismatch\n")


# ---------------------------------------------------------------------------
# tm skill version: exit 1 only for a missing file or no frontmatter
# ---------------------------------------------------------------------------


def test_version_missing_file_exits_one(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "SKILL.md"
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.strip() == f"Error: no such skill file: {path.resolve()}"


def test_version_without_frontmatter_exits_one(tmp_path: Path) -> None:
    directory = tmp_path / "bare"
    directory.mkdir()
    path = directory / "SKILL.md"
    path.write_text("# No frontmatter here\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "version", "--file", str(path)])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.strip() == (
        f"Error: {path.resolve()} has no YAML frontmatter"
    )


def test_version_unknown_name_uses_the_not_found_message() -> None:
    result = runner.invoke(app, ["skill", "version", "does-not-exist"])
    assert result.exit_code == 1, result.output
    assert result.stderr.startswith(
        "Error: no tetramark skill named 'does-not-exist' found:"
    )


# ---------------------------------------------------------------------------
# Ledger-free
# ---------------------------------------------------------------------------


def test_skill_commands_work_with_no_ledger_in_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .dolt/ and no ledger.yaml anywhere above
    for argv in (
        ["skill", "path"],
        ["skill", "version"],
        ["skill", "version", "--json"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, (argv, result.output)
