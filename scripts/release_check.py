#!/usr/bin/env python3
"""Check that Tetramark's version sources agree, and report release state.

The version is carried in five places, and a release is only as correct as the
weakest of them::

    pyproject.toml                    [project] version   -- the canonical value
    src/tetramark/__init__.py         __version__         -- what `tm --version` prints
    uv.lock                           tetramark package   -- what `uv run` resolves
    skills/tetramark/SKILL.md         frontmatter version -- the shipped manual
    skills/tetramark-schema/SKILL.md  frontmatter version -- the schema manual

Bumping some of them is the classic release mistake: the wheel then calls itself
0.5.1 while `tm --version` says 0.5.0, or the shipped skill still describes the
previous release.  The release workflow runs this script before it tags
anything, and `tests/test_release_version.py` runs it in the fast lane, so a
drift is caught before the merge.

The skill versions are read with a regex rather than a YAML parser: this script
must stay stdlib-only, because the release workflow invokes it with a bare
``python3`` outside the project's virtualenv.

    python3 scripts/release_check.py                  # human report
    python3 scripts/release_check.py --json           # the same report, for CI
    python3 scripts/release_check.py --since HEAD~1   # also say whether the version moved

`--since` is how the release workflow decides that a push deserves a tag: it
compares the version at the previous tip of `main` with the version in the tree
being evaluated, and reports it as `version_changed`.

Exit status is 0 when the sources agree and 1 when they do not.  Warnings (a
missing notes file, a dirty tree, an unreachable remote) never fail the run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

NOTES_DIR = Path("docs") / "releases"
INIT_FILE = Path("src") / "tetramark" / "__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
SKILL_FILES = {
    "skill": Path("skills") / "tetramark" / "SKILL.md",
    "skill_schema": Path("skills") / "tetramark-schema" / "SKILL.md",
}
#: The skill frontmatter ``version:`` line; the same pattern lives in
#: ``src/tetramark/skills.py`` (which this stdlib-only script cannot import).
#: A quoted value is the convention: unquoted, YAML reads a version like
#: ``0.9`` as a float.
SKILL_VERSION_RE = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)
#: ``---`` delimited YAML frontmatter at the very start of a skill file.
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def run(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command, returning its result instead of raising."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - environment
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def find_root() -> Path:
    here = Path(__file__).resolve().parent
    result = run("git", "rev-parse", "--show-toplevel", cwd=here)
    if result.returncode != 0:
        raise SystemExit(f"release_check: not inside a git checkout: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def pyproject_version(root: Path) -> str | None:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"release_check: cannot read pyproject.toml: {exc}") from exc
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) else None


def init_version(root: Path) -> str | None:
    try:
        text = (root / INIT_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def lock_version(root: Path) -> str | None:
    try:
        data = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for package in data.get("package", []):
        if package.get("name") == "tetramark":
            version = package.get("version")
            return version if isinstance(version, str) else None
    return None


def skill_version(root: Path, relative: Path) -> str | None:
    """The frontmatter ``version:`` of a shipped skill, or None when absent.

    Only the frontmatter is searched: the schema skill's body contains a fenced
    ``version: 1`` preset example, which is not the skill's version.
    """
    try:
        text = (root / relative).read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = FRONTMATTER_RE.match(text)
    if frontmatter is None:
        return None
    match = SKILL_VERSION_RE.search(frontmatter.group(1))
    return match.group(1) if match else None


def tag_exists(root: Path, tag: str) -> bool:
    return run("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}", cwd=root).returncode == 0


def remote_tag_exists(root: Path, tag: str) -> bool | None:
    """True/False, or None when origin cannot be asked (no remote, no network)."""
    result = run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", cwd=root)
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())

def version_at_tag(root: Path, tag: str) -> str | None:
    """The version recorded in the tree the tag points at."""
    result = run("git", "show", f"{tag}:pyproject.toml", cwd=root)
    if result.returncode != 0:
        return None
    try:
        return tomllib.loads(result.stdout)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None


def version_at_rev(root: Path, rev: str) -> str | None:
    """The `[project] version` recorded in another commit's pyproject.toml."""
    result = run("git", "show", f"{rev}:pyproject.toml", cwd=root)
    if result.returncode != 0:
        return None
    try:
        version = tomllib.loads(result.stdout)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None
    return version if isinstance(version, str) else None


