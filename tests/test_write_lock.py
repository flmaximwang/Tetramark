"""P11: the per-ledger write lock (Tetramark-l8g.7).

Fast lane: the lock itself is filesystem-only, so the serialization, busy
timeout, release-on-error, holder-probe and no-lock-for-reads tests run with a
stub driver and two ``Ledger`` objects on one directory -- no ``dolt`` needed.
The last section drives the real CLI against real ledgers (embedded lane) to
prove that a write command is refused with the typed busy error and succeeds
after the holder releases, while reads and ``--dry-run`` ignore the lock.

Process note: the lock is ``fcntl.flock``, so contention is between *open file
descriptions*.  Two ``Ledger`` objects (or two ``WriteLock`` objects) on one
path contend even inside one process; the same ``Ledger`` is intentionally
re-entrant so a command can open several write sessions under one lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tetramark.cli import options as cli_options
from tetramark.cli import (
    aliquot,
    alias,
    box,
    branch,
    config,
    csv_io,
    entity,
    remote,
    schema as schema_cli,
    stock,
)
from tetramark.cli.app import app
from tetramark.core.ledger import Ledger
from tetramark.core.writelock import (
    DEFAULT_LOCK_TIMEOUT,
    LOCK_FILENAME,
    RETRY_INTERVAL,
    TIMEOUT_ENV,
    LockHolder,
    WriteLock,
    lock_path,
    lock_timeout,
    read_lock_holder,
    write_lock_holder,
)
from tetramark.driver.base import Driver
from tetramark.errors import LedgerBusyError, UsageError
from tetramark.schema.loader import load_schema
from tetramark.schema.model import LedgerConfig

from conftest import PRESETS, REPO_PRESETS

BASE = PRESETS / "base"


# ---------------------------------------------------------------------------
# Fast-lane helpers: a real Ledger over a stub driver
# ---------------------------------------------------------------------------


class FakeDriver(Driver):
    """A Driver that records statements/commits without any Dolt process."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.commits: list[str] = []
        self.transactions = 0
        self.rolled_back = 0
        self.in_transaction = False
        #: Called inside ``commit`` so a test can observe the lock state there.
        self.on_commit: Any = None

    @property
    def database(self) -> str:
        return "locktest"

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        self.in_transaction = True
        try:
            yield self
        except BaseException:
            self.rolled_back += 1
            raise
        finally:
            self.in_transaction = False

    def commit(self, message: str, author: str | None = None) -> str | None:
        if callable(self.on_commit):
            self.on_commit()
        self.commits.append(message)
        return "deadbeef"


def make_ledger(
    directory: Path,
    *,
    name: str = "locktest",
    dry_run: bool = False,
) -> Ledger:
    """A real ``Ledger`` object (schema/config included) on a stub driver."""
    ledger_dir = directory / name
    (ledger_dir / ".dolt").mkdir(parents=True, exist_ok=True)
    config = LedgerConfig(
        name=name,
        presets=[str((REPO_PRESETS / "base.yaml").resolve())],
        ids={"stock": {"strategy": "datecode", "prefix": "77"}},
    )
    schema = load_schema(config)
    return Ledger(
        path=ledger_dir,
        name=name,
        driver=FakeDriver(),
        config=config,
        schema=schema,
        author="Lock Test <lock@tetramark.invalid>",
        dry_run=dry_run,
    )


def lock_file(ledger: Ledger) -> Path:
    return lock_path(ledger.path)


# ---------------------------------------------------------------------------
# The lock file and the holder record
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_lock_path_is_in_dot_dolt() -> None:
    assert lock_path(Path("/tmp/some-ledger")) == Path(
        "/tmp/some-ledger/.dolt/tetramark.lock"
    )
    assert LOCK_FILENAME == "tetramark.lock"


