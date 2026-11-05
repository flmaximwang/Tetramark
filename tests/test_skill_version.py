"""The shipped skills carry the package version, and the release check enforces it.

A skill is a normative description of the CLI and drifts like code, so both
``skills/tetramark/SKILL.md`` and ``skills/tetramark-schema/SKILL.md`` carry a
quoted ``version:`` in their frontmatter equal to ``[project] version`` in
``pyproject.toml``.  This is the fast-lane guard:

* the frontmatter parses, the version is quoted and equals the package version;
* ``updated:`` parses as a date -- and is deliberately never compared with
  today, so a release does not start failing the day after it ships;
* the YAML reader, ``src/tetramark/skills.py`` and the release check's regex
  all read the same value (and the release check looks at the frontmatter
  only: the schema skill's body contains a fenced ``version: 1`` example);
* ``scripts/release_check.py`` fails with the exact problem strings when a
  skill version is missing or differs from ``pyproject.toml``;
* :func:`tetramark.skills.compare` classifies a copy against ``tm --version``;
* the shared body-line helper enforces the ceilings for both skills.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from tetramark import skills

from conftest import SKILL_LINE_BUDGETS, skill_body_lines, skill_line_count

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "release_check.py"

#: The two shipped skills and the spec chapter each mirrors.
SKILL_SPECS = {"tetramark": "06-cli", "tetramark-schema": "02-schema-language"}

PYPROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


def skill_file(name: str) -> Path:
    return ROOT / "skills" / name / "SKILL.md"


def frontmatter(path: Path) -> dict[str, Any]:
    """The YAML frontmatter of a skill file, as a mapping."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} must start with YAML frontmatter"
    parsed = yaml.safe_load(match.group(1))
    assert isinstance(parsed, dict), f"{path} frontmatter must be a mapping"
    return parsed


