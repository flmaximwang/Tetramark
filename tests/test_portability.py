"""P12/P13: portability rules for a self-contained ledger.

A ledger is one directory, so **sharing it means copying that directory**
(``cp -a``, ``rsync``, ``dolt clone``/``dolt backup``) -- exactly like a git
repository; two machines writing one ledger over a network share is not
supported (P12).  Two rules keep a copied ledger working:

* ``kinds_dir`` / ``rules_dir`` must resolve **inside** the ledger: an
  absolute path, a ``..`` escape or a symlink that leaves the ledger is refused
  with a usage error (exit code 2) when the ledger is opened -- through the
  CLI and through the Python API;
* ``tm check`` reports an informational rule when a Dolt remote or backup is
  stored as an absolute ``file://`` URL outside the ledger directory: it keeps
  pointing at the old machine after the copy.  An informational rule never
  changes the exit code.

The unit tests are Dolt-free (``fast``); the ones that open a real ledger or
run the CLI against one are marked ``embedded``.
"""

from __future__ import annotations

import json
import shutil
import sys
import warnings
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import adopt_ledger, init_ledger, open as open_ledger
from tetramark.errors import EXIT_USAGE, UsageError
from tetramark.schema.loader import load_ledger_schema, resolve_local_dir

from conftest import FACTORY_AUTHOR

LEDGERS = Path(__file__).parent / "data" / "ledgers"
PRESETS = Path(__file__).parent / "data" / "presets"

#: A local kind and a local rule, the way a ledger carries its own extras.
LOCAL_KIND = """\
kinds:
  portable_entity:
    object: entity
    fields:
      label: {type: string, required: true}
"""