@pytest.mark.fast
def test_default_timeout_is_ten_seconds() -> None:
    assert DEFAULT_LOCK_TIMEOUT == 10.0
    assert 0 < RETRY_INTERVAL < 1
    assert lock_timeout(env={}) == DEFAULT_LOCK_TIMEOUT
    assert lock_timeout(env={TIMEOUT_ENV: "0.25"}) == 0.25


@pytest.mark.fast
def test_ledger_exposes_the_write_lock_helpers() -> None:
    assert Ledger.writing is Ledger.hold_write_lock
    assert callable(Ledger.write_lock_holder)
    assert DEFAULT_LOCK_TIMEOUT > 0


@pytest.mark.fast
def test_invalid_timeout_env_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match=TIMEOUT_ENV):
        lock_timeout(env={TIMEOUT_ENV: "soon"})
    with pytest.raises(UsageError, match=TIMEOUT_ENV):
        lock_timeout(env={TIMEOUT_ENV: "-1"})


@pytest.mark.fast
def test_write_session_takes_the_lock_and_leaves_the_file(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    file = lock_file(ledger)
    assert not file.exists()
    assert write_lock_holder(ledger) is None

    commits_under_lock: list[LockHolder | None] = []
    ledger.driver.on_commit = lambda: commits_under_lock.append(
        write_lock_holder(ledger)
    )

    with ledger.write("add entity e1", verb="add", object="entity") as session:
        session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))
        assert file.is_file()
        holder = write_lock_holder(ledger)
        assert holder is not None
        assert holder.pid == os.getpid()
        assert holder.command is not None and "add(entity)" in holder.command
        assert holder.since is not None
        # The lock file is a one-line JSON record naming the writer.
        payload = json.loads(file.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["command"] == holder.command
        assert ledger.write_lock_holder() is not None
        assert ledger.driver.in_transaction

    assert ledger.driver.commits == ["add(entity): add entity e1"]
    assert commits_under_lock and commits_under_lock[0] is not None, (
        "the Dolt commit must happen while the writer lock is held"
    )
    assert write_lock_holder(ledger) is None
    # The lock file is created once and left in place after release.
    assert file.is_file()


@pytest.mark.fast
def test_read_lock_holder_reads_the_file_without_locking(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    with ledger.hold_write_lock(command="tm stock add"):
        holder = read_lock_holder(ledger)
        assert holder is not None
        assert holder.pid == os.getpid()
        assert holder.command == "tm stock add"
        assert holder.description == f"pid {os.getpid()} (tm stock add)"
        assert holder.as_dict()["command"] == "tm stock add"


# ---------------------------------------------------------------------------
# Serialization: one writer at a time
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_second_writer_waits_and_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    first = make_ledger(tmp_path)
    second = make_ledger(tmp_path)  # separate Ledger -> separate flock description

    order: list[str] = []
    acquired = threading.Event()

    def writer() -> None:
        with second.write("second writer", verb="add", object="entity") as session:
            session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e2",))
            order.append("second")
            acquired.set()

    thread: threading.Thread | None = None
    try:
        with first.write("first writer", verb="add", object="entity") as session:
            session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))
            thread = threading.Thread(target=writer)
            thread.start()
            # While the first session holds the lock the second writer waits;
            # its 10 s timeout is far away, so it must not finish here.
            assert not acquired.wait(0.4)
            order.append("first")
    finally:
        if thread is not None:
            thread.join(10)

    assert acquired.is_set()
    assert order == ["first", "second"]
    assert write_lock_holder(first) is None


@pytest.mark.fast
def test_busy_timeout_raises_the_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TIMEOUT_ENV, "0.2")
    holder = make_ledger(tmp_path)
    writer = make_ledger(tmp_path)

    with holder.hold_write_lock(command="tm stock add"):
        started = time.monotonic()
        with pytest.raises(LedgerBusyError) as caught:
            with writer.write("blocked writer", verb="add", object="entity"):
                pass  # pragma: no cover - the lock is taken before the body
        elapsed = time.monotonic() - started

    error = caught.value
    assert isinstance(error, UsageError)
    assert error.exit_code == 2
    message = str(error)
    assert message.startswith("ledger busy: ")
    assert f"pid {os.getpid()}" in message
    assert "tm stock add" in message
    assert TIMEOUT_ENV in message
    assert elapsed >= 0.15
    assert elapsed < 5


