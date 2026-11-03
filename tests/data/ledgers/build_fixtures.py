#!/usr/bin/env python3
"""Build the checked-in Dolt fixtures under ``tests/data/ledgers/``.

The migration tests exercise ``migration/presets/molbio/1-to-2`` on a **molbio
v1** ledger: the shape the 0.1.2 tool created from the frozen v1 molbio preset
(``construct``/``strain``/``bacterial``/``pellet`` kind tables plus the
``aliquot_bacterial`` extension table, ``spec_version=2`` and
``preset_versions={"base": 1, "molbio": 1}``).  No current preset can create
that shape any more (spec 3 removed ``aliquot_fields``), so the fixture is a
real database built by driving the last checkout that could: commit
``3ec57d5`` ("the wave-9 tree with ticket 22 merged", version 0.1.2).

This script is the only supported way to (re)produce the fixture.  It:

1. creates a **temporary git worktree** of ``3ec57d5`` outside the repository
   (under the system temp dir, never inside the repo) and syncs it with
   ``uv sync --frozen`` (its own ``uv.lock``);
2. runs that checkout's **own ledger-init code** (``init_ledger_on_server``,
   the code behind its ``tm init``) against a throwaway ``dolt sql-server`` to
   create a molbio-1 ledger from the frozen v1 preset
   (``tests/data/presets/molbio-v1/molbio-v1.yaml``).  The server path is the
   same schema/DDL/meta/tests/commit as the embedded ``tm init`` but takes ~4 s
   instead of ~25 s (embedded spawns one ``dolt sql`` per DDL statement).  The
   frozen preset directory is the legacy checkout's **input**, so it must stay
   v1-shaped: keys a later spec introduced (top-level ``status_sets``, kind
   ``statuses``) are rejected up front with a clear :class:`BuildError` instead
   of surfacing as pydantic noise from inside the temporary checkout;
3. strips the ``aliquot_fields`` key from the ledger's own
   ``presets/molbio.yaml`` copy after the build: the key is what made the old
   tool create ``aliquot_bacterial``, but the current (spec 3) loader refuses
   to load a preset that still declares it.  The **database shape stays a
   genuine v1 one**; only the preset *file* is made loadable by today's tool,
   which is what lets today's step open the fixture at all (the step README
   documents why a real v1 ledger cannot be opened by 0.2.0+);
4. verifies the built ledger has the expected v1 tables, meta and generated
   ``dolt_tests``, then copies the ledger directory to ``--out`` (default:
   ``tests/data/ledgers/molbio-v1/``) and removes the temporary worktree.

The build is deterministic *by construction* (same commit, same preset input,
fixed author): the resulting Dolt history has the same commits/messages/meta,
but commit hashes and file bytes are not reproducible because Dolt timestamps
them.  ``tests/test_migrate.py::test_molbio_v1_fixture_matches_its_builder``
guards the checked-in shape; it compares the schema fingerprint, the
table/view list, the ``dolt_tests`` count and the checked-in preset files — not
byte identity.

Usage
-----

Rebuild the checked-in fixture after changing the build logic::

    uv run python tests/data/ledgers/build_fixtures.py --force

Write a throwaway copy somewhere else (used by the drift-guard test)::

    uv run python tests/data/ledgers/build_fixtures.py --out /tmp/molbio-v1

Requires ``git``, ``uv`` and ``dolt`` on PATH, and network or a warm uv cache
for the legacy checkout's dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

#: ``tests/data/ledgers/build_fixtures.py`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The checkout called out by the step README: the wave-9 tree with ticket 22
#: merged (tetramark 0.1.2), the last one that still loads ``aliquot_fields``.
LEGACY_COMMIT = "3ec57d5bf6b8d708cff92a27c596a9095c6fd331"

#: The frozen molbio v1 preset (kept as raw test data for exactly this build).
#: The whole directory is the 0.1.2 checkout's input, not a current-schema
#: fixture: it must keep v1-shaped files, because that loader rejects the keys
#: later specs added (see :func:`_assert_legacy_preset_inputs`).
MOLBIO_V1_PRESET = (
    REPO_ROOT / "tests" / "data" / "presets" / "molbio-v1" / "molbio-v1.yaml"
)

#: Fixed identity: the checked-in fixture must not depend on the builder's
#: global git/dolt configuration.
AUTHOR = "Fixture Builder <fixture@tetramark.invalid>"
LEDGER_NAME = "molbio-v1"

#: What a genuine molbio-1 database must contain (asserted after the build).
EXPECTED_TABLES = {
    # the four renamed kind tables and the bacterial tube extension table
    "entity_construct",
    "entity_strain",
    "stock_bacterial",
    "stock_pellet",
    "aliquot_bacterial",
    # enough core tables to prove this is a real ledger, not a DDL dump
    "entity",
    "stock",
    "aliquot",
    "ledger_meta",
}
EXPECTED_META = {
    "spec_version": "2",
    "preset_versions": '{"base": 1, "molbio": 1}',
    "tetramark_version": "0.1.2",
}
EXPECTED_DOLT_TESTS = 20

DEFAULT_OUT = Path(__file__).resolve().parent / LEDGER_NAME

#: The spec-3 key the current loader refuses; it must not remain in the
#: fixture's preset copy even though the built database *shape* includes it.
REMOVED_PRESET_KEY = "aliquot_fields"

#: Keys added by later specs that the 0.1.2 loader rejects outright.  A frozen
#: v1 preset input carrying one cannot be loaded, so the build would fail and
#: the drift guard would skip instead of guarding.
FORBIDDEN_LEGACY_PRESET_KEYS = ("status_sets",)
FORBIDDEN_LEGACY_KIND_KEYS = ("statuses",)


class BuildError(RuntimeError):
    """The fixture could not be built; the message says what to install/fix."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, raising :class:`BuildError` with its output on failure."""
    timed = bool(os.environ.get("TETRAMARK_BUILDER_TIMING"))
    start = time.monotonic()
    process = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if timed:
        print(
            f"  [{time.monotonic() - start:5.1f}s] {' '.join(argv[:3])}",
            file=sys.stderr,
        )
    if process.returncode != 0:
        output = "\n".join(
            part for part in (process.stdout.strip(), process.stderr.strip()) if part
        )
        raise BuildError(
            f"command failed ({process.returncode}): {' '.join(argv)}\n"
            f"{output or '(no output)'}"
        )
    return process


