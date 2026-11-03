"""``tm lineage`` and the ``lineage: true`` field attribute (ticket 26).

Everything runs against the real ``dolt`` binary.  The main fixture is built
**through the CLI** (``tm init``, ``tm entity add``, ``tm stock add``) the way
ticket 24's migration tests build theirs, so the graph is exercised through the
same code path a user takes:

    protein_construct ← plasmid ← bacterial_glycerol stock
                                 ← bacterial_pellet stock (parent_stock_id)
                                 ← plasmid_prep stock (source_aliquot_id)

plus a ``cell_line`` with a ``parental`` line.  A ledger with a custom preset
covers the "two ``lineage`` refs reach one node" diamond, since the bundled
preset has only one lineage ref per kind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.schema.loader import load_preset, load_schema
from tetramark.schema.model import FieldType, LedgerConfig, SchemaError

from conftest import REPO_PRESETS

runner = CliRunner()

MOLBIO = REPO_PRESETS / "molbio.yaml"

#: ``via`` values the settled design allows (core relations + ``<kind>.<field>``).
CORE_VIA = {"entity", "parent_stock", "source_aliquot"}

#: A minimal status set for the inline presets below: since spec 4 every stock
#: kind must name one (the bundled ``tube`` set, trimmed to what these tests use).
TUBE_SET = {
    "tube": {
        "pending": {"in_stock": False, "to": ["stored", "consumed"]},
        "stored": {"in_stock": True, "to": ["consumed"]},
        "consumed": {"in_stock": False},
    }
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def base_args(ledger) -> list[str]:
    return ["--ledger", str(ledger.path)]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def ok(ledger, *args: str) -> str:
    result = invoke(ledger, *args)
    assert result.exit_code == 0, f"tm {' '.join(args)} failed:\n{result.output}"
    return result.output


def ok_json(ledger, *args: str):
    """Run a command with ``--json`` (the writes' and reads' local option)."""
    return json.loads(ok(ledger, *args, "--json"))


def cli_exit(monkeypatch: pytest.MonkeyPatch, capsys, ledger, *args: str):
    """Run ``tm`` through ``main()`` so the process exit code is real."""
    monkeypatch.setattr(sys, "argv", ["tm", *base_args(ledger), *args])
    capsys.readouterr()
    code = 0
    try:
        main()
    except SystemExit as error:
        code = int(error.code or 0)
    return code, capsys.readouterr().err


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")[0]["n"])


def dirty(ledger) -> list[dict]:
    return ledger.driver.query("SELECT * FROM `dolt_status`")


def fixture(ledger) -> dict:
    """The ticket's descent chain, built through the CLI."""
    construct = ok_json(
        ledger,
        "entity",
        "add",
        "protein_construct",
        "--name",
        "CT1",
        "--set",
        "sequence=atgcatgcatgc",
    )
    plasmid = ok_json(
        ledger,
        "entity",
        "add",
        "plasmid",
        "--name",
        "pZET035",
        "--alias",
        "p035",
        "--set",
        f"construct={construct['id']}",
        "--set",
        "sequence=ATGCATGCATGC",
    )
    ok(ledger, "box", "add", "grid8x12", "BOX-1")
    glycerol = ok_json(
        ledger,
        "stock",
        "add",
        "bacterial_glycerol",
        "--entity",
        "p035",
        "--set",
        "strain=BL21(DE3)",
        "--aliquots",
        "2",
        "--box",
        "BOX-1",
        "--slot",
        "A1",
        "--slot",
        "A2",
    )
    pellet = ok_json(
        ledger,
        "stock",
        "add",
        "bacterial_pellet",
        "--entity",
        plasmid["id"],
        "--parent",
        glycerol["id"],
    )
    source_aliquot = pellet["aliquots"][0]
    prep = ok_json(
        ledger,
        "stock",
        "add",
        "plasmid_prep",
        "--entity",
        plasmid["id"],
        "--source-aliquot",
        source_aliquot,
    )
    parent_line = ok_json(ledger, "entity", "add", "cell_line", "--name", "HEK293")
    child_line = ok_json(
        ledger,
        "entity",
        "add",
        "cell_line",
        "--name",
        "HEK293T",
        "--set",
        f"parental={parent_line['id']}",
    )
    return {
        "construct": construct["id"],
        "plasmid": plasmid["id"],
        "glycerol": glycerol["id"],
        "glycerol_aliquots": glycerol["aliquots"],
        "pellet": pellet["id"],
        "source_aliquot": source_aliquot,
        "prep": prep["id"],
        "parent_line": parent_line["id"],
        "child_line": child_line["id"],
    }


