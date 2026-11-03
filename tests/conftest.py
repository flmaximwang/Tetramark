"""Shared pytest fixtures.

Tests run against the real ``dolt`` binary (never a mock). Every fixture that
shells out points ``DOLT_ROOT_PATH``, ``HOME``, ``XDG_CONFIG_HOME`` and the Git
config env vars at the pytest ``tmp_path`` so the developer's global Dolt/Git
configuration is never read or written.

Two lanes:

* ``uv run pytest -q -m fast`` — the Dolt-free unit tests only; seconds, no
  ``dolt`` binary needed. Run this while iterating.
* ``uv run pytest -q -n 4`` — the full suite, every integration test against a
  real database. Run this before merging or releasing.

The full suite is still real-Dolt end to end: ``ledger_factory`` and
``embedded_ledger_factory`` hand out layouts of a *session template* (one real
``init_ledger`` per standard shape, then copies) so a fresh ledger costs a
directory copy instead of a full ``tm init``. Anything that changes what
``init`` produces — a custom preset, several presets, another
``id_prefix``/``author`` — keeps the real init path; see
:data:`TEMPLATE_SHAPES`.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from tetramark.core.ledger import (
    LEDGER_FILE,
    Ledger,
    init_ledger,
    open as open_ledger,
)
from tetramark.driver.workspace import Workspace
from tetramark.errors import LedgerError
from tetramark.schema.loader import resolve_preset_spec

DOLT_BINARY = shutil.which("dolt")

#: The presets from ticket 03's test data (directory presets, path-style).
PRESETS = Path(__file__).parent / "data" / "presets"

#: The repository's own presets; workspaces point `presets_dir` at it so
#: ``tm init --preset NAME`` works (ticket 20: presets are not package data).
REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"

#: Author used by :func:`ledger_factory` so tests never read a global identity.
FACTORY_AUTHOR = "Ledger Factory <factory@tetramark.invalid>"


#: ``subprocess.run`` signature use in fixtures/tests: run_dolt("sql", "-q", "SELECT 1")
RunDolt = Callable[..., subprocess.CompletedProcess[str]]


def isolated_env(root: Path, home: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``root``/``home``."""
    root.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DOLT_ROOT_PATH"] = str(root)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env.pop("TETRAMARK_WORKSPACE", None)
    return env


@pytest.fixture(scope="session")
def dolt_binary() -> str:
    """Path to the real ``dolt`` binary; skips the test if it is not installed."""
    if DOLT_BINARY is None:
        pytest.skip("the 'dolt' binary is not installed or not on PATH")
    return DOLT_BINARY


@pytest.fixture
def dolt_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``tmp_path``."""
    return isolated_env(tmp_path / "dolt-root", tmp_path / "home")


@pytest.fixture
def dolt_run(dolt_binary: str, dolt_env: dict[str, str], tmp_path: Path) -> RunDolt:
    """Run a ``dolt`` command in ``tmp_path`` (or a given ``cwd``), capturing output."""

    def run(
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [dolt_binary, *args],
            cwd=cwd or tmp_path,
            env=env or dolt_env,
            capture_output=True,
            text=True,
            check=check,
        )

    return run


@pytest.fixture
def dolt_conf(dolt_run: RunDolt) -> None:
    """Give the isolated global Dolt config a user name and email."""
    dolt_run("config", "--global", "--add", "user.name", "Tetramark Tests")
    dolt_run("config", "--global", "--add", "user.email", "tests@tetramark.invalid")


@pytest.fixture
def dolt_repo(tmp_path: Path, dolt_run: RunDolt, dolt_conf: None) -> Iterator[Path]:
    """An initialised Dolt data repository with an isolated global config.

    ``dolt config --global`` writes into ``$DOLT_ROOT_PATH`` (a tmp directory),
    so the user's real global config is untouched.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    dolt_run("init", cwd=repo)
    assert (repo / ".dolt").is_dir(), "dolt init did not create .dolt"
    yield repo