def load_release_check() -> ModuleType:
    """Load ``scripts/release_check.py``; it is a script, not an importable module."""
    spec = importlib.util.spec_from_file_location("tetramark_release_check", CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_root(tmp_path: Path, skill_version: str | None) -> Path:
    """A release-check root whose only defect is the skill version stamp."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "tetramark"\nversion = "0.8.25"\n', encoding="utf-8"
    )
    (root / "src" / "tetramark").mkdir(parents=True)
    (root / "src" / "tetramark" / "__init__.py").write_text(
        '__version__ = "0.8.25"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "tetramark"\nversion = "0.8.25"\n', encoding="utf-8"
    )
    for name in SKILL_SPECS:
        path = root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        fields = f"---\nname: {name}\n"
        if skill_version is not None:
            fields += f'version: "{skill_version}"\n'
        fields += "---\n\n# Body\n"
        path.write_text(fields, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SKILL_SPECS))
def test_frontmatter_parses_for_both_skills(name: str) -> None:
    front = frontmatter(skill_file(name))
    assert front["name"] == name
    assert front["description"].startswith("Use when")
    assert front["spec"] == SKILL_SPECS[name]


@pytest.mark.parametrize("name", sorted(SKILL_SPECS))
def test_version_is_quoted_and_equals_pyproject(name: str) -> None:
    text = skill_file(name).read_text(encoding="utf-8")
    assert re.search(r'^version: "[^"]+"\s*$', text, re.MULTILINE), (
        f"{name}: version must be quoted -- YAML reads an unquoted 0.9 as a float"
    )
    assert frontmatter(skill_file(name))["version"] == PYPROJECT_VERSION


@pytest.mark.parametrize("name", sorted(SKILL_SPECS))
def test_updated_parses_as_a_date(name: str) -> None:
    updated = frontmatter(skill_file(name))["updated"]
    assert isinstance(updated, dt.date) and not isinstance(updated, dt.datetime)
    # Deliberately not compared with today: the field records when the text was
    # stamped, and the release is re-stamped (not expired) on a later rebase.


@pytest.mark.parametrize("name", sorted(SKILL_SPECS))
def test_visible_version_line_is_the_first_body_line(name: str) -> None:
    body = skill_body_lines(skill_file(name))
    assert body[0] == ""  # the blank line that follows the frontmatter
    assert body[1] == (
        f"> Tetramark {PYPROJECT_VERSION} skill — if `tm --version` differs, "
        "use the copy at `tm skill path`."
    )
    assert body[2] == ""
    assert body[3].startswith("# ")


# ---------------------------------------------------------------------------
# The YAML reader and the release check's regex agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SKILL_SPECS))
def test_yaml_reader_and_release_check_regex_agree(name: str) -> None:
    module = load_release_check()
    relative = Path("skills") / name / "SKILL.md"
    regex_version = module.skill_version(ROOT, relative)
    assert regex_version == frontmatter(skill_file(name))["version"]
    assert regex_version == skills.read_skill_version(skill_file(name))
    assert regex_version == PYPROJECT_VERSION
    # The two implementations may not drift apart; the script is stdlib-only
    # and cannot import the module, so the pattern itself is the contract.
    assert skills.VERSION_RE.pattern == module.SKILL_VERSION_RE.pattern
    # The schema skill's body shows a fenced `version: 1` preset example; only
    # the frontmatter may be read.
    assert regex_version != "1"


# ---------------------------------------------------------------------------
# scripts/release_check.py: missing and differing skill versions fail
# ---------------------------------------------------------------------------


def test_collect_reports_a_missing_skill_version(tmp_path: Path) -> None:
    root = fake_root(tmp_path, skill_version=None)
    report = load_release_check().collect(root, offline=True)
    assert report["ready"] is False
    assert "skills/tetramark/SKILL.md does not carry a version" in report["problems"]
    assert "skills/tetramark-schema/SKILL.md does not carry a version" in report["problems"]


def test_collect_reports_a_differing_skill_version(tmp_path: Path) -> None:
    root = fake_root(tmp_path, skill_version="0.8.24")
    report = load_release_check().collect(root, offline=True)
    assert report["ready"] is False
    assert (
        "skills/tetramark/SKILL.md says 0.8.24, pyproject.toml says 0.8.25"
        in report["problems"]
    )
    assert (
        "skills/tetramark-schema/SKILL.md says 0.8.24, pyproject.toml says 0.8.25"
        in report["problems"]
    )


# ---------------------------------------------------------------------------
# tetramark.skills: resolve a copy, read its stamp, classify it
# ---------------------------------------------------------------------------


def test_resolve_skill_finds_the_checkout_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(skills.SKILLS_DIR_ENV, raising=False)
    resolved = skills.resolve_skill("tetramark")
    assert (resolved / "SKILL.md").is_file()
    assert resolved.name == "tetramark"
    assert skills.read_skill_version(resolved) == PYPROJECT_VERSION


def test_resolve_skill_prefers_the_environment_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    (root / "tetramark").mkdir(parents=True)
    (root / "tetramark" / "SKILL.md").write_text(
        '---\nname: tetramark\nversion: "0.8.13"\n---\n\n# Stale copy\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(skills.SKILLS_DIR_ENV, str(root))
    resolved = skills.resolve_skill("tetramark")
    assert resolved == (root / "tetramark").resolve()
    assert skills.read_skill_version(resolved) == "0.8.13"


def test_resolve_skill_errors_for_an_unknown_name() -> None:
    with pytest.raises(skills.SkillNotFoundError):
        skills.resolve_skill("tetramark-does-not-exist")


@pytest.mark.parametrize(
    ("skill_version", "tm_version", "expected"),
    [
        ("0.8.25", "0.8.25", "match"),
        ("0.9", "0.9.0", "match"),
        ("0.8.25", "0.8.26", "older"),
        ("0.8.26", "0.8.25", "newer"),
        ("0.9", "0.8.25", "newer"),
        ("0.4.1", "0.8.25", "older"),
        (None, "0.8.25", "missing"),
        ("", "0.8.25", "missing"),
        ("0.8.25", None, "unknown"),
        ("0.8.25", "", "unknown"),
        ("not-a-version", "0.8.25", "unknown"),
        ("0.8.25", "not-a-version", "unknown"),
    ],
)
def test_compare_classifies_a_skill_copy(
    skill_version: str | None, tm_version: str | None, expected: str
) -> None:
    assert skills.compare(skill_version, tm_version) == expected


# ---------------------------------------------------------------------------
# Line budgets, shared with tests/test_docs_commands.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "mode", "limit"),
    [(name, mode, limit) for name, (mode, limit) in sorted(SKILL_LINE_BUDGETS.items())],
)
def test_skill_stays_within_its_line_budget(name: str, mode: str, limit: int) -> None:
    counted = skill_line_count(name)
    assert counted <= limit, (
        f"{name} {mode} is {counted} lines; the budget is {limit}. "
        "The tetramark-schema budget may only go down: move detail into "
        "references/ instead of raising it."
    )
