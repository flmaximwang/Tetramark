"""Shared pytest fixtures.

Tests run against the real ``dolt`` binary (never a mock). Every fixture that
shells out points ``DOLT_ROOT_PATH``, ``HOME``, ``XDG_CONFIG_HOME`` and the Git
config env vars at the pytest ``tmp_path`` so the developer's global Dolt/Git
configuration is never read or written.

The authoritative commands:

* ``uv run pytest`` — the **whole** real-Dolt suite, including the
  ``embedded_driver`` process-budget tests.  Ledger tests use the real
  ``dolt sql-server`` backend; the budget tests switch to the embedded driver
  per test, so both backends run in the same session.  The run enforces its own
  300 s wall-clock budget (``TETRAMARK_SUITE_BUDGET_SECONDS`` overrides it for
  profiling) and a 60 s per-test timeout.
* ``uv run pytest -q -m fast`` — the Dolt-free unit tests only; seconds, no
  ``dolt`` binary needed.  This lane enforces a 30 s wall-clock budget.

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

import fcntl
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Skill line budgets (the documentation contract)
# ---------------------------------------------------------------------------

#: Line budget per shipped skill, with the counting mode it uses.  ``file``
#: counts every line of ``SKILL.md`` -- YAML frontmatter included.  ``body``
#: counts every line *after* the closing frontmatter delimiter, including the
#: blank line that separates it from the heading -- so the visible version line
#: and its blank line count.  ``tetramark-schema`` sits at its ceiling; **150
#: may only go down**.  Do not raise a budget to fit new prose: move detail into
#: ``references/`` instead.
SKILL_LINE_BUDGETS: dict[str, tuple[str, int]] = {
    "tetramark": ("file", 150),
    "tetramark-schema": ("body", 150),
}

#: ``---`` delimited YAML frontmatter at the very start of a SKILL.md.
SKILL_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def skill_body_lines(path: Path) -> list[str]:
    """The lines of a ``SKILL.md`` after its YAML frontmatter."""
    text = Path(path).read_text(encoding="utf-8")
    match = SKILL_FRONTMATTER_RE.match(text)
    if match is None:
        raise AssertionError(f"{path} does not start with YAML frontmatter")
    return text[match.end() :].splitlines()


def skill_line_count(name: str) -> int:
    """The lines a shipped skill's budget counts, in the mode its entry names.

    Shared by ``tests/test_docs_commands.py`` (the documentation contract) and
    ``tests/test_skill_version.py`` (the version stamp contract) so the two
    cannot disagree about what "150 lines" or "150 body lines" means.
    """
    mode, _limit = SKILL_LINE_BUDGETS[name]
    path = Path(__file__).resolve().parents[1] / "skills" / name / "SKILL.md"
    if mode == "file":
        return len(path.read_text(encoding="utf-8").splitlines())
    if mode == "body":
        return len(skill_body_lines(path))
    raise AssertionError(f"{name}: unknown skill budget mode {mode!r}")


#: ``subprocess.run`` signature use in fixtures/tests: run_dolt("sql", "-q", "SELECT 1")
RunDolt = Callable[..., subprocess.CompletedProcess[str]]


# ---------------------------------------------------------------------------
# Backend per test, and the suite wall-clock budget
# ---------------------------------------------------------------------------

#: Environment variable selecting the backend for a ledger operation.
DRIVER_ENV = "TETRAMARK_DRIVER"

#: Full-suite wall-clock ceiling.  A run that exceeds it FAILS (non-zero exit)
#: even when every test passed.  Override only to profile a slower machine:
#: ``TETRAMARK_SUITE_BUDGET_SECONDS=600 uv run pytest``; an empty value disables
#: the guard.
SUITE_BUDGET_ENV = "TETRAMARK_SUITE_BUDGET_SECONDS"
DEFAULT_SUITE_BUDGET_SECONDS = 300.0
#: ``-m fast`` is the inner loop; it gets its own much smaller ceiling.
FAST_SUITE_BUDGET_SECONDS = 30.0

#: Run ledger tests through the opt-in ``dolt sql-server`` backend by default.
#: This is a *test harness* default only: the product's default backend is
#: still the embedded driver (one ``dolt sql`` process per batch).  The
#: ``embedded_driver``-marked tests switch back per test (fixture below), so a
#: plain ``uv run pytest`` still executes the process-budget tests on the
#: embedded driver in the same session.  The suite is real Dolt either way.
os.environ.setdefault(DRIVER_ENV, "server")
#: The backend unmarked tests use: server by default, embedded when the whole
#: session was explicitly started with TETRAMARK_DRIVER=embedded.
SESSION_DEFAULT_BACKEND = (
    os.environ.get(DRIVER_ENV, "").strip().lower() or "embedded"
)


@pytest.fixture(autouse=True)
def _backend_for_test(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select the backend per test.

    Ledger tests default to the server driver (fast, same real Dolt); tests
    marked ``embedded_driver`` pin the per-statement embedded driver's process
    budget, and tests marked ``embedded`` promise the embedded driver
    explicitly, so both run on that backend.  The fixture runs before the
    function-scoped ``dolt_env`` fixture, so every env mapping a test passes to
    ``init_ledger``/``open``/a CLI subprocess carries the right value.
    """
    marker = request.node.get_closest_marker("embedded_driver")
    if marker is None:
        marker = request.node.get_closest_marker("embedded")
    mode = "embedded" if marker is not None else SESSION_DEFAULT_BACKEND
    monkeypatch.setenv(DRIVER_ENV, mode)