@pytest.fixture
def make_ledger(dolt_run: RunDolt, dolt_conf: None) -> Callable[[Path], Path]:
    """Initialise a Dolt database in a directory and return it."""

    def make(directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        dolt_run("init", cwd=directory)
        assert (directory / ".dolt").is_dir(), "dolt init did not create .dolt"
        return directory

    return make


@pytest.fixture
def embedded_ledger_factory(
    dolt_env: dict[str, str],
    tmp_path: Path,
    template_ledgers: LedgerTemplates,
) -> Iterator[Callable[..., Ledger]]:
    """Create real ledgers in a ``tmp_path`` workspace (embedded driver).

    ``embedded_ledger_factory(presets=[...], id_prefix="77")`` returns the
    opened :class:`~tetramark.core.ledger.Ledger`.  The workspace is created
    once and reused; every call gets a fresh ledger name unless one is given.

    A standard shape with the default options (see :data:`TEMPLATE_SHAPES`,
    :func:`template_matches_options`) is copied from the session template; any
    other call runs the real ``init_ledger``.  ``real_init=True`` forces the
    latter for tests that exist to assert what init generates.
    """
    counter = itertools.count(1)
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        real_init: bool = False,
    ) -> Ledger:
        workspace_dir = tmp_path / "workspace"
        if (workspace_dir / "workspace.yaml").is_file():
            workspace = Workspace.load(workspace_dir, env=dolt_env)
        else:
            workspace = Workspace.create(
                workspace_dir, presets_dir=str(REPO_PRESETS), env=dolt_env
            )
        ledger_name = name or f"ledger{next(counter)}"
        sources = list(presets) if presets else [PRESETS / "base"]
        key = None
        if not real_init and template_matches_options(
            id_prefix=id_prefix, author=author
        ):
            key = template_key(sources, workspace)
        if key is None:
            ledger = init_ledger(
                workspace,
                ledger_name,
                sources,
                id_prefix=id_prefix,
                author=author,
            )
        else:
            ledger = template_ledgers.install(key, workspace, ledger_name)
        assert isinstance(ledger, Ledger)
        created.append(ledger)
        return ledger

    yield factory
    for ledger in created:
        ledger.close()


@pytest.fixture
def ledger_factory(
    embedded_ledger_factory: Callable[..., Ledger],
) -> Callable[..., Ledger]:
    """Alias kept for existing tests: the embedded factory (unchanged behaviour)."""
    return embedded_ledger_factory


# ---------------------------------------------------------------------------
# Session templates: one real init per standard shape, then copies
# ---------------------------------------------------------------------------

#: ``id_prefix`` and author every template is built with.  A factory call that
#: asks for anything else keeps the real init path: a template must be exactly
#: the ledger ``init_ledger`` would have produced for those options.
TEMPLATE_ID_PREFIX = "77"
TEMPLATE_LEDGER_NAME = "template"

#: The standard ledger shapes: built once per pytest session (per xdist worker)
#: and copied afterwards.  Values are *resolved* preset files, so the key
#: follows what ``tm init --preset`` would load, not how a test spelled it.
#: Anything else (a custom preset directory, several presets, a shape not
#: listed here) keeps the real ``init_ledger`` path — see :func:`template_key`.
TEMPLATE_SHAPES: dict[str, tuple[str, ...]] = {
    "presets-base": (str((PRESETS / "base").resolve()),),
    "presets-full": (str((PRESETS / "full").resolve()),),
    "preset-base": (str((REPO_PRESETS / "base.yaml").resolve()),),
}


def template_key(
    presets: Sequence[str | Path], workspace: Workspace
) -> str | None:
    """The template covering this preset list, or ``None`` when init must run.

    Resolution goes through :func:`~tetramark.schema.loader.resolve_preset_spec`
    with the workspace's ``presets_dir``, exactly like init does.  A bad preset
    returns ``None`` (never raises) so the real ``init_ledger`` stays the one
    that reports the error.
    """
    resolved: list[str] = []
    for preset in presets:
        try:
            found = resolve_preset_spec(
                str(preset),
                presets_dir=workspace.presets_dir_path,
                base_dir=Path.cwd(),
            )
        except Exception:  # noqa: BLE001 - init must raise the real error
            return None
        resolved.append(str(found.path.resolve()))
    for key, shape in TEMPLATE_SHAPES.items():
        if tuple(resolved) == shape:
            return key
    return None


