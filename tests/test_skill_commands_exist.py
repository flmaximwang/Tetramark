"""Every ``tm`` command documented in the shipped skills exists in this CLI.

A skill is a normative description of the CLI, and a stale copy fails on its
first removed command.  This fast-lane sweep extracts every ``tm`` invocation
from the fenced code blocks and inline code of ``skills/**/*.md`` and resolves
its command path against the real command tree:

* the root command must exist and, for a command group, the subcommand must
  exist (``getattr(command, "commands", None)`` duck-types the groups because
  ``click`` is not importable with typer 0.27.3);
* ``a|b`` and ``a\\|b`` alternations are expanded -- every alternative has to
  resolve, so ``tm entity\\|stock\\|aliquot\\|box add-form`` checks all four
  groups; bracket syntax such as ``[--ledger|-l PATH]`` is skipped as a whole
  and counted, because it is a documented *form*, not a command;
* the value-taking global options ``-l/--ledger``, ``-b/--branch`` and
  ``-f/--format`` consume their value, so ``tm -l PATH status`` resolves
  ``status`` and never treats the path as a command; any other option before
  the command is a miss (``tm -w ...`` is the 0.4 workspace option).

Two deliberately narrow literal rules cover what a command-tree walk cannot:

* ``--ledger NAME`` / ``-l NAME`` is banned **anywhere**: that is 0.4's
  ledger-name form (a ledger is a directory now, and ``--ledger PATH`` stays);
* ``workspace.yaml`` is banned **only inside fenced code blocks**: prose that
  explains the removed file is intentional documentation.

Negative cases are verified against synthetic text, so a broken skill text is
never committed.  The whole module is Dolt-free and belongs to the fast lane.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import typer.main

from tetramark.cli.app import app

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"

#: A fenced code block: the opening fence line, its body, the closing fence.
FENCED_RE = re.compile(r"^```[^\n]*\n(.*?)^```[^\n]*$", re.MULTILINE | re.DOTALL)

#: An inline code span on one line.
INLINE_RE = re.compile(r"`([^`\n]+)`")

#: Global options that take a value; the next token is not a command.
GLOBAL_VALUE_OPTIONS = frozenset({"-l", "--ledger", "-b", "--branch", "-f", "--format"})

#: Global options that take no value.
GLOBAL_FLAG_OPTIONS = frozenset(
    {"--dry-run", "--no-commit", "--json", "--version", "-h", "--help"}
)

#: ``--ledger=PATH`` and friends: the value rides on the same token.
GLOBAL_VALUE_RE = re.compile(r"^--(?:ledger|branch|format)=")

#: Tokens that end one documented command and start the next inside a span.
SEPARATORS = frozenset({";", "&&", "then", "|"})

#: 0.4's ledger-name form: banned everywhere (``--ledger PATH`` is valid).
LEDGER_NAME_RE = re.compile(r"(?<![\w-])(?:--ledger|-l)\s+NAME\b")

#: The removed workspace file: banned inside fenced code blocks only.
WORKSPACE_FILE = "workspace.yaml"

#: The minimum number of invocations this tree really documents; a regex or
#: parser change that silently matches nothing must fail here.
MIN_INVOCATIONS = 300


@dataclass(frozen=True)
class SweepResult:
    """What one sweep found: how much it read, skipped and could not resolve."""

    invocations: int
    alternations: int
    skipped: tuple[str, ...]
    misses: tuple[str, ...]


@lru_cache(maxsize=1)
def command_tree() -> Any:
    """The real root command group, built the way ``tm`` builds it."""
    return typer.main.get_command(app)


def _children(command: Any) -> dict[str, Any] | None:
    """The subcommands of a command group; ``None`` for a standalone command."""
    children = getattr(command, "commands", None)
    return children if isinstance(children, dict) else None


def _root_names() -> dict[str, Any]:
    return _children(command_tree()) or {}


def _is_group(name: str) -> bool:
    return _children(_root_names()[name]) is not None


def _expand(token: str) -> list[str]:
    """``"entity|stock"`` -> ``["entity", "stock"]`` (empty parts dropped)."""
    return [part for part in token.split("|") if part]


def _placeholder(token: str) -> bool:
    """True for documented placeholders: ``...``, ``<object>``, ``COMMAND``."""
    if token in {"...", "…"} or "…" in token:
        return True
    if token.startswith("<") and token.endswith(">"):
        return True
    return token.isupper() and token.isalpha() and len(token) > 1


def _tokenize(text: str) -> list[str]:
    """Split one line/span into shell-ish tokens, documenting alternations.

    ``\\|`` is markdown's escaped pipe (the skill uses it inside table cells);
    ``;`` gets spaces so ``7726KU09; tm lineage`` keeps its command start; the
    ellipsis is split off so a trailing placeholder token is recognisable.
    """
    prepared = text.replace(";", " ; ").replace("\\|", "|").replace("…", " … ")
    try:
        return shlex.split(prepared)
    except ValueError:  # pragma: no cover - no unbalanced quotes in the skills
        return prepared.split()


def resolve_invocation(tokens: list[str]) -> tuple[str, str]:
    """Classify the tokens after one ``tm``: ``ok``, ``skipped`` or ``miss``.

    ``miss`` is a command path the CLI does not have (or an option that is not
    a global one).  ``skipped`` is a documented form that cannot be resolved
    from its token alone -- a placeholder or bracket syntax -- which is not a
    miss: the sweep still validates everything it can read.
    """
    roots: list[str] | None = None
    index = 0
    bracket = 0
    while index < len(tokens):
        token = tokens[index]
        if bracket:
            if token.endswith("]"):
                bracket = 0
            index += 1
            continue
        if token.startswith("["):
            bracket = 0 if token.endswith("]") else 1
            index += 1
            continue
        if token in SEPARATORS:
            index += 1
            continue
        if token.startswith("-"):
            if GLOBAL_VALUE_RE.match(token):
                index += 1
                continue
            if token in GLOBAL_VALUE_OPTIONS:
                index += 2
                continue
            if token in GLOBAL_FLAG_OPTIONS:
                index += 1
                continue
            return "miss", f"unknown global option {token!r}"
        if _placeholder(token):
            return "skipped", f"placeholder {token!r}"
        names = _expand(token)
        if roots is None:
            for name in names:
                if name not in _root_names():
                    return "miss", f"unknown command {name!r}"
            roots = names
            if not any(_is_group(name) for name in names):
                return "ok", " ".join(names)
            index += 1
            continue
        for name in names:
            if any(name not in (_children(_root_names()[root]) or {}) for root in roots):
                return "miss", (
                    f"unknown subcommand {name!r} of {', '.join(roots)}"
                )
        return "ok", " ".join(roots + names)
    if bracket:
        return "skipped", "unclosed bracket syntax"
    if roots is None:
        return "ok", "(global options only)"
    return "skipped", "a command group shown without its subcommand"


def iter_invocations(text: str) -> Iterator[list[str]]:
    """Every ``tm`` invocation in fenced code and inline code of one document.

    Only a ``tm`` at the start of a fenced line, at the start of an inline code
    span, or right after ``;``/``&&``/``then`` starts an invocation: a quoted
    warning such as ``... spec N, tm is spec N+1 ...`` is prose, not a command.
    """
    for fence in FENCED_RE.finditer(text):
        for line in fence.group(1).splitlines():
            line = re.sub(r"\s+#.*$", "", line).rstrip("\\")
            tokens = _tokenize(line)
            for index, token in enumerate(tokens):
                if token == "tm" and (
                    index == 0 or tokens[index - 1] in SEPARATORS
                ):
                    yield tokens[index + 1 :]
    prose = FENCED_RE.sub("", text)
    for span in INLINE_RE.finditer(prose):
        tokens = _tokenize(span.group(1))
        for index, token in enumerate(tokens):
            if token == "tm" and (index == 0 or tokens[index - 1] in SEPARATORS):
                yield tokens[index + 1 :]


def sweep_text(text: str, source: str = "<text>") -> SweepResult:
    """Run the command sweep over one document's text."""
    invocations = alternations = 0
    skipped: list[str] = []
    misses: list[str] = []
    for argv in iter_invocations(text):
        invocations += 1
        if any(any(marker in token for marker in "[]|") for token in argv):
            alternations += 1
        status, detail = resolve_invocation(argv)
        printed = "tm " + " ".join(argv)
        if status == "skipped":
            skipped.append(f"{source}: {printed} -> {detail}")
        elif status == "miss":
            misses.append(f"{source}: {printed} -> {detail}")
    return SweepResult(
        invocations=invocations,
        alternations=alternations,
        skipped=tuple(skipped),
        misses=tuple(misses),
    )