def _server_lane() -> bool:
    """Whether the process environment selected the ``dolt sql-server`` backend."""
    return os.environ.get(DRIVER_ENV, "").strip().lower() == "server"


def _suite_budget_seconds(config: pytest.Config) -> float | None:
    """The wall-clock budget for this run, or ``None`` when disabled."""
    raw = os.environ.get(SUITE_BUDGET_ENV)
    if raw is not None:
        if not raw.strip():
            return None
        try:
            return float(raw)
        except ValueError as error:
            raise pytest.UsageError(
                f"{SUITE_BUDGET_ENV} must be a number of seconds or empty, "
                f"got {raw!r}"
            ) from error
    markexpr = str(config.getoption("markexpr", default="") or "")
    if "fast" in markexpr:
        return FAST_SUITE_BUDGET_SECONDS
    return DEFAULT_SUITE_BUDGET_SECONDS


#: The controller session while it runs (``None`` inside xdist workers).
_CONTROLLER: pytest.Session | None = None
#: ``(duration, nodeid)`` for every test call, for the over-budget report.
_SLOW_TESTS: list[tuple[float, str]] = []


#: Controller clock start, set in ``pytest_configure`` (``None`` on xdist
#: workers).  The budget covers the whole run: worker start-up, collection,
#: tests, worker teardown and every plugin's session-finish cleanup.
_SUITE_STARTED: float | None = None


