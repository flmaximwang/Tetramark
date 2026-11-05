"""Ticket 29: the ``angenet`` bundle and its four organism parts.

The bundle assembles ``base``, ``nucleic_acids``, ``biosamples`` and the
``drosophila``, ``c_elegans``, ``zebrafish`` and ``mouse`` parts.  Like the rest
of the suite these tests use a **real** ``dolt`` database and the real CLI, no
mocks, and the mouse/litter/cross workflows are exercised through the same
commands a user runs (``tm init``, ``tm entity add``, ``tm stock add``,
``tm aliquot status``, ``tm lineage``).

The organism model (design §3): a line is an **entity**, a colony is a
**stock**, and an aliquot is one animal (mouse, tagged fish) or one housing
unit's population (fly vial, worm plate, fish tank).  Individuals of one stock
are identical in every recorded property, so the female and male breeders are
two stocks; their aliquots are the ``dam``/``sire`` the progeny stock points at.
A litter is a ``mouse_cohort`` with ``genotype`` NULL and one group aliquot; at
genotyping it splits into per-genotype × sex stocks linked through the core
``parent_stock_id``, with no core change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import open as open_ledger
from tetramark.schema.loader import list_presets, load_schema
from tetramark.schema.model import (
    FieldType,
    GeometryType,
    LedgerConfig,
    ObjectType,
)

from conftest import FACTORY_AUTHOR, REPO_PRESETS

ANGENET = REPO_PRESETS / "angenet.yaml"

#: The parts the bundle includes, in file order.
ANGENET_PARTS = (
    "base",
    "nucleic_acids",
    "biosamples",
    "drosophila",
    "c_elegans",
    "zebrafish",
    "mouse",
)

ORGANISM_PARTS = ("drosophila", "c_elegans", "zebrafish", "mouse")

#: ``preset_versions`` a ``--preset angenet`` ledger records (loader order: the
#: parts come before the bundle that lists them, depth first).
ANGENET_VERSIONS = {
    "base": 6,
    "proteins": 4,
    "nucleic_acids": 3,
    "biosamples": 2,
    "drosophila": 2,
    "c_elegans": 2,
    "zebrafish": 2,
    "mouse": 2,
    "angenet": 1,
}

#: What the bundle loads: base plus nucleic acids (and proteins below it), the
#: animal biosample and the four organism parts.  Cell kinds are *not* included.
ANGENET_KINDS = {
    # base v4
    "item",
    "lot",
    "grid9x9",
    "grid8x12",
    "rack",
    "shelf",
    "freezer",
    "drawer",
    "incubator",
    # nucleic_acids (+ proteins through it)
    "plasmid",
    "plasmid_prep",
    "oligo",
    "oligo_stock",
    "protein_construct",
    "protein_prep",
    # biosamples (the animal side)
    "animal_biosample",
    # drosophila
    "fly_line",
    "fly_culture",
    # c_elegans
    "worm_strain",
    "worm_culture",
    "worm_cryo",
    # zebrafish
    "fish_line",
    "fish_group",
    "fish_clutch",
    "fish_sperm_cryo",
    # mouse
    "mouse_strain",
    "mouse_cohort",
    "mouse_sperm_cryo",
    "mouse_embryo_cryo",
    "cage",
}

#: Spec 6: every aliquot of every organism kind uses the one fixed status
#: set (spec 1.1).  The detailed life state is a stock property instead:
#: ``fate`` on the mouse/fish/fly stocks, ``condition`` on the worm culture.
FIXED_STATUSES = ["pending", "stored", "missing", "consumed"]

#: The optional life-cycle properties the organism parts carry.
ANIMAL_FATE = ["euthanized", "found_dead", "transferred", "cancelled"]
FLY_FATE = ["discarded", "lost"]
WORM_CONDITION = ["growing", "starved", "contaminated"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def repo_preset(name: str) -> Path:
    return REPO_PRESETS / f"{name}.yaml"


def preset_document(name: str) -> dict:
    return yaml.safe_load(repo_preset(name).read_text(encoding="utf-8"))


def load_repo_preset(name: str):
    return load_schema(
        LedgerConfig(name="preset_test", presets=[str(repo_preset(name))])
    )


def base_args(ledger) -> list[str]:
    return ["--ledger", str(ledger.path)]


def run(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def ok(ledger, *args: str):
    result = run(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


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


def statuses_for(schema, kind: str) -> list[str]:
    return schema.statuses_for(kind)


def refused(result) -> str:
    """The message of a CLI call that the app refused (exit code 1, no output)."""
    assert result.exit_code != 0, result.output
    assert result.exception is not None, result.output
    return str(result.exception)


# ---------------------------------------------------------------------------
# Shape: the bundle and the parts
# ---------------------------------------------------------------------------


def test_angenet_is_a_bundle_of_organism_parts() -> None:
    """A bundle only includes parts: no kinds, mixins, rules or status sets."""
    document = preset_document("angenet")
    assert document["preset"] == "angenet"
    assert document["version"] == 1
    assert document["include"] == list(ANGENET_PARTS)
    for key in ("kinds", "mixins", "rules", "status_sets"):
        assert key not in document, key
    # A part never includes a bundle.
    for part in ANGENET_PARTS:
        part_doc = preset_document(part)
        for name in part_doc.get("include", []):
            assert name not in ("molbio", "cellbio", "angenet"), (part, name)

    schema = load_repo_preset("angenet")
    assert schema.preset_versions == ANGENET_VERSIONS
    assert set(schema.kinds) == ANGENET_KINDS
    # The organism entity and stock kinds live in the organism parts (design
    # §3); base keeps the inventory and the shared box kinds.
    base = preset_document("base")
    for name in (
        "fly_line",
        "fly_culture",
        "worm_strain",
        "worm_culture",
        "worm_cryo",
        "fish_line",
        "fish_group",
        "fish_clutch",
        "fish_sperm_cryo",
        "mouse_strain",
        "mouse_cohort",
        "mouse_sperm_cryo",
        "mouse_embryo_cryo",
        "cage",
    ):
        assert name not in base["kinds"], name
    # Spec 6: no part declares a status set any more, so the whole closure
    # loads with the fixed status set.
    assert schema.status_sets == {}
    assert "status_sets" not in base
    for part in ("drosophila", "c_elegans", "zebrafish", "mouse", "biosamples"):
        part_doc = preset_document(part)
        assert part_doc["include"] == ["base"], part
        assert "status_sets" not in part_doc, part
        for kind_name, kind in part_doc["kinds"].items():
            assert "statuses" not in kind, (part, kind_name)


def test_organism_parts_credit_biomanager_and_name_their_bundle() -> None:
    for part in (*ORGANISM_PARTS, "angenet"):
        description = preset_document(part)["description"].strip()
        assert description.endswith("Adapted from BioManager (MIT)."), part
        if part != "angenet":
            assert "Included by the" in description, part
            assert "angenet" in description, part
    # Each new kind credits BioManager too (README "adding a part", step 3).
    for part in ORGANISM_PARTS:
        for kind in preset_document(part)["kinds"].values():
            assert kind["description"].strip().endswith(
                "Adapted from BioManager (MIT)."
            ), part


def test_each_organism_part_loads_alone_with_its_kinds() -> None:
    """A part is self-contained: its include closure is base, nothing else."""
    expected = {
        "drosophila": {"fly_line", "fly_culture"},
        "c_elegans": {"worm_strain", "worm_culture", "worm_cryo"},
        "zebrafish": {
            "fish_line",
            "fish_group",
            "fish_clutch",
            "fish_sperm_cryo",
        },
        "mouse": {
            "mouse_strain",
            "mouse_cohort",
            "mouse_sperm_cryo",
            "mouse_embryo_cryo",
            "cage",
        },
    }
    base_kinds = set(preset_document("base")["kinds"])
    for part, kinds in expected.items():
        schema = load_repo_preset(part)
        assert set(schema.kinds) == base_kinds | kinds, part
        assert schema.preset_versions == {"base": 6, part: 2}
        assert all(rule.sql.strip().upper().startswith("SELECT") for rule in schema.rules)


def test_organism_kinds_match_the_design() -> None:
    """Design §3: fields, entity links, lineage edges and status sets."""
    schema = load_repo_preset("angenet")

    # -- the four line entities: named_fields + parental to their own kind ----
    lines = {
        "fly_line": {"balancers", "stock_centre", "stock_number", "flybase_id"},
        "worm_strain": {"allele", "lab_prefix", "wormbase_id", "source"},
        "fish_line": {"zfin_name", "allele", "background"},
        "mouse_strain": {"background", "supplier", "mgi_id"},
    }
    for kind_name, own_fields in lines.items():
        kind = schema.kinds[kind_name]
        assert kind.object is ObjectType.entity, kind_name
        assert kind.mixins == ["named_fields"], kind_name
        assert set(kind.fields) == {
            "name",
            "description",
            "parental",
            *own_fields,
        }, kind_name
        parental = kind.fields["parental"]
        assert parental.type is FieldType.ref, kind_name
        assert parental.to == kind_name, kind_name
        assert parental.lineage is True, kind_name

    # -- fly culture: a vial with the cross fields and its fate ---------------
    fly = schema.kinds["fly_culture"]
    assert fly.object is ObjectType.stock
    assert fly.entity_kinds == ["fly_line"]
    assert statuses_for(schema, "fly_culture") == FIXED_STATUSES
    assert set(fly.fields) == {
        "temperature_c",
        "generation",
        "food_type",
        "virgins_collected_on",
        "selection_marker",
        "fate",
        "dam",
        "sire",
    }
    assert fly.fields["temperature_c"].type is FieldType.decimal
    assert fly.fields["virgins_collected_on"].type is FieldType.date
    assert "sex" not in fly.fields  # fly cultures carry no sex field
    for parent in ("dam", "sire"):
        field = fly.fields[parent]
        assert field.type is FieldType.ref and field.to == "aliquot", parent
        assert field.lineage is True, parent

    # -- worm strain: a plate culture with its condition and a cryo stock ------
    worm = schema.kinds["worm_culture"]
    assert worm.entity_kinds == ["worm_strain"]
    assert statuses_for(schema, "worm_culture") == FIXED_STATUSES
    assert set(worm.fields) == {
        "temperature_c",
        "plate_type",
        "generation",
        "stage",
        "condition",
    }
    # C. elegans selfers have a single-parent strain lineage, not dam/sire.
    assert "dam" not in worm.fields and "sire" not in worm.fields
    worm_cryo = schema.kinds["worm_cryo"]
    assert worm_cryo.entity_kinds == ["worm_strain"]
    assert statuses_for(schema, "worm_cryo") == FIXED_STATUSES
    assert set(worm_cryo.fields) == {
        "frozen_on",
        "method",
        "recovery_tested_on",
        "recovery_ok",
    }
    assert worm_cryo.fields["recovery_ok"].type is FieldType.bool

    # -- zebrafish: group/clutch animals, sperm cryo, no embryo cryo ----------
    assert set(schema.kinds["fish_group"].fields) == {
        "birth_on",
        "sex",
        "genotype",
        "density_per_l",
        "fate",
        "dam",
        "sire",
    }
    assert set(schema.kinds["fish_clutch"].fields) == {
        "set_up_on",
        "embryo_count",
        "larvae_count",
        "fate",
        "dam",
        "sire",
    }
    for kind_name in ("fish_group", "fish_clutch"):
        assert statuses_for(schema, kind_name) == FIXED_STATUSES, kind_name
        assert schema.kinds[kind_name].entity_kinds == ["fish_line"], kind_name
    assert schema.kinds["fish_group"].fields["density_per_l"].type is FieldType.decimal
    assert schema.kinds["fish_clutch"].fields["embryo_count"].type is FieldType.int
    assert schema.kinds["fish_clutch"].fields["larvae_count"].type is FieldType.int
    assert statuses_for(schema, "fish_sperm_cryo") == FIXED_STATUSES
    assert "fish_embryo_cryo" not in schema.kinds

    # -- mouse: cohorts, sperm + embryo cryo, and the cage box kind -----------
    cohort = schema.kinds["mouse_cohort"]
    assert cohort.entity_kinds == ["mouse_strain"]
    assert statuses_for(schema, "mouse_cohort") == FIXED_STATUSES
    assert set(cohort.fields) == {
        "birth_on",
        "sex",
        "genotype",
        "fate",
        "dam",
        "sire",
    }
    species = {name: schema.kinds[name] for name in ("fish_group", "mouse_cohort")}
    for kind_name, kind in species.items():
        sex = kind.fields["sex"]
        assert sex.type is FieldType.enum, kind_name
        assert sex.values == ["f", "m", "u"], kind_name
        assert kind.fields["genotype"].type is FieldType.text, kind_name
    for kind_name in ("mouse_sperm_cryo", "mouse_embryo_cryo"):
        assert statuses_for(schema, kind_name) == FIXED_STATUSES, kind_name
        assert schema.kinds[kind_name].entity_kinds == ["mouse_strain"], kind_name
    assert "embryo_stage" in schema.kinds["mouse_embryo_cryo"].fields
    assert "embryo_stage" not in schema.kinds["mouse_sperm_cryo"].fields

    cage = schema.kinds["cage"]
    assert cage.object is ObjectType.box
    assert cage.geometry is not None
    assert cage.geometry.type is GeometryType.slotless
    assert cage.geometry.capacity == 5
    assert cage.geometry.labels == []


def test_organism_life_state_lives_in_optional_stock_properties() -> None:
    """Spec 6: the old per-kind vocabularies are optional enum properties."""
    schema = load_repo_preset("angenet")

    # The animals: ``fate`` on the mouse cohort and the fish group/clutch.
    for kind_name in ("mouse_cohort", "fish_group", "fish_clutch"):
        fate = schema.kinds[kind_name].fields["fate"]
        assert fate.type is FieldType.enum, kind_name
        assert fate.values == ANIMAL_FATE, kind_name
        # Optional: no default and not required.
        assert not fate.required, kind_name
        assert not fate.has_default, kind_name

    # The fly: ``fate`` with its own two words.
    fly_fate = schema.kinds["fly_culture"].fields["fate"]
    assert fly_fate.type is FieldType.enum
    assert fly_fate.values == FLY_FATE
    assert not fly_fate.required and not fly_fate.has_default

    # The worm: ``condition`` instead of ``fate``.
    condition = schema.kinds["worm_culture"].fields["condition"]
    assert condition.type is FieldType.enum
    assert condition.values == WORM_CONDITION
    assert not condition.required and not condition.has_default
    assert "fate" not in schema.kinds["worm_culture"].fields

    # A frozen stock has no life-state property at all: it is stored in the
    # freezer and consumed once it is thawed.
    for kind_name in (
        "mouse_sperm_cryo",
        "mouse_embryo_cryo",
        "fish_sperm_cryo",
        "worm_cryo",
    ):
        assert "fate" not in schema.kinds[kind_name].fields, kind_name
        assert "condition" not in schema.kinds[kind_name].fields, kind_name


# ---------------------------------------------------------------------------
# ``tm init --preset angenet`` and the catalogue
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_isolated(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> None:
    """Run the CLI from ``tmp_path`` with the isolated Dolt/Git config."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)


