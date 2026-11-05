"""The isolated console-script entry point for ``tm``.

``tm`` can start in an environment the package did not plan for: a caller's
``PYTHONPATH`` may put another interpreter's ``site-packages`` ahead of this
one, where a compiled dependency (``pydantic_core``) was built for a different
Python.  The interpreter imports that copy first and dies with an ``ImportError``
traceback before the CLI can print one useful line.

This module is imported by the console script before the rest of the package,
so it uses the standard library only.  :func:`main` drops foreign
``site-packages`` entries, reports what it dropped in exactly one stderr line,
and only then imports the CLI -- still guarding that import so an unfixable
shadow fails with one actionable line instead of a traceback.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from importlib import metadata
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path

#: A ``python3.<minor>`` (or free-threaded ``python3.<minor>t``) path component.
_INTERPRETER_DIR = re.compile(r"^python3\.(\d+)t?$")

#: Directory names that hold installed distributions.
_SITE_DIRS = frozenset({"site-packages", "dist-packages"})


def _is_foreign_site_package(entry: str) -> bool:
    """Whether *entry* is another Python minor version's install directory."""
    parts = Path(entry).parts
    if not any(part in _SITE_DIRS for part in parts):
        return False
    for part in parts:
        match = _INTERPRETER_DIR.fullmatch(part)
        if match is not None and int(match.group(1)) != sys.version_info.minor:
            return True
    return False


def _is_protected(entry: str) -> bool:
    """Whether *entry* is our own environment or the checkout's ``src``."""
    try:
        path = Path(entry).resolve()
        roots = (
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            Path(__file__).resolve().parent.parent,
        )
    except OSError:  # pragma: no cover - an unresolvable cwd is already fatal
        return False
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def sanitize_path(entries: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split *entries* into ``(kept, dropped)``.

    An entry is dropped only when all of these hold:

    * it has a ``python3.<X>[t]`` path component whose *X* is not the running
      minor version,
    * it has a ``site-packages`` or ``dist-packages`` component,
    * it is not under ``sys.prefix``, ``sys.base_prefix`` or the checkout's
      ``src`` directory that holds this package.

    Everything else -- ``""``, the project ``src``, the running interpreter's
    own ``site-packages``, a plain directory named on ``PYTHONPATH`` -- is kept
    untouched.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for entry in entries:
        if _is_foreign_site_package(entry) and not _is_protected(entry):
            dropped.append(entry)
        else:
            kept.append(entry)
    return kept, dropped


def _first_entry_holding(module: str) -> str | None:
    """The first ``sys.path`` entry that holds top-level *module*, if any."""
    for entry in sys.path:
        if not entry:
            continue
        directory = Path(entry)
        if (directory / module).is_dir() or (directory / f"{module}.py").is_file():
            return entry
        if any(
            (directory / f"{module}{suffix}").is_file()
            for suffix in EXTENSION_SUFFIXES
        ):
            return entry
    return None


def _failing_top_level_module(error: ImportError) -> str:
    """The top-level module whose import failed, without a traceback."""
    name = getattr(error, "name", None)
    if name:
        return str(name).split(".", 1)[0]
    found = ""
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module_name = frame.f_globals.get("__name__", "")
        if (
            module_name
            and not module_name.startswith("importlib")
            and "importlib" not in frame.f_code.co_filename
        ):
            found = module_name
        traceback = traceback.tb_next
    if found:
        return found.split(".", 1)[0]
    return "tetramark.cli.app"


def _report_import_failure(error: ImportError) -> None:
    """Print exactly one actionable line for a CLI that cannot be imported."""
    module = _failing_top_level_module(error)
    detail = " ".join(str(error).splitlines())
    shadow = _first_entry_holding(module) if "PYTHONPATH" in os.environ else None
    if shadow is not None:
        print(
            f"tm: cannot start: importing {module} failed ({detail}); "
            f"{shadow} on PYTHONPATH shadows this tool's environment -- "
            "unset PYTHONPATH or remove that entry",
            file=sys.stderr,
        )
    else:
        print(
            f"tm: cannot start: importing {module} failed ({detail}); "
            "the installation is broken -- reinstall tm",
            file=sys.stderr,
        )


def installed_version_drift() -> tuple[str, str | None] | None:
    """Return ``(installed_metadata_version, editable_source_dir)``.

    ``None`` means installed ``tetramark`` metadata cannot be found at all (a
    bare source checkout); there is nothing to compare in that case.  The
    editable source directory comes from ``direct_url.json`` and is ``None``
    for a regular wheel install or when that file is absent.  Never touches
    the network.
    """
    try:
        distribution = metadata.distribution("tetramark")
    except metadata.PackageNotFoundError:
        return None
    return distribution.version, _editable_source_dir(distribution)


def _editable_source_dir(distribution: metadata.Distribution) -> str | None:
    """The checkout behind an editable install, or ``None``."""
    try:
        raw = distribution.read_text("direct_url.json")
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        direct_url = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(direct_url, dict):
        return None
    dir_info = direct_url.get("dir_info")
    if not isinstance(dir_info, dict) or dir_info.get("editable") is not True:
        return None
    url = direct_url.get("url")
    if not isinstance(url, str):
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "file":
        return None
    return urllib.request.url2pathname(parsed.path)


def main() -> None:
    """Console-script entry point: sanitize ``sys.path``, then run the CLI."""
    kept, dropped = sanitize_path(sys.path)
    sys.path[:] = kept
    if dropped:
        print(
            "tm: ignoring PYTHONPATH entries from another Python: "
            + ", ".join(dropped)
            + " (unset PYTHONPATH to silence this)",
            file=sys.stderr,
        )
    try:
        from tetramark.cli.app import main as cli_main
    except ImportError as error:
        _report_import_failure(error)
        raise SystemExit(2) from None
    cli_main()