def skill_files() -> list[Path]:
    """Every markdown file the two shipped skills carry."""
    return sorted(SKILLS_ROOT.glob("**/*.md"))


def sweep_skills() -> SweepResult:
    """The command sweep over the whole shipped skill tree."""
    invocations = alternations = 0
    skipped: list[str] = []
    misses: list[str] = []
    for path in skill_files():
        result = sweep_text(path.read_text(encoding="utf-8"), source=str(path))
        invocations += result.invocations
        alternations += result.alternations
        skipped.extend(result.skipped)
        misses.extend(result.misses)
    return SweepResult(
        invocations=invocations,
        alternations=alternations,
        skipped=tuple(skipped),
        misses=tuple(misses),
    )


def ledger_name_violations(text: str) -> list[str]:
    """Lines using 0.4's ``--ledger NAME`` / ``-l NAME`` form (banned anywhere)."""
    return [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if LEDGER_NAME_RE.search(line)
    ]


def workspace_yaml_violations(text: str) -> list[str]:
    """Fenced code blocks naming ``workspace.yaml`` (banned only in code)."""
    return [
        block.group(1)
        for block in FENCED_RE.finditer(text)
        if WORKSPACE_FILE in block.group(1)
    ]


def _fenced(command: str) -> str:
    return f"```sh\n{command}\n```\n"


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------


