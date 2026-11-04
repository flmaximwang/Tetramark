"""Ticket 28: the ``cellbio`` bundle and its new parts.

The bundle assembles ``base``, ``nucleic_acids``, ``cell_culture``, ``immuno``
and ``viral_vectors``; ``biosamples`` (animal_biosample) is the angenet side
(ticket 29).  Like the rest of the suite these tests use a **real** ``dolt``
database and the real CLI, no mocks.

The recorded ``biosample`` decision (ticket 28, design §2.1): the loader refuses
an ``entity_kinds`` entry that the same ledger does not load (a ``SchemaError``
naming the missing kind), so the model is split instead of changing the loader.
``entity_kinds`` is static and kind names are global, therefore the cell-side
kind (``cell_biosample``) lives in ``cell_culture`` with ``entity_kinds:
[cell_line]``, and the animal-side kind (``animal_biosample``) lives in
``biosamples`` with the organism line kinds of ticket 29's parts.  Each part is
loaded with exactly the entity kinds it names - which is what the bundles do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import LedgerError
from tetramark.schema.loader import load_schema
from tetramark.schema.model import (
    FIXED_STATUSES,
    FieldType,
    LedgerConfig,
    ObjectType,
    SchemaError,
)

from conftest import REPO_PRESETS

CELLBIO = REPO_PRESETS / "cellbio.yaml"

#: The parts the bundle includes, in file order.
CELLBIO_PARTS = ("base", "nucleic_acids", "cell_culture", "immuno", "viral_vectors")

#: Kinds the bundle adds on top of the molbio set.
NEW_KINDS = {"antibody", "antibody_lot", "virus", "virus_prep", "cell_biosample"}


def repo_preset(name: str) -> Path:
    return REPO_PRESETS / f"{name}.yaml"


def load_repo_preset(name: str):
    return load_schema(
        LedgerConfig(name="preset_test", presets=[str(repo_preset(name))])
    )


def base_args(ledger) -> list[str]:
    return ["--ledger", str(ledger.path)]


def run(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def ok_json(ledger, *args: str):
    result = run(ledger, *args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def table_names(ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


def tm_check(ledger):
    return run(ledger, "check")


# ---------------------------------------------------------------------------
# Shape: the bundle and the parts
# ---------------------------------------------------------------------------


def test_cellbio_is_a_bundle_of_shared_parts() -> None:
    """A bundle only includes parts: no kinds, mixins, rules or status sets."""
    document = yaml.safe_load(CELLBIO.read_text(encoding="utf-8"))
    assert document["preset"] == "cellbio"
    assert document["version"] == 1
    assert document["include"] == list(CELLBIO_PARTS)
    for key in ("kinds", "mixins", "rules", "status_sets"):
        assert key not in document, key
    # A part never includes a bundle.
    for part in CELLBIO_PARTS:
        part_doc = yaml.safe_load(repo_preset(part).read_text(encoding="utf-8"))
        for name in part_doc.get("include", []):
            assert name not in ("molbio", "cellbio", "angenet"), (part, name)

    schema = load_repo_preset("cellbio")
    assert schema.preset_versions == {
        "base": 5,
        "proteins": 3,
        "nucleic_acids": 3,
        "cell_culture": 3,
        "immuno": 2,
        "viral_vectors": 2,
        "cellbio": 1,
    }
    assert NEW_KINDS <= set(schema.kinds)
    # The cell-side biosample comes from cell_culture (option (a)), not from
    # the angenet-only biosamples part.
    assert "biosamples" not in CELLBIO_PARTS
    assert "animal_biosample" not in schema.kinds


def test_new_parts_credit_biomanager_in_their_description() -> None:
    for part in ("immuno", "viral_vectors", "biosamples", "cellbio"):
        document = yaml.safe_load(repo_preset(part).read_text(encoding="utf-8"))
        description = document["description"].strip()
        assert description.endswith("Adapted from BioManager (MIT)."), part
        assert "Included by the" in description or part == "cellbio", part


def test_immuno_follows_the_design() -> None:
    """Design §2.1: antibody entity and antibody_lot stock."""
    schema = load_repo_preset("cellbio")
    antibody = schema.kinds["antibody"]
    assert antibody.object is ObjectType.entity
    assert antibody.mixins == ["named_fields"]
    assert set(antibody.fields) == {
        "name",
        "description",
        "host",
        "clonality",
        "clone",
        "isotype",
        "reactivity",
        "conjugate",
        "applications",
        "rrid",
    }
    assert antibody.fields["clonality"].values == [
        "monoclonal",
        "polyclonal",
        "recombinant",
    ]

    lot = schema.kinds["antibody_lot"]
    assert lot.object is ObjectType.stock
    assert lot.entity_kinds == ["antibody"]
    # Spec 6: the lot's own ``tube`` set is gone; every aliquot uses the
    # fixed status set.
    assert schema.statuses_for("antibody_lot") == list(FIXED_STATUSES)
    assert set(lot.fields) == {
        "supplier",
        "catalog_no",
        "lot_no",
        "concentration_mg_ml",
        "recommended_dilution",
        "received_on",
        "expires_on",
    }
    assert {"IMM01"} <= {rule.id for rule in schema.rules}


def test_viral_vectors_follow_the_design() -> None:
    """Design §2.1: virus entity with a plain payload ref, virus_prep stock."""
    schema = load_repo_preset("cellbio")
    virus = schema.kinds["virus"]
    assert virus.object is ObjectType.entity
    assert virus.mixins == ["named_fields"]
    assert set(virus.fields) == {
        "name",
        "description",
        "virus_type",
        "serotype",
        "payload",
        "promoter",
        "biosafety_level",
    }
    assert virus.fields["virus_type"].values == [
        "aav",
        "lentivirus",
        "retrovirus",
        "adenovirus",
        "other",
    ]
    payload = virus.fields["payload"]
    assert payload.type is FieldType.ref and payload.to == "plasmid"
    # A plain field, not a lineage edge: the plasmid is what the prep carries.
    assert payload.lineage is False

    prep = schema.kinds["virus_prep"]
    assert prep.object is ObjectType.stock
    assert prep.entity_kinds == ["virus"]
    assert schema.statuses_for("virus_prep") == list(FIXED_STATUSES)
    assert set(prep.fields) == {"titer", "titer_unit", "made_on", "made_by"}
    assert prep.fields["titer_unit"].values == ["vg_ml", "tu_ml", "pfu_ml"]
    assert {"VIR01"} <= {rule.id for rule in schema.rules}


def test_cell_culture_v2_adds_the_vial_extras_and_cell_biosample() -> None:
    """Ticket 28: cell_culture 1 -> 2 content (the extras and the biosample)."""
    schema = load_repo_preset("cellbio")
    vial = schema.kinds["cell_vial"]
    assert {"selection", "mycoplasma", "mycoplasma_tested_on"} <= set(vial.fields)
    mycoplasma = vial.fields["mycoplasma"]
    assert mycoplasma.values == ["not_tested", "negative", "positive"]
    assert mycoplasma.default == "not_tested"
    # Spec 6: the cryo set is gone - a vial is stored in the freezer and
    # consumed once it is thawed, like every other aliquot.
    assert schema.statuses_for("cell_vial") == list(FIXED_STATUSES)

    biosample = schema.kinds["cell_biosample"]
    assert biosample.object is ObjectType.stock
    assert biosample.entity_kinds == ["cell_line"]
    assert schema.statuses_for("cell_biosample") == list(FIXED_STATUSES)
    assert set(biosample.fields) == {
        "sample_type",
        "tissue",
        "collected_on",
        "storage_temp_c",
    }
    assert biosample.fields["sample_type"].values == [
        "tissue",
        "blood",
        "dna",
        "rna",
        "serum",
        "other",
    ]
    # The origin is the core stock column, not a schema field.
    assert "source_aliquot" not in biosample.fields
    # cell_line.parental stays a lineage edge (ticket 26).
    assert schema.kinds["cell_line"].fields["parental"].lineage is True


# ---------------------------------------------------------------------------
# The recorded biosample decision
# ---------------------------------------------------------------------------


def test_loader_refuses_an_entity_kinds_entry_that_is_not_loaded() -> None:
    """Why ticket 28 splits the biosample kind instead of changing the loader.

    ``biosamples`` names the organism line kinds of ticket 29's parts and is
    loaded by the angenet bundle together with those parts.  On its own the
    loaded schema has no such entity kind, and the loader refuses it - there is
    no ``entity_kinds_optional`` switch, and adding one would be a core change.
    """
    with pytest.raises(SchemaError) as error:
        load_schema(
            LedgerConfig(
                name="biosamples-alone",
                presets=[str(repo_preset("biosamples"))],
            )
        )
    message = str(error.value)
    assert "kinds.animal_biosample.entity_kinds" in message
    assert "'fly_line' is not an entity kind" in message

    document = yaml.safe_load(repo_preset("biosamples").read_text(encoding="utf-8"))
    assert document["kinds"]["animal_biosample"]["entity_kinds"] == [
        "fly_line",
        "worm_strain",
        "fish_line",
        "mouse_strain",
    ]


def test_biosamples_loads_with_the_organism_parts_it_names(tmp_path: Path) -> None:
    """Loaded as angenet loads it (with the organism parts), the part is valid."""
    stub = tmp_path / "organism_stub.yaml"
    stub.write_text(
        """preset: organism_stub
