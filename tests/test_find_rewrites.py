"""``ledger.yaml`` ``find.rewrites`` and ``tm find --with-rewrite`` (53r.1).

Rules turn a handwritten handle that was never stored (``old-alpha``) into the
stored one (``alpha``).  They belong to the ledger, are validated at open, use
the Python ``re`` dialect and only ever rewrite a *literal* ``tm find QUERY``:
never ``-E``, never a ``--where`` pattern, never a single-object ref.  The
default is a zero-hit fallback; ``--with-rewrite`` runs every rule next to the
original query and ranks the original hits first.

Everything runs against the real ``dolt`` binary, like the rest of the suite.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import Ledger, open as open_ledger
from tetramark.errors import LedgerError
from tetramark.ops.find import resolve_any
from tetramark.ops.lineage import lineage
from tetramark.schema.model import Rewrite, SchemaError

BASE = "base"

#: ``old-<x>`` -> ``<x>``: the type-code / suffix rule of the spec.
STRIP_OLD = {
    "name": "strip-old",
    "pattern": r"^old-(?P<rest>.+)$",
    "replacement": r"\g<rest>",
}
#: ``<x>-tube`` -> ``<x>-01``: a second, independent rule.
TUBE_SUFFIX = {
    "name": "tube-suffix",
    "pattern": r"^(?P<base>.+)-tube$",
    "replacement": r"\g<base>-01",
}
#: ``demo-<x>`` -> ``<x>``: matches the --with-rewrite examples.
DEMO_PREFIX = {
    "name": "prefix",
    "pattern": r"^demo-(?P<rest>.+)$",
    "replacement": r"\g<rest>",
}


def write_rewrites(ledger: Ledger, rules: list[dict[str, object]]) -> None:
    """Put ``find.rewrites: rules`` into a ledger's ``ledger.yaml``."""
    path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["find"] = {"rewrites": rules}
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


@pytest.fixture
def rewrite_factory(ledger_factory, dolt_env):
    """Build a real base ledger whose ``ledger.yaml`` carries rewrite rules."""
    created: list[Ledger] = []

    def build(rules: list[dict[str, object]], *, name: str = "rewrites") -> Ledger:
        ledger = ledger_factory([BASE], name=name)
        write_rewrites(ledger, rules)
        reopened = open_ledger(ledger.path, env=dolt_env)
        created.append(reopened)
        return reopened

    yield build
    for ledger in created:
        ledger.close()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def add_item(ledger: Ledger, name: str, **values: object) -> dict:
    return ledger.entities.add("item", name=name, values=values)


def invoke(ledger: Ledger, *args: str):
    return CliRunner().invoke(
        app, ["--ledger", str(ledger.path), "find", *args]
    )


def ids(rows: list[dict]) -> list[str]:
    return [row["id"] for row in rows]


# ---------------------------------------------------------------------------
# Config: ledger.yaml find.rewrites
# ---------------------------------------------------------------------------


def test_valid_rewrites_load_at_open(rewrite_factory) -> None:
    """A valid find: section is part of the opened ledger's config."""
    ledger = rewrite_factory([STRIP_OLD, TUBE_SUFFIX])

    assert [rule.name for rule in ledger.config.find.rewrites] == [
        "strip-old",
        "tube-suffix",
    ]
    assert ledger.config.find.rewrites_for("old-alpha") == [
        Rewrite(rule="strip-old", query="alpha")
    ]
    # A non-matching rule is not a candidate; a rule that reproduces the query
    # is not either.
    assert ledger.config.find.rewrites_for("alpha") == []


