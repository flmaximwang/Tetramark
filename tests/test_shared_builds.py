"""Guards for the cross-worker shared fixture builds.

Expensive module fixtures are built once per pytest session by
:class:`conftest.SharedBuilds` and copied per worker.  These fast tests pin the
publishing protocol without Dolt: exactly one builder run for concurrent
callers, payload and metadata published atomically together, recovery from an
interrupted publisher (including a rename that never got its ``.ready``
marker), builder skips that reach every caller and are never cached, and a
published build that a copy's writes cannot modify.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import SHARED_BUILD_METADATA, SharedBuilds

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

#: A caller in another process, so the flock is a real cross-process lock.
CALLER = """
import sys
import time
from pathlib import Path

from conftest import SharedBuilds

root, counter, destination, key, delay = sys.argv[1:6]


def build(out):
    with open(counter, "a", encoding="utf-8") as handle:
        handle.write("run\\n")
        handle.flush()
    time.sleep(float(delay))
    (out / "payload").mkdir()
    (out / "payload" / "data.txt").write_text("metal", encoding="utf-8")
    return {"key": key, "ids": {"entity": "E1"}}


copy, metadata = SharedBuilds(Path(root)).build(key, build, Path(destination))
assert (copy / "payload" / "data.txt").read_text(encoding="utf-8") == "metal"
assert metadata == {"key": key, "ids": {"entity": "E1"}}
"""


def _run_callers(
    root: Path,
    counter: Path,
    keys_and_destinations: list[tuple[str, Path]],
    *,
    delay: float = 0.0,
    extra_env: dict[str, str] | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    """Start one caller process per destination and return their results."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(TESTS), str(ROOT / "src")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    if extra_env:
        env.update(extra_env)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                CALLER,
                str(root),
                str(counter),
                str(destination),
                key,
                str(delay),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for key, destination in keys_and_destinations
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        results.append(
            subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        )
    return results


def _counter_lines(counter: Path) -> int:
    if not counter.exists():
        return 0
    return len(counter.read_text(encoding="utf-8").splitlines())


def test_concurrent_callers_build_exactly_once(tmp_path: Path) -> None:
    """Two processes race one key: one builder run, both get the same build."""
    root = tmp_path / "shared"
    counter = tmp_path / "counter"
    first = tmp_path / "copy-a"
    second = tmp_path / "copy-b"
    results = _run_callers(
        root,
        counter,
        [("race", first), ("race", second)],
        delay=0.4,
    )

    for result in results:
        assert result.returncode == 0, result.stdout + result.stderr
    assert _counter_lines(counter) == 1
    assert (first / "payload" / "data.txt").read_text(encoding="utf-8") == "metal"
    assert (second / "payload" / "data.txt").read_text(encoding="utf-8") == "metal"
    # Each caller receives a private copy, not the published directory.
    assert first != second
    assert json.loads((first / SHARED_BUILD_METADATA).read_text(encoding="utf-8")) == {
        "key": "race",
        "ids": {"entity": "E1"},
    }


def test_payload_and_metadata_are_published_by_one_rename(tmp_path: Path) -> None:
    """A reader gets the payload and the metadata of the same build."""
    root = tmp_path / "shared"
    counter = tmp_path / "counter"
    shared = SharedBuilds(root)

    def build(out: Path) -> dict[str, object]:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("run\n")
        (out / "payload").write_text("first", encoding="utf-8")
        return {"build": 1}

    copy, metadata = shared.build("atomic", build, tmp_path / "copy-1")
    assert metadata == {"build": 1}
    assert (copy / "payload").read_text(encoding="utf-8") == "first"
    assert (copy / SHARED_BUILD_METADATA).is_file()