def _which(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise BuildError(
            f"the {name!r} executable was not found on PATH; "
            "install it before rebuilding the fixture"
        )
    return binary


def _legacy_env(dolt_root: Path) -> dict[str, str]:
    """The environment a legacy checkout's own interpreter must run with.

    The test runner exports ``PYTHONPATH`` pointing at the current worktree's
    ``src``.  The fixture build runs the *pinned legacy checkout's* code (its
    ``init_ledger_on_server``), so an inherited ``PYTHONPATH`` would import the
    current 0.5.0 package instead and fail with an ImportError.  Strip every
    Python/legacy-tetramark injection so the checkout's own virtualenv is used.
    """
    env = {**os.environ}
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "TETRAMARK_WORKSPACE",
        "TETRAMARK_LEDGER",
        "TETRAMARK_PRESETS_DIR",
    ):
        env.pop(name, None)
    env["DOLT_ROOT_PATH"] = str(dolt_root)
    return env


def _git_env() -> dict[str, str]:
    """An environment that cannot pick up the developer's git identity."""
    env = {**os.environ}
    env.pop("TETRAMARK_WORKSPACE", None)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _dolt_query(dolt: str, ledger: Path, sql: str) -> list[dict[str, Any]]:
    """Run one read-only ``dolt sql`` query and return its rows."""
    process = _run(
        [dolt, "sql", "--result-format", "json", "-q", sql],
        cwd=ledger,
    )
    payload = json.loads(process.stdout)
    return list(payload.get("rows", []))


