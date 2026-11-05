"""P8: bare preset names resolve env dir -> bundled presets; ledgers keep a copy.

Resolution order (settled decision P8):

1. ``$TETRAMARK_PRESETS_DIR`` when set;
2. the presets bundled with tetramark -- the source checkout's ``presets/`` or,
   in an installed wheel, the package data at
   ``<site-packages>/tetramark/presets``;
3. a ``--preset`` value that looks like a path is used as-is, with no preset
   directory at all.

A ledger still copies the preset and its include closure into
``<ledger>/presets/`` and reopens **only** from that copy: there is no fallback
to the env var or the bundled presets, so a copied ledger is self-contained.

The lookup tests are Dolt-free (marked ``fast``); the copy/reopen tests build a
real embedded ledger (marked ``embedded``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import tetramark
from tetramark.core.ledger import init_ledger, open as open_ledger
from tetramark.errors import LedgerError, UsageError
from tetramark.schema import loader
from tetramark.schema.loader import (
    default_presets_dir,
    resolve_preset_spec,
)

from conftest import FACTORY_AUTHOR, REPO_PRESETS


# ---------------------------------------------------------------------------
# default_presets_dir: env -> checkout -> wheel package data
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_bare_name_uses_the_checkout_presets_without_an_env_var() -> None:
    """The source checkout's ``presets/`` is the bundled fallback (P8)."""
    resolved = default_presets_dir(env={})
    assert resolved == REPO_PRESETS
    assert resolved.is_dir()


@pytest.mark.fast
def test_the_env_var_wins_over_the_bundled_presets(tmp_path: Path) -> None:
    override = tmp_path / "presets-from-env"
    override.mkdir()

    resolved = default_presets_dir(env={"TETRAMARK_PRESETS_DIR": str(override)})

    assert resolved == override


@pytest.mark.fast
def test_the_env_var_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = default_presets_dir(env={"TETRAMARK_PRESETS_DIR": "~/presets"})
    assert resolved == tmp_path / "presets"


@pytest.mark.fast
def test_an_installed_wheel_resolves_package_data_presets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel has no ``<repo>/presets``: ``tetramark/presets`` must be found."""
    package = tmp_path / "site-packages" / "tetramark"
    (package / "schema").mkdir(parents=True)
    fake_loader = package / "schema" / "loader.py"
    fake_loader.write_text("", encoding="utf-8")
    bundled = package / "presets"
    bundled.mkdir()
    monkeypatch.setattr(loader, "__file__", str(fake_loader))

    assert default_presets_dir(env={}) == bundled.resolve()


@pytest.mark.fast
def test_the_checkout_presets_win_over_wheel_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable install still uses the checkout's own presets."""
    checkout = tmp_path / "checkout"
    (checkout / "src" / "tetramark" / "schema").mkdir(parents=True)
    (checkout / "presets").mkdir()
    fake_loader = checkout / "src" / "tetramark" / "schema" / "loader.py"
    fake_loader.write_text("", encoding="utf-8")
    packaged = checkout / "src" / "tetramark" / "presets"
    packaged.mkdir()
    monkeypatch.setattr(loader, "__file__", str(fake_loader))

    assert default_presets_dir(env={}) == (checkout / "presets").resolve()


