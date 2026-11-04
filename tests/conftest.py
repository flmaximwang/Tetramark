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

A ledger is one self-contained directory in 0.5.0: ``.dolt/`` plus
``ledger.yaml``, created in ``tmp_path/<name>``.  There is no workspace layer
and no server.  The full suite is still real-Dolt end to end:
``ledger_factory`` and ``embedded_ledger_factory`` hand out layouts of a
*session template* (one real ``init_ledger`` per standard shape, then copies)
so a fresh ledger costs a directory copy instead of a full ``tm init``.
Anything that changes what ``init`` produces — a custom preset, several
presets, another ``id_prefix``/``author`` — keeps the real init path; see
:data:`TEMPLATE_SHAPES`.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
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
from tetramark.errors import LedgerError
from tetramark.schema.loader import default_presets_dir, resolve_preset_spec

DOLT_BINARY = shutil.which("dolt")

#: The presets from ticket 03's test data (directory presets, path-style).
PRESETS = Path(__file__).parent / "data" / "presets"

#: The repository's own presets; bare preset names resolve here when
#: ``$TETRAMARK_PRESETS_DIR`` is unset (the checkout keeps ``presets/``).
REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"

#: Author used by :func:`ledger_factory` so tests never read a global identity.
FACTORY_AUTHOR = "Ledger Factory <factory@tetramark.invalid>"


#: ``subprocess.run`` signature use in fixtures/tests: run_dolt("sql", "-q", "SELECT 1")
RunDolt = Callable[..., subprocess.CompletedProcess[str]]


# ---------------------------------------------------------------------------
# The server lane (default in tests, opt-in in the product)
# ---------------------------------------------------------------------------

#: Run the suite through the opt-in ``dolt sql-server`` backend by default.
#: This is a *test harness* default only: the product's default backend is
#: still the embedded driver (one ``dolt sql`` process per batch).  Set
#: ``TETRAMARK_DRIVER=embedded`` (the ``embedded`` lane does) to exercise that
#: default backend; the ``embedded_driver`` budget tests below only mean
#: something there.  The suite is still real Dolt either way.
os.environ.setdefault("TETRAMARK_DRIVER", "server")