def collect(root: Path, since: str | None = None, offline: bool = False) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []

    canonical = pyproject_version(root)
    initial = init_version(root)
    locked = lock_version(root)
    skills = {key: skill_version(root, path) for key, path in SKILL_FILES.items()}
    if canonical is None:
        problems.append("pyproject.toml has no [project] version")
    for value, path in (
        (initial, INIT_FILE),
        (locked, Path("uv.lock")),
        *((skills[key], path) for key, path in SKILL_FILES.items()),
    ):
        if value is None:
            problems.append(f"{path.as_posix()} does not carry a version")
        elif canonical is not None and value != canonical:
            problems.append(f"{path.as_posix()} says {value}, pyproject.toml says {canonical}")

    version = canonical or initial or ""
    tag = f"v{version}" if version else ""
    present = tag_exists(root, tag) if tag else False
    target = version_at_tag(root, tag) if present else None
    if present and target is not None and version and target != version:
        problems.append(f"tag {tag} points at a tree whose pyproject.toml says {target}")

    notes_path = NOTES_DIR / f"{tag}.md"
    notes_present = (root / notes_path).is_file()
    if not notes_present:
        warnings.append(
            f"no notes file at {notes_path.as_posix()}; the release falls back to generated notes"
        )

    status = run("git", "status", "--porcelain", cwd=root)
    dirty = bool(status.stdout.strip())
    if dirty:
        warnings.append("working tree is dirty; the tag follows the commit, not the checkout")

    remote = None
    if tag and not offline:
        remote = remote_tag_exists(root, tag)
        if remote is None:
            warnings.append("could not ask origin about the tag; remote state unknown")

    head = run("git", "rev-parse", "--short", "HEAD", cwd=root).stdout.strip() or None
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip() or None

    version_before = version_at_rev(root, since) if since else None
    if since is None:
        version_changed: bool | None = True  # a manual run releases whatever it is given
    elif version_before is None:
        version_changed = None  # history unavailable; the caller decides, and should release
    else:
        version_changed = version_before != version

    return {
        "version": version,
        "version_before": version_before,
        "version_changed": version_changed,
        "tag": tag,
        "sources": {"pyproject": canonical, "init": initial, "lock": locked, **skills},
        "notes_path": notes_path.as_posix(),
        "notes_present": notes_present,
        "tag_exists": present,
        "tag_exists_remote": remote,
        "offline": offline,
        "tag_target_version": target,
        "head": head,
        "branch": branch,
        "dirty": dirty,
        "problems": problems,
        "warnings": warnings,
        "ready": not problems,
    }


def render(report: dict[str, Any]) -> str:
    sources = report["sources"]
    canonical = sources["pyproject"]
    lines = [f"version    {report['version'] or '(unknown)'}"]
    for label, key, path in (
        ("pyproject", "pyproject", "pyproject.toml"),
        ("__init__", "init", INIT_FILE.as_posix()),
        ("uv.lock", "lock", "uv.lock"),
        ("skill", "skill", SKILL_FILES["skill"].as_posix()),
        ("skill-schema", "skill_schema", SKILL_FILES["skill_schema"].as_posix()),
    ):
        value = sources[key]
        agree = value is not None and value == canonical
        lines.append(
            f"  {label:<12} {str(value):<8} {path}" + ("" if agree else "   <-- mismatch")
        )
    if report["version_before"] is not None:
        moved = "moved" if report["version_changed"] else "unchanged"
        lines.append(f"previous   {report['version_before']}  -> {moved}")
    if report["tag_exists"]:
        target = report["tag_target_version"]
        state = "matches" if target == report["version"] else f"tree says {target}"
        if report["offline"]:
            where = "remote not checked"
        else:
            where = {True: "pushed", False: "local only", None: "remote unknown"}[
                report["tag_exists_remote"]
            ]
        lines.append(f"tag        {report['tag']}  present ({state}, {where})")
    else:
        lines.append(f"tag        {report['tag']}  absent -- the workflow will create it")
    lines.append(
        f"notes      {report['notes_path']}  "
        f"{'present' if report['notes_present'] else 'missing (generated notes)'}"
    )
    lines.append(
        f"checkout   {report['branch']} @ {report['head']}  "
        f"{'dirty' if report['dirty'] else 'clean'}"
    )
    for problem in report["problems"]:
        lines.append(f"FAIL       {problem}")
    for warning in report["warnings"]:
        lines.append(f"warn       {warning}")
    lines.append("ready" if report["ready"] else "NOT READY")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print a JSON report instead of prose")
    parser.add_argument(
        "--since",
        metavar="REV",
        help="also report whether the version differs from REV (the release workflow passes the "
        "previous tip of main)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the network probe of origin (the fast lane and any air-gapped run use this)",
    )
    args = parser.parse_args(argv)

    report = collect(find_root(), since=args.since, offline=args.offline)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