LOCAL_RULE = """\
- id: PORT01
  description: a local rule from rules_dir
  severity: info
  sql: "SELECT COUNT(*) FROM entity_portable_entity"
  expect: {single_value: 0}
"""

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_ledger_yaml(ledger: Path, **updates: object) -> None:
    """Rewrite ``ledger.yaml`` with ``updates`` applied."""
    path = ledger / "ledger.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def write_repo_state(ledger: Path, state: dict[str, object]) -> None:
    """Write ``<ledger>/.dolt/repo_state.json``."""
    (ledger / ".dolt" / "repo_state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


def repo_state(ledger: Path) -> dict[str, object]:
    """The parsed ``<ledger>/.dolt/repo_state.json``."""
    raw = (ledger / ".dolt" / "repo_state.json").read_text(encoding="utf-8")
    state = json.loads(raw)
    assert isinstance(state, dict)
    return state


def run_cli(
    args: list[str | Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run the console entry point and return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["tm", *[str(argument) for argument in args]])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return excinfo.value.code, captured.out, captured.err


# ---------------------------------------------------------------------------
# P13: kinds_dir / rules_dir stay inside the ledger
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_relative_local_dir_resolves_inside_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    (ledger / "kinds").mkdir(parents=True)
    expected = (ledger / "kinds").resolve()
    assert resolve_local_dir("kinds", key="kinds_dir", ledger_dir=ledger) == expected
    # a trailing slash and a "./" are the same directory
    assert resolve_local_dir("kinds/", key="kinds_dir", ledger_dir=ledger) == expected
    assert resolve_local_dir("./kinds", key="kinds_dir", ledger_dir=ledger) == expected


@pytest.mark.fast
def test_an_absolute_kinds_dir_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    with pytest.raises(UsageError) as error:
        resolve_local_dir("/elsewhere/kinds", key="kinds_dir", ledger_dir=ledger)
    message = str(error.value)
    assert "kinds_dir" in message
    assert "/elsewhere/kinds" in message
    assert str(ledger) in message


@pytest.mark.fast
def test_an_absolute_rules_dir_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    with pytest.raises(UsageError) as error:
        resolve_local_dir("/elsewhere/rules", key="rules_dir", ledger_dir=ledger)
    message = str(error.value)
    assert "rules_dir" in message
    assert "/elsewhere/rules" in message
    assert str(ledger) in message


@pytest.mark.fast
def test_a_local_dir_that_escapes_with_dotdot_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    with pytest.raises(UsageError) as error:
        resolve_local_dir("../kinds", key="rules_dir", ledger_dir=ledger)
    message = str(error.value)
    assert "rules_dir" in message
    assert "../kinds" in message


@pytest.mark.fast
def test_a_symlink_that_leaves_the_ledger_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ledger / "kinds").symlink_to(outside)
    with pytest.raises(UsageError) as error:
        resolve_local_dir("kinds", key="kinds_dir", ledger_dir=ledger)
    assert "kinds_dir" in str(error.value)


@pytest.mark.fast
def test_loading_a_ledger_with_an_absolute_kinds_dir_is_refused(
    tmp_path: Path,
) -> None:
    """The open path refuses it, not just the helper."""
    ledger = tmp_path / "local"
    shutil.copytree(LEDGERS / "local", ledger)
    (ledger / "presets").mkdir()
    shutil.copytree(PRESETS / "chain", ledger / "presets" / "chain")
    shutil.copytree(PRESETS / "base", ledger / "presets" / "base")
    write_ledger_yaml(ledger, kinds_dir="/elsewhere/kinds")
    with pytest.raises(UsageError) as error:
        load_ledger_schema(ledger)
    message = str(error.value)
    assert "kinds_dir" in message
    assert "/elsewhere/kinds" in message
    assert str(ledger) in message


@pytest.mark.fast
def test_a_ledger_with_relative_local_dirs_still_loads(tmp_path: Path) -> None:
    """The reference ledger's shape (``kinds`` / ``rules``) keeps working."""
    ledger = tmp_path / "local"
    shutil.copytree(LEDGERS / "local", ledger)
    (ledger / "presets").mkdir()
    shutil.copytree(PRESETS / "chain", ledger / "presets" / "chain")
    shutil.copytree(PRESETS / "base", ledger / "presets" / "base")
    config, schema = load_ledger_schema(ledger)
    assert config.kinds_dir == "kinds/"
    assert config.rules_dir == "rules/"
    assert "custom_entity" in schema.kinds
    assert "LOCAL01" in [rule.id for rule in schema.rules]


# ---------------------------------------------------------------------------
# The open path (CLI and Python API)
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_open_refuses_a_ledger_whose_kinds_dir_leaves_the_ledger(
    ledger_factory,
) -> None:
    ledger = ledger_factory(name="escape")
    write_ledger_yaml(ledger.path, kinds_dir="/elsewhere/kinds")
    with pytest.raises(UsageError) as error:
        open_ledger(ledger.path)
    assert "kinds_dir" in str(error.value)


@pytest.mark.embedded
def test_the_cli_refuses_a_ledger_with_an_absolute_kinds_dir(
    ledger_factory, monkeypatch, capsys
) -> None:
    ledger = ledger_factory(name="cli-escape")
    write_ledger_yaml(ledger.path, kinds_dir="/elsewhere/kinds")
    code, out, err = run_cli(["--ledger", str(ledger.path), "check"], monkeypatch, capsys)
    assert code == EXIT_USAGE, out
    assert "kinds_dir" in err


@pytest.mark.embedded
def test_a_ledger_with_relative_local_dirs_opens(ledger_factory) -> None:
    """The reference ledger's shape opens through the Python API."""
    ledger = ledger_factory(name="relative")
    (ledger.path / "kinds").mkdir()
    (ledger.path / "kinds" / "local.yaml").write_text(LOCAL_KIND, encoding="utf-8")
    (ledger.path / "rules").mkdir()
    (ledger.path / "rules" / "local.yaml").write_text(LOCAL_RULE, encoding="utf-8")
    write_ledger_yaml(ledger.path, kinds_dir="kinds", rules_dir="rules")
    with warnings.catch_warnings():
        # the local kind is not in the database yet: the fingerprint warning is
        # expected and not what this test is about
        warnings.simplefilter("ignore", UserWarning)
        reopened = open_ledger(ledger.path)
    try:
        assert "portable_entity" in reopened.schema.kinds
        assert "PORT01" in [rule.id for rule in reopened.schema.rules]
    finally:
        reopened.close()


@pytest.mark.embedded
def test_init_and_adopt_write_no_absolute_local_dirs(tmp_path, dolt_env) -> None:
    """A new ledger keeps its configuration relative (P13)."""
    target = tmp_path / "fresh"
    init_ledger(
        target,
        "fresh",
        [str(PRESETS / "base")],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=dolt_env,
    )
    payload = yaml.safe_load((target / "ledger.yaml").read_text(encoding="utf-8"))
    for key in ("kinds_dir", "rules_dir"):
        value = payload.get(key)
        assert value is None or not Path(str(value)).is_absolute()


@pytest.mark.embedded
def test_adopt_writes_no_absolute_local_dirs(tmp_path, dolt_env, make_ledger) -> None:
    database = make_ledger(tmp_path / "bare")
    adopt_ledger(
        database,
        "bare",
        [str(PRESETS / "base")],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=dolt_env,
    )
    payload = yaml.safe_load((database / "ledger.yaml").read_text(encoding="utf-8"))
    for key in ("kinds_dir", "rules_dir"):
        value = payload.get(key)
        assert value is None or not Path(str(value)).is_absolute()