def test_every_documented_tm_invocation_exists() -> None:
    result = sweep_skills()
    assert result.invocations >= MIN_INVOCATIONS, (
        f"only {result.invocations} documented invocations were found; the "
        "extractor stopped seeing the skills"
    )
    assert result.misses == (), "\n".join(result.misses)


def test_no_documented_ledger_name_form() -> None:
    violations = [
        f"{path.relative_to(ROOT)}: {line}"
        for path in skill_files()
        for line in ledger_name_violations(path.read_text(encoding="utf-8"))
    ]
    assert violations == [], "\n".join(violations)


def test_no_workspace_yaml_in_skill_code_blocks() -> None:
    violations = [
        f"{path.relative_to(ROOT)}: {block!r}"
        for path in skill_files()
        for block in workspace_yaml_violations(path.read_text(encoding="utf-8"))
    ]
    assert violations == [], "\n".join(violations)


# ---------------------------------------------------------------------------
# The sweep catches the acceptance negatives (synthetic text, never committed)
# ---------------------------------------------------------------------------


def test_sweep_catches_the_removed_server_group() -> None:
    result = sweep_text(_fenced("tm server status"))
    assert result.misses, "tm server status must be a miss"
    assert "server" in result.misses[0]


def test_sweep_catches_the_removed_workspace_option() -> None:
    result = sweep_text(_fenced("tm -w lab101 entity list"))
    assert result.misses, "tm -w ... must be a miss"
    assert "-w" in result.misses[0]


def test_sweep_catches_an_unknown_subcommand() -> None:
    result = sweep_text(_fenced("tm entity ls"))
    assert result.misses
    assert "ls" in result.misses[0]


def test_sweep_expands_alternations() -> None:
    result = sweep_text(_fenced("tm entity|stock|aliquot|box add-form"))
    assert result.misses == ()


def test_sweep_skips_bracket_syntax_without_a_miss() -> None:
    result = sweep_text(_fenced("tm [--ledger|-l PATH] status"))
    assert result.misses == ()
    assert result.alternations == 1


def test_sweep_consumes_value_taking_global_options() -> None:
    text = _fenced("tm -l /lab --branch main --format json entity list")
    assert sweep_text(text).misses == ()
    assert sweep_text(_fenced("tm --ledger=/lab status")).misses == ()


def test_sweep_checks_both_skill_roots_in_an_alternation() -> None:
    result = sweep_text(_fenced("tm entity|stock bogus"))
    assert result.misses
    assert "bogus" in result.misses[0]


# ---------------------------------------------------------------------------
# The two literal token rules, on synthetic text
# ---------------------------------------------------------------------------


def test_ledger_name_form_is_banned_anywhere() -> None:
    assert ledger_name_violations("tm --ledger NAME entity list")
    assert ledger_name_violations("run `tm -l NAME status`")
    assert ledger_name_violations("tm --ledger PATH entity list") == []
    assert ledger_name_violations("tm -l /lab/lab101 status") == []


def test_workspace_yaml_is_banned_only_in_fenced_blocks() -> None:
    prose = "A 0.4 `workspace.yaml` is not a ledger marker.\n"
    assert workspace_yaml_violations(prose) == []
    assert workspace_yaml_violations(_fenced("tm -l workspace.yaml status"))
