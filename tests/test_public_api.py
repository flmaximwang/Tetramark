"""P15: the public Python API is ``tetramark.open(path=None, branch=None)``.

The epic's settled decision P15 spells the signature with **two**
positional-or-keyword parameters and no workspace argument.  The signature is
asserted directly (a Dolt-free guard in the fast lane) and then exercised on a
real ledger: opening by path, discovering the ledger upward from a
subdirectory, and selecting a session branch as the second positional
argument.

Dolt-free tests are marked ``fast``; the behaviour tests use the real embedded
driver and are marked ``embedded``.

The bottom of the file pins the package root itself: ``tetramark/__init__.py``
resolves the ledger API lazily (PEP 562) and leaves pydantic, typer and yaml
out of ``sys.modules`` until one of those names is first used.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tetramark
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import UsageError

#: The documented package-level API, in its historical order.
PUBLIC_NAMES = ["Ledger", "__version__", "adopt_ledger", "init_ledger", "open"]

#: The names resolved through PEP 562; ``__version__`` is deliberately eager.
LAZY_NAMES = ("Ledger", "adopt_ledger", "init_ledger", "open")

# ---------------------------------------------------------------------------
# Signature: the public contract (P15), no database needed
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_tetramark_exports_open() -> None:
    """``tetramark.open`` is the public entry point, exported from the package."""
    assert "open" in tetramark.__all__
    assert callable(tetramark.open)
    assert tetramark.open is open_ledger


@pytest.mark.fast
def test_open_signature_is_path_and_branch() -> None:
    """``tetramark.open(path=None, branch=None)`` -- and no workspace argument."""
    signature = inspect.signature(tetramark.open)
    parameters = list(signature.parameters.values())

    assert [parameter.name for parameter in parameters[:2]] == ["path", "branch"]
    path, branch = parameters[0], parameters[1]
    assert path.default is None
    assert branch.default is None
    # P15 spells both as ordinary positional-or-keyword parameters; a caller may
    # write ``tetramark.open(ledger_path, "feature")``.
    assert path.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert branch.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert "workspace" not in signature.parameters


@pytest.mark.fast
def test_open_has_no_workspace_parameter() -> None:
    """A workspace argument is gone: there is no workspace layer in 0.5.0."""
    assert not any(
        "workspace" in name for name in inspect.signature(tetramark.open).parameters
    )


@pytest.mark.fast
def test_aliquots_use_takes_an_optional_amount_and_a_required_message() -> None:
    """``ledger.aliquots.use(ref, amount=None, unit=None, *, message)``."""
    from tetramark.core.objects import AliquotOps

    parameters = inspect.signature(AliquotOps.use).parameters
    assert list(parameters) == ["self", "ref", "amount", "unit", "message"]
    # AMOUNT and UNIT are optional but come together (both default to None).
    assert parameters["amount"].default is None
    assert parameters["unit"].default is None
    # The message is required and keyword-only.
    assert parameters["message"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["message"].default is inspect.Parameter.empty
    # ``purpose``/``note`` are gone for good.
    assert "purpose" not in parameters
    assert "note" not in parameters


@pytest.mark.fast
def test_aliquots_status_takes_an_optional_message_and_no_note() -> None:
    """``ledger.aliquots.status(ref, status, *, message=None, force=False)``."""
    from tetramark.core.objects import AliquotOps

    signature = inspect.signature(AliquotOps.status)
    parameters = signature.parameters
    assert list(parameters) == ["self", "ref", "status", "message", "force"]
    assert parameters["message"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["message"].default is None
    assert parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["force"].default is False
    assert "note" not in parameters


@pytest.mark.fast
def test_aliquots_edit_takes_set_values_and_an_optional_message() -> None:
    """``ledger.aliquots.edit(ref, *, values=(), message=None)``."""
    from tetramark.core.objects import AliquotOps

    signature = inspect.signature(AliquotOps.edit)
    parameters = signature.parameters
    assert list(parameters) == ["self", "ref", "values", "message"]
    # --set fields are the repeated keyword-only VALUES; the reason is optional.
    assert parameters["values"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["values"].default == ()
    assert parameters["message"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["message"].default is None


# ---------------------------------------------------------------------------
# Behaviour on a real ledger
# ---------------------------------------------------------------------------


@pytest.mark.embedded
def test_open_by_path(ledger_factory, dolt_env) -> None:
    """``tetramark.open(ledger.path)`` opens the standalone ledger directory."""
    ledger = ledger_factory(name="public-api-path")

    opened = tetramark.open(ledger.path, env=dolt_env)
    try:
        assert opened.path == ledger.path.resolve()
        assert opened.name == "public-api-path"
    finally:
        opened.close()


@pytest.mark.embedded
def test_open_discovers_the_ledger_upward_from_the_cwd(
    ledger_factory, dolt_env, tmp_path, monkeypatch
) -> None:
    """``tetramark.open()`` walks up from the process cwd to the nearest ledger."""
    ledger = ledger_factory(name="public-api-discovery")
    nested = ledger.path / "docs" / "figures"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    opened = tetramark.open(env=dolt_env)
    try:
        assert opened.path == ledger.path.resolve()
        assert opened.name == "public-api-discovery"
    finally:
        opened.close()


@pytest.mark.embedded
def test_open_takes_the_branch_as_the_second_positional_argument(
    ledger_factory, dolt_env
) -> None:
    """``tetramark.open(path, branch)`` selects a session branch positionally."""
    ledger = ledger_factory(name="public-api-branch")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))

    opened = tetramark.open(ledger.path, "feature", env=dolt_env)
    try:
        assert opened.branch == "feature"
        assert opened.driver.query("SELECT active_branch() AS `branch`") == [
            {"branch": "feature"}
        ]
    finally:
        opened.close()

    with pytest.raises(UsageError, match="does not exist"):
        tetramark.open(ledger.path, "no-such-branch", env=dolt_env)


# ---------------------------------------------------------------------------
# The package root stays import-light: PEP 562 lazy exports, no dependencies
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_all_is_unchanged() -> None:
    assert tetramark.__all__ == PUBLIC_NAMES


@pytest.mark.fast
def test_the_public_names_import_from_the_package_root() -> None:
    """AC3: the five documented names import without touching the ledger first."""
    from tetramark import Ledger, __version__, adopt_ledger, init_ledger, open

    assert Ledger is tetramark.Ledger
    assert init_ledger is tetramark.init_ledger
    assert adopt_ledger is tetramark.adopt_ledger
    assert open is tetramark.open
    assert __version__ == tetramark.__version__
    assert isinstance(__version__, str)
    assert __version__


@pytest.mark.fast
def test_the_lazy_names_are_the_ledger_api() -> None:
    from tetramark.core import ledger as ledger_module

    for name in LAZY_NAMES:
        assert getattr(tetramark, name) is getattr(ledger_module, name)


@pytest.mark.fast
def test_dir_lists_the_public_api() -> None:
    assert set(PUBLIC_NAMES) <= set(dir(tetramark))


@pytest.mark.fast
def test_unknown_attributes_still_raise_attribute_error() -> None:
    with pytest.raises(AttributeError):
        getattr(tetramark, "not_a_public_name")


@pytest.mark.fast
def test_version_is_eager_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """``__version__`` is available even when the ledger API is never touched."""
    code = (
        "import sys\n"
        "import tetramark\n"
        "assert tetramark.__version__\n"
        "assert 'tetramark.core' not in sys.modules\n"
        "print(tetramark.__version__)\n"
    )
    result = _run_python(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == tetramark.__version__


@pytest.mark.fast
def test_import_tetramark_imports_no_third_party_dependencies(
    tmp_path: Path,
) -> None:
    """AC3: a fresh ``import tetramark`` has not loaded pydantic, typer or yaml."""
    code = (
        "import sys\n"
        "import tetramark\n"
        "banned = [name for name in ('pydantic', 'typer', 'yaml') "
        "if name in sys.modules]\n"
        "if banned:\n"
        "    raise SystemExit('imported: ' + ', '.join(banned))\n"
        "print(tetramark.__version__)\n"
    )
    result = _run_python(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == tetramark.__version__


def _run_python(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh interpreter without an inherited ``PYTHONPATH``."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.embedded
def test_open_exposes_the_object_facades(ledger_factory, dolt_env) -> None:
    """The opened ledger is the public API: every object type has a facade."""
    ledger = ledger_factory(name="public-api-facades")

    opened = tetramark.open(ledger.path, env=dolt_env)
    try:
        item = opened.entities.add("item", name="alpha")
        stock = opened.stocks.add("lot", entity=item["id"])
        aliquot = stock["aliquots"][0]
        opened.boxes.add("grid96", "G-1")
        opened.aliquots.move(aliquot, box="G-1", slot="A1", message="moved")

        assert opened.entities.show(item["id"])["id"] == item["id"]
        assert opened.stocks.show(stock["id"])["id"] == stock["id"]
        assert opened.aliquots.show(aliquot)["id"] == aliquot
        assert [row["id"] for row in opened.aliquots.ls()] == [aliquot]
        assert [row["id"] for row in opened.aliquots.ls(stock["id"])] == [aliquot]
        assert opened.aliquots.ls(stock["id"], status=["missing"]) == []
    finally:
        opened.close()