def test_ledger_without_find_is_unchanged(ledger_factory) -> None:
    """Omitting find: keeps the old behaviour (no rules, plain matches)."""
    ledger = ledger_factory([BASE], name="no-find")
    item = add_item(ledger, "alpha")

    assert ledger.config.find.rewrites == []
    assert ledger.config.find.rewrites_for("old-alpha") == []
    hits = ledger.find("alpha")
    assert ids(hits) == [item["id"]]
    assert hits[0]["match"] == "name"
    assert "rewrite" not in hits[0]


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param(
            [
                {
                    "name": "bad-pattern",
                    "pattern": "(abc",
                    "replacement": "x",
                }
            ],
            r"find\.rewrites\[0\] \('bad-pattern'\): invalid pattern",
            id="invalid-pattern",
        ),
        pytest.param(
            [
                {
                    "name": "bad-group",
                    "pattern": r"^(a)$",
                    "replacement": r"\2",
                }
            ],
            r"find\.rewrites\[0\] \('bad-group'\): invalid replacement .*group",
            id="bad-backreference",
        ),
        pytest.param(
            [{"pattern": r"^x$", "replacement": "y"}],
            r"find\.rewrites\[0\]: every rewrite rule needs a name",
            id="missing-name",
        ),
        pytest.param(
            [
                {"name": "dup", "pattern": r"^a$", "replacement": "b"},
                {"name": "dup", "pattern": r"^c$", "replacement": "d"},
            ],
            r"find\.rewrites\[1\] \('dup'\): duplicate rewrite name",
            id="duplicate-name",
        ),
        pytest.param(
            [],
            r"find\.rewrites must not be empty",
            id="empty-rewrites",
        ),
    ],
)
def test_invalid_rewrites_are_refused_at_open_with_the_rule_named(
    ledger_factory, dolt_env, rules, expected
) -> None:
    """Every bad rule names itself and the ledger.yaml it came from."""
    ledger = ledger_factory([BASE], name="bad-rewrites")
    write_rewrites(ledger, rules)

    with pytest.raises(SchemaError) as caught:
        open_ledger(ledger.path, env=dolt_env)

    message = str(caught.value)
    assert "ledger.yaml" in message
    assert re.search(expected, message), message