def _scratch_root(config: pytest.Config) -> Path:
    """The temporary tree this harness owns (pytest's per-user base temp root)."""
    basetemp = config.getoption("basetemp", default=None)
    if basetemp:
        return Path(basetemp).resolve()
    from _pytest.tmpdir import get_user

    return Path(tempfile.gettempdir(), f"pytest-of-{get_user() or 'unknown'}")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Start the budget clock and reap servers that killed runs abandoned.

    A run that is killed (Ctrl-C twice, a cancelled agent command, a worker
    crash) never reaches :func:`shutdown_all_servers`, so its ``dolt
    sql-server`` children are reparented to pid 1 and keep their memory.  Left
    alone they starve every later run, whose timeouts then leak more servers.
    Only reparented servers in Tetramark's argv shape whose ledger lives in this
    harness's temporary tree (or whose recorded owner has exited) are stopped.
    """
    global _SUITE_STARTED
    if hasattr(config, "workerinput"):
        return
    _SUITE_STARTED = time.monotonic()
    try:
        from tetramark.driver.server import reap_orphaned_servers
    except Exception:  # pragma: no cover - PyMySQL absent
        return
    reaped = reap_orphaned_servers(scratch_root=_scratch_root(config))
    if reaped:
        print(
            f"tetramark: stopped {len(reaped)} orphaned dolt sql-server "
            "process(es) left by an interrupted run",
            file=sys.stderr,
        )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Remember the controller session (never on xdist workers)."""
    global _CONTROLLER, _SLOW_TESTS
    if hasattr(session.config, "workerinput"):
        return
    _CONTROLLER = session
    _SLOW_TESTS = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Collect per-test call durations for the over-budget report.

    xdist workers run this hook locally too, but ``_CONTROLLER`` is only set in
    the controller process, whose report objects cover every worker's tests.
    """
    if _CONTROLLER is None or report.when != "call":
        return
    _SLOW_TESTS.append((report.duration, report.nodeid))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Stop servers, then enforce the wall-clock budget on the controller.

    ``trylast`` puts the check after every other ``sessionfinish``.  Plugins
    registered before this conftest -- pytest's own tmp_path plugin among them
    -- are otherwise called *after* it: that plugin deletes old ``pytest-N``
    directories full of Dolt ledgers, which under load took minutes that the
    old guard never saw (a 158 s fast lane exited 0; GitHub issue #68).
    """
    try:
        from tetramark.driver.server import (
            server_start_count,
            shutdown_all_servers,
        )
    except Exception:  # pragma: no cover - PyMySQL absent or module not imported
        pass
    else:
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

    if hasattr(session.config, "workerinput"):
        return
    if _SUITE_STARTED is None:  # pragma: no cover - configure always ran
        return
    elapsed = time.monotonic() - _SUITE_STARTED
    budget = _suite_budget_seconds(session.config)
    if budget is None or elapsed <= budget:
        return
    slow = sorted(_SLOW_TESTS, reverse=True)[:25]
    print(
        f"\n!! SUITE TIME BUDGET EXCEEDED: {elapsed:.1f}s > {budget:g}s "
        f"(override with {SUITE_BUDGET_ENV}=<seconds>, empty to disable)",
        file=sys.stderr,
    )
    for duration, nodeid in slow:
        print(f"   {duration:7.2f}s  {nodeid}", file=sys.stderr)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


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
    "presets-fixed": (str((PRESETS / "fixed").resolve()),),
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

    The root is shared by every xdist worker of the session: the first worker
    that needs a shape builds it under an exclusive ``flock`` and publishes a
    ``<key>.ready`` marker; the others wait for the lock and copy the same
    closed directory.  One init per shape per *session*, not per worker.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
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
            ready = self._root / f"{key}.ready"
            with open(self._root / f"{key}.lock", "w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not ready.exists():
                    # A half-built directory from a worker that died mid-init.
                    shutil.rmtree(target, ignore_errors=True)
                    self._build(key, target)
                    ready.touch()
            self._ledgers[key] = target
        return self._ledgers[key]

    def _build(self, key: str, target: Path) -> None:
        env = isolated_env(self._root / key / "dolt-root", self._root / key / "home")
        ledger = init_ledger(
            target,
            TEMPLATE_LEDGER_NAME,
            list(TEMPLATE_SHAPES[key]),
            id_prefix=TEMPLATE_ID_PREFIX,
            author=FACTORY_AUTHOR,
            env=env,
        )
        try:
            assert isinstance(ledger, Ledger)
        finally:
            ledger.close()
            _discard_server(target)

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
    """The session's pre-built ledgers, one per standard shape (lazily built).

    Under xdist every worker's base temp is ``<session base>/popen-gwN``, so the
    parent is the one directory all workers of *this* session share (and no
    other session sees).
    """
    base = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        base = base.parent
    root = base / "ledger-templates"
    root.mkdir(exist_ok=True)
    return LedgerTemplates(root)


# ---------------------------------------------------------------------------
# Expensive fixture builds shared by every xdist worker of one session
# ---------------------------------------------------------------------------

#: The file a published shared build carries beside its payload.  It holds the
#: JSON metadata the builder produced (the original ids a test asserts on, and
#: anything else derived while building).  It is written inside the temporary
#: build directory, so the metadata and the payload it describes are published
#: by the same atomic rename and can never come from different builds.
SHARED_BUILD_METADATA = ".shared-build.json"

#: Keys name files under the shared root, so they stay boring on purpose.
SHARED_BUILD_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def _record_shared_build(key: str) -> None:
    """Append one build event when ``TETRAMARK_SHARED_BUILD_STATS`` is set.

    Every xdist worker appends its own line; summing/counting the lines is the
    evidence that a fixture was built once per session instead of once per
    worker.  The append is a single small ``write(2)`` on an ``O_APPEND`` file,
    so concurrent workers cannot interleave or tear a line.
    """
    path = os.environ.get("TETRAMARK_SHARED_BUILD_STATS")
    if not path:
        return
    line = f"tetramark-shared-build-stats: key={key} builds=1\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:  # pragma: no cover - unusable stats path
        print(line, end="", file=sys.stderr, flush=True)
        return
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)