@pytest.mark.fast
def test_no_preset_directory_errors_point_at_the_env_var_and_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken installation (no bundled presets) gives actionable advice."""
    monkeypatch.setattr(loader, "_bundled_presets_dirs", lambda: ())
    assert default_presets_dir(env={}) is None

    with pytest.raises(UsageError, match="TETRAMARK_PRESETS_DIR"):
        resolve_preset_spec("base", presets_dir=default_presets_dir(env={}))


# ---------------------------------------------------------------------------
# resolve_preset_spec: bare names and explicit paths
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_bare_name_resolves_inside_the_env_dir(tmp_path: Path) -> None:
    env_dir = tmp_path / "env-presets"
    env_dir.mkdir()
    shutil.copyfile(REPO_PRESETS / "base.yaml", env_dir / "base.yaml")

    resolved = resolve_preset_spec(
        "base", presets_dir=default_presets_dir(env={"TETRAMARK_PRESETS_DIR": str(env_dir)})
    )

    assert resolved.name == "base"
    assert resolved.path == (env_dir / "base.yaml").resolve()


@pytest.mark.fast
def test_a_bare_name_missing_from_the_env_dir_is_an_error(tmp_path: Path) -> None:
    env_dir = tmp_path / "env-presets"
    env_dir.mkdir()

    with pytest.raises(LedgerError, match="not found in presets_dir"):
        resolve_preset_spec("base", presets_dir=env_dir)


@pytest.mark.fast
def test_an_explicit_preset_path_needs_no_preset_directory() -> None:
    """``--preset PATH`` is unchanged: it works with no lookup configured."""
    for name in ("base", "molbio"):
        resolved = resolve_preset_spec(REPO_PRESETS / f"{name}.yaml", presets_dir=None)
        assert resolved.name == name
        assert resolved.path == (REPO_PRESETS / f"{name}.yaml").resolve()


@pytest.mark.fast
def test_a_bare_name_uses_the_env_dir_at_init_plan_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var selects the preset the new ledger is built from (P8)."""
    env_dir = tmp_path / "env-presets"
    env_dir.mkdir()
    # A distinguishable preset with the same name: its version proves which
    # file the plan loaded.
    text = (REPO_PRESETS / "base.yaml").read_text(encoding="utf-8")
    current = int(text.splitlines()[1].split(":", 1)[1])
    assert current != 99, "the sentinel must differ from the shipped version"
    (env_dir / "base.yaml").write_text(
        text.replace(f"version: {current}", "version: 99", 1), encoding="utf-8"
    )
    target = tmp_path / "lab"
    plan = init_ledger(
        target,
        None,
        ["base"],
        author=FACTORY_AUTHOR,
        dry_run=True,
        env={"TETRAMARK_PRESETS_DIR": str(env_dir)},
    )

    assert plan.config.presets == ["base"]
    assert plan.schema.preset_versions == {"base": 99}
    assert "item" in plan.schema.kinds
    assert not target.exists()


# ---------------------------------------------------------------------------
# A ledger copies presets and reopens only from its own copy
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_an_env_dir_preset_is_copied_into_the_ledger(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    env_dir = tmp_path / "env-presets"
    env_dir.mkdir()
    source = env_dir / "base.yaml"
    source.write_bytes((REPO_PRESETS / "base.yaml").read_bytes() + b"# from env\n")

    ledger = init_ledger(
        tmp_path / "lab",
        None,
        ["base"],
        author=FACTORY_AUTHOR,
        env={**dolt_env, "TETRAMARK_PRESETS_DIR": str(env_dir)},
    )

    copy = ledger.path / "presets" / "base.yaml"
    assert copy.read_bytes() == source.read_bytes()
    # The ledger records the bare name, not the env path.
    assert ledger.config.presets == ["base"]
    # Reopening needs no preset directory at all.
    assert open_ledger(ledger.path, env=dolt_env).name == "lab"


@pytest.mark.embedded
def test_an_explicit_preset_path_is_copied_and_reopens(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    custom = tmp_path / "custom-base.yaml"
    custom.write_bytes((REPO_PRESETS / "base.yaml").read_bytes() + b"# explicit path\n")

    ledger = init_ledger(
        tmp_path / "lab",
        None,
        [custom],
        author=FACTORY_AUTHOR,
        env=dolt_env,
    )

    assert (ledger.path / "presets" / "base.yaml").read_bytes() == custom.read_bytes()
    assert open_ledger(ledger.path, env=dolt_env).config.presets == ["base"]


@pytest.mark.embedded
def test_the_ledger_copy_is_the_only_source_on_reopen(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """No fallback to ``$TETRAMARK_PRESETS_DIR`` or the bundled presets (P8)."""
    ledger = init_ledger(
        tmp_path / "lab", None, ["base"], author=FACTORY_AUTHOR, env=dolt_env
    )
    (ledger.path / "presets" / "base.yaml").unlink()

    fallback_env = {**dolt_env, "TETRAMARK_PRESETS_DIR": str(REPO_PRESETS)}
    with pytest.raises(LedgerError, match="presets only from"):
        open_ledger(ledger.path, env=fallback_env)


@pytest.mark.fast
def test_adopt_and_init_share_the_same_preset_lookup() -> None:
    """Both setup commands resolve bare names through the same loader (P8/P9)."""
    import inspect

    from tetramark.core.ledger import adopt_ledger

    for function in (tetramark.init_ledger, adopt_ledger):
        parameters = inspect.signature(function).parameters
        assert "presets" in parameters
        assert "env" in parameters
