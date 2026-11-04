"""``tm skill path`` and ``tm skill version``: which skill copy describes this CLI.

A shipped skill states the Tetramark version it was written against in its
frontmatter (spec §10).  These two commands read that stamp, so a stale copy is
detected instead of failing on its first removed command.  Both are
**ledger-free**: they never resolve or open a ledger, so they work in any
directory, including one that holds no ledger at all.

``tm skill path`` resolves a copy the same way the rest of the tool does --
through :func:`tetramark.skills.resolve_skill_source` -- and prints its bare
directory (human) or ``{"name", "path", "source"}`` (``--json``).  ``source`` is
``env`` (``$TETRAMARK_SKILLS_DIR``), ``checkout`` (``<repo>/skills``) or
``package`` (installed package data, once the wheel ships skills; Tetramark-bfq).

``tm skill version`` compares the copy's frontmatter ``version:`` with
``tm --version`` and prints a single line::

    skill 0.8.15 (/repo/skills/tetramark/SKILL.md); tm 0.8.15; match

``--json`` prints ``{"name", "skill_version", "skill_path", "tm_version",
"match"}``.  ``--file PATH`` checks another copy instead -- a ``SKILL.md`` or
the skill directory that holds it -- which is how a stale 0.4-era export is
inspected.  A mismatch is deliberately **not** an error: the command warns on
stderr and exits 0, because an old skill is a documentation problem, not a
reason to fail a command that reads only local files.  The warnings are:

* ``warning: skill <s> describes an older|a newer CLI than tm <t>; use the
  copy at $(tm skill path) instead``
* ``warning: <path> carries no version; it predates versioned skills
  (tetramark < 0.8.14)`` -- ``skill_version`` is ``null`` and ``match`` false;
* ``warning: skill version '<s>' is not comparable with tm <t>``.

Only a missing file or a file without YAML frontmatter exits 1.  The warnings
exist only in this command: a mismatch never leaks into other ``tm`` calls.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from tetramark import __version__, skills
from tetramark.cli.options import group, json_output
from tetramark.cli.output import emit_payload
from tetramark.errors import EXIT_ERROR

__all__ = ["DEFAULT_SKILL", "skill_app"]

#: The skill ``tm skill path`` / ``tm skill version`` resolve by default.
DEFAULT_SKILL = "tetramark"

skill_app = group("Locate the shipped skills and compare their version with tm.")


def _error(message: str) -> None:
    """Print a business error (exit 1) on stderr, like the root entry point."""
    typer.echo(f"Error: {message}", err=True)


def not_found_message(name: str) -> str:
    """The exact not-found error, with three clauses that match this machine.

    The bead fixes the shape::

        no tetramark skill named 'NAME' found: $TETRAMARK_SKILLS_DIR is unset,
        <checkout>/skills/NAME does not exist, and this installation ships no
        skills

    Each clause is adapted to the real state instead of hard-coded: an env root
    that is set and present says it does not contain the name, an env root that
    is set but missing says so, and the package clause mentions a path only
    when the installation really has a skills directory.
    """
    roots = {source: root for root, source in skills.skill_roots()}
    override = os.environ.get(skills.SKILLS_DIR_ENV, "").strip()

    if not override:
        env_clause = f"${skills.SKILLS_DIR_ENV} is unset"
    elif skills.SOURCE_ENV in roots:
        env_clause = (
            f"${skills.SKILLS_DIR_ENV} ({roots[skills.SOURCE_ENV]}) "
            f"does not contain {name!r}"
        )
    else:
        env_clause = f"${skills.SKILLS_DIR_ENV} ({override}) does not exist"

    checkout = roots.get(skills.SOURCE_CHECKOUT)
    checkout_clause = (
        "the checkout has no skills/ directory"
        if checkout is None
        else f"{checkout / name} does not exist"
    )

    package = roots.get(skills.SOURCE_PACKAGE)
    package_clause = (
        "this installation ships no skills"
        if package is None
        else f"{package / name} does not exist"
    )

    return (
        f"no tetramark skill named {name!r} found: {env_clause}, "
        f"{checkout_clause}, and {package_clause}"
    )


def resolve_or_exit(name: str) -> tuple[Path, str]:
    """The resolved skill directory and its source, or the not-found error."""
    try:
        return skills.resolve_skill_source(name)
    except skills.SkillNotFoundError:
        _error(not_found_message(name))
        raise typer.Exit(EXIT_ERROR) from None


@skill_app.command("path")
def skill_path(
    ctx: typer.Context,
    name: str = typer.Argument(
        DEFAULT_SKILL,
        help="Skill name: tetramark or tetramark-schema.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print name, path and source as JSON.",
    ),
) -> None:
    """Print the directory of the skill copy that describes this CLI."""
    directory, source = resolve_or_exit(name)

    if json_output(ctx, as_json):
        emit_payload(
            {
                "name": name,
                "path": str(directory),
                "source": source,
            },
            fmt="json",
        )
        return
    typer.echo(str(directory))


@skill_app.command("version")
def skill_version(
    ctx: typer.Context,
    name: str = typer.Argument(
        DEFAULT_SKILL,
        help="Skill name: tetramark or tetramark-schema.",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Check this SKILL.md (or its skill directory) instead of the resolved copy.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the versions and the match flag as JSON.",
    ),
) -> None:
    """Compare the skill's stated version with tm's version; warn on a mismatch."""
    if file is None:
        skill_file = resolve_or_exit(name)[0] / skills.SKILL_FILENAME
    else:
        target = Path(file).expanduser()
        skill_file = target / skills.SKILL_FILENAME if target.is_dir() else target
        skill_file = skill_file.resolve()  # absolute in both outputs
        if not skill_file.is_file():
            _error(f"no such skill file: {skill_file}")
            raise typer.Exit(EXIT_ERROR)

    if skills.read_skill_frontmatter(skill_file) is None:
        _error(f"{skill_file} has no YAML frontmatter")
        raise typer.Exit(EXIT_ERROR)

    skill_version_value = skills.read_skill_version(skill_file)
    classification = skills.compare(skill_version_value, __version__)
    matched = classification == "match"

    if classification in ("older", "newer"):
        _warn(
            f"skill {skill_version_value} describes "
            f"{'an older' if classification == 'older' else 'a newer'} CLI than "
            f"tm {__version__}; use the copy at $(tm skill path) instead"
        )
    elif classification == "missing":
        _warn(
            f"{skill_file} carries no version; it predates versioned skills "
            "(tetramark < 0.8.14)"
        )
    elif classification == "unknown":
        _warn(
            f"skill version {skill_version_value!r} is not comparable with "
            f"tm {__version__}"
        )

    if json_output(ctx, as_json):
        emit_payload(
            {
                "name": name,
                "skill_version": skill_version_value,
                "skill_path": str(skill_file),
                "tm_version": __version__,
                "match": matched,
            },
            fmt="json",
        )
        return

    shown = skill_version_value if skill_version_value is not None else "?"
    typer.echo(
        f"skill {shown} ({skill_file}); tm {__version__}; "
        f"{'match' if matched else 'mismatch'}"
    )


def _warn(message: str) -> None:
    """Print one of this command's warnings on stderr (never a failure)."""
    typer.echo(f"warning: {message}", err=True)