def test_interrupted_publisher_is_recovered_and_rebuilt(tmp_path: Path) -> None:
    """No ``.ready`` means no trust — including after a successful rename."""
    root = tmp_path / "shared"
    root.mkdir()
    counter = tmp_path / "counter"
    # The crash window: the rename happened, writing <key>.ready did not.  The
    # half-published directory must not be reused.
    (root / "resume-build").mkdir()
    (root / "resume-build" / "payload").write_text("stale", encoding="utf-8")
    (root / "resume-build" / SHARED_BUILD_METADATA).write_text(
        json.dumps({"build": "stale"}), encoding="utf-8"
    )
    # A half-built temporary sibling from another interrupted process.
    (root / "resume-tmp-dead").mkdir()
    (root / "resume-tmp-dead" / "partial").write_text("partial", encoding="utf-8")

    def build(out: Path) -> dict[str, object]:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("run\n")
        (out / "payload").write_text("fresh", encoding="utf-8")
        return {"build": "fresh"}

    copy, metadata = SharedBuilds(root).build("resume", build, tmp_path / "copy")
    assert metadata == {"build": "fresh"}
    assert (copy / "payload").read_text(encoding="utf-8") == "fresh"
    assert _counter_lines(counter) == 1
    assert not (root / "resume-tmp-dead").exists()
    assert (root / "resume.ready").exists()


def test_a_complete_published_build_is_reused(tmp_path: Path) -> None:
    """A ready marker plus a complete directory is copied, never rebuilt."""
    root = tmp_path / "shared"
    counter = tmp_path / "counter"
    shared = SharedBuilds(root)

    def build(out: Path) -> dict[str, object]:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("run\n")
        (out / "payload").write_text("built", encoding="utf-8")
        return {"build": 1}

    shared.build("reuse", build, tmp_path / "copy-1")
    copy, metadata = shared.build("reuse", build, tmp_path / "copy-2")
    assert metadata == {"build": 1}
    assert (copy / "payload").read_text(encoding="utf-8") == "built"
    assert _counter_lines(counter) == 1


def test_builder_skip_reaches_every_caller_and_is_not_cached(tmp_path: Path) -> None:
    """A skip publishes nothing and the next caller evaluates the skip again."""
    root = tmp_path / "shared"
    counter = tmp_path / "counter"
    shared = SharedBuilds(root)

    def build(out: Path) -> dict[str, object]:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("run\n")
        pytest.skip("the legacy fixture cannot be built here")

    for attempt in ("copy-1", "copy-2"):
        with pytest.raises(pytest.skip.Exception) as skipped:
            shared.build("skip", build, tmp_path / attempt)
        assert "the legacy fixture cannot be built here" in str(skipped.value)

    assert _counter_lines(counter) == 2
    assert not (root / "skip.ready").exists()
    assert not (root / "skip-build").exists()


def test_copy_writes_never_touch_the_published_build(tmp_path: Path) -> None:
    """Each copy is independent; the shared directory stays immutable."""
    root = tmp_path / "shared"
    shared = SharedBuilds(root)

    def build(out: Path) -> dict[str, object]:
        (out / "payload").write_text("original", encoding="utf-8")
        return {"build": 1}

    first, _ = shared.build("immutable", build, tmp_path / "copy-1")
    (first / "payload").write_text("mutated", encoding="utf-8")
    second, _ = shared.build("immutable", build, tmp_path / "copy-2")
    assert (second / "payload").read_text(encoding="utf-8") == "original"
    assert (root / "immutable-build" / "payload").read_text(
        encoding="utf-8"
    ) == "original"


def test_build_stats_record_one_event_per_build(tmp_path: Path) -> None:
    """``TETRAMARK_SHARED_BUILD_STATS`` gets one line per real build."""
    root = tmp_path / "shared"
    counter = tmp_path / "counter"
    stats = tmp_path / "stats"
    results = _run_callers(
        root,
        counter,
        [("stats", tmp_path / "copy-a"), ("stats", tmp_path / "copy-b")],
        delay=0.3,
        extra_env={"TETRAMARK_SHARED_BUILD_STATS": str(stats)},
    )
    for result in results:
        assert result.returncode == 0, result.stdout + result.stderr
    lines = stats.read_text(encoding="utf-8").splitlines()
    assert lines == ["tetramark-shared-build-stats: key=stats builds=1"]


def test_keys_stay_filesystem_safe(tmp_path: Path) -> None:
    """A key becomes file names under the shared root, so it is validated."""
    shared = SharedBuilds(tmp_path / "shared")
    with pytest.raises(ValueError):
        shared.build("../escape", lambda out: {}, tmp_path / "copy")