def test_cli_init_preset_angenet_creates_the_ledger_and_passes_check(
    cli_isolated: None, tmp_path: Path
) -> None:
    """Verify bullet 1: ``tm init --preset angenet`` builds and validates."""
    result = CliRunner().invoke(
        app,
        [
            "init",
            "angenetled",
            "--preset",
            "angenet",
            "--id-prefix",
            "77",
            "--author",
            FACTORY_AUTHOR,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created ledger angenetled" in result.output
    assert "30 kinds" in result.output

    ledger = open_ledger(tmp_path / "angenetled")
    try:
        assert set(ledger.schema.kinds) == ANGENET_KINDS
        assert ledger.schema.preset_versions == ANGENET_VERSIONS
        tables = table_names(ledger)
        for kind_name, kind in ledger.schema.kinds.items():
            assert f"{kind.object.value}_{kind_name}" in tables, kind_name
            # Spec 5: box kinds have no v_box_<kind> view; each box gets its own.
            if kind.object.value != "box":
                assert f"v_{kind.object.value}_{kind_name}" in tables, kind_name
        assert "aliquot_animal_biosample" not in tables  # the tube model, spec 3
        # The ledger copies the bundle and every included part, byte for byte.
        copies = ledger.path / "presets"
        assert sorted(item.name for item in copies.iterdir()) == [
            "angenet.yaml",
            "base.yaml",
            "biosamples.yaml",
            "c_elegans.yaml",
            "drosophila.yaml",
            "mouse.yaml",
            "nucleic_acids.yaml",
            "proteins.yaml",
            "zebrafish.yaml",
        ]
        assert ledger.check().passed

        checked = ok(ledger, "check")
        assert "FAIL" not in checked.output
        assert "OK:" in checked.output
        planned = ok(ledger, "schema", "plan")
        assert "up to date" in planned.output
    finally:
        ledger.close()


def test_angenet_parts_are_listed_by_the_preset_catalogue() -> None:
    assert set(ORGANISM_PARTS) | {"angenet"} <= set(list_presets(REPO_PRESETS))


# ---------------------------------------------------------------------------
# Mouse: strain -> breeders -> litter -> genotyping split -> lineage
# ---------------------------------------------------------------------------


def test_mouse_litter_workflow_and_lineage(ledger_factory) -> None:
    """Verify bullets 2 and 5: the litter chain, both parents and the split."""
    ledger = ledger_factory(["angenet"], name="angenet_mouse", real_init=True)

    strain = ok_json(
        ledger,
        "entity",
        "add",
        "mouse_strain",
        "--name",
        "B6J-Apobec",
        "--set",
        "background=C57BL/6J",
        "--set",
        "supplier=Jackson Laboratory",
        "--set",
        "mgi_id=MGI:3029029",
    )
    cage = ok_json(ledger, "box", "add", "cage", "MC-01")
    dam_stock = ok_json(
        ledger,
        "stock",
        "add",
        "mouse_cohort",
        "--entity",
        strain["id"],
        "--set",
        "sex=f",
        "--set",
        "birth_on=2026-05-01",
        "--quantity",
        "1",
        "--unit",
        "mouse",
        "--box",
        "MC-01",
    )
    sire_stock = ok_json(
        ledger,
        "stock",
        "add",
        "mouse_cohort",
        "--entity",
        strain["id"],
        "--set",
        "sex=m",
        "--set",
        "birth_on=2026-05-01",
        "--quantity",
        "1",
        "--unit",
        "mouse",
        "--box",
        "MC-01",
    )
    dam_aliquot, sire_aliquot = dam_stock["aliquots"][0], sire_stock["aliquots"][0]
    # One female and one male breeder, each one mouse in the same cage.
    breeders = ledger.driver.query(
        "SELECT c.`sex`, a.`box_id`, a.`slot` FROM `stock_mouse_cohort` c "
        "JOIN `aliquot` a ON a.`stock_id` = c.`id` "
        "WHERE c.`id` IN (%s, %s) ORDER BY c.`sex`",
        (dam_stock["id"], sire_stock["id"]),
    )
    assert [(row["sex"], row["box_id"], row["slot"]) for row in breeders] == [
        ("f", cage["id"], None),
        ("m", cage["id"], None),
    ]

    # The litter: genotype NULL, one group aliquot with quantity 7, parents the
    # two breeder aliquots.
    ok_json(ledger, "box", "add", "cage", "MC-02")
    litter = ok_json(
        ledger,
        "stock",
        "add",
        "mouse_cohort",
        "--entity",
        strain["id"],
        "--box",
        "MC-02",
        "--quantity",
        "7",
        "--unit",
        "individuals",
        "--set",
        "birth_on=2026-07-01",
        "--set",
        f"dam={dam_aliquot}",
        "--set",
        f"sire={sire_aliquot}",
    )
    litter_aliquot = litter["aliquots"][0]
    litter_row = ledger.driver.query(
        "SELECT `genotype`, `dam`, `sire` FROM `stock_mouse_cohort` WHERE `id` = %s",
        (litter["id"],),
    )[0]
    assert litter_row == {
        "genotype": None,
        "dam": dam_aliquot,
        "sire": sire_aliquot,
    }
    litter_aliquot_row = ledger.driver.query(
        "SELECT `quantity`, `unit`, `status`, `box_id` FROM `aliquot` WHERE `id` = %s",
        (litter_aliquot,),
    )[0]
    assert float(litter_aliquot_row["quantity"]) == 7.0
    assert litter_aliquot_row["unit"] == "individuals"
    assert litter_aliquot_row["status"] == "stored"

    # Genotyping: one stock per genotype × sex, each pup an aliquot, linked to
    # the litter through the core parent_stock_id.
    ok_json(ledger, "box", "add", "cage", "MC-03")
    ok_json(ledger, "box", "add", "cage", "MC-04")
    pups = []
    for genotype, sex, box in (
        ("Apobec+/-", "f", "MC-03"),
        ("Apobec-/-", "m", "MC-04"),
    ):
        pups.append(
            ok_json(
                ledger,
                "stock",
                "add",
                "mouse_cohort",
                "--entity",
                strain["id"],
                "--parent",
                litter["id"],
                "--box",
                box,
                "--aliquots",
                "2",
                "--quantity",
                "1",
                "--unit",
                "mouse",
                "--set",
                f"genotype={genotype}",
                "--set",
                f"sex={sex}",
                "--set",
                "birth_on=2026-07-01",
            )
        )
    split = ledger.driver.query(
        "SELECT c.`genotype`, c.`sex`, s.`parent_stock_id` "
        "FROM `stock_mouse_cohort` c JOIN `stock` s ON s.`id` = c.`id` "
        "WHERE c.`id` IN (%s, %s) ORDER BY c.`genotype`",
        (pups[0]["id"], pups[1]["id"]),
    )
    assert [(row["genotype"], row["sex"], row["parent_stock_id"]) for row in split] == [
        ("Apobec+/-", "f", litter["id"]),
        ("Apobec-/-", "m", litter["id"]),
    ]
    for pup in pups:
        assert len(pup["aliquots"]) == 2

    # The litter aliquot is finished once the litter is split: the fixed
    # status says the material is gone, the detail is a stock property.
    status = ok(ledger, "aliquot", "status", litter_aliquot, "consumed")
    assert "stored -> consumed" in status.output
    ok(ledger, "stock", "edit", litter["id"], "--set", "fate=euthanized")
    assert ledger.driver.query(
        "SELECT `fate` FROM `stock_mouse_cohort` WHERE `id` = %s", (litter["id"],)
    ) == [{"fate": "euthanized"}]

    # Descendants of the strain: breeders -> their aliquots -> litter -> pups.
    down = ok(ledger, "lineage", strain["id"], "--down").output
    for text in (
        f"mouse_cohort {dam_stock['id']}",
        f"mouse_cohort {sire_stock['id']}",
        f"mouse_cohort {litter['id']}",
        f"mouse_cohort {pups[0]['id']}",
        f"mouse_cohort {pups[1]['id']}",
        "↓ mouse_cohort.dam",
        "↓ mouse_cohort.sire",
        "↓ parent_stock",
    ):
        assert text in down, (text, down)

    # Ancestors of a split stock reach the litter and both parents.
    up = ok(ledger, "lineage", pups[0]["id"], "--up").output
    for text in (
        f"parent_stock mouse_cohort {litter['id']}",
        f"aliquot {dam_aliquot} of stock {dam_stock['id']}",
        f"aliquot {sire_aliquot} of stock {sire_stock['id']}",
        "↑ mouse_cohort.dam",
        "↑ mouse_cohort.sire",
    ):
        assert text in up, (text, up)

    payload = ok_json(ledger, "lineage", pups[0]["id"], "--up")
    edges = {(edge["from"], edge["to"], edge["via"]) for edge in payload["edges"]}
    assert (pups[0]["id"], litter["id"], "parent_stock") in edges
    assert (litter["id"], dam_aliquot, "mouse_cohort.dam") in edges
    assert (dam_aliquot, dam_stock["id"], "mouse_cohort.dam") in edges
    assert (litter["id"], sire_aliquot, "mouse_cohort.sire") in edges
    assert (sire_aliquot, sire_stock["id"], "mouse_cohort.sire") in edges
    assert ledger.check().passed


def test_animal_transitions_follow_the_fixed_set(ledger_factory) -> None:
    """Spec 6: stored -> consumed works; the reverse needs --force."""
    ledger = ledger_factory(["angenet"], name="angenet_animal_status")
    strain = ok_json(ledger, "entity", "add", "mouse_strain", "--name", "C57BL/6J")
    # An in-stock animal sits in a cage; consuming it clears the position and
    # restoring it means placing it again.
    ok_json(ledger, "box", "add", "cage", "CAGE-1")
    cohort = ok_json(
        ledger,
        "stock",
        "add",
        "mouse_cohort",
        "--entity",
        strain["id"],
        "--box",
        "CAGE-1",
        "--set",
        "sex=f",
        "--set",
        "birth_on=2026-05-01",
        "--quantity",
        "1",
        "--unit",
        "mouse",
    )
    aliquot = cohort["aliquots"][0]
    assert "stored -> consumed" in ok(
        ledger, "aliquot", "status", aliquot, "consumed"
    ).output
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (aliquot,)
    ) == [{"box_id": None, "slot": None}]

    message = refused(run(ledger, "aliquot", "status", aliquot, "stored"))
    assert "cannot change an aliquot" in message
    assert "consumed" in message and "stored" in message

    # Forcing the status does not bypass the position rule: ``stored`` is in
    # stock, so the mouse needs a cage first.
    forced = run(
        ledger,
        "aliquot",
        "status",
        aliquot,
        "stored",
        "--force",
        "-m",
        "the consumption was recorded on the wrong mouse",
    )
    assert forced.exit_code == 1
    assert "tm aliquot move" in str(forced.exception)

    # Placing the mouse again restores the in-stock status through one move.
    restored = ok(
        ledger, "aliquot", "move", aliquot, "--box", "CAGE-1",
        "-m", "placed back in CAGE-1",
    )
    assert "stored" in restored.output
    row = ledger.driver.query(
        "SELECT `old_status`, `new_status` FROM `aliquot_event` "
        "WHERE `aliquot_id` = %s AND `event` = 'move' ORDER BY `id` DESC LIMIT 1",
        (aliquot,),
    )[0]
    assert (row["old_status"], row["new_status"]) == ("consumed", "stored")
    assert ledger.check().passed


def test_mouse_cohort_view_counts_drop_as_mice_are_euthanized(ledger_factory) -> None:
    """Verify bullet 5: ``v_stock_mouse_cohort.aliquots_in_stock`` is live."""
    ledger = ledger_factory(["angenet"], name="angenet_mouse_view")
    strain = ok_json(ledger, "entity", "add", "mouse_strain", "--name", "BALB/c")
    ok_json(ledger, "box", "add", "cage", "CAGE-1")
    cohort = ok_json(
        ledger,
        "stock",
        "add",
        "mouse_cohort",
        "--entity",
        strain["id"],
        "--box",
        "CAGE-1",
        "--set",
        "sex=f",
        "--set",
        "birth_on=2026-04-01",
        "--aliquots",
        "3",
        "--quantity",
        "1",
        "--unit",
        "mouse",
    )

    def counts() -> tuple[int, int, str]:
        row = ledger.driver.query(
            "SELECT `aliquots_in_stock`, `aliquots_total`, `status` "
            "FROM `v_stock_mouse_cohort` WHERE `id` = %s",
            (cohort["id"],),
        )[0]
        return int(row["aliquots_in_stock"]), int(row["aliquots_total"]), str(row["status"])

    assert counts() == (3, 3, "active")
    ok(ledger, "aliquot", "status", cohort["aliquots"][0], "consumed")
    assert counts() == (2, 3, "active")
    ok(ledger, "aliquot", "status", cohort["aliquots"][1], "consumed")
    assert counts() == (1, 3, "active")
    ok(ledger, "aliquot", "status", cohort["aliquots"][2], "consumed")
    assert counts() == (0, 3, "depleted")
    assert ledger.check().passed


# ---------------------------------------------------------------------------
# Fly cross and worm cryo
# ---------------------------------------------------------------------------


def test_fly_cross_vial_names_the_virgins_aliquot(ledger_factory) -> None:
    """Verify bullet 4: a cross vial's dam is a virgins vial aliquot."""
    ledger = ledger_factory(["angenet"], name="angenet_fly")
    line = ok_json(
        ledger,
        "entity",
        "add",
        "fly_line",
        "--name",
        "w1118",
        "--set",
        "balancers=CyO",
        "--set",
        "stock_centre=BDSC",
        "--set",
        "stock_number=12345",
    )
    ok_json(ledger, "box", "add", "incubator", "INC-25")
    virgins = ok_json(
        ledger,
        "stock",
        "add",
        "fly_culture",
        "--entity",
        line["id"],
        "--box",
        "INC-25",
        "--quantity",
        "20",
        "--unit",
        "individuals",
        "--set",
        "temperature_c=25",
        "--set",
        "food_type=standard",
        "--set",
        "generation=P0",
        "--set",
        "notes=virgin females collected 2026-10-20",
    )
    males = ok_json(
        ledger,
        "stock",
        "add",
        "fly_culture",
        "--entity",
        line["id"],
        "--box",
        "INC-25",
        "--quantity",
        "10",
        "--unit",
        "individuals",
        "--set",
        "temperature_c=25",
        "--set",
        "food_type=standard",
        "--set",
        "generation=P0",
    )
    virgins_aliquot = virgins["aliquots"][0]
    cross = ok_json(
        ledger,
        "stock",
        "add",
        "fly_culture",
        "--entity",
        line["id"],
        "--box",
        "INC-25",
        "--quantity",
        "5",
        "--unit",
        "individuals",
        "--set",
        "temperature_c=25",
        "--set",
        "food_type=standard",
        "--set",
        "virgins_collected_on=2026-10-20",
        "--set",
        f"dam={virgins_aliquot}",
        "--set",
        f"sire={males['aliquots'][0]}",
    )

    stored = ledger.driver.query(
        "SELECT `dam`, `sire`, `virgins_collected_on` FROM `stock_fly_culture` "
        "WHERE `id` = %s",
        (cross["id"],),
    )[0]
    assert stored["dam"] == virgins_aliquot
    assert stored["sire"] == males["aliquots"][0]
    assert stored["virgins_collected_on"] == "2026-10-20"

    up = ok(ledger, "lineage", cross["id"], "--up").output
    assert f"↑ fly_culture.dam aliquot {virgins_aliquot}" in up
    assert f"fly_culture {virgins['id']}" in up
    assert f"fly_culture.sire aliquot {males['aliquots'][0]}" in up
    # The cross is recorded by its dam/sire refs, not by a status; the vial
    # itself follows the fixed set like every other aliquot.
    assert "stored -> consumed" in ok(
        ledger, "aliquot", "status", cross["aliquots"][0], "consumed"
    ).output
    assert ledger.check().passed


def test_worm_cryo_stored_consumed_is_final(ledger_factory) -> None:
    """Spec 6: a thawed cryo vial is consumed, and consumed is final."""
    ledger = ledger_factory(["angenet"], name="angenet_worm")
    strain = ok_json(
        ledger,
        "entity",
        "add",
        "worm_strain",
        "--name",
        "N2",
        "--set",
        "allele=N2",
        "--set",
        "lab_prefix=CB",
        "--set",
        "wormbase_id=WBStrain00000001",
    )
    ok_json(ledger, "box", "add", "freezer", "CRYO-1")
    cryo = ok_json(
        ledger,
        "stock",
        "add",
        "worm_cryo",
        "--entity",
        strain["id"],
        "--box",
        "CRYO-1",
        "--quantity",
        "1",
        "--unit",
        "vial",
        "--set",
        "frozen_on=2026-01-10",
        "--set",
        "method=-80 C soft agar",
        "--set",
        "recovery_tested_on=2026-01-20",
        "--set",
        "recovery_ok=true",
    )
    vial = cryo["aliquots"][0]
    row = ledger.driver.query(
        "SELECT `status`, `recovery_ok` FROM `aliquot` "
        "JOIN `stock_worm_cryo` ON `stock_worm_cryo`.`id` = `aliquot`.`stock_id` "
        "WHERE `aliquot`.`id` = %s",
        (vial,),
    )[0]
    assert row["status"] == "stored"  # cryo has no pending: created in stock
    assert int(row["recovery_ok"]) == 1

    assert "stored -> consumed" in ok(
        ledger, "aliquot", "status", vial, "consumed"
    ).output
    message = refused(run(ledger, "aliquot", "status", vial, "stored"))
    assert "consumed" in message and "stored" in message
    assert "final status" in message
    assert ledger.check().passed


# ---------------------------------------------------------------------------
# Coexistence with the merged bundles
# ---------------------------------------------------------------------------


def test_angenet_does_not_disturb_molbio_and_cellbio(ledger_factory) -> None:
    """Verify: ``[molbio, cellbio]`` still loads with the angenet files present."""
    ledger = ledger_factory(["molbio", "cellbio"], name="molbio_cellbio_after29", real_init=True)
    kinds = set(ledger.schema.kinds)
    assert {"antibody", "virus", "bacterial_strain", "cell_biosample"} <= kinds
    # The organism kinds are angenet-only: they are not pulled in by existing
    # bundle names.
    assert "mouse_cohort" not in kinds and "animal_biosample" not in kinds
    assert "fly_line" not in kinds
    checked = ok(ledger, "check")
    assert "FAIL" not in checked.output
    assert "up to date" in ok(ledger, "schema", "plan").output

    # All three bundles also share the common parts once and load together.
    union = ledger_factory(
        ["molbio", "cellbio", "angenet"], name="all_bundles", real_init=True
    )
    assert ANGENET_KINDS <= set(union.schema.kinds)
    assert "bacterial_strain" in union.schema.kinds  # molbio
    assert "antibody" in union.schema.kinds  # cellbio
    assert union.check().passed
    assert "up to date" in ok(union, "schema", "plan").output
