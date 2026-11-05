"""Locate the shipped Tetramark skills and read their version stamps.

The two skills under ``skills/`` (``tetramark`` and ``tetramark-schema``) are
documentation rather than Python modules, but each one carries the version of
the CLI it describes in its YAML frontmatter.  This module is the one place
that knows where a skill copy lives and how its ``version:`` field is read, so
the CLI (``tm skill path`` / ``tm skill version``, Tetramark-ytf.2) and the
tests agree instead of re-implementing the resolution.

Deliberately Dolt-free and CLI-free: importing this module must never open a
ledger, spawn ``dolt`` or read anything outside the skill directories.

Resolution order for a skill name:

1. ``$TETRAMARK_SKILLS_DIR`` -- an explicit root, for a copy kept outside a
   checkout (a test fixture or a hand-edited export);
2. the repository checkout -- ``<repo>/skills`` (this file's ``parents[2]``);
3. the installed package -- ``<site-packages>/tetramark/skills``, once the
   wheel ships the skills as package data (Tetramark-bfq).

The first root that contains ``<name>/SKILL.md`` wins.  The environment
variable adds a location; when it does not hold the named skill, the checkout
and package copies are still tried.  This mirrors how a bare preset name
resolves through ``$TETRAMARK_PRESETS_DIR``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "SKILL_FILENAME",
    "SKILLS_DIR_ENV",
    "VERSION_RE",
    "SkillNotFoundError",
    "compare",
    "read_skill_version",
    "resolve_skill",
    "skill_dirs",
]

#: The file that makes a directory a skill.
SKILL_FILENAME = "SKILL.md"

#: Environment variable holding an extra skills root, tried first.
SKILLS_DIR_ENV = "TETRAMARK_SKILLS_DIR"

#: The frontmatter ``version:`` line.  Identical to the pattern in
#: ``scripts/release_check.py`` (which must stay stdlib-only and cannot import
#: this module); ``tests/test_skill_version.py`` asserts the two agree.  The
#: value may be quoted, and a quoted value is what the release check wants:
#: unquoted, YAML reads a version like ``0.9`` as a float.
VERSION_RE = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)

#: ``---`` delimited YAML frontmatter at the very start of the file.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class SkillNotFoundError(LookupError):
    """No directory called ``<name>`` with a ``SKILL.md`` exists in any root."""


def skill_dirs() -> list[Path]:
    """The skills roots present on this machine, in resolution order.

    Existing directories only, deduplicated, resolved (symlinks followed).  An
    installed wheel that does not ship ``tetramark/skills`` simply contributes
    no entry.
    """
    candidates: list[Path] = []
    override = os.environ.get(SKILLS_DIR_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "skills")  # repository checkout
    candidates.append(here.parent / "skills")  # installed package data

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - unreadable path
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def resolve_skill(name: str) -> Path:
    """The directory of the first ``<name>/SKILL.md`` copy, or raise.

    Raises :class:`SkillNotFoundError` naming every root that was searched.
    """
    roots = skill_dirs()
    for root in roots:
        candidate = root / name
        if (candidate / SKILL_FILENAME).is_file():
            return candidate
    looked = ", ".join(str(root) for root in roots) or "(no skills directory found)"
    raise SkillNotFoundError(
        f"skill {name!r} not found; looked in {looked} "
        f"(set ${SKILLS_DIR_ENV} to point at another skills root)"
    )


def read_skill_version(path: Path) -> str | None:
    """The frontmatter ``version:`` of a skill, or ``None`` when it has none.

    ``path`` may be the ``SKILL.md`` itself or the skill directory that holds
    it.  Only the frontmatter is searched: a skill body may legitimately show a
    fenced ``version: 1`` preset example (``skills/tetramark-schema/SKILL.md``
    does), and that is not the skill's version.  An unreadable file reads as
    "no version" rather than raising, so a stale copy is reported, not crashed.
    """
    path = Path(path)
    if path.is_dir():
        path = path / SKILL_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = _FRONTMATTER_RE.match(text)
    if frontmatter is None:
        return None
    match = VERSION_RE.search(frontmatter.group(1))
    return match.group(1) if match else None


def compare(skill_version: str | None, tm_version: str | None) -> str:
    """Classify a skill copy against the running CLI's version.

    Returns exactly one of:

    ``match``
        both sides are dotted numeric versions and describe the same CLI;
    ``older``
        the skill describes an older CLI than ``tm_version``;
    ``newer``
        the skill describes a newer CLI than ``tm_version``;
    ``unknown``
        the CLI version is absent or either side is not a dotted numeric
        version, so no ordering can be claimed;
    ``missing``
        the skill carries no version at all.
    """
    if not skill_version:
        return "missing"
    if not tm_version:
        return "unknown"
    skill_key = _version_key(skill_version)
    tm_key = _version_key(tm_version)
    if skill_key is None or tm_key is None:
        return "unknown"
    if skill_key == tm_key:
        return "match"
    return "older" if skill_key < tm_key else "newer"


def _version_key(text: str) -> tuple[int, ...] | None:
    """``"0.8.25"`` -> ``(0, 8, 25)``; ``None`` for anything else.

    Trailing zero components are dropped so ``0.9`` and ``0.9.0`` compare
    equal.  Pre-release suffixes (``0.8.25rc1``) are not modelled: the CLI
    prints plain dotted versions, and an unrecognised string must not be
    silently ordered.
    """
    stripped = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+)*", stripped) is None:
        return None
    parts = [int(part) for part in stripped.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
