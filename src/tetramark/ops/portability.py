"""Portability of a self-contained ledger (P12/P13).

A ledger is one directory: the Dolt database (``.dolt/``), ``ledger.yaml`` and
the local schema inputs.  **Sharing a ledger means copying that directory**
(``cp -a``, ``rsync``, ``dolt clone``/``dolt backup``) -- exactly like a git
repository.  Two machines writing one ledger over a network share is not
supported: the embedded driver reads and writes through separate ``dolt sql``
processes, so a shared directory has no single writer.

Two things make a copied ledger stop working:

* local schema inputs (``kinds_dir`` / ``rules_dir``) that point outside the
  ledger -- refused when the ledger is opened, see
  :func:`tetramark.schema.loader.resolve_local_dir`;
* a Dolt remote or backup stored as an absolute ``file://`` URL: it keeps
  pointing at the machine it was configured on, so it breaks after the copy.
  :func:`absolute_file_urls` finds those and ``tm check`` reports them as an
  informational rule (never a failure).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

__all__ = [
    "PORTABILITY_GROUP",
    "absolute_file_urls",
    "repo_state_path",
]

#: The informational group the portability rule is reported in.  The ``:info``
#: suffix keeps it out of the blocking failures, so ``tm check`` still exits 0.
PORTABILITY_GROUP = "portability:info"

#: Dolt's own repository state: the remotes and backups it knows about.
REPO_STATE = ".dolt/repo_state.json"


def repo_state_path(ledger_dir: str | Path) -> Path:
    """``<ledger>/.dolt/repo_state.json`` -- Dolt's remote/backup state."""
    return Path(ledger_dir).expanduser() / REPO_STATE


def absolute_file_urls(ledger_dir: str | Path) -> list[dict[str, str]]:
    """Absolute ``file://`` URLs in the ledger's repo state that leave the ledger.

    A remote or backup stored as an absolute ``file://`` URL keeps pointing at
    the machine it was configured on, so it breaks when the ledger directory is
    copied elsewhere (P12).  ``ssh://`` and ``https://`` URLs are not local
    paths and are fine; a relative path is fine; and a ``file://`` URL inside the
    ledger directory is fine too -- it moves with the ledger.

    Every finding is a mapping with ``kind`` (``remote`` or ``backup``),
    ``name``, ``url`` and ``path``.
    """
    ledger = Path(ledger_dir).expanduser()
    try:
        root = ledger.resolve()
    except OSError:  # pragma: no cover - an unreadable path has no findings
        return []
    state = _read_state(repo_state_path(ledger))
    if state is None:
        return []

    findings: list[dict[str, str]] = []
    for kind in ("remote", "backup"):
        entries = state.get(f"{kind}s")
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            url = _entry_url(entry)
            if url is None:
                continue
            path = _file_url_path(url)
            if path is None:
                continue
            if _inside(root, path):
                continue
            findings.append(
                {"kind": kind, "name": str(name), "url": url, "path": path}
            )
    return findings


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_state(path: Path) -> dict[str, Any] | None:
    """The parsed ``repo_state.json``, or ``None`` when it cannot be read."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def _entry_url(entry: Any) -> str | None:
    """The URL of one remote/backup entry, or ``None``."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        url = entry.get("url")
        if isinstance(url, str):
            return url
    return None


def _file_url_path(url: str) -> str | None:
    """The local path of a ``file://`` URL, or ``None`` for anything else."""
    if not url.startswith("file://"):
        return None
    rest = url[len("file://") :]
    if not rest.startswith("/"):
        # ``file://host/path`` names another host, and a relative form is not
        # an absolute path: neither is a portability problem.
        return None
    return unquote(rest)


def _inside(root: Path, path: str) -> bool:
    """True when ``path`` points inside the ledger directory."""
    try:
        resolved = Path(path).resolve()
    except OSError:  # pragma: no cover - an unresolvable path is not inside
        return False
    return resolved == root or root in resolved.parents
