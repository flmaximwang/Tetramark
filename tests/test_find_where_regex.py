"""``tm find -E`` regex queries and ``--where`` filters (Tetramark-6ms.2).

Two features on top of the literal search, both backed by the shared filter
module (Tetramark-6ms.1):

* ``-E``/``--regex`` makes the query a SQL (ICU) regular expression matched
  case-insensitively over ids, aliases, names and kind ``string``/``text``
  fields, with the flat tiers id > alias > name > field;
* ``--where``/``-w`` applies the listing filters to every hit; with no query it
  lists every matching object (``match == "filter"``).  Aliquots are excluded
  whenever a filter is given, and only kinds having every filtered field
  contribute.

Everything runs against the real ``dolt`` binary, like the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import LedgerError, UsageError
from tetramark.ops import find as find_module
from tetramark.ops.find import find, search

from conftest import PRESETS

FULL = [str(PRESETS / "full")]


@pytest.fixture
def regex_ledger(ledger_factory):
    """A full-preset ledger whose datecode stock ids start with ``pUC``."""
    return ledger_factory(FULL, name="find-regex", id_prefix="pUC")


@pytest.fixture
def where_ledger(ledger_factory):
    """A full-preset ledger with every entity/stock/box kind available."""
    return ledger_factory(FULL, name="find-where")


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def run_cli(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


# ---------------------------------------------------------------------------
# -E/--regex
# ---------------------------------------------------------------------------


def test_regex_search_ranks_id_alias_name_field(regex_ledger) -> None:
    ledger = regex_ledger
    host = ledger.entities.add("item", name="host")
    # ``aliquots=0`` keeps the stock id the only id-tier hit.
    stock = ledger.stocks.add(
        "lot", entity=host["id"], values={"supplier": "acme"}, aliquots=0
    )
    by_alias = ledger.entities.add("item", name="alias-holder")
    ledger.aliases.add("entity", by_alias["id"], "pUC18")
    by_name = ledger.entities.add("plasmid", name="pUC57")
    by_field = ledger.entities.add(
        "item", name="field-holder", values={"description": "pUC9 vector"}
    )

    hits = ledger.find(r"^pUC\d+", regex=True)

    assert [row["match"] for row in hits] == ["id", "alias", "name", "field"]
    assert [row["id"] for row in hits] == [
        stock["id"],
        by_alias["id"],
        by_name["id"],
        by_field["id"],
    ]
    assert [row["score"] for row in hits] == [1000, 900, 800, 500]
    assert [row["rank"] for row in hits] == [1, 2, 3, 4]

    # The regex is case-insensitive: the same lowercase pattern matches the
    # mixed-case ids, aliases and names above.
    assert [row["id"] for row in ledger.find(r"^puc\d+", regex=True)] == [
        row["id"] for row in hits
    ]


def test_regex_search_covers_aliquot_ids(regex_ledger) -> None:
    ledger = regex_ledger
    host = ledger.entities.add("item", name="host")
    stock = ledger.stocks.add("lot", entity=host["id"], values={"supplier": "acme"})
    aliquot = stock["aliquots"][0]

    hits = ledger.find(r"\.1$", regex=True)

    assert [row["id"] for row in hits] == [aliquot]
    assert hits[0]["match"] == "id"
    assert hits[0]["object"] == "aliquot"


def test_regex_search_covers_aliases_of_every_object_type(regex_ledger) -> None:
    ledger = regex_ledger
    item = ledger.entities.add("item", name="host")
    stock = ledger.stocks.add("lot", entity=item["id"], values={"supplier": "acme"})
    box = ledger.boxes.add("shelf", "SHELF")
    objects = [
        ("entity", item["id"]),
        ("stock", stock["id"]),
        ("aliquot", stock["aliquots"][0]),
        ("box", box["id"]),
    ]
    for index, (object_type, object_id) in enumerate(objects):
        ledger.aliases.add(object_type, object_id, f"tagged-{index}")

    hits = ledger.find(r"^TAGGED-\d$", regex=True)

    # One alias tier, then the usual object-type order for the tie.
    assert [row["match"] for row in hits] == ["alias"] * 4
    assert [row["object"] for row in hits] == ["entity", "stock", "aliquot", "box"]
    assert [row["id"] for row in hits] == [object_id for _, object_id in objects]


def test_an_invalid_regex_is_a_usage_error_without_a_fallback(regex_ledger) -> None:
    ledger = regex_ledger
    ledger.entities.add("item", name="pUC19")

    with pytest.raises(UsageError) as error:
        ledger.find("pUC(", regex=True)

    message = str(error.value)
    assert message.startswith("invalid regex 'pUC('")
    assert "not a valid regular expression" in message

    # The same text without -E is still the literal search and matches nothing.
    assert ledger.find("pUC(") == []


def test_the_literal_search_is_unchanged(regex_ledger) -> None:
    ledger = regex_ledger
    underscore = ledger.entities.add("item", name="under_score")
    ledger.entities.add("item", name="underXscore")

    # ``_`` is a literal character, not a regex wildcard, and the search is
    # still case-insensitive.
    assert [row["id"] for row in ledger.find("under_")] == [underscore["id"]]
    assert [row["id"] for row in ledger.find("UNDER_SCORE")] == [underscore["id"]]


# ---------------------------------------------------------------------------
# --where
# ---------------------------------------------------------------------------


def test_where_filters_query_hits(where_ledger) -> None:
    ledger = where_ledger
    plasmid = ledger.entities.add("plasmid", name="pUC19")
    item = ledger.entities.add("item", name="pUC19-item")
    ledger.aliases.add("entity", item["id"], "pUC19-ALIAS")

    hits = ledger.find("pUC19", where=["kind=plasmid"])

    # The item matches the query by name prefix and by alias, but not the
    # filter: only the plasmid contributes.
    assert [row["id"] for row in hits] == [plasmid["id"]]
    assert hits[0]["match"] == "name"

    assert ledger.find("pUC19-ALIAS", where=["kind=plasmid"]) == []
    assert ledger.find("pUC19-ALIAS")[0]["id"] == item["id"]

    # A regex query is filtered the same way.
    assert [
        row["id"]
        for row in ledger.find(r"^pUC\d+$", regex=True, where=["kind=plasmid"])
    ] == [plasmid["id"]]


def test_where_keeps_positive_id_and_alias_hits(where_ledger) -> None:
    ledger = where_ledger
    host = ledger.entities.add("item", name="host")
    stock = ledger.stocks.add(
        "lot", entity=host["id"], values={"supplier": "acme"}
    )
    plasmid = ledger.entities.add("plasmid", name="pUC19")
    ledger.aliases.add("entity", plasmid["id"], "pUC-ALIAS")

    # A positive id hit survives the filter and keeps the id tier.
    by_id = ledger.find(stock["id"], where=["kind=lot"])
    assert [row["id"] for row in by_id] == [stock["id"]]
    assert [row["match"] for row in by_id] == ["id"]
    assert [row["score"] for row in by_id] == [1000]
    assert [row["rank"] for row in by_id] == [1]

    # So does a positive alias hit, at the alias tier.
    by_alias = ledger.find("pUC-ALIAS", where=["kind=plasmid"])
    assert [row["id"] for row in by_alias] == [plasmid["id"]]
    assert [row["match"] for row in by_alias] == ["alias"]
    assert [row["score"] for row in by_alias] == [900]
    assert [row["rank"] for row in by_alias] == [1]


def test_where_filters_on_a_stock_kind_field(where_ledger) -> None:
    ledger = where_ledger
    item = ledger.entities.add("item", name="host")
    acme = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "acme"}
    )
    ledger.stocks.add("lot", entity=item["id"], values={"supplier": "other"})

    hits = ledger.find(where=["supplier=acme"])

    assert [row["id"] for row in hits] == [acme["id"]]
    assert hits[0]["object"] == "stock"
    assert hits[0]["kind"] == "lot"
    assert hits[0]["match"] == "filter"

    # A kind field composes with a query and with a regex filter as well.
    assert [
        row["id"] for row in ledger.find("acme", where=["supplier~^ac"])
    ] == [acme["id"]]


def test_where_alone_lists_every_matching_object(where_ledger) -> None:
    ledger = where_ledger
    first = ledger.entities.add("item", name="pUC19")
    second = ledger.entities.add("plasmid", name="pUC57")
    ledger.entities.add("item", name="alpha")
    box = ledger.boxes.add("shelf", "pUC-box")

    hits = ledger.find(where=["name~^pUC"])

    # Sorted by object type, kind, id; every hit says it came from the filter.
    assert [row["match"] for row in hits] == ["filter"] * 3
    assert [row["score"] for row in hits] == [0, 0, 0]
    assert [row["rank"] for row in hits] == [1, 2, 3]
    assert [row["object"] for row in hits] == ["entity", "entity", "box"]
    assert [row["kind"] for row in hits] == ["item", "plasmid", "shelf"]
    assert [row["id"] for row in hits] == [first["id"], second["id"], box["id"]]
    # The usual enrichment still applies.
    assert hits[0]["summary"].startswith("item pUC19")
    assert hits[2]["name"] == "pUC-box"


def test_where_excludes_aliquots(where_ledger) -> None:
    ledger = where_ledger
    item = ledger.entities.add("item", name="host")
    stock = ledger.stocks.add("lot", entity=item["id"], values={"supplier": "acme"})
    aliquot = stock["aliquots"][0]
    ledger.aliases.add("aliquot", aliquot, "pUC-tube")

    # Without a filter the aliquot resolves by id and by alias ...
    assert ledger.find(aliquot)[0]["object"] == "aliquot"
    assert ledger.find("pUC-tube")[0]["object"] == "aliquot"
    # ... but --where only ever considers entity/stock/box objects.
    assert ledger.find(aliquot, where=["kind~."]) == []
    assert ledger.find("pUC-tube", where=["kind~."]) == []


def test_a_where_field_no_kind_has_contributes_nothing(where_ledger) -> None:
    ledger = where_ledger
    ledger.entities.add("item", name="alpha", values={"description": "text"})
    ledger.entities.add("plasmid", name="beta")

    # ``description`` is an item field and ``sequence`` a plasmid field: no
    # entity/stock/box kind has both, so nothing matches.
    assert ledger.find(where=["description~.", "sequence~."]) == []
    assert ledger.find("alpha", where=["description~.", "sequence~."]) == []

    # An invalid pattern is still reported instead of silently matching none.
    with pytest.raises(UsageError) as error:
        ledger.find(where=["description~[", "sequence~."])
    assert "description~[" in str(error.value)


def test_an_unknown_where_field_lists_the_valid_ones(where_ledger) -> None:
    ledger = where_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError) as error:
        ledger.find("alpha", where=["bogus~x"])

    message = str(error.value)
    assert "unknown field(s) 'bogus'" in message
    assert "valid fields" in message
    # Core columns and fields of every entity/stock/box kind are listed.
    for name in ("name", "kind", "entity_id", "sequence", "supplier", "batch"):
        assert name in message


def test_where_uses_the_shared_filter_syntax_and_errors(where_ledger) -> None:
    ledger = where_ledger
    alpha = ledger.entities.add("item", name="alpha")

    # A mapping is equality only, exactly like the listings.
    assert [row["id"] for row in ledger.find(where={"name": "alpha"})] == [
        alpha["id"]
    ]

    with pytest.raises(UsageError, match="appears twice"):
        ledger.find(where=["name=alpha", "name=beta"])
    with pytest.raises(UsageError, match="both"):
        ledger.find(where=["name=alpha", "name~beta"])
    with pytest.raises(UsageError, match="must not be empty"):
        ledger.find(where=["name~"])

    # An invalid pattern names the filter to fix, not a raw driver error.
    with pytest.raises(UsageError) as error:
        ledger.find("alpha", where=["name~["])
    message = str(error.value)
    assert "name~[" in message
    assert "--where" in message


def test_neither_a_query_nor_a_filter_is_a_usage_error(where_ledger) -> None:
    ledger = where_ledger

    with pytest.raises(UsageError, match="--where"):
        ledger.find()
    with pytest.raises(UsageError, match="must not be empty"):
        ledger.find("")
    with pytest.raises(UsageError, match="--where"):
        ledger.find(where=[])


# ---------------------------------------------------------------------------
# The other --where operators (Tetramark-d0x.1)
# ---------------------------------------------------------------------------


def seed_operator_rows(ledger) -> dict[str, str]:
    """Create the rows the ``!=``/``!~``/ordered find tests read.

    ``pUC19``, ``pUC57`` and ``pUC-box`` are the query hits; the two
    ``everything`` rows carry the ``count``/``legacy`` values the filters
    address, and the ``prep`` stock gives the search an aliquot to exclude.
    """
    puc19 = ledger.entities.add("plasmid", name="pUC19")
    puc57 = ledger.entities.add("item", name="pUC57")
    box = ledger.boxes.add("shelf", "pUC-box")
    vector = ledger.entities.add(
        "everything",
        name="vector",
        values={"code": "AAA", "count": 1, "legacy": "L1"},
    )
    carrier = ledger.entities.add(
        "everything",
        name="pUC-vec",
        values={"code": "BBB", "count": 5, "legacy": "M1"},
    )
    stock = ledger.stocks.add(
        "prep", entity=carrier["id"], values={"batch": "pUC-batch"}
    )
    aliquot = stock["aliquots"][0]
    ledger.aliases.add("aliquot", aliquot, "pUC-tube")
    created = {
        "puc19": puc19["id"],
        "puc57": puc57["id"],
        "box": box["id"],
        "vector": vector["id"],
        "carrier": carrier["id"],
        "stock": stock["id"],
        "aliquot": aliquot,
    }
    return {name: str(value) for name, value in created.items()}


def test_where_filters_with_the_negated_and_ordered_operators(where_ledger) -> None:
    """``!=``, ``!~`` and an ordered comparison narrow the find hits."""
    ledger = where_ledger
    rows = seed_operator_rows(ledger)

    # ``!=`` is the complement of ``=``: the filter-only listing keeps every
    # entity and box whose name differs (stock kinds have no name column).
    not_equals = ledger.find(where=["name!=pUC19"])
    assert {row["id"] for row in not_equals} == {
        rows["puc57"],
        rows["vector"],
        rows["carrier"],
        rows["box"],
    }
    assert {row["match"] for row in not_equals} == {"filter"}
    assert {row["object"] for row in not_equals} == {"entity", "box"}

    # ``!~`` is the complement of ``~``: only the everything kind declares
    # ``legacy``, and the row whose value starts with L stays out.
    negated = ledger.find(where=["legacy!~^L"])
    assert [row["id"] for row in negated] == [rows["carrier"]]
    assert negated[0]["match"] == "filter"

    # An ordered comparison keeps the rows whose value is in range.
    ordered = ledger.find(where=["count>=4"])
    assert [row["id"] for row in ordered] == [rows["carrier"]]
    assert ordered[0]["match"] == "filter"

    # The same filters narrow a QUERY's hits instead of listing everything.
    narrowed = ledger.find("pUC", where=["name!=pUC19"])
    assert {row["id"] for row in narrowed} == {
        rows["puc57"],
        rows["box"],
        rows["carrier"],
    }
    assert {row["match"] for row in narrowed} == {"name"}

    ordered_query = ledger.find("pUC", where=["count>=4"])
    assert [row["id"] for row in ordered_query] == [rows["carrier"]]
    assert ordered_query[0]["match"] == "name"

    negated_query = ledger.find("pUC", where=["legacy!~^L"])
    assert [row["id"] for row in negated_query] == [rows["carrier"]]

    # An invalid ``!~`` pattern is the shared usage error, like ``~``.
    with pytest.raises(UsageError) as error:
        ledger.find("pUC", where=["name!~["])
    assert "invalid regular expression in the --where filter 'name!~['" in str(
        error.value
    )

    # Aliquots stay excluded whenever a filter is given, even with the new
    # operators: the filter-only listing keeps the one matching stock and
    # never lists its tube; without a filter the id and alias still resolve.
    listed = ledger.find(where=["batch!=zzz"])
    assert [row["object"] for row in listed] == ["stock"]
    assert [row["id"] for row in listed] == [rows["stock"]]
    assert ledger.find(rows["aliquot"], where=["batch!=zzz"]) == []
    assert ledger.find("pUC-tube", where=["batch!=zzz"]) == []
    assert ledger.find(rows["aliquot"])[0]["object"] == "aliquot"
    assert ledger.find("pUC-tube")[0]["object"] == "aliquot"


def test_cli_where_with_the_negated_and_ordered_operators(
    cli_env: None, where_ledger
) -> None:
    """The CLI passes ``!=``, ``!~`` and ordered comparisons to the search."""
    ledger = where_ledger
    rows = seed_operator_rows(ledger)

    listed = run_cli(ledger, "find", "--where", "count>=4", "--json")
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert [row["id"] for row in listed_payload] == [rows["carrier"]]
    assert listed_payload[0]["match"] == "filter"
    assert listed_payload[0]["score"] == 0
    assert listed_payload[0]["rank"] == 1

    narrowed = run_cli(ledger, "find", "pUC", "--where", "name!=pUC19", "--json")
    assert narrowed.exit_code == 0, narrowed.output
    narrowed_payload = json.loads(narrowed.output)
    assert {row["id"] for row in narrowed_payload} == {
        rows["puc57"],
        rows["box"],
        rows["carrier"],
    }
    assert {row["match"] for row in narrowed_payload} == {"name"}

    negated = run_cli(ledger, "find", "pUC", "--where", "legacy!~^L", "--json")
    assert negated.exit_code == 0, negated.output
    assert [row["id"] for row in json.loads(negated.output)] == [rows["carrier"]]

    batches = run_cli(ledger, "find", "--where", "batch!=zzz", "--json")
    assert batches.exit_code == 0, batches.output
    batches_payload = json.loads(batches.output)
    assert [row["object"] for row in batches_payload] == ["stock"]
    assert [row["id"] for row in batches_payload] == [rows["stock"]]

    # A filter still excludes the aliquot: its id and alias match the query,
    # but no entity/stock/box object does, so the CLI reports a miss.
    for query in (rows["aliquot"], "pUC-tube"):
        excluded = run_cli(ledger, "find", query, "--where", "batch!=zzz")
        assert isinstance(excluded.exception, LedgerError)
        assert "no match" in str(excluded.exception)


# ---------------------------------------------------------------------------
# The search() seam (Tetramark-53r.1)
# ---------------------------------------------------------------------------


def test_search_is_the_pure_one_pass_seam(where_ledger) -> None:
    """``search`` is one unranked pass with no rewrite fallback of its own."""
    ledger = where_ledger
    plasmid = ledger.entities.add("plasmid", name="pUC19")

    hits = search(ledger, r"^puc\d+$", regex=True)

    assert [row["id"] for row in hits] == [plasmid["id"]]
    assert hits[0]["match"] == "name"
    assert hits[0]["score"] == 800
    # Ranks belong to the orchestration, not to the pure pass.
    assert "rank" not in hits[0]

    # A miss is an empty list: no rewrite fallback lives in search().
    assert search(ledger, "no-such-object") == []

    # The where syntax is accepted directly as well.
    filtered = search(ledger, "pUC19", where=["kind=plasmid"])
    assert [row["id"] for row in filtered] == [plasmid["id"]]
    assert "rank" not in filtered[0]


def test_search_runs_one_pass_and_find_delegates_to_it(
    where_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = where_ledger
    ledger.entities.add("item", name="alpha")
    scans: list[tuple[str, bool]] = []

    def fake_pass(ledger_arg, text, *, regex, filters):
        scans.append((text, regex))
        return []

    monkeypatch.setattr(find_module, "_search_pass", fake_pass)
    assert search(ledger, "alpha", regex=True, where=["kind=item"]) == []
    assert scans == [("alpha", True)]

    # find()'s query branch goes through search() with the parsed filters.
    delegated: list[dict[str, object]] = []

    def fake_search(ledger_arg, query, **kwargs):
        delegated.append({"query": query, **kwargs})
        return [
            {
                "object": "entity",
                "kind": "item",
                "id": "x",
                "name": None,
                "score": 10,
                "match": "name",
                "summary": "",
                "location": "",
                "aliases": [],
            }
        ]

    monkeypatch.setattr(find_module, "search", fake_search)
    rows = find(ledger, "alpha", where=["kind=item"])

    assert delegated[0]["query"] == "alpha"
    assert delegated[0]["regex"] is False
    assert delegated[0]["filters"]  # parsed once, passed as Filter objects
    assert rows[0]["rank"] == 1


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_regex_json_and_table(cli_env: None, regex_ledger) -> None:
    ledger = regex_ledger
    item = ledger.entities.add("item", name="pUC19")

    result = run_cli(ledger, "find", "-E", r"^pUC\d+$", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["id"] == item["id"]
    assert payload[0]["match"] == "name"
    assert payload[0]["score"] == 800
    assert payload[0]["rank"] == 1

    table = run_cli(ledger, "find", "--regex", r"^pUC\d+$")
    assert table.exit_code == 0, table.output
    assert "rank" in table.output
    assert "pUC19" in table.output


def test_cli_invalid_regex_is_a_usage_error(cli_env: None, regex_ledger) -> None:
    ledger = regex_ledger
    ledger.entities.add("item", name="pUC19")

    result = run_cli(ledger, "find", "-E", "pUC(")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "invalid regex" in str(result.exception)
    assert "pUC(" in str(result.exception)

    # Without -E the same text is a literal search: no match, not a usage error.
    literal = run_cli(ledger, "find", "pUC(")
    assert isinstance(literal.exception, LedgerError)
    assert not isinstance(literal.exception, UsageError)
    assert "no match" in str(literal.exception).lower()


def test_cli_where_with_and_without_a_query(cli_env: None, where_ledger) -> None:
    ledger = where_ledger
    plasmid = ledger.entities.add("plasmid", name="pUC19")
    item = ledger.entities.add("item", name="pUC19-item")

    filtered = run_cli(
        ledger, "find", "pUC19", "--where", "kind=plasmid", "--json"
    )
    assert filtered.exit_code == 0, filtered.output
    filtered_payload = json.loads(filtered.output)
    assert [row["id"] for row in filtered_payload] == [plasmid["id"]]
    assert filtered_payload[0]["match"] == "name"
    assert filtered_payload[0]["score"] == 800
    assert filtered_payload[0]["rank"] == 1

    listed = run_cli(ledger, "find", "--where", "name~^pUC", "--json")
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    # Both entities match the filter, sorted by object type, kind and id.
    assert [row["id"] for row in payload] == [item["id"], plasmid["id"]]
    assert [row["match"] for row in payload] == ["filter", "filter"]
    assert [row["score"] for row in payload] == [0, 0]
    assert [row["rank"] for row in payload] == [1, 2]


def test_cli_empty_query_and_short_where_are_filter_mode(
    cli_env: None, where_ledger
) -> None:
    ledger = where_ledger
    plasmid = ledger.entities.add("plasmid", name="pUC19")
    item = ledger.entities.add("item", name="pUC19-item")

    # An explicitly empty QUERY with --where is the no-query filter mode.
    empty = run_cli(ledger, "find", "", "--where", "name~^pUC", "--json")
    assert empty.exit_code == 0, empty.output
    payload = json.loads(empty.output)
    assert [row["id"] for row in payload] == [item["id"], plasmid["id"]]
    assert [row["match"] for row in payload] == ["filter", "filter"]

    # The -w short option is the same option.
    short = run_cli(ledger, "find", "-w", "kind=plasmid", "--json")
    assert short.exit_code == 0, short.output
    short_payload = json.loads(short.output)
    assert [row["id"] for row in short_payload] == [plasmid["id"]]
    assert short_payload[0]["score"] == 0
    assert short_payload[0]["rank"] == 1


def test_cli_where_reports_unknown_fields_and_no_matches(
    cli_env: None, where_ledger
) -> None:
    ledger = where_ledger
    ledger.entities.add("item", name="alpha")

    unknown = run_cli(ledger, "find", "--where", "bogus=x")
    assert isinstance(unknown.exception, UsageError)
    assert "valid fields" in str(unknown.exception)

    missing = run_cli(ledger, "find", "--where", "name~nothing-like-this")
    assert isinstance(missing.exception, LedgerError)
    assert "no objects match" in str(missing.exception)


def test_cli_bare_find_is_a_usage_error(cli_env: None, where_ledger) -> None:
    result = run_cli(where_ledger, "find")

    assert isinstance(result.exception, UsageError)
    assert "--where" in str(result.exception)


# ---------------------------------------------------------------------------
# The read budget
# ---------------------------------------------------------------------------


@pytest.mark.embedded_driver
def test_regex_and_where_keep_one_read_batch(
    regex_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both features ride in the one batch the literal search already builds."""
    ledger = regex_ledger
    item = ledger.entities.add("item", name="pUC19")

    query_many_calls: list[list[tuple[str, object]]] = []
    invoke_calls: list[str] = []
    original_query_many = EmbeddedDriver.query_many
    original_invoke = EmbeddedDriver._invoke

    def counted_query_many(
        self: EmbeddedDriver, statements: list[tuple[str, object]]
    ) -> list[list[dict[str, object]]]:
        query_many_calls.append(list(statements))
        return original_query_many(self, statements)

    def counted_invoke(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        invoke_calls.append(result_format)
        return original_invoke(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "query_many", counted_query_many)
    monkeypatch.setattr(EmbeddedDriver, "_invoke", counted_invoke)

    assert [row["id"] for row in ledger.find(r"^puc\d+$", regex=True)] == [
        item["id"]
    ]
    assert len(query_many_calls) == 1
    assert invoke_calls == ["json", "csv"]

    query_many_calls.clear()
    invoke_calls.clear()
    assert [row["id"] for row in ledger.find(where=["name~^pUC"])] == [
        item["id"]
    ]
    assert len(query_many_calls) == 1
    assert invoke_calls == ["json", "csv"]

    query_many_calls.clear()
    invoke_calls.clear()
    assert [row["id"] for row in ledger.find("pUC", where=["kind=item"])] == [
        item["id"]
    ]
    assert len(query_many_calls) == 1
    assert invoke_calls == ["json", "csv"]
    # The batch really carries the search, the filter queries and the maps.
    assert len(query_many_calls[0]) >= 10