def write_preset(tmp_path: Path, name: str, document: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The field attribute
# ---------------------------------------------------------------------------


def test_lineage_flag_loads_on_the_bundled_refs() -> None:
    """``lineage: true`` loads on the two molbio refs and normalises into them."""
    schema = load_schema(LedgerConfig(name="lineage", presets=[str(MOLBIO)]))

    construct = schema.kinds["plasmid"].fields["construct"]
    assert construct.type is FieldType.ref
    assert construct.to == "protein_construct"
    assert construct.lineage is True
    parental = schema.kinds["cell_line"].fields["parental"]
    assert parental.type is FieldType.ref
    assert parental.to == "cell_line"
    assert parental.lineage is True
    # The flag is part of the canonical form (and so of the fingerprint).
    assert construct.normalised()["lineage"] is True
    assert parental.normalised()["lineage"] is True


def test_lineage_normalisation_is_emitted_only_when_set(tmp_path: Path) -> None:
    without = write_preset(
        tmp_path,
        "without",
        {
            "preset": "without",
            "status_sets": TUBE_SET,
            "kinds": {
                "design": {"object": "entity", "fields": {"name": {"type": "string"}}},
                "batch": {
                    "object": "stock",
                    "statuses": "tube",
                    "entity_kinds": ["design"],
                    "fields": {
                        "from_design": {"type": "ref", "to": "design"},
                    },
                },
            },
        },
    )
    with_flag = write_preset(
        tmp_path,
        "with",
        {
            "preset": "with",
            "status_sets": TUBE_SET,
            "kinds": {
                "design": {"object": "entity", "fields": {"name": {"type": "string"}}},
                "batch": {
                    "object": "stock",
                    "statuses": "tube",
                    "entity_kinds": ["design"],
                    "fields": {
                        "from_design": {
                            "type": "ref",
                            "to": "design",
                            "lineage": True,
                        },
                    },
                },
            },
        },
    )
    plain_schema = load_schema(LedgerConfig(name="a", presets=[str(without)]))
    lineage_schema = load_schema(LedgerConfig(name="b", presets=[str(with_flag)]))

    assert "lineage" not in plain_schema.kinds["batch"].fields["from_design"].normalised()
    assert (
        lineage_schema.kinds["batch"].fields["from_design"].normalised()["lineage"] is True
    )
    # Same DDL (the flag is read-only), different schema identity.
    assert plain_schema.fingerprint() != lineage_schema.fingerprint()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            {
                "preset": "bad-string",
                "kinds": {
                    "thing": {
                        "object": "entity",
                        "fields": {"code": {"type": "string", "lineage": True}},
                    }
                },
            },
            "'lineage' is only valid for type 'ref'",
        ),
        (
            {
                "preset": "bad-box-kind",
                "kinds": {
                    "thing": {"object": "entity", "fields": {"name": {"type": "string"}}},
                    "shelf": {
                        "object": "box",
                        "geometry": {"type": "slotless"},
                        "fields": {
                            "owner": {"type": "ref", "to": "thing", "lineage": True}
                        },
                    },
                },
            },
            "'lineage' is only valid on entity and stock kinds",
        ),
        (
            {
                "preset": "bad-box-target",
                "kinds": {
                    "thing": {
                        "object": "entity",
                        "fields": {
                            "boxed": {"type": "ref", "to": "shelf", "lineage": True}
                        },
                    },
                    "shelf": {"object": "box", "geometry": {"type": "slotless"}},
                },
            },
            "is a box; a lineage edge connects entities, stocks and aliquots",
        ),
        (
            {
                "preset": "bad-core-box",
                "kinds": {
                    "thing": {
                        "object": "entity",
                        "fields": {"boxed": {"type": "ref", "to": "box", "lineage": True}},
                    }
                },
            },
            "is a box; a lineage edge connects entities, stocks and aliquots",
        ),
    ],
    ids=["string-field", "box-kind", "box-kind-target", "core-box-target"],
)
def test_lineage_flag_fails_to_load_outside_entity_and_stock_refs(
    tmp_path: Path, document: dict, message: str
) -> None:
    path = write_preset(tmp_path, str(document["preset"]), document)
    with pytest.raises(SchemaError) as caught:
        load_schema(LedgerConfig(name="bad", presets=[str(path)]))
    assert message in str(caught.value)