def _dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def _assert_legacy_preset_inputs() -> None:
    """Fail fast when a later spec polluted the frozen v1 preset inputs.

    The directory beside :data:`MOLBIO_V1_PRESET` is the legacy checkout's
    *input*; ``status_sets`` on a preset and ``statuses`` on a kind are spec-4
    concepts that checkout cannot parse.  The old code surfaced that as
    pydantic noise from inside the temporary checkout and the drift guard
    skipped; naming the file and key here makes the failure actionable (remove
    the key — the checked-in fixture does not need a rebuild for that).
    """
    offenders: list[str] = []
    for preset_file in sorted(MOLBIO_V1_PRESET.parent.glob("*.yaml")):
        document = yaml.safe_load(preset_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise BuildError(f"{preset_file}: not a YAML mapping")
        for key in FORBIDDEN_LEGACY_PRESET_KEYS:
            if key in document:
                offenders.append(f"{preset_file.name}: top-level {key!r}")
        for name, kind in (document.get("kinds") or {}).items():
            if not isinstance(kind, dict):
                continue
            for key in FORBIDDEN_LEGACY_KIND_KEYS:
                if key in kind:
                    offenders.append(f"{preset_file.name}: kinds.{name}.{key}")
    if offenders:
        raise BuildError(
            "the frozen v1 presets are the 0.1.2 checkout's input and must stay "
            "v1-shaped, but they carry keys its loader rejects: "
            + ", ".join(offenders)
            + "; remove those keys (no fixture rebuild is needed for that)"
        )


def _materialise_legacy_checkout(tmp_root: Path, git: str) -> Path:
    """Create the temporary ``3ec57d5`` worktree *outside* the repository."""
    resolved = tmp_root.resolve()
    if resolved.is_relative_to(REPO_ROOT.resolve()):
        raise BuildError(
            f"refusing to build inside the repository ({resolved}); "
            "the temporary worktree must live outside it"
        )
    legacy = tmp_root / "src"
    try:
        _run(
            [
                git,
                "-C",
                str(REPO_ROOT),
                "worktree",
                "add",
                "--detach",
                str(legacy),
                LEGACY_COMMIT,
            ],
            cwd=REPO_ROOT,
            env=_git_env(),
        )
    except BuildError:
        _run(
            [git, "-C", str(REPO_ROOT), "worktree", "prune"],
            cwd=REPO_ROOT,
            env=_git_env(),
        )
        raise
    return legacy


def _sync_legacy_checkout(legacy: Path, uv: str) -> None:
    """Install the legacy checkout's own locked dependencies."""
    _run([uv, "sync", "--frozen", "--no-dev"], cwd=legacy)
    python = legacy / ".venv" / "bin" / "python"
    if not python.is_file():
        raise BuildError(f"uv sync did not install {python}")
    init_file = legacy / "src" / "tetramark" / "__init__.py"
    version = re.search(
        r'^__version__\s*=\s*"([^"]+)"', init_file.read_text(encoding="utf-8"), re.M
    )
    if version is None or version.group(1) != "0.1.2":
        raise BuildError(
            f"expected tetramark 0.1.2 in the legacy checkout, got "
            f"{version.group(1) if version else 'unknown'}"
        )


#: The legacy checkout's own ledger-init code, run against a throwaway
#: ``dolt sql-server``.  This is the same code path as its ``tm init`` (same
#: ``_prepare_init``, DDL, ``ledger_meta``, generated tests and one
#: ``init(ledger)`` commit); the embedded path shells out to one ``dolt sql``
#: process per DDL statement and takes ~25 s, the server path ~4 s.  The
#: database shape is identical — the drift guard compares it.
_INIT_SNIPPET = '''\
import sys

from tetramark.core.ledger import init_ledger_on_server
from tetramark.driver.workspace import Workspace

workspace_dir, presets_dir, preset, author, port = sys.argv[1:6]
workspace = Workspace.create(
    workspace_dir, port=int(port), presets_dir=presets_dir
)
try:
    workspace.start(daemon=True)
    ledger = init_ledger_on_server(workspace, "molbio-v1", [preset], author=author)
    try:
        print(f"built {ledger.path}")
    finally:
        ledger.close()
finally:
    workspace.stop()
'''


def _init_ledger_with_legacy_tool(
    legacy: Path, tmp_root: Path, dolt: str
) -> Path:
    """Create the pristine molbio-v1 ledger with the legacy checkout's own code."""
    workdir = tmp_root / "work"
    # ``Workspace.start`` refuses an empty data dir; a bare bootstrap database
    # is enough, and it is deleted before the fixture is copied out.
    bootstrap = workdir / "bootstrap"
    bootstrap.mkdir(parents=True)
    _run(
        [
            dolt,
            "init",
            "--name=Fixture Builder",
            "--email=fixture@tetramark.invalid",
        ],
        cwd=bootstrap,
    )

    snippet = tmp_root / "init_on_server.py"
    snippet.write_text(_INIT_SNIPPET, encoding="utf-8")
    env = _legacy_env(tmp_root / "dolt-root")
    _run(
        [
            str(legacy / ".venv" / "bin" / "python"),
            str(snippet),
            str(workdir),
            str(legacy / "presets"),
            str(MOLBIO_V1_PRESET),
            AUTHOR,
            str(_free_port()),
        ],
        cwd=tmp_root,
        env=env,
    )
    shutil.rmtree(bootstrap, ignore_errors=True)
    ledger = workdir / LEDGER_NAME
    if not (ledger / ".dolt").is_dir():
        raise BuildError(
            f"the legacy tool did not create {ledger} "
            f"(expected a .dolt directory there)"
        )
    return ledger


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _strip_removed_preset_key(preset_copy: Path) -> None:
    """Make the ledger's v1 preset copy loadable by the current (spec 3) loader.

    The database shape was built *with* the key (hence ``aliquot_bacterial``);
    only the file is rewritten, exactly like the old in-test downgrade helper
    did.  Keep this function and ``tests/test_migrate.py``'s expectations in
    sync: the drift test compares the checked-in copy byte for byte.
    """
    document = yaml.safe_load(preset_copy.read_text(encoding="utf-8"))
    assert isinstance(document, dict), preset_copy
    for kind in document.get("kinds", {}).values():
        kind.pop(REMOVED_PRESET_KEY, None)
    text = yaml.safe_dump(document, sort_keys=False)
    if REMOVED_PRESET_KEY in yaml.safe_load(text):
        raise BuildError(f"{preset_copy}: failed to strip {REMOVED_PRESET_KEY!r}")
    preset_copy.write_text(text, encoding="utf-8")


def _verify_built_ledger(ledger: Path, dolt: str) -> dict[str, Any]:
    """Assert the built directory really is the molbio-1 shape the tests need."""
    rows = _dolt_query(
        dolt,
        ledger,
        "SELECT `table_name` AS `name`, `table_type` AS `kind` "
        "FROM `information_schema`.`tables` WHERE `table_schema` = DATABASE()",
    )
    tables = {
        str(row["name"])
        for row in rows
        if str(row["kind"]).upper() == "BASE TABLE"
    }
    views = {str(row["name"]) for row in rows if str(row["kind"]).upper() == "VIEW"}
    missing = sorted(EXPECTED_TABLES - tables)
    if missing:
        raise BuildError(
            f"the built ledger is missing v1 tables {missing}; "
            f"it has {sorted(tables)}"
        )

    meta = {
        str(row["key"]): str(row["value"])
        for row in _dolt_query(
            dolt,
            ledger,
            "SELECT `key`, `value` FROM `ledger_meta`",
        )
    }
    for key, expected in EXPECTED_META.items():
        if meta.get(key) != expected:
            raise BuildError(
                f"the built ledger has ledger_meta[{key!r}] = "
                f"{meta.get(key)!r}, expected {expected!r}"
            )

    (tests,) = _dolt_query(dolt, ledger, "SELECT COUNT(*) AS `n` FROM `dolt_tests`")
    count = int(tests["n"])
    if count != EXPECTED_DOLT_TESTS:
        raise BuildError(
            f"the built ledger has {count} generated dolt_tests, "
            f"expected {EXPECTED_DOLT_TESTS}"
        )

    preset_copy = ledger / "presets" / "molbio.yaml"
    if REMOVED_PRESET_KEY in preset_copy.read_text(encoding="utf-8"):
        raise BuildError(
            f"{preset_copy} still declares {REMOVED_PRESET_KEY!r}; the current "
            "loader would refuse to open the fixture"
        )

    return {
        "tables": tables,
        "views": views,
        "tests": count,
        "fingerprint": meta.get("fingerprint", ""),
        "meta": meta,
    }


def build(
    out: Path,
    *,
    force: bool = False,
    keep: bool = False,
) -> Path:
    """Build a molbio-v1 ledger and copy it to ``out``; return ``out``."""
    out = out.expanduser().resolve()
    if out.exists():
        if not force:
            raise BuildError(
                f"{out} already exists; pass --force to replace it "
                "(the checked-in fixture is under tests/data/ledgers/)"
            )
        shutil.rmtree(out)

    git = _which("git")
    uv = _which("uv")
    dolt = _which("dolt")
    if not MOLBIO_V1_PRESET.is_file():
        raise BuildError(f"the frozen v1 preset is missing: {MOLBIO_V1_PRESET}")
    _assert_legacy_preset_inputs()

    tmp_root = Path(tempfile.mkdtemp(prefix="tetramark-legacy-"))
    if keep:
        print(f"temporary work root (kept): {tmp_root}")
    try:
        legacy = _materialise_legacy_checkout(tmp_root, git)
        _sync_legacy_checkout(legacy, uv)
        print(f"legacy checkout : {LEGACY_COMMIT[:12]} in a temporary worktree")

        # The v1 preset's ``include: [base]`` resolves against the frozen
        # preset directory next to it (whose base.yaml is byte-identical to the
        # legacy checkout's presets/base.yaml).
        ledger = _init_ledger_with_legacy_tool(legacy, tmp_root, dolt)
        _strip_removed_preset_key(ledger / "presets" / "molbio.yaml")
        shape = _verify_built_ledger(ledger, dolt)

        # ``LOCK`` is a transient Dolt lock file; a closed database does not
        # need it and Dolt recreates it on the next open.
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ledger, out, ignore=shutil.ignore_patterns("LOCK"))

        size = _dir_size(out)
        print(f"wrote           : {out} ({_human(size)})")
        print(
            f"shape           : {len(shape['tables'])} tables, "
            f"{len(shape['views'])} views, {shape['tests']} dolt_tests"
        )
        print(
            "ledger_meta     : "
            f"spec_version={shape['meta']['spec_version']}, "
            f"preset_versions={shape['meta']['preset_versions']}, "
            f"tetramark_version={shape['meta']['tetramark_version']}"
        )
        print(f"fingerprint     : {shape['fingerprint']}")
        print(
            "preset copy     : presets/molbio.yaml (v1, "
            f"{REMOVED_PRESET_KEY!r} stripped so the spec-3 loader can read it)"
        )
        return out
    finally:
        if not keep:
            _cleanup(tmp_root, git)
        else:
            print(f"left the legacy worktree at {tmp_root / 'src'}; prune with: "
                  f"git -C {REPO_ROOT} worktree prune")


def _cleanup(tmp_root: Path, git: str) -> None:
    """Remove the temporary worktree and its checkout, best effort."""
    legacy = tmp_root / "src"
    subprocess.run(
        [git, "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(legacy)],
        cwd=str(REPO_ROOT),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    shutil.rmtree(tmp_root, ignore_errors=True)
    # A crashed earlier run can leave the metadata behind; prune it silently.
    subprocess.run(
        [git, "-C", str(REPO_ROOT), "worktree", "prune"],
        cwd=str(REPO_ROOT),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the checked-in molbio-v1 Dolt fixture from the 0.1.2 "
            f"checkout {LEGACY_COMMIT[:12]} (a temporary worktree outside the repo)."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"destination ledger directory (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace --out if it already exists",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary legacy worktree (debugging; the repo is unchanged)",
    )
    args = parser.parse_args(argv)
    try:
        build(args.out, force=args.force, keep=args.keep)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