def template_matches_options(
    *, id_prefix: str | None, author: str | None
) -> bool:
    """Whether the requested options are the ones the templates were built with."""
    return id_prefix == TEMPLATE_ID_PREFIX and author == FACTORY_AUTHOR


def _rewrite_ledger_name(ledger_dir: Path, name: str) -> None:
    """Point a copied ``ledger.yaml`` at the caller's ledger name.

    A real init writes ``name: <ledger>``; a copy carries the template's, so
    exactly that field is replaced (formatting and every other setting stay
    what init produced).
    """
    path = ledger_dir / LEDGER_FILE
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["name"] = name
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


class LedgerTemplates:
    """One real ``init_ledger`` per standard shape, then copies of it.

    A template is built lazily in its own isolated workspace and is never
    written to again: :meth:`install` copies the closed database directory into
    the test's workspace (the ledgers are otherwise identical to a fresh init —
    same schema, ``ledger_meta``, generated ``dolt_tests``, same two commits).

    The templates are copies of real ``init_ledger`` runs, so the schema,
    history, ``ledger_meta`` and generated ``dolt_tests`` are exactly what
    ``tm init`` produces.
    """

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self._root = root
        self._env = env
        self._ledgers: dict[str, Path] = {}

    def ledger(self, key: str) -> Path:
        """The template ledger directory for ``key``, built on first use."""
        if key not in self._ledgers:
            workspace = Workspace.create(
                self._root / key, presets_dir=str(REPO_PRESETS), env=self._env
            )
            ledger = init_ledger(
                workspace,
                TEMPLATE_LEDGER_NAME,
                list(TEMPLATE_SHAPES[key]),
                id_prefix=TEMPLATE_ID_PREFIX,
                author=FACTORY_AUTHOR,
            )
            ledger.close()
            self._ledgers[key] = workspace.ledger_path(TEMPLATE_LEDGER_NAME)
        return self._ledgers[key]

    def install(self, key: str, workspace: Workspace, name: str) -> Ledger:
        """Copy the template into ``workspace`` as ``name`` and open it."""
        target = workspace.ledger_path(name)
        if target.exists():
            raise LedgerError(f"ledger {name!r} already exists at {target}")
        shutil.copytree(self.ledger(key), target)
        _rewrite_ledger_name(target, name)
        return open_ledger(workspace, name)


@pytest.fixture(scope="session")
def template_ledgers(tmp_path_factory: pytest.TempPathFactory) -> LedgerTemplates:
    """The session's pre-built ledgers, one per standard shape (lazily built)."""
    root = tmp_path_factory.mktemp("ledger-templates")
    return LedgerTemplates(root, isolated_env(root / "dolt-root", root / "home"))


@pytest.fixture
def fast_ledger_factory(
    embedded_ledger_factory: Callable[..., Ledger],
) -> Callable[..., Ledger]:
    """Alias of :func:`embedded_ledger_factory` (template-backed by default)."""
    return embedded_ledger_factory


# ---------------------------------------------------------------------------
# Spec 5: per-box views
# ---------------------------------------------------------------------------


def create_box_views(
    ledger: Ledger,
    boxes: Sequence[Mapping[str, object]] | None = None,
) -> None:
    """Create the per-box views the ledger's boxes need (spec 5).

    ``tm box add`` and CSV import create a box's view themselves in the same
    commit (wave 11's behavior layer).  Tests that insert boxes with raw SQL, or
    that use a path predating the views, call this so the ledger is in the
    spec-5 shape before ``tm check``/``tm schema plan``.  Only missing views are
    created, so the helper is a no-op once the app path creates them.
    """
    from tetramark.schema.ddl import box_view_statements

    rows = (
        list(boxes)
        if boxes is not None
        else ledger.driver.query("SELECT `id`, `name`, `kind` FROM `box`")
    )
    existing = {
        str(row["name"]).lower()
        for row in ledger.driver.query(
            "SELECT table_name AS name FROM information_schema.views "
            "WHERE table_schema = DATABASE()"
        )
    }
    for statement in box_view_statements(ledger.schema, rows):
        if statement.name.lower() not in existing:
            ledger.driver.execute(statement.sql)