version: 1
description: Test-only stand-in for ticket 29's organism entity kinds.
kinds:
  fly_line: {object: entity, description: A fly line.}
  worm_strain: {object: entity, description: A worm strain.}
  fish_line: {object: entity, description: A fish line.}
  mouse_strain: {object: entity, description: A mouse strain.}
""",
        encoding="utf-8",
    )
    schema = load_schema(
        LedgerConfig(
            name="angenet-shape",
            presets=[str(repo_preset("biosamples")), str(stub)],
        )
    )
    biosample = schema.kinds["animal_biosample"]
    assert biosample.object is ObjectType.stock
    assert biosample.entity_kinds == [
        "fly_line",
        "worm_strain",
        "fish_line",
        "mouse_strain",
    ]
    assert schema.statuses_for("animal_biosample") == list(FIXED_STATUSES)
    assert set(biosample.fields) == {
        "sample_type",
        "tissue",
        "collected_on",
        "storage_temp_c",
    }


def test_cell_biosample_accepts_cell_lines_only(ledger_factory) -> None:
    """The cell-side kind carries the restriction the loader enforces."""
    ledger = ledger_factory(["cellbio"], name="cell_biosample_kinds", real_init=True)
    line = ledger.entities.add(
        "cell_line", name="HEK293T", values={"species": "Homo sapiens"}
    )
    sample = ledger.stocks.add(
        "cell_biosample",
        entity=line["id"],
        values={
            "sample_type": "rna",
            "collected_on": "2026-10-02",
            "storage_temp_c": -80,
        },
        aliquots=1,
        quantity=1,
        unit="tube",
    )
    assert sample["kind"] == "cell_biosample"

    plasmid = ledger.entities.add(
        "plasmid", name="pLenti-GFP", values={"backbone": "pLenti"}
    )
    with pytest.raises(LedgerError, match="only accepts entity kinds"):
        ledger.stocks.add(
            "cell_biosample", entity=plasmid["id"], values={"sample_type": "dna"}
        )


# ---------------------------------------------------------------------------
# Init, objects and lineage through the CLI
# ---------------------------------------------------------------------------


def test_cellbio_init_creates_the_tables_and_passes_check(ledger_factory) -> None:
    """Verify bullet 1: ``tm init --preset cellbio`` builds and validates."""
    ledger = ledger_factory(["cellbio"], name="cellbio_init", real_init=True)
    tables = table_names(ledger)
    for kind in NEW_KINDS | {"cell_line", "cell_vial", "plasmid", "oligo"}:
        object_type = ledger.schema.kinds[kind].object.value
        assert f"{object_type}_{kind}" in tables, kind
        assert f"v_{object_type}_{kind}" in tables, kind
    assert "aliquot_bacterial_glycerol" not in tables
    assert ledger.check().passed

    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output
    planned = run(ledger, "schema", "plan")
    assert planned.exit_code == 0, planned.output
    assert "up to date" in planned.output


def test_add_one_object_of_each_new_kind_and_check(ledger_factory) -> None:
    """Verify bullet 1: one row per new kind, then ``tm check`` is clean."""
    ledger = ledger_factory(["cellbio"], name="cellbio_objects")
    plasmid = ok_json(
        ledger,
        "entity",
        "add",
        "plasmid",
        "--name",
        "pLenti-GFP",
        "--set",
        "backbone=pLenti",
        "--set",
        "resistance=amp",
    )
    antibody = ok_json(
        ledger,
        "entity",
        "add",
        "antibody",
        "--name",
        "anti-GFP",
        "--set",
        "host=rabbit",
        "--set",
        "clonality=polyclonal",
        "--set",
        "applications=WB",
        "--set",
        "rrid=AB_123456",
    )
    lot = ok_json(
        ledger,
        "stock",
        "add",
        "antibody_lot",
        "--entity",
        antibody["id"],
        "--set",
        "supplier=abcam",
        "--set",
        "catalog_no=ab290",
        "--set",
        "lot_no=GR123",
        "--set",
        "concentration_mg_ml=1.0",
        "--set",
        "recommended_dilution=1:1000",
        "--set",
        "received_on=2026-03-01",
        "--set",
        "expires_on=2027-03-01",
    )
    virus = ok_json(
        ledger,
        "entity",
        "add",
        "virus",
        "--name",
        "LV-GFP",
        "--set",
        "virus_type=lentivirus",
        "--set",
        "serotype=VSV-G",
        "--set",
        f"payload={plasmid['id']}",
        "--set",
        "promoter=EF1a",
        "--set",
        "biosafety_level=BSL-2",
    )
    prep = ok_json(
        ledger,
        "stock",
        "add",
        "virus_prep",
        "--entity",
        virus["id"],
        "--set",
        "titer=1200000000",
        "--set",
        "titer_unit=tu_ml",
        "--set",
        "made_on=2026-04-01",
        "--set",
        "made_by=vector core",
    )
    line = ok_json(
        ledger,
        "entity",
        "add",
        "cell_line",
        "--name",
        "HEK293T",
        "--set",
        "species=Homo sapiens",
    )
    ok_json(ledger, "box", "add", "freezer", "CRYO-1")
    vial = ok_json(
        ledger,
        "stock",
        "add",
        "cell_vial",
        "--entity",
        line["id"],
        "--box",
        "CRYO-1",
        "--set",
        "passage=7",
        "--set",
        "selection=puromycin 1 ug/mL",
        "--set",
        "mycoplasma=negative",
        "--set",
        "mycoplasma_tested_on=2026-10-01",
        "--quantity",
        "1",
        "--unit",
        "vial",
    )
    sample = ok_json(
        ledger,
        "stock",
        "add",
        "cell_biosample",
        "--entity",
        line["id"],
        "--source-aliquot",
        vial["aliquots"][0],
        "--set",
        "sample_type=dna",
        "--set",
        "collected_on=2026-10-02",
        "--set",
        "storage_temp_c=-20",
    )

    rows = ledger.driver.query(
        "SELECT `id`, `kind` FROM `stock` WHERE `id` IN (%s, %s, %s, %s)",
        (lot["id"], prep["id"], vial["id"], sample["id"]),
    )
    assert {row["kind"] for row in rows} == {
        "antibody_lot",
        "virus_prep",
        "cell_vial",
        "cell_biosample",
    }
    stored = ledger.driver.query(
        "SELECT `selection`, `mycoplasma`, `mycoplasma_tested_on` "
        "FROM `stock_cell_vial` WHERE `id` = %s",
        (vial["id"],),
    )
    assert stored == [
        {
            "selection": "puromycin 1 ug/mL",
            "mycoplasma": "negative",
            "mycoplasma_tested_on": "2026-10-01",
        }
    ]
    assert ledger.driver.query(
        "SELECT `source_aliquot_id` FROM `stock` WHERE `id` = %s", (sample["id"],)
    ) == [{"source_aliquot_id": vial["aliquots"][0]}]
    assert ledger.check().passed


def test_rules_flag_a_lot_that_expired_before_receipt_and_a_bad_titer(
    ledger_factory,
) -> None:
    """The two new rules are real ``tm check`` failures, not decoration."""
    ledger = ledger_factory(["cellbio"], name="cellbio_rules")
    antibody = ledger.entities.add("antibody", name="anti-actin")
    ledger.stocks.add(
        "antibody_lot",
        entity=antibody["id"],
        values={"received_on": "2026-02-01", "expires_on": "2026-01-01"},
    )
    virus = ledger.entities.add("virus", name="AAV2-GFP", values={"virus_type": "aav"})
    ledger.stocks.add("virus_prep", entity=virus["id"], values={"titer": 0})

    checked = tm_check(ledger)
    assert checked.exit_code == 1, checked.output
    assert "rule_IMM01" in checked.output
    assert "rule_VIR01" in checked.output


def test_lineage_ignores_virus_payload_but_shows_cell_line_parental(
    ledger_factory,
) -> None:
    """Verify bullet 2: ``payload`` is not an edge, ``parental`` is."""
    ledger = ledger_factory(["cellbio"], name="cellbio_lineage")
    plasmid = ok_json(
        ledger, "entity", "add", "plasmid", "--name", "pLenti-GFP"
    )
    virus = ok_json(
        ledger,
        "entity",
        "add",
        "virus",
        "--name",
        "LV-GFP",
        "--set",
        "virus_type=lentivirus",
        "--set",
        f"payload={plasmid['id']}",
    )
    parent = ok_json(
        ledger, "entity", "add", "cell_line", "--name", "HEK293T"
    )
    child = ok_json(
        ledger,
        "entity",
        "add",
        "cell_line",
        "--name",
        "HEK293T-EGFR",
        "--set",
        f"parental={parent['id']}",
    )

    virus_tree = run(ledger, "lineage", virus["id"])
    assert virus_tree.exit_code == 0, virus_tree.output
    assert "payload" not in virus_tree.output
    assert plasmid["id"] not in virus_tree.output

    cell_tree = run(ledger, "lineage", child["id"])
    assert cell_tree.exit_code == 0, cell_tree.output
    assert f"cell_line.parental cell_line {parent['id']}" in cell_tree.output


def test_cell_biosample_appears_under_its_cell_vial_in_lineage(ledger_factory) -> None:
    """Verify bullet 4: ``source_aliquot_id`` makes the sample a descendant."""
    ledger = ledger_factory(["cellbio"], name="cellbio_biosample_lineage")
    line = ok_json(ledger, "entity", "add", "cell_line", "--name", "HEK293T")
    ok_json(ledger, "box", "add", "freezer", "CRYO-1")
    vial = ok_json(
        ledger,
        "stock",
        "add",
        "cell_vial",
        "--entity",
        line["id"],
        "--box",
        "CRYO-1",
        "--set",
        "passage=7",
        "--quantity",
        "1",
        "--unit",
        "vial",
    )
    aliquot = vial["aliquots"][0]
    sample = ok_json(
        ledger,
        "stock",
        "add",
        "cell_biosample",
        "--entity",
        line["id"],
        "--source-aliquot",
        aliquot,
        "--set",
        "sample_type=rna",
    )

    up = run(ledger, "lineage", sample["id"], "--up")
    assert up.exit_code == 0, up.output
    assert f"source_aliquot aliquot {aliquot}" in up.output
    assert f"cell_vial {vial['id']}" in up.output
    assert f"cell_line {line['id']}" in up.output

    down = run(ledger, "lineage", aliquot, "--down")
    assert down.exit_code == 0, down.output
    assert f"source_aliquot cell_biosample {sample['id']}" in down.output

    payload = ok_json(ledger, "lineage", aliquot)
    edges = {(edge["from"], edge["to"], edge["via"]) for edge in payload["edges"]}
    assert (sample["id"], aliquot, "source_aliquot") in edges
    assert (aliquot, vial["id"], "source_aliquot") in edges


def test_cellbio_and_molbio_load_together(ledger_factory) -> None:
    """Verify bullet 3: shared parts load once and the union passes ``tm check``."""
    ledger = ledger_factory(
        ["molbio", "cellbio"], name="molbio_cellbio", real_init=True
    )
    kinds = set(ledger.schema.kinds)
    # cellbio adds the immuno/viral kinds; molbio adds the bacteria kinds.
    assert NEW_KINDS <= kinds
    assert {"bacterial_strain", "bacterial_glycerol", "bacterial_pellet"} <= kinds
    assert "animal_biosample" not in kinds
    copies = ledger.path / "presets"
    assert sorted(item.name for item in copies.iterdir()) == [
        "bacteria.yaml",
        "base.yaml",
        "cell_culture.yaml",
        "cellbio.yaml",
        "immuno.yaml",
        "molbio.yaml",
        "nucleic_acids.yaml",
        "proteins.yaml",
        "viral_vectors.yaml",
    ]
    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output
    planned = run(ledger, "schema", "plan")
    assert planned.exit_code == 0, planned.output
    assert "up to date" in planned.output