def _server_lane() -> bool:
    """Whether this pytest process selected the ``dolt sql-server`` backend."""
    return os.environ.get("TETRAMARK_DRIVER", "").strip().lower() == "server"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip embedded-process budget tests when the server lane is selected.

    ``tests/test_*_round_trips*.py``, ``test_find_batch.py`` and the driver
    contract tests count ``EmbeddedDriver._invoke`` calls to pin "one dolt
    process per batch".  Under ``TETRAMARK_DRIVER=server`` the ledger never uses
    that driver, so the counts are zero and the budget is not the subject of the
    lane.  They stay enforced whenever the backend is the default embedded one.
    """
    if not _server_lane():
        return
    skip = pytest.mark.skip(
        reason="TETRAMARK_DRIVER=server: budget test for the embedded driver"
    )
    for item in items:
        if item.get_closest_marker("embedded_driver") is not None:
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Kill every ``dolt sql-server`` this worker started and report its starts.

    The driver keeps servers alive between commands (that cache is what makes
    the lane fast), so a pytest run must not leave one behind for the next
    process on the machine.
    """
    try:
        from tetramark.driver.server import (
            server_start_count,
            shutdown_all_servers,
        )
    except Exception:  # pragma: no cover - PyMySQL absent or module not imported
        return
    shutdown_all_servers()
    stats_path = os.environ.get("TETRAMARK_SERVER_STATS")
    if _server_lane() and stats_path:
        line = f"tetramark-server-stats: starts={server_start_count()}\n"
        try:
            # The lane script points this at a file so every xdist worker's
            # count can be summed after the run.
            with open(stats_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:  # pragma: no cover - unusable stats path
            print(line, end="", file=sys.stderr, flush=True)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Second cleanup point: xdist workers and early exits still stop servers."""
    try:
        from tetramark.driver.server import shutdown_all_servers
    except Exception:  # pragma: no cover - PyMySQL absent or module not imported
        return
    shutdown_all_servers()


def isolated_env(root: Path, home: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``root``/``home``.

    No ledger-selection environment variable exists in 0.5.0 (P3); the legacy
    ones are removed so a developer's shell cannot influence a test.
    """
    root.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DOLT_ROOT_PATH"] = str(root)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    for legacy in ("TETRAMARK_LEDGER", "TETRAMARK_WORKSPACE", "TETRAMARK_PRESETS_DIR"):
        env.pop(legacy, None)
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
    """Create real standalone ledgers in ``tmp_path`` (embedded driver).

    ``embedded_ledger_factory(presets=[...], id_prefix="77")`` returns the
    opened :class:`~tetramark.core.ledger.Ledger`.  One call creates one ledger
    directory, ``tmp_path/<name>``; every call gets a fresh name unless one is
    given.  Several ledgers can live next to each other, but nothing couples
    them: each directory is complete on its own.

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
        ledger_name = name or f"ledger{next(counter)}"
        target = tmp_path / ledger_name
        sources = list(presets) if presets else [PRESETS / "base"]
        key = None
        if not real_init and template_matches_options(
            id_prefix=id_prefix, author=author
        ):
            key = template_key(sources)
        if key is None:
            ledger = init_ledger(
                target,
                ledger_name,
                sources,
                id_prefix=id_prefix,
                author=author,
                env=dolt_env,
            )
        else:
            ledger = template_ledgers.install(
                key, target, ledger_name, env=dolt_env
            )
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


def template_key(presets: Sequence[str | Path]) -> str | None:
    """The template covering this preset list, or ``None`` when init must run.

    Resolution goes through :func:`~tetramark.schema.loader.resolve_preset_spec`
    with the same preset directory init uses (P8), exactly like init does.  A
    bad preset returns ``None`` (never raises) so the real ``init_ledger`` stays
    the one that reports the error.
    """
    resolved: list[str] = []
    for preset in presets:
        try:
            found = resolve_preset_spec(
                str(preset),
                presets_dir=default_presets_dir(),
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


def _discard_server(path: Path) -> None:
    """Stop a cached ``dolt sql-server`` for ``path`` (a no-op on the embedded lane)."""
    try:
        from tetramark.driver.server import discard_server
    except Exception:  # pragma: no cover - pymysql not installed
        return
    discard_server(path)


class LedgerTemplates:
    """One real ``init_ledger`` per standard shape, then copies of it.

    A template is built lazily in its own directory under the session root and
    is never written to again: :meth:`install` copies the closed ledger
    directory to the test's target path (the ledgers are otherwise identical to
    a fresh init — same schema, ``ledger_meta``, generated ``dolt_tests``, same
    two commits).  A ledger is one directory, so the copy needs no workspace.
    """

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self._root = root
        self._env = env
        self._ledgers: dict[str, Path] = {}

    def ledger(self, key: str) -> Path:
        """The template ledger directory for ``key``, built on first use.

        The directory is copied for every ``install``, so the ``Ledger`` init
        returns is closed and (in the opt-in server lane) its cached
        ``dolt sql-server`` is stopped before the path is published: a template
        must be a closed directory, not one a server is mutating or whose
        discovery files point at a live process.
        """
        if key not in self._ledgers:
            target = self._root / f"{key}-template"
            ledger = init_ledger(
                target,
                TEMPLATE_LEDGER_NAME,
                list(TEMPLATE_SHAPES[key]),
                id_prefix=TEMPLATE_ID_PREFIX,
                author=FACTORY_AUTHOR,
                env=self._env,
            )
            try:
                assert isinstance(ledger, Ledger)
            finally:
                ledger.close()
                _discard_server(target)
            self._ledgers[key] = target
        return self._ledgers[key]

    def install(
        self,
        key: str,
        target: Path,
        name: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Ledger:
        """Copy the template to ``target`` as ``name`` and open it."""
        if target.exists():
            raise LedgerError(f"ledger {name!r} already exists at {target}")
        shutil.copytree(self.ledger(key), target)
        _rewrite_ledger_name(target, name)
        return open_ledger(target, env=env)


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