def test_schema_show_prints_the_lineage_flag(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-schema")
    shown = ok(ledger, "schema", "show", "plasmid")
    assert "lineage: true" in shown
    payload = ok_json(ledger, "schema", "show", "cell_line")
    assert payload["fields"]["parental"]["lineage"] is True


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_lineage_shows_the_construct_above_and_the_stocks_below(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-tree")
    ids = fixture(ledger)

    lines = ok(ledger, "lineage", ids["plasmid"]).splitlines()
    assert lines[0].startswith("plasmid ") and ids["plasmid"] in lines[0]
    assert "(pZET035)" in lines[0]

    above = [line for line in lines if "↑" in line]
    below = [line for line in lines if "↓" in line]
    assert any("plasmid.construct" in line and ids["construct"] in line for line in above)
    assert any(ids["glycerol"] in line for line in below)
    assert any(ids["pellet"] in line for line in below)
    assert any(ids["prep"] in line for line in below)
    # The source aliquot is an edge endpoint, so it is a node between the two:
    # the downward walk passes child stock → aliquot → the stock it came from.
    assert any(
        "source_aliquot aliquot " in line and ids["source_aliquot"] in line
        for line in lines
    )

    # A stock node shows kind, id, the entity name and in-stock/total counts.
    glycerol_line = next(line for line in lines if ids["glycerol"] in line)
    assert glycerol_line.strip().startswith("↓ entity bacterial_glycerol ")
    assert "of pZET035 (aliquots 2/2)" in glycerol_line
    pellet_line = next(line for line in lines if ids["pellet"] in line)
    assert "(aliquots 0/1)" in pellet_line


def test_lineage_cell_line_parental_chain(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-cell")
    ids = fixture(ledger)

    above = [
        line
        for line in ok(ledger, "lineage", ids["child_line"], "--up").splitlines()
        if "↑" in line
    ]
    assert len(above) == 1
    assert above[0].strip().startswith("↑ cell_line.parental cell_line ")
    assert ids["parent_line"] in above[0]

    below = [
        line
        for line in ok(ledger, "lineage", ids["parent_line"], "--down").splitlines()
        if "↓" in line
    ]
    assert len(below) == 1
    assert below[0].strip().startswith("↓ cell_line.parental cell_line ")
    assert ids["child_line"] in below[0]


def test_up_down_and_depth_trim_the_walk(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-trim")
    ids = fixture(ledger)

    # --up: ancestors only, so no stock below the plasmid.
    up = ok(ledger, "lineage", ids["plasmid"], "--up")
    assert "↓" not in up
    assert ids["construct"] in up
    assert ids["glycerol"] not in up

    # --down: descendants only, so the construct is gone.
    down = ok(ledger, "lineage", ids["plasmid"], "--down")
    assert "↑" not in down
    for key in ("glycerol", "pellet", "prep"):
        assert ids[key] in down
    assert ids["construct"] not in down

    # --depth 0 is the root alone; --depth 1 is its neighbours only.
    assert ok(ledger, "lineage", ids["plasmid"], "--depth", "0").splitlines() == [
        f"plasmid {ids['plasmid']} (pZET035)"
    ]
    direct = ok(ledger, "lineage", ids["plasmid"], "--depth", "1")
    assert ids["construct"] in direct and ids["glycerol"] in direct
    assert ids["source_aliquot"] not in direct  # two edges below the plasmid
    assert ids["pellet"] in direct  # its own entity_id edge

    # --up --depth 1 from the derived stock: entity + source aliquot.
    first = ok(ledger, "lineage", ids["prep"], "--up", "--depth", "1").splitlines()
    assert first[0].startswith("plasmid_prep ")
    assert sorted(line.strip().split()[1] for line in first[1:]) == [
        "entity",
        "source_aliquot",
    ]
    # --depth 2 reaches the source aliquot's stock.
    second = ok(ledger, "lineage", ids["prep"], "--up", "--depth", "2")
    assert ids["pellet"] in second
    assert ids["glycerol"] not in second

    both, err = cli_exit(
        monkeypatch, capsys, ledger, "lineage", ids["prep"], "--up", "--down"
    )
    assert both == 2
    assert "--up and --down" in err


def test_lineage_resolves_an_alias_and_starts_at_an_aliquot(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-alias")
    ids = fixture(ledger)

    assert ok(ledger, "lineage", "p035") == ok(ledger, "lineage", ids["plasmid"])

    if_any = ok(ledger, "lineage", ids["source_aliquot"])
    lines = if_any.splitlines()
    assert lines[0] == f"aliquot {ids['source_aliquot']} of stock {ids['pellet']}"
    assert any(ids["pellet"] in line and "↑ source_aliquot" in line for line in lines)
    assert any(ids["prep"] in line and "↓ source_aliquot" in line for line in lines)


def test_an_unreferenced_aliquot_root_still_reaches_its_stock(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-bare-aliquot")
    ids = fixture(ledger)
    extra = ok_json(
        ledger,
        "stock",
        "add",
        "bacterial_glycerol",
        "--entity",
        ids["plasmid"],
        "--set",
        "strain=DH5a",
    )

    up = ok(ledger, "lineage", extra["aliquots"][0], "--up").splitlines()
    assert up[1].strip().startswith("↑ source_aliquot bacterial_glycerol ")
    assert extra["id"] in up[1]


def test_lineage_errors_are_usage_and_missing_object(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-errors")
    ids = fixture(ledger)
    ok(ledger, "box", "add", "grid8x12", "BOX-9")

    box, box_err = cli_exit(monkeypatch, capsys, ledger, "lineage", "BOX-9")
    assert box == 2
    assert "no lineage" in box_err and "entities" in box_err

    missing, _err = cli_exit(monkeypatch, capsys, ledger, "lineage", "does-not-exist")
    assert missing == 1

    bad_format, format_err = cli_exit(
        monkeypatch, capsys, ledger, "lineage", ids["plasmid"], "--format", "yaml"
    )
    assert bad_format == 2
    assert "text, dot" in format_err

    negative, _err = cli_exit(
        monkeypatch, capsys, ledger, "lineage", ids["plasmid"], "--depth", "-1"
    )
    assert negative == 2


# ---------------------------------------------------------------------------
# JSON and DOT
# ---------------------------------------------------------------------------


def test_json_payload_shape_and_consistency(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-json")
    ids = fixture(ledger)

    payload = ok_json(ledger, "lineage", ids["plasmid"])
    assert payload["root"] == ids["plasmid"]
    assert isinstance(payload["nodes"], list) and isinstance(payload["edges"], list)

    node_ids = [node["id"] for node in payload["nodes"]]
    assert node_ids[0] == ids["plasmid"]
    assert len(node_ids) == len(set(node_ids)), "each node appears once"

    for node in payload["nodes"]:
        assert set(node) >= {"id", "object", "kind", "label"}
        assert node["object"] in {"entity", "stock", "aliquot"}
        if node["object"] == "stock":
            assert set(node["counts"]) == {"in_stock", "total"}
        else:
            assert "counts" not in node

    allowed = CORE_VIA | {"plasmid.construct"}
    for edge in payload["edges"]:
        assert set(edge) == {"from", "to", "via"}
        assert edge["from"] in node_ids and edge["to"] in node_ids
        assert edge["via"] in allowed

    vias = {edge["via"] for edge in payload["edges"]}
    assert {"entity", "parent_stock", "source_aliquot", "plasmid.construct"} <= vias

    glycerol = next(node for node in payload["nodes"] if node["id"] == ids["glycerol"])
    assert glycerol["counts"] == {"in_stock": 2, "total": 2}
    assert glycerol["kind"] == "bacterial_glycerol"
    assert glycerol["label"] == "pZET035"

    # The global --json selects the same shape.
    assert ok_json(ledger, "--json", "lineage", ids["plasmid"], "--depth", "1")[
        "root"
    ] == ids["plasmid"]


def test_diamond_prints_see_above_once_and_lists_the_node_once(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    preset = write_preset(
        tmp_path,
        "tworefs",
        {
            "preset": "tworefs",
            "kinds": {
                "design": {
                    "object": "entity",
                    "fields": {"name": {"type": "string"}},
                },
                "variant": {
                    "object": "entity",
                    "fields": {
                        "name": {"type": "string"},
                        "from_father": {
                            "type": "ref",
                            "to": "design",
                            "lineage": True,
                        },
                        "from_mother": {
                            "type": "ref",
                            "to": "design",
                            "lineage": True,
                        },
                    },
                },
            },
        },
    )
    ledger = ledger_factory([preset], name="lineage-diamond")
    design = ok_json(ledger, "entity", "add", "design", "--name", "D0")
    variant = ok_json(
        ledger,
        "entity",
        "add",
        "variant",
        "--name",
        "V1",
        "--set",
        f"from_father={design['id']}",
        "--set",
        f"from_mother={design['id']}",
    )

    text = ok(ledger, "lineage", variant["id"], "--up")
    assert text.count("(see above)") == 1
    assert f"{design['id']} (see above)" in text
    assert text.count(f"design {design['id']}") == 1

    lines = text.splitlines()
    assert lines[0].startswith("variant ")
    assert [line.strip().split()[1] for line in lines[1:]] == [
        "variant.from_father",
        "variant.from_mother",
    ]

    payload = ok_json(ledger, "lineage", variant["id"], "--up")
    design_nodes = [node for node in payload["nodes"] if node["id"] == design["id"]]
    assert len(design_nodes) == 1
    edge_vias = sorted(edge["via"] for edge in payload["edges"])
    assert edge_vias == ["variant.from_father", "variant.from_mother"]


def test_lineage_ref_to_an_aliquot_and_to_a_stock_kind(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    """A ref to a core aliquot (ticket 29's ``dam``/``sire`` shape) and to a
    stock kind are edges; an aliquot endpoint continues to its owning stock."""
    preset = write_preset(
        tmp_path,
        "aliquotref",
        {
            "preset": "aliquotref",
            "status_sets": TUBE_SET,
            "kinds": {
                "line": {
                    "object": "entity",
                    "fields": {"name": {"type": "string"}},
                },
                "culture": {
                    "object": "stock",
                    "statuses": "tube",
                    "entity_kinds": ["line"],
                    "fields": {
                        "dam": {"type": "ref", "to": "aliquot", "lineage": True},
                        "parent_culture": {
                            "type": "ref",
                            "to": "culture",
                            "lineage": True,
                        },
                    },
                },
            },
        },
    )
    ledger = ledger_factory([preset], name="lineage-aliquotref")
    line = ok_json(ledger, "entity", "add", "line", "--name", "L1")
    parent = ok_json(
        ledger, "stock", "add", "culture", "--entity", line["id"], "--aliquots", "1"
    )
    dam_tube = parent["aliquots"][0]
    child = ok_json(
        ledger,
        "stock",
        "add",
        "culture",
        "--entity",
        line["id"],
        "--set",
        f"dam={dam_tube}",
        "--set",
        f"parent_culture={parent['id']}",
    )

    up = ok(ledger, "lineage", child["id"], "--up")
    assert f"culture.dam aliquot {dam_tube} of stock {parent['id']}" in up
    assert "culture.parent_culture" in up
    assert f"↑ entity line {line['id']} (L1)" in up

    down = ok(ledger, "lineage", parent["id"], "--down")
    assert dam_tube in down and child["id"] in down

    payload = ok_json(ledger, "lineage", child["id"], "--up")
    vias = {edge["via"] for edge in payload["edges"]}
    assert {"entity", "culture.dam", "culture.parent_culture"} <= vias
    # The aliquot node continues to the stock that owns it, same via.
    assert {
        "from": dam_tube,
        "to": parent["id"],
        "via": "culture.dam",
    } in payload["edges"]


def test_dot_format_is_a_valid_digraph(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-dot")
    ids = fixture(ledger)

    dot = ok(ledger, "lineage", ids["plasmid"], "--format", "dot")
    assert dot.startswith("digraph ")
    assert dot.rstrip().endswith("}")
    assert 'label="entity"' in dot
    assert 'label="parent_stock"' in dot
    assert 'label="plasmid.construct"' in dot
    assert f'"{ids["plasmid"]}"' not in dot  # ids are namespaced by object

    binary = shutil.which("dot")
    if binary is None:
        pytest.skip("Graphviz 'dot' is not installed")
    completed = subprocess.run(
        [binary, "-Tsvg"],
        input=dot,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "<svg" in completed.stdout


# ---------------------------------------------------------------------------
# Read-only, branches and cycles
# ---------------------------------------------------------------------------


def test_lineage_is_read_only(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-readonly")
    ids = fixture(ledger)
    before = commit_count(ledger)
    assert dirty(ledger) == []

    ok(ledger, "lineage", ids["plasmid"])
    ok(ledger, "lineage", ids["prep"], "--json")
    ok(ledger, "lineage", ids["source_aliquot"], "--format", "dot")

    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_lineage_respects_the_session_branch(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-branch")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))

    design = ok_json(
        ledger,
        "-b",
        "feature",
        "entity",
        "add",
        "protein_construct",
        "--name",
        "BRANCH-CT",
    )
    stock = ok_json(
        ledger,
        "-b",
        "feature",
        "stock",
        "add",
        "protein_prep",
        "--entity",
        design["id"],
        "--set",
        "concentration_mg_ml=1.5",
    )

    # On the checked-out branch the branch-only object does not exist.
    assert invoke(ledger, "lineage", design["id"]).exit_code == 1
    on_branch = ok(ledger, "-b", "feature", "lineage", design["id"], "--down")
    assert stock["id"] in on_branch
    assert ledger.driver.query("SELECT active_branch() AS `b`") == [{"b": "main"}]


def test_a_parental_cycle_terminates(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([MOLBIO], name="lineage-cycle")
    ids = fixture(ledger)
    # A FK cannot stop cell_line.parental from pointing back; the walk must.
    ok(
        ledger,
        "entity",
        "edit",
        ids["parent_line"],
        "--set",
        f"parental={ids['child_line']}",
    )

    text = ok(ledger, "lineage", ids["child_line"], "--up")
    assert text.count("(see above)") == 1
    payload = ok_json(ledger, "lineage", ids["child_line"], "--json")
    node_ids = [node["id"] for node in payload["nodes"]]
    assert len(node_ids) == len(set(node_ids))


# ---------------------------------------------------------------------------
# Loading helpers used by the bundled preset
# ---------------------------------------------------------------------------


def test_bundled_molbio_loads_through_load_preset_too() -> None:
    preset = load_preset(str(MOLBIO))
    assert preset.kinds["plasmid"].fields["construct"].lineage is True
    assert preset.kinds["cell_line"].fields["parental"].lineage is True
    # molbio is version 3 since the parts split (ticket 27); ``lineage: true``
    # itself stays a read-only flag that needs no bump of its own.
    assert preset.version == 3