@pytest.mark.fast
def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    with pytest.raises(RuntimeError):
        with ledger.write("failing", verb="add", object="entity") as session:
            session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))
            raise RuntimeError("boom")

    assert ledger.driver.rolled_back == 1
    assert ledger.driver.commits == []
    assert write_lock_holder(ledger) is None

    # The next writer -- from another Ledger -- gets the lock immediately.
    other = make_ledger(tmp_path)
    with other.hold_write_lock(command="after the failure"):
        pass


@pytest.mark.fast
def test_lock_is_released_on_keyboard_interrupt(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with ledger.write("interrupted", verb="add", object="entity"):
            raise KeyboardInterrupt
    assert write_lock_holder(ledger) is None
    with ledger.hold_write_lock(command="after the interrupt"):
        pass


@pytest.mark.fast
def test_lock_is_released_when_the_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = make_ledger(tmp_path)

    def failing_commit(message: str, author: str | None = None) -> str | None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(ledger.driver, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        with ledger.write("failing commit", verb="add", object="entity") as session:
            session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))

    assert write_lock_holder(ledger) is None
    with ledger.hold_write_lock(command="after the failed commit"):
        pass


@pytest.mark.fast
def test_lock_released_when_the_transaction_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between lock acquisition and ``__enter__`` must not leak it."""
    ledger = make_ledger(tmp_path)

    @contextmanager
    def broken_transaction():
        raise RuntimeError("cannot begin")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(ledger.driver, "transaction", broken_transaction)
    with pytest.raises(RuntimeError, match="cannot begin"):
        with ledger.write("broken", verb="add", object="entity"):
            pass  # pragma: no cover
    assert write_lock_holder(ledger) is None
    with ledger.hold_write_lock(command="after the broken transaction"):
        pass


# ---------------------------------------------------------------------------
# Reads and --dry-run never lock
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_dry_run_takes_no_lock_and_creates_no_file(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, dry_run=True)
    with ledger.write("add entity e1", verb="add", object="entity") as session:
        session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))
        assert not lock_file(ledger).exists()
        assert write_lock_holder(ledger) is None
    assert not lock_file(ledger).exists()
    assert ledger.driver.commits == [] and ledger.driver.executed == []


@pytest.mark.fast
def test_write_lock_holder_never_creates_or_steals_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TIMEOUT_ENV, "0.2")
    ledger = make_ledger(tmp_path)
    assert write_lock_holder(ledger) is None
    assert not lock_file(ledger).exists(), "the probe must not create the lock file"

    holder = WriteLock(ledger.path, command="tm entity add")
    holder.acquire()
    try:
        assert write_lock_holder(ledger) is not None
        # The probe did not leave the lock free: another writer still times out.
        other = make_ledger(tmp_path)
        with pytest.raises(LedgerBusyError):
            with other.hold_write_lock(command="blocked"):
                pass
    finally:
        holder.release()

    other = make_ledger(tmp_path)
    with other.hold_write_lock(command="after the probe"):
        pass


@pytest.mark.fast
def test_write_lock_holder_reports_another_process(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    script = (
        "import sys, time\n"
        "from tetramark.core.writelock import WriteLock\n"
        "lock = WriteLock(sys.argv[1], command='helper writer')\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ledger.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line.strip() == "locked", process.stderr.read()  # type: ignore[union-attr]
        holder = write_lock_holder(ledger)
        assert holder is not None
        assert holder.pid == process.pid
        assert holder.command == "helper writer"
        assert holder.since is not None
    finally:
        process.kill()
        process.wait(timeout=10)

    # The kernel releases a dead holder's flock; retry briefly for the close.
    deadline = time.monotonic() + 5
    while True:
        try:
            with ledger.hold_write_lock(command="after the helper", timeout=0.2):
                break
        except LedgerBusyError:
            if time.monotonic() > deadline:  # pragma: no cover - never hangs
                raise
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# CLI wiring: write commands hold the lock, reads/--dry-run do not
# ---------------------------------------------------------------------------

WRITE_COMMANDS = [
    entity.add_entity_command,
    entity.edit_entity_command,
    stock.add_stock_command,
    stock.edit_stock_command,
    stock.renumber_stocks_command,
    aliquot.add_aliquots_command,
    aliquot.move_aliquot_command,
    aliquot.use_aliquot_command,
    aliquot.status_aliquot_command,
    aliquot.purge_aliquot_command,
    box.add_box_command,
    box.move_box_command,
    alias.add_alias_command,
    alias.remove_alias_command,
    config.config_set,
    schema_cli.apply_schema,
    csv_io.import_command,
    branch.branch_create,
    branch.branch_delete,
    remote.remote_add,
    remote.remote_push,
    remote.remote_pull,
    remote.remote_remove,
]

READ_COMMANDS = [
    entity.show_entity_command,
    entity.list_entities_command,
    stock.show_stock_command,
    stock.list_stocks_command,
    box.show_box_command,
    box.list_boxes_command,
    config.config_get,
    schema_cli.show_schema,
    schema_cli.plan_schema,
    csv_io.export_command,
    branch.branch_list,
    remote.remote_list,
    remote.backup_command,  # mixed: locks only its sync half, see remote.py
]


@pytest.mark.fast
@pytest.mark.parametrize("command", WRITE_COMMANDS, ids=lambda fn: fn.__name__)
def test_cli_write_commands_are_decorated(command: Any) -> None:
    assert getattr(command, cli_options.WRITE_COMMAND_MARKER, False) is True


@pytest.mark.fast
@pytest.mark.parametrize("command", READ_COMMANDS, ids=lambda fn: fn.__name__)
def test_cli_read_commands_are_not_decorated(command: Any) -> None:
    assert getattr(command, cli_options.WRITE_COMMAND_MARKER, False) is False


@pytest.mark.fast
def test_write_command_decorator_locks_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    ledger_dir = tmp_path / "decorator-ledger"
    (ledger_dir / ".dolt").mkdir(parents=True)

    class FakeLedger:
        def _adopt_write_lock(self, lock: WriteLock) -> None:
            self.lock = lock
            log.append("adopt")

        def _release_adopted_write_lock(self) -> None:
            log.append("release")
            if self.lock is not None:
                self.lock.release()
                self.lock = None

    fake_ledger = FakeLedger()

    def fake_open(*args: Any, **kwargs: Any) -> FakeLedger:
        # The writer lock is held *before* open() reads config/schema/meta.
        assert write_lock_holder(ledger_dir) is not None
        log.append("open")
        return fake_ledger

    monkeypatch.setattr(
        cli_options,
        "resolve_ledger_dir",
        lambda path: SimpleNamespace(path=ledger_dir, name="decorator-ledger"),
    )
    monkeypatch.setattr(cli_options, "open_ledger", fake_open)
    options = cli_options.GlobalOptions()
    ctx = SimpleNamespace(obj=options)

    @cli_options.write_command
    def command(ctx: Any, value: str = "x") -> str:
        locked = write_lock_holder(ledger_dir) is not None
        log.append(f"body:{value}:{locked}")
        return value

    assert command(ctx, value="one") == "one"
    assert log == ["open", "adopt", "body:one:True", "release"]
    assert write_lock_holder(ledger_dir) is None
    assert lock_path(ledger_dir).is_file(), "the lock file is left in place"

    # --dry-run writes nothing: no resolve, no open, no lock.
    log.clear()
    options.opened = None
    options.dry_run = True
    assert command(ctx, value="two") == "two"
    assert log == ["body:two:False"]

    # An exception in the body still releases the adopted lock.
    log.clear()
    options.opened = None
    options.dry_run = False
    assert command(ctx, value="three") == "three"
    assert log == ["open", "adopt", "body:three:True", "release"]
    log.clear()
    options.opened = None

    @cli_options.write_command
    def boom(ctx: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        boom(ctx)
    assert log == ["open", "adopt", "release"]
    assert write_lock_holder(ledger_dir) is None


@pytest.mark.fast
def test_adopted_lock_is_the_outermost_write_scope(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    lock = WriteLock(ledger.path, command="tm entity add")
    lock.acquire()
    ledger._adopt_write_lock(lock)
    try:
        assert write_lock_holder(ledger) is not None
        with ledger.write("nested", verb="add", object="entity") as session:
            session.execute("INSERT INTO `entity` (`id`) VALUES (%s)", ("e1",))
            assert write_lock_holder(ledger) is not None
        # The session released its nested scope; the adopted one still holds.
        assert write_lock_holder(ledger) is not None
    finally:
        ledger._release_adopted_write_lock()

    assert write_lock_holder(ledger) is None
    other = make_ledger(tmp_path)
    with other.hold_write_lock(command="after the adopted lock"):
        pass


# ---------------------------------------------------------------------------
# Embedded lane: the real CLI against a real ledger
# ---------------------------------------------------------------------------


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def entity_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM entity")[0]["n"])


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the ledger's isolated Dolt/Git environment."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)


def invoke(ledger: Ledger, *args: str) -> Any:
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


@pytest.mark.embedded
def test_cli_write_is_busy_then_succeeds(
    cli_env: None,
    ledger_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = ledger_factory([BASE], name="lock-cli-busy")
    monkeypatch.setenv(TIMEOUT_ENV, "0.2")

    holder = WriteLock(ledger.path, command="tm stock add")
    holder.acquire()
    try:
        before = commit_count(ledger)
        blocked = invoke(ledger, "entity", "add", "item", "--name", "alpha")
        assert isinstance(blocked.exception, LedgerBusyError), blocked.output
        message = str(blocked.exception)
        assert message.startswith("ledger busy: ")
        assert f"pid {os.getpid()}" in message
        assert "tm stock add" in message
        assert commit_count(ledger) == before
        assert entity_count(ledger) == 0
    finally:
        holder.release()

    done = invoke(ledger, "entity", "add", "item", "--name", "alpha")
    assert done.exit_code == 0, done.output
    assert entity_count(ledger) == 1
    assert commit_count(ledger) == before + 1


@pytest.mark.embedded
def test_cli_reads_and_dry_run_ignore_the_write_lock(
    cli_env: None,
    ledger_factory: Any,
) -> None:
    ledger = ledger_factory([BASE], name="lock-cli-reads")

    # Reads never create the lock file.
    assert invoke(ledger, "entity", "list").exit_code == 0
    assert invoke(ledger, "config", "get", "author").exit_code == 0
    assert invoke(ledger, "backup", "--list").exit_code == 0
    assert not lock_path(ledger.path).exists()

    holder = WriteLock(ledger.path, command="tm stock add")
    holder.acquire()
    try:
        # --dry-run writes nothing, so it may proceed while the lock is held.
        before = commit_count(ledger)
        dry = invoke(
            ledger, "--dry-run", "entity", "add", "item", "--name", "alpha"
        )
        assert dry.exit_code == 0, dry.output
        assert "-- target branch:" in dry.output
        assert "INSERT INTO" in dry.output
        assert commit_count(ledger) == before
        assert entity_count(ledger) == 0

        # Read commands do not wait either.
        assert invoke(ledger, "entity", "list").exit_code == 0
        assert invoke(ledger, "config", "get", "author").exit_code == 0
        assert invoke(ledger, "backup", "--list").exit_code == 0
    finally:
        holder.release()

    # The lock file stays in .dolt after a writer is done (P11: left in place).
    assert lock_path(ledger.path).is_file()