def test_unknown_find_key_is_refused(ledger_factory, dolt_env) -> None:
    """find: is strict like the rest of ledger.yaml (extra keys are refused)."""
    ledger = ledger_factory([BASE], name="typo-rewrites")
    path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["find"] = {"rewrite": [STRIP_OLD]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(SchemaError, match="Extra inputs are not permitted"):
        open_ledger(ledger.path, env=dolt_env)


# ---------------------------------------------------------------------------
# Default fallback
# ---------------------------------------------------------------------------


def test_zero_hit_fallback_uses_the_first_productive_rule(rewrite_factory) -> None:
    """Every rule is tried, but only the first one with hits is shown."""
    ledger = rewrite_factory(
        [
            STRIP_OLD,
            {
                "name": "the-other",
                "pattern": r"^old-(?P<rest>.+)$",
                "replacement": "beta",
            },
        ]
    )
    alpha = add_item(ledger, "alpha")
    add_item(ledger, "beta")

    hits = ledger.find("old-alpha")

    assert ids(hits) == [alpha["id"]]
    assert [row["rank"] for row in hits] == [1]
    assert hits[0]["match"] == "name via strip-old"
    assert hits[0]["rewrite"] == {"rule": "strip-old", "query": "alpha"}


def test_fallback_is_only_a_zero_hit_fallback(rewrite_factory) -> None:
    """A query with hits never runs the rules (that is what --with-rewrite is)."""
    ledger = rewrite_factory([DEMO_PREFIX])
    demo = add_item(ledger, "demo-alpha")
    alpha = add_item(ledger, "alpha")

    hits = ledger.find("demo-alpha")

    assert ids(hits) == [demo["id"]]
    assert "rewrite" not in hits[0]
    # The rule would have matched, and --with-rewrite does apply it.
    assert ids(ledger.find("demo-alpha", with_rewrite=True)) == [
        demo["id"],
        alpha["id"],
    ]


def test_rules_that_do_not_match_or_reproduce_the_query_are_skipped(
    rewrite_factory,
) -> None:
    """A no-op or non-matching rule is silent: no hits, no hint, no error."""
    ledger = rewrite_factory(
        [
            {"name": "identity", "pattern": r"^demo-(.*)$", "replacement": r"demo-\1"},
            {"name": "elsewhere", "pattern": r"^zzz$", "replacement": "no-such-object"},
        ]
    )
    demo = add_item(ledger, "demo-alpha")

    assert ledger.config.find.rewrites_for("demo-alpha") == []
    hits = ledger.find("demo-alpha")
    assert ids(hits) == [demo["id"]]
    assert hits[0]["match"] == "name"
    # --with-rewrite has nothing to add: both rules are skipped.
    assert ids(ledger.find("demo-alpha", with_rewrite=True)) == [demo["id"]]

    assert ledger.find("zzz") == []
    assert ledger.find("zzz", with_rewrite=True) == []


def test_rewrite_rules_never_chain(rewrite_factory) -> None:
    """Rule two only sees the original query, never rule one's output."""
    ledger = rewrite_factory(
        [
            {"name": "one", "pattern": r"^a-(.*)$", "replacement": r"b-\1"},
            {"name": "two", "pattern": r"^b-(.*)$", "replacement": r"c-\1"},
        ]
    )
    add_item(ledger, "c-x")

    # ``one`` produces ``b-x`` (a miss) and ``two`` never sees ``b-x``.
    assert ledger.find("a-x") == []
    assert ledger.find("a-x", with_rewrite=True) == []


# ---------------------------------------------------------------------------
# --with-rewrite
# ---------------------------------------------------------------------------


def test_with_rewrite_orders_original_then_rules_then_tier(rewrite_factory) -> None:
    """Originals first, then rule order; inside a rule the tier order holds."""
    ledger = rewrite_factory(
        [
            DEMO_PREFIX,
            {"name": "alias", "pattern": r"^demo-(?P<rest>.+)$", "replacement": "ALP"},
        ]
    )
    demo = add_item(ledger, "demo-alpha")
    ledger.aliases.add("entity", demo["id"], "alpha")
    alpha = add_item(ledger, "alpha")
    alphabet = add_item(ledger, "alphabet")
    alias_holder = add_item(ledger, "alias-holder")
    ledger.aliases.add("entity", alias_holder["id"], "ALP")

    hits = ledger.find("demo-alpha", with_rewrite=True)

    # The original hit keeps its entry even though the ``prefix`` rewrite also
    # hits it through the alias; rule hits follow, the ``alias`` hit last
    # because it comes from the second rule (tier order is inside a rule only).
    assert ids(hits) == [
        demo["id"],
        alpha["id"],
        alphabet["id"],
        alias_holder["id"],
    ]
    assert [row["rank"] for row in hits] == [1, 2, 3, 4]
    assert [row["match"] for row in hits] == [
        "name",
        "name via prefix",
        "name via prefix",
        "alias via alias",
    ]
    assert "rewrite" not in hits[0]
    assert hits[1]["rewrite"] == {"rule": "prefix", "query": "alpha"}
    # The row keeps the tier score it matched with; the rank is the merge order.
    assert [row["score"] for row in hits] == [800, 800, 700, 900]


def test_with_rewrite_keeps_one_entry_per_object(rewrite_factory) -> None:
    """Two rules finding the same object append it once, under the first rule."""
    ledger = rewrite_factory(
        [
            {"name": "first", "pattern": r"^green-(.+)$", "replacement": r"\1"},
            {"name": "second", "pattern": r"^green-(.+)$", "replacement": "ALP"},
        ]
    )
    holder = add_item(ledger, "alias-holder")
    ledger.aliases.add("entity", holder["id"], "ALP")
    target = add_item(ledger, "alpha")

    hits = ledger.find("green-alpha", with_rewrite=True)

    assert ids(hits) == [target["id"], holder["id"]]
    assert [row["match"] for row in hits] == [
        "name via first",
        "alias via second",
    ]
    assert [row["rank"] for row in hits] == [1, 2]


def test_with_rewrite_skips_duplicates_but_keeps_original_labels(
    rewrite_factory,
) -> None:
    """An object found by the original query keeps its unannotated entry."""
    ledger = rewrite_factory([DEMO_PREFIX])
    demo = add_item(ledger, "demo-alpha")
    ledger.aliases.add("entity", demo["id"], "alpha")

    hits = ledger.find("demo-alpha", with_rewrite=True)

    assert ids(hits) == [demo["id"]]
    assert hits[0]["match"] == "name"
    assert "rewrite" not in hits[0]


# ---------------------------------------------------------------------------
# --where, -E and JSON/table annotation
# ---------------------------------------------------------------------------


def test_where_filters_still_narrow_rewrite_hits(rewrite_factory) -> None:
    """Filters apply to rewrite hits; they are never rewritten themselves."""
    ledger = rewrite_factory([STRIP_OLD])
    keep = add_item(ledger, "alpha", description="keep")
    add_item(ledger, "alphabet", description="drop")

    hits = ledger.find("old-alpha", where=["description=keep"])

    assert ids(hits) == [keep["id"]]
    assert hits[0]["match"] == "name via strip-old"
    assert hits[0]["rewrite"] == {"rule": "strip-old", "query": "alpha"}

    # The same narrowing applies to --with-rewrite.
    with_hits = ledger.find(
        "old-alpha", where=["description=keep"], with_rewrite=True
    )
    assert ids(with_hits) == [keep["id"]]


def test_regex_queries_never_rewrite(rewrite_factory) -> None:
    """-E is its own dialect: no fallback, no --with-rewrite, no 'also tried'."""
    ledger = rewrite_factory([STRIP_OLD])
    alpha = add_item(ledger, "alpha")

    assert ledger.find("old-alpha", regex=True) == []
    assert ledger.find("old-alpha", regex=True, with_rewrite=True) == []
    # -E still matches what it always matched.
    assert ids(ledger.find(r"^alp", regex=True)) == [alpha["id"]]


def test_cli_fallback_hint_and_annotations(cli_env, rewrite_factory) -> None:
    """The stderr hint names the applied rule; table and JSON carry the label."""
    ledger = rewrite_factory([STRIP_OLD])
    alpha = add_item(ledger, "alpha")

    result = invoke(ledger, "old-alpha")
    assert result.exit_code == 0, result.output
    assert result.stderr.strip() == (
        "no match for 'old-alpha'; showing matches for 'alpha' (rule 'strip-old')"
    )
    assert "name via strip-old" in result.stdout

    as_json = invoke(ledger, "old-alpha", "--json")
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload[0]["id"] == alpha["id"]
    assert payload[0]["match"] == "name via strip-old"
    assert payload[0]["rewrite"] == {"rule": "strip-old", "query": "alpha"}
    assert as_json.stderr.strip() == result.stderr.strip()

    # --with-rewrite never prints the fallback hint.
    with_rewrite = invoke(ledger, "old-alpha", "--with-rewrite", "--json")
    assert with_rewrite.exit_code == 0, with_rewrite.output
    assert with_rewrite.stderr == ""


def test_cli_no_match_lists_the_tried_rewrites(cli_env, rewrite_factory) -> None:
    """Nothing found: exit 1 and name every rewrite that was tried."""
    ledger = rewrite_factory([STRIP_OLD, TUBE_SUFFIX])

    result = invoke(ledger, "old-zzz-tube")

    assert result.exit_code == 1
    assert isinstance(result.exception, LedgerError)
    assert str(result.exception) == (
        "no match for 'old-zzz-tube' (also tried: 'zzz-tube' via strip-old, "
        "'old-zzz-01' via tube-suffix)"
    )
    assert result.exception.exit_code == 1


# ---------------------------------------------------------------------------
# Single-object resolution stays exact and points at tm find
# ---------------------------------------------------------------------------


def test_single_object_resolution_never_rewrites_and_hints_find(
    rewrite_factory,
) -> None:
    """show/history/lineage/alias/--entity keep exact refs; the error hints."""
    ledger = rewrite_factory([STRIP_OLD])
    alpha = add_item(ledger, "alpha")

    # The rewrite works for find...
    assert ids(ledger.find("old-alpha")) == [alpha["id"]]

    # ...but never for a single-object resolver.
    with pytest.raises(LedgerError) as show_error:
        ledger.entities.show("old-alpha")
    assert str(show_error.value) == (
        "unknown entity 'old-alpha'; try tm find old-alpha"
    )

    with pytest.raises(LedgerError) as box_error:
        ledger.boxes.show("old-box")
    assert str(box_error.value) == "unknown box 'old-box'; try tm find old-box"

    with pytest.raises(LedgerError) as history_error:
        ledger.history("old-alpha")
    assert str(history_error.value) == (
        "unknown object 'old-alpha' (no such id, box name or alias); "
        "try tm find old-alpha"
    )

    with pytest.raises(LedgerError) as lineage_error:
        lineage(ledger, "old-alpha")
    assert str(lineage_error.value) == str(history_error.value)

    with pytest.raises(LedgerError) as resolve_error:
        resolve_any(ledger, "old-alpha")
    assert str(resolve_error.value) == str(history_error.value)

    # ``tm alias add`` resolves its target exactly.
    with pytest.raises(LedgerError) as alias_error:
        ledger.aliases.add("entity", "old-alpha", "X")
    assert str(alias_error.value) == (
        "unknown entity 'old-alpha'; try tm find old-alpha"
    )

    # ``tm stock add --entity`` resolves its entity exactly.
    with pytest.raises(LedgerError) as entity_error:
        ledger.stocks.add("lot", entity="old-alpha")
    assert str(entity_error.value) == (
        "unknown entity 'old-alpha'; try tm find old-alpha"
    )


def test_show_keeps_the_wrong_type_hint_instead_of_the_find_hint(
    rewrite_factory,
) -> None:
    """A ref that resolved to the wrong type gets the precise show verb only."""
    ledger = rewrite_factory([STRIP_OLD])
    holder = add_item(ledger, "holder")
    stock = ledger.stocks.add(
        "lot", entity=holder["id"], values={"supplier": "acme"}
    )

    with pytest.raises(LedgerError) as caught:
        ledger.aliquots.show(stock["id"])

    message = str(caught.value)
    assert "is a stock; try tm stock show" in message
    assert "try tm find" not in message
