"""P15: the public Python API is ``tetramark.open(path=None, branch=None)``.

The epic's settled decision P15 spells the signature with **two**
positional-or-keyword parameters and no workspace argument.  The signature is
asserted directly (a Dolt-free guard in the fast lane) and then exercised on a
real ledger: opening by path, discovering the ledger upward from a
subdirectory, and selecting a session branch as the second positional
argument.

Dolt-free tests are marked ``fast``; the behaviour tests use the real embedded
driver and are marked ``embedded``.
"""

from __future__ import annotations

import inspect

import pytest

import tetramark
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import UsageError

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
