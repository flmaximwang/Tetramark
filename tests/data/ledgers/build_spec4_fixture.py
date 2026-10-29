#!/usr/bin/env python3
"""Build the checked-in **spec-4** Dolt fixture under ``tests/data/ledgers/``.

The spec-5 migration test (``migration/spec/4-to-5``) runs on a **spec-4**
ledger: ``spec_version=4``, base preset version **3** (so ``grid96`` is still a
box kind), spec-4 per-box-**kind** views (``v_box_grid9x9`` …) and aliquot
positions that do not follow the status yet.  No current preset can create that
shape any more — spec 5 drops the box kind views and base 4 renames ``grid96``
to ``grid8x12`` — so the fixture is a real database built by driving the last
checkout that could: commit ``2aee18b`` (the 0.3.0 release, wave 10).

This script is the only supported way to (re)produce the fixture.  It:

1. creates a **temporary git worktree** of ``2aee18b`` outside the repository
   (under the system temp dir, never inside the repo) and syncs it with
   ``uv sync --frozen`` (its own ``uv.lock``);
2. runs that checkout's **own ledger-init code** (``init_ledger_on_server``,
   the code behind its ``tm init``) against a throwaway ``dolt sql-server`` to
   create a spec-4 ledger from its own ``presets/base.yaml`` (version 3);
3. creates the acceptance shape with that checkout's own operations
   (``add_box``/``add_entity``/``add_stock``/``set_aliquot_status``, the code
   behind its ``tm box/entity/stock/aliquot`` commands):

   * ``R`` (rack): holds child **box** ``G`` in slot 1 — the issue's named-view
     acceptance row (``1 | G``); a rack may hold boxes (only grid boxes hold
     tubes exclusively);
   * ``G`` (grid9x9, nested in ``R`` slot 1): its own tubes in A1 and B2;
   * ``H`` (grid96): tubes in A1 and B2, plus a third tube in C3 that is created
     placed and then set ``consumed`` — 0.3.0 kept the position, which is the
     spec-4 shape ``migration/spec/4-to-5`` has to clear;
   * ``S`` (shelf, slotless): two tubes;
   * ``F`` (freezer, slotless): empty;

4. verifies the built ledger has the expected spec-4 tables/views/meta and
   ``dolt_tests``, that its own ``tm check`` passes (it is a valid spec-4
   ledger, not a hand-edited dump), then copies the ledger directory to ``--out``
   (default: ``tests/data/ledgers/spec4-boxes/``) and removes the temporary
   worktree.

The build is deterministic *by construction* (same commit, same preset input,
fixed author, same operation order): the resulting Dolt history has the same
commits/messages/meta and the same logical rows, but commit hashes, random
UUIDs and file bytes are not reproducible because Dolt timestamps commits and
``box``/``entity``/``stock`` ids are UUIDs in this shape.
``tests/test_migrate.py::test_spec4_boxes_fixture_matches_its_builder`` guards
the checked-in shape; it compares the schema fingerprint, the table/view list,
the ``dolt_tests`` count, the logical box/aliquot/slot rows and the checked-in
preset files — not byte identity.

Usage
-----

Rebuild the checked-in fixture after changing the build logic::

    uv run python tests/data/ledgers/build_spec4_fixture.py --force

Write a throwaway copy somewhere else (used by the drift-guard test)::

    uv run python tests/data/ledgers/build_spec4_fixture.py --out /tmp/spec4-boxes

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

#: ``tests/data/ledgers/build_spec4_fixture.py`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The checkout called out by the step README: the 0.3.0 release (wave 10), the
#: last tool that creates spec-4 per-box-kind views and base-3 ``grid96``.
LEGACY_COMMIT = "2aee18b8fa2f5799b0f4c6e0797094bfd91f3494"
LEGACY_VERSION = "0.3.0"

#: Fixed identity: the checked-in fixture must not depend on the builder's
#: global git/dolt configuration.
AUTHOR = "Fixture Builder <fixture@tetramark.invalid>"
LEDGER_NAME = "spec4-boxes"

DEFAULT_OUT = Path(__file__).resolve().parent / LEDGER_NAME

#: What a genuine spec-4/base-3 database must contain (asserted after the build).
EXPECTED_TABLES = {
    # the core tables a real ledger has
    "ledger_meta",
    "entity",
    "stock",
    "box",
    "slot",
    "aliquot",
    "aliquot_event",
    "alias",
    # the kind tables of base 3 (box kinds are what spec 5 replaces)
    "entity_item",
    "stock_lot",
    "box_grid9x9",
    "box_grid96",
    "box_rack",
    "box_shelf",
    "box_freezer",
    "box_drawer",
    "box_incubator",
}
EXPECTED_VIEWS = {
    # core views
    "v_box_slots",
    "v_aliquot_location",
    "v_stock_status",
    # kind views of the selected entity/stock kinds
    "v_entity_item",
    "v_stock_lot",
    # the spec-4 per-box-KIND views (one per base box kind): spec 5 drops them
    "v_box_grid9x9",
    "v_box_grid96",
    "v_box_rack",
    "v_box_shelf",
    "v_box_freezer",
    "v_box_drawer",
    "v_box_incubator",
}
EXPECTED_META = {
    "spec_version": "4",
    "preset_versions": '{"base": 3}',
    "tetramark_version": LEGACY_VERSION,
}
EXPECTED_DOLT_TESTS = 14

#: The logical data shape (ids are random UUIDs, so they are never compared).
#: ``(name, kind, parent box name, parent slot)``: the rack holds child box G in
#: slot 1 (the issue's named-view acceptance), everything else is top-level.
EXPECTED_BOXES = [
    ("F", "freezer", None, None),
    ("G", "grid9x9", "R", "1"),
    ("H", "grid96", None, None),
    ("R", "rack", None, None),
    ("S", "shelf", None, None),
]
#: ``(box name, slot, status)`` of every aliquot, ordered by box then slot.
EXPECTED_ALIQUOTS = [
    ("G", "A1", "stored"),
    ("G", "B2", "stored"),
    ("H", "A1", "stored"),
    ("H", "B2", "stored"),
    # created placed, then set consumed: 0.3.0 kept the position (spec-4 shape)
    ("H", "C3", "consumed"),
    ("S", None, "stored"),
    ("S", None, "stored"),
]
#: Materialised ``slot`` rows per box (grids and the rack are slotted).
EXPECTED_SLOTS = {"F": 0, "G": 81, "H": 96, "R": 10, "S": 0}

#: The spec-4 status event of the consumed tube (created stored, then consumed);
#: it is the row whose position the migration clears.
CONSUMED_BOX = "H"
CONSUMED_SLOT = "C3"

#: The kind the spec-5 base rename changes; the fixture must still carry it.
LEGACY_BOX_KIND = "grid96"


class BuildError(RuntimeError):
    """The fixture could not be built; the message says what to install/fix."""


# ---------------------------------------------------------------------------
# Small helpers (kept in step with tests/data/ledgers/build_fixtures.py)
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def _materialise_legacy_checkout(tmp_root: Path, git: str) -> Path:
    """Create the temporary ``2aee18b`` worktree *outside* the repository."""
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
    if version is None or version.group(1) != LEGACY_VERSION:
        raise BuildError(
            f"expected tetramark {LEGACY_VERSION} in the legacy checkout, got "
            f"{version.group(1) if version else 'unknown'}"
        )


def _assert_legacy_preset_inputs(legacy: Path) -> None:
    """The checkout must still carry the spec-4/base-3 preset, not a later one.

    The base preset is the build **input**: ``version: 3``, ``grid96`` (not
    ``grid8x12``) and the spec-4 ``status_sets:`` key are what make the fixture
    a spec-4 shape.  If this file were replaced by a newer checkout the build
    would silently produce the wrong fixture, so name the mismatch here.
    """
    preset = legacy / "presets" / "base.yaml"
    if not preset.is_file():
        raise BuildError(f"the legacy checkout has no {preset}")
    document = yaml.safe_load(preset.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise BuildError(f"{preset}: not a YAML mapping")
    problems: list[str] = []
    if document.get("version") != 3:
        problems.append(f"version is {document.get('version')!r}, expected 3")
    kinds = document.get("kinds") or {}
    if not isinstance(kinds, dict) or LEGACY_BOX_KIND not in kinds:
        problems.append(f"the box kind {LEGACY_BOX_KIND!r} is missing")
    if "grid8x12" in kinds:
        problems.append("the box kind 'grid8x12' is present (spec-5 base 4 shape)")
    if "status_sets" not in document:
        problems.append("the spec-4 'status_sets:' key is missing")
    if problems:
        raise BuildError(
            f"{preset} is not the spec-4/base-3 input this fixture needs: "
            + "; ".join(problems)
            + f". The build must drive commit {LEGACY_COMMIT[:12]} (0.3.0)."
        )


#: The legacy checkout's own operations, run against a throwaway
#: ``dolt sql-server``.  This is the same code path as its ``tm`` commands
#: (``init_ledger_on_server`` + the object operations); the server path takes
#: seconds instead of one ``dolt sql`` process per DDL statement.  The database
#: shape is identical — the drift guard compares it.
_BUILD_SNIPPET = '''\
import sys

from tetramark.core import objects
from tetramark.core.ledger import init_ledger_on_server
from tetramark.driver.workspace import Workspace
from tetramark.schema.tests_gen import run_check

workspace_dir, presets_dir, preset, author, port, ledger_name = sys.argv[1:7]
workspace = Workspace.create(
    workspace_dir, port=int(port), presets_dir=presets_dir
)
ledger = None
try:
    workspace.start(daemon=True)
    ledger = init_ledger_on_server(
        workspace, ledger_name, [preset], author=author
    )
    for kind, name in (
        ("rack", "R"),
        ("grid96", "H"),
        ("shelf", "S"),
        ("freezer", "F"),
    ):
        objects.add_box(ledger, kind, name)
    # The issue's named-view acceptance: the rack holds child box G in slot 1,
    # while G keeps its own tubes (a rack may hold boxes; grids hold tubes).
    objects.add_box(ledger, "grid9x9", "G", parent="R", slot="1")

    reagent_a = objects.add_entity(ledger, "item", name="Reagent A")
    reagent_b = objects.add_entity(ledger, "item", name="Reagent B")
    values = {"supplier": "acme"}

    def lot(entity, **kwargs):
        return objects.add_stock(
            ledger,
            "lot",
            entity=entity["id"],
            values=values,
            quantity=1,
            unit="ml",
            **kwargs,
        )

    # G (grid9x9, in R slot 1): two tubes in A1 and B2.
    lot(reagent_a, aliquots=2, box="G", slots=["A1", "B2"])
    # H (grid96): two tubes in A1 and B2, and a third tube in C3 created placed
    # and then set consumed: 0.3.0 kept its position, the spec-4 shape the
    # migration must clear.
    lot(reagent_b, aliquots=2, box="H", slots=["A1", "B2"])
    consumed = lot(reagent_a, aliquots=1, box="H", slots=["C3"])
    objects.set_aliquot_status(ledger, consumed["aliquots"][0], "consumed")
    # S (shelf, slotless): two direct tubes.  F (freezer): empty.
    lot(reagent_a, aliquots=2, box="S")

    report = run_check(ledger.driver, ledger.schema)
    failed = [result for result in report.failures if not result.is_info]
    if failed:
        for result in failed:
            print(f"FAIL {result.name}: {result.message}")
        raise SystemExit(
            f"the built ledger fails its own spec-4 tm check "
            f"({len(failed)} blocking failure(s)); refusing to check it in"
        )
    print(f"check: {report.count} test(s) passed on the built ledger")
finally:
    if ledger is not None:
        ledger.close()
    workspace.stop()
'''


def _build_spec4_ledger(legacy: Path, tmp_root: Path, dolt: str) -> Path:
    """Create the pristine spec-4 ledger with the legacy checkout's own code."""
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

    snippet = tmp_root / "build_on_server.py"
    snippet.write_text(_BUILD_SNIPPET, encoding="utf-8")
    env = {**os.environ}
    env.pop("TETRAMARK_WORKSPACE", None)
    env["DOLT_ROOT_PATH"] = str(tmp_root / "dolt-root")
    _run(
        [
            str(legacy / ".venv" / "bin" / "python"),
            str(snippet),
            str(workdir),
            str(legacy / "presets"),
            str(legacy / "presets" / "base.yaml"),
            AUTHOR,
            str(_free_port()),
            LEDGER_NAME,
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


def _verify_built_ledger(ledger: Path, dolt: str) -> dict[str, Any]:
    """Assert the built directory really is the spec-4 shape the tests need."""
    objects = _dolt_query(
        dolt,
        ledger,
        "SELECT `table_name` AS `name`, `table_type` AS `kind` "
        "FROM `information_schema`.`tables` WHERE `table_schema` = DATABASE()",
    )
    tables = {
        str(row["name"]) for row in objects if str(row["kind"]).upper() == "BASE TABLE"
    }
    views = {str(row["name"]) for row in objects if str(row["kind"]).upper() == "VIEW"}
    missing_tables = sorted(EXPECTED_TABLES - tables)
    if missing_tables:
        raise BuildError(
            f"the built ledger is missing spec-4 tables {missing_tables}; "
            f"it has {sorted(tables)}"
        )
    missing_views = sorted(EXPECTED_VIEWS - views)
    if missing_views:
        raise BuildError(
            f"the built ledger is missing spec-4 views {missing_views}; "
            f"it has {sorted(views)}"
        )
    per_box = sorted(name for name in views if name.startswith("v_box_") and name not in EXPECTED_VIEWS)
    if per_box:
        raise BuildError(
            f"the built ledger already has per-box views {per_box}; "
            "the spec-4 shape must not"
        )

    meta = {
        str(row["key"]): str(row["value"])
        for row in _dolt_query(
            dolt, ledger, "SELECT `key`, `value` FROM `ledger_meta`"
        )
    }
    for key, expected in EXPECTED_META.items():
        if meta.get(key) != expected:
            raise BuildError(
                f"the built ledger has ledger_meta[{key!r}] = "
                f"{meta.get(key)!r}, expected {expected!r}"
            )

    (generated,) = _dolt_query(
        dolt, ledger, "SELECT COUNT(*) AS `n` FROM `dolt_tests`"
    )
    tests = int(generated["n"])
    if tests != EXPECTED_DOLT_TESTS:
        raise BuildError(
            f"the built ledger has {tests} generated dolt_tests, "
            f"expected {EXPECTED_DOLT_TESTS}"
        )

    boxes = _dolt_query(
        dolt,
        ledger,
        "SELECT c.`name` AS `name`, c.`kind` AS `kind`, "
        "p.`name` AS `parent`, c.`parent_slot` AS `parent_slot` "
        "FROM `box` c LEFT JOIN `box` p ON p.`id` = c.`parent_box_id` "
        "ORDER BY c.`name`",
    )
    # ``dolt sql --result-format json`` omits NULL columns from a row; the
    # parent is compared by *name* because box ids are random UUIDs.
    box_shape = [
        (
            str(row["name"]),
            str(row["kind"]),
            row.get("parent"),
            row.get("parent_slot"),
        )
        for row in boxes
    ]
    if box_shape != EXPECTED_BOXES:
        raise BuildError(
            f"the built ledger has boxes {box_shape}, expected {EXPECTED_BOXES}"
        )
    if ("G", "grid9x9", "R", "1") not in box_shape:
        raise BuildError(
            "the built ledger must nest box G in rack R slot 1 (the issue's "
            f"named-view acceptance), got {box_shape}"
        )

    aliquots = _dolt_query(
        dolt,
        ledger,
        "SELECT b.`name` AS `box`, a.`slot` AS `slot`, a.`status` AS `status` "
        "FROM `aliquot` a LEFT JOIN `box` b ON b.`id` = a.`box_id` "
        "ORDER BY b.`name`, a.`slot`, a.`seq`",
    )
    aliquot_shape = [
        (str(row["box"]), row.get("slot"), str(row["status"])) for row in aliquots
    ]
    if aliquot_shape != EXPECTED_ALIQUOTS:
        raise BuildError(
            f"the built ledger has aliquots {aliquot_shape}, "
            f"expected {EXPECTED_ALIQUOTS}"
        )
    positioned_consumed = [
        row for row in aliquot_shape if row[2] != "stored" and row[0] is not None
    ]
    if positioned_consumed != [("H", "C3", "consumed")]:
        raise BuildError(
            "the built ledger must keep exactly the consumed tube's position "
            f"(spec-4 shape), got {positioned_consumed}"
        )

    slot_counts = {
        str(row["box"]): int(row["n"])
        for row in _dolt_query(
            dolt,
            ledger,
            "SELECT b.`name` AS `box`, COUNT(s.`label`) AS `n` "
            "FROM `box` b LEFT JOIN `slot` s ON s.`box_id` = b.`id` "
            "GROUP BY b.`name`",
        )
    }
    if slot_counts != EXPECTED_SLOTS:
        raise BuildError(
            f"the built ledger has slot counts {slot_counts}, "
            f"expected {EXPECTED_SLOTS}"
        )

    config = yaml.safe_load((ledger / "ledger.yaml").read_text(encoding="utf-8"))
    preset_copy = ledger / "presets" / "base.yaml"
    if not preset_copy.is_file():
        raise BuildError(f"the built ledger has no {preset_copy}")
    preset = yaml.safe_load(preset_copy.read_text(encoding="utf-8"))
    if preset.get("version") != 3 or LEGACY_BOX_KIND not in (preset.get("kinds") or {}):
        raise BuildError(
            f"{preset_copy} is not the base-3/spec-4 preset copy "
            f"(version {preset.get('version')!r})"
        )

    return {
        "tables": sorted(tables),
        "views": sorted(views),
        "tests": tests,
        "fingerprint": meta.get("fingerprint", ""),
        "meta": meta,
        "config": config,
        "boxes": box_shape,
        "aliquots": aliquot_shape,
        "slots": slot_counts,
    }


def build(
    out: Path,
    *,
    force: bool = False,
    keep: bool = False,
) -> Path:
    """Build a spec-4 ledger and copy it to ``out``; return ``out``."""
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

    tmp_root = Path(tempfile.mkdtemp(prefix="tetramark-spec4-"))
    if keep:
        print(f"temporary work root (kept): {tmp_root}")
    try:
        legacy = _materialise_legacy_checkout(tmp_root, git)
        _sync_legacy_checkout(legacy, uv)
        _assert_legacy_preset_inputs(legacy)
        print(f"legacy checkout : {LEGACY_COMMIT[:12]} ({LEGACY_VERSION}) in a temporary worktree")

        ledger = _build_spec4_ledger(legacy, tmp_root, dolt)
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
            "boxes           : "
            + ", ".join(f"{name} ({kind})" for name, kind, *_ in shape["boxes"])
        )
        print(
            "aliquots        : "
            + ", ".join(
                f"{box or 'unplaced'}:{slot or '-'}:{status}"
                for box, slot, status in shape["aliquots"]
            )
        )
        print(
            "preset copy     : presets/base.yaml (version 3, grid96, spec-4 "
            "status_sets) so the current loader can open the fixture"
        )
        return out
    finally:
        if not keep:
            _cleanup(tmp_root, git)
        else:
            print(
                f"left the legacy worktree at {tmp_root / 'src'}; prune with: "
                f"git -C {REPO_ROOT} worktree prune"
            )


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
            "Build the checked-in spec-4 Dolt fixture from the 0.3.0 checkout "
            f"{LEGACY_COMMIT[:12]} (a temporary worktree outside the repo)."
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