class SharedBuilds:
    """Build an expensive fixture once per pytest session, then copy it.

    A module-scoped fixture is rebuilt once *per xdist worker*, and under
    ``--dist worksteal`` one module's tests can be spread over several workers,
    so a 20–25 s build is paid again per worker.  This helper applies the
    :class:`LedgerTemplates` protocol to any directory-shaped fixture:

    * the root is shared by every worker of the session (the parent of the
      per-worker base temp);
    * the first caller takes an exclusive ``flock`` on ``<key>.lock``, builds
      into a temporary sibling directory, then publishes it with one atomic
      ``os.rename`` and only afterwards writes ``<key>.ready``;
    * the builder writes into that temporary directory and returns the JSON
      metadata the tests need (the original ids and expected values).  The
      helper writes it as :data:`SHARED_BUILD_METADATA` *inside* the temporary
      directory, so payload and metadata are published together;
    * an interrupted publisher is recovered: a missing ``.ready`` marker (even
      when the rename already happened) makes the next caller discard the
      half-published directory and rebuild.  A caller that dies while holding
      the lock releases it with the process;
    * a builder skip (``pytest.skip``) propagates to that caller, publishes
      nothing, and the next caller runs the same builder and skips with the
      same message — a skip is never cached;
    * every caller gets its own ``shutil.copytree`` of the published directory,
      so the published build is immutable and no live ``Ledger`` object or
      server is shared across workers.  The builder must publish a **closed**
      directory (no cached ``dolt sql-server``, no discovery file pointing at a
      live port) exactly like the ledger templates do.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _is_complete(self, ready: Path, published: Path) -> bool:
        return (
            ready.exists()
            and published.is_dir()
            and (published / SHARED_BUILD_METADATA).is_file()
        )

    def build(
        self,
        key: str,
        build_fn: Callable[[Path], Mapping[str, Any]],
        destination: Path,
    ) -> tuple[Path, dict[str, Any]]:
        """Ensure ``key`` is built, then return a private copy and its metadata.

        ``build_fn`` receives an empty temporary directory and must fill it; it
        returns the metadata mapping to publish beside the payload.  It may
        raise :class:`pytest.skip.Exception` (or any other exception) to abort
        without publishing.  ``destination`` is the directory to create for
        *this* caller; it must not exist yet and its parent must.
        """
        if not SHARED_BUILD_KEY_RE.match(key):
            raise ValueError(f"invalid shared-build key {key!r}")
        published = self._root / f"{key}-build"
        ready = self._root / f"{key}.ready"
        with open(self._root / f"{key}.lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self._is_complete(ready, published):
                # Recover from any interrupted publisher.  That includes the
                # crash window where os.rename succeeded but <key>.ready was
                # never written: the marker is what makes a build public, so a
                # directory without one is not trusted and is rebuilt.
                shutil.rmtree(published, ignore_errors=True)
                if ready.exists():
                    ready.unlink()
                for stale in self._root.glob(f"{key}-tmp-*"):
                    shutil.rmtree(stale, ignore_errors=True)
                temporary = Path(
                    tempfile.mkdtemp(prefix=f"{key}-tmp-", dir=self._root)
                )
                try:
                    metadata = build_fn(temporary)
                    (temporary / SHARED_BUILD_METADATA).write_text(
                        json.dumps(dict(metadata), sort_keys=True), encoding="utf-8"
                    )
                    os.rename(temporary, published)
                    ready.touch()
                except BaseException:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
                _record_shared_build(key)
        shutil.copytree(published, destination)
        metadata = json.loads(
            (destination / SHARED_BUILD_METADATA).read_text(encoding="utf-8")
        )
        return destination, metadata


@pytest.fixture(scope="session")
def shared_builds(tmp_path_factory: pytest.TempPathFactory) -> SharedBuilds:
    """Session-wide shared expensive builds (one root per pytest session).

    Like :func:`template_ledgers`, the root is the parent of every worker's base
    temp under xdist, which is the one directory all workers of *this* session
    share and no other session sees.
    """
    base = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        base = base.parent
    return SharedBuilds(base / "shared-builds")


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
