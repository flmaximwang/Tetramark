"""The documentation's commands must actually run (ticket 14).

Four flows are executed against real temporary standalone ledgers, all through
the embedded driver (no ``dolt sql-server``):

* the README quickstart;
* the command reference (every command group);
* the schema skill's local-kind / rules / destructive workflow;
* the migration recipe's explicit old IDs and CSV round-trip.

A ledger is one self-contained directory in 0.5.0 (``.dolt/`` plus
``ledger.yaml``), so each flow runs from a plain lab directory and points the
CLI at the ledger with ``--ledger PATH`` -- the same way a user with several
ledger directories does.  Nothing is mocked: each CLI call goes through the
real ``dolt`` binary with an isolated ``DOLT_ROOT_PATH``/``HOME`` from
``conftest.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app

runner = CliRunner()

AUTHOR = "Docs Test <docs@tetramark.invalid>"

#: The ledger every ``run`` targets (``--ledger PATH``); see :func:`new_ledger`.
_CURRENT: Path | None = None


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def docs_env(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> Path:
    """Run the CLI from an empty lab directory with isolated Dolt/Git config."""
    global _CURRENT
    _CURRENT = None
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    # P8: bare preset names resolve from the bundled presets, never the shell.
    monkeypatch.delenv("TETRAMARK_PRESETS_DIR", raising=False)
    lab = tmp_path / "lab"
    lab.mkdir()
    monkeypatch.chdir(lab)
    return lab


def use_ledger(path: Path) -> Path:
    """Make ``path`` the ledger every subsequent :func:`run` operates on."""
    global _CURRENT
    _CURRENT = path.resolve()
    return _CURRENT


def new_ledger(
    name: str = "lab101",
    *,
    preset: str = "molbio",
    id_prefix: str = "12",
) -> Path:
    """``tm init NAME`` from the lab directory, then target that ledger."""
    run("init", name, "--preset", preset, "--id-prefix", id_prefix, "--author", AUTHOR)
    ledger = use_ledger(Path.cwd() / name)
    assert (ledger / ".dolt").is_dir()
    assert (ledger / "ledger.yaml").is_file()
    return ledger


def run(*args: str, expect: int = 0):
    """Invoke the CLI (targeting the current ledger) and assert its exit code."""
    argv = list(args)
    if _CURRENT is not None:
        argv = ["--ledger", str(_CURRENT), *argv]
    result = runner.invoke(app, argv)
    assert result.exit_code == expect, (
        f"ledger {' '.join(argv)} exited {result.exit_code} (expected {expect}):\n"
        f"{result.output}\n{result.exception!r}"
    )
    return result


def run_json(*args: str):
    result = run(*args, "--json")
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# README quickstart
# ---------------------------------------------------------------------------


def test_readme_quickstart_runs(docs_env: Path) -> None:
    """init -> box/entity/stock -> show -> check (README quickstart)."""
    del docs_env  # the fixture only changes cwd / environment
    ledger = new_ledger()
    assert ledger.name == "lab101"
    assert (ledger / ".dolt").is_dir()
    assert (ledger / "ledger.yaml").is_file()

    created_box = run_json("box", "add", "grid8x12", "WFL-12-01")
    assert created_box["slots"] == 96

    created_entity = run_json(
        "entity",
        "add",
        "plasmid",
        "--name",
        "pZET035",
        "--set",
        "backbone=pZET035",
        "--set",
        "resistance=kan",
        "--alias",
        "p035",
    )
    assert created_entity["aliases"] == ["p035"]

    created_stock = run_json(
        "stock",
        "add",
        "bacterial_glycerol",
        "--entity",
        "p035",
        "--set",
        "strain=BL21(DE3)",
        "--set",
        "medium=LB",
        "--box",
        "WFL-12-01",
        "--slot",
        "A1",
        "--quantity",
        "500",
        "--unit",
        "ul",
    )
    stock_id = created_stock["id"]
    assert created_stock["aliquots"] == [f"{stock_id}.1"]

    shown = run("box", "show", "WFL-12-01")
    assert "WFL-12-01" in shown.output
    assert "1 of 96 slots used" in shown.output
    assert f"{stock_id}.1" in shown.output

    found = run("find", "pZET035")
    assert "pZET035" in found.output
    assert "WFL-12-01:A1" in found.output

    checked = run("check")
    assert "OK:" in checked.output

    history = run("history", stock_id)
    assert stock_id in history.output
    assert "create" in history.output




# ---------------------------------------------------------------------------
# Command reference: every command group
# ---------------------------------------------------------------------------


def test_command_reference_object_and_read_flows(docs_env: Path) -> None:
    """Every command in references/commands.md except schema changes and import."""
    del docs_env
    ledger = new_ledger(id_prefix="77")

    presets = run("presets", "list")
    assert "molbio" in presets.output
    preset = run("presets", "show", "molbio")
    assert "preset: molbio" in preset.output

    schema = run("schema", "show")
    assert "plasmid" in schema.output
    assert "kinds" in run_json("schema", "show")
    one_kind = run("schema", "show", "item")
    assert "kind: item" in one_kind.output
    assert "schema is up to date" in run("schema", "plan").output

    # Entities: add/show/edit/ls, with --set, --name, --alias and --json.
    item = run_json(
        "entity", "add", "item", "--name", "Reagent", "--set", "category=chemical",
        "--alias", "rea",
    )
    item_id = item["id"]
    shown = run_json("entity", "show", "rea")
    assert shown["id"] == item_id and shown["category"] == "chemical"
    edited = run_json("entity", "edit", "rea", "--set", "category=reagent")
    assert edited["category"] == "reagent"
    listed = run_json("entity", "ls", "item", "--where", "category=reagent")
    assert [row["id"] for row in listed] == [item_id]
    assert [row["id"] for row in run_json("entity", "ls") if row["kind"] == "item"] == [
        item_id
    ]

    # Boxes: grid, rack, slotless; nest, move, list, show.
    assert run_json("box", "add", "grid8x12", "BOX-1")["slots"] == 96
    assert run_json("box", "add", "grid9x9", "BOX-2")["slots"] == 81
    assert run_json("box", "add", "rack", "RACK-1")["slots"] == 10
    run("box", "add", "freezer", "FR-1")
    run("box", "move", "BOX-1", "--in", "FR-1")
    # Spec 5: grid boxes hold tubes only, so a box nests in a slotless freezer
    # (a grid box can no longer be a parent).  The doc reference matches.
    run("box", "move", "BOX-2", "--in", "FR-1")
    boxes = run("box", "ls", "--kind", "grid8x12")
    assert "BOX-1" in boxes.output
    box_path = run_json("box", "show", "BOX-2")
    assert "BOX-2" in json.dumps(box_path)
    grid = run("box", "show", "BOX-2")
    assert "0 of 81 slots used" in grid.output
    grid_csv = run("--format", "csv", "box", "show", "BOX-2")
    assert grid_csv.output.splitlines()[0] == "slot,row,col,occupants"

    # Stocks: add/show/edit/ls, aliquots, placement.
    construct = run_json(
        "entity", "add", "protein_construct", "--name", "C1",
        "--set", "sequence=atgc", "--alias", "c1",
    )
    assert construct["name"] == "C1"
    stock = run_json(
        "stock", "add", "protein_prep", "--entity", "c1",
        "--set", "concentration_mg_ml=1.5", "--aliquots", "2",
        "--box", "BOX-2", "--slot", "A1", "--slot", "A2",
        "--quantity", "100", "--unit", "ul",
    )
    stock_id = stock["id"]
    assert stock["aliquots"] == [f"{stock_id}.1", f"{stock_id}.2"]
    stock_shown = run_json("stock", "show", stock_id)
    assert len(stock_shown["aliquots"]) == 2
    run("stock", "edit", stock_id, "--set", "buffer=TE")
    assert "buffer" in run("stock", "show", stock_id).output
    filtered = run_json("stock", "ls", "protein_prep", "--where", "buffer=TE")
    assert [row["id"] for row in filtered] == [stock_id]
    assert any(
        row["id"] == stock_id for row in run_json("stock", "ls")
    )

    # Aliquot add/move/use/status/purge and per-tube events.
    added = run_json(
        "aliquot", "add", stock_id, "--count", "2",
        "--box", "BOX-2", "--slot", "B1", "--slot", "B2",
        "--quantity", "50", "--unit", "ul",
    )
    assert added["ids"] == [f"{stock_id}.3", f"{stock_id}.4"]
    run("aliquot", "move", f"{stock_id}.4", "--box", "BOX-1", "--slot", "C3")
    used = run_json(
        "aliquot", "use", f"{stock_id}.3", "20", "ul", "--purpose", "assay"
    )
    assert used["status"] == "stored"
    missing = run_json(
        "aliquot", "status", f"{stock_id}.4", "missing", "--note", "not found"
    )
    assert missing["status"] == "missing"
    purged = run_json("aliquot", "purge", f"{stock_id}.4")
    assert purged["id"] == f"{stock_id}.4"

    # Aliases, search, history.
    run("alias", "add", "stock", stock_id, "protein-c1")
    alias_found = run("find", "protein-c1")
    assert stock_id in alias_found.output and "alias" in alias_found.output
    run("alias", "rm", "protein-c1")
    run("alias", "add", "entity", item_id, "reagent-1")
    run("alias", "rm", "entity", "reagent-1")
    found_entity = run("find", "reagent")
    assert "Reagent" in found_entity.output
    by_id = run("find", item_id)
    assert item_id in by_id.output
    history = run("history", stock_id)
    assert stock_id in history.output and "Events" in history.output
    history_json = run_json("history", stock_id)
    assert history_json["id"] == stock_id
    assert history_json["events"]

    # Check variants.
    checked = run("check")
    assert "OK:" in checked.output
    verbose = run("check", "-v")
    assert "OK:" in verbose.output
    grouped = run("check", "--group", "core")
    assert "core" in grouped.output
    as_csv = run("check", "--format", "csv")
    assert as_csv.output.splitlines()[0] == "status,group,name,message"
    assert run_json("check")["ok"] is True

    # Global output formats and dry run.
    assert run("--format", "csv", "entity", "ls", "item").output.startswith("id,kind,name")
    dry = run("--dry-run", "entity", "add", "item", "--name", "dry-run-only")
    assert "INSERT INTO" in dry.output
    names = [row["name"] for row in run_json("entity", "ls", "item")]
    assert "dry-run-only" not in names
    run("--no-commit", "entity", "add", "item", "--name", "no-commit-item")

    # Single-kind export/import (the other import form in commands.md).
    single = Path.cwd() / "single-export"
    run("export", "item", "--out", str(single))
    assert (single / "item.csv").is_file()
    new_ledger("itemcopy", id_prefix="77")
    run("import", "item", str(single / "item.csv"))
    copied_items = run_json("entity", "ls", "item")
    assert {row["name"] for row in copied_items} >= {"Reagent", "no-commit-item"}
    use_ledger(ledger)

    # Configuration.
    assert AUTHOR in run("config", "get", "author").output
    new_author = "New Author <new@tetramark.invalid>"
    run("config", "set", "author", new_author)
    assert new_author in run("config", "get", "author").output

    # Remotes and backups.
    remote_dir = Path.cwd() / "remote"
    run("remote", "add", "origin", str(remote_dir))
    assert str(remote_dir) in run("remote", "ls").output
    run("remote", "push")
    run("remote", "pull")
    run("remote", "rm", "origin")
    backup_dir = Path.cwd() / "backup"
    run("backup", str(backup_dir))
    backups = run("backup", "--ls")
    assert "lab101" in backups.output
    assert "lab101" in run("backup", "ls").output


# ---------------------------------------------------------------------------
# Schema skill workflow
# ---------------------------------------------------------------------------


def test_schema_skill_local_kind_rules_and_destructive_flow(
    docs_env: Path,
) -> None:
    """kinds_dir + rules_dir, plan/apply, destructive refusal and override."""
    del docs_env
    ledger_dir = new_ledger(preset="base", id_prefix="77")

    (ledger_dir / "kinds").mkdir()
    (ledger_dir / "kinds" / "equipment.yaml").write_text(
        yaml.safe_dump(
            {
                "kinds": {
                    "equipment": {
                        "object": "entity",
                        "mixins": ["named_fields"],
                        "fields": {
                            "serial_no": {"type": "string", "length": 64, "unique": True},
                            "purchased_on": {"type": "date"},
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = yaml.safe_load((ledger_dir / "ledger.yaml").read_text(encoding="utf-8"))
    config["kinds_dir"] = "kinds/"
    (ledger_dir / "ledger.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    effective = run("schema", "show", "equipment")
    assert "kind: equipment" in effective.output
    planned = run("schema", "plan")
    assert "[additive] create_table entity_equipment" in planned.output
    run("schema", "apply")
    assert "schema is up to date" in run("schema", "plan").output

    equipment = run_json(
        "entity", "add", "equipment", "--name", "Centrifuge", "--set", "serial_no=SN-1"
    )
    assert equipment["kind"] == "equipment"

    # Additive nullable field.
    kind_file = ledger_dir / "kinds" / "equipment.yaml"
    payload = yaml.safe_load(kind_file.read_text(encoding="utf-8"))
    payload["kinds"]["equipment"]["fields"]["location"] = {"type": "string"}
    kind_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    assert "[additive] add_column" in run("schema", "plan").output
    run("schema", "apply")

    # Destructive required field: refused without the flag, applied with it.
    payload["kinds"]["equipment"]["fields"]["inventory_no"] = {
        "type": "string",
        "required": True,
    }
    kind_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    destructive_plan = run("schema", "plan")
    assert "[destructive] add_column" in destructive_plan.output
    refused = run("schema", "apply", expect=1)
    assert "destructive" in refused.output
    run("schema", "apply", "--allow-destructive")
    assert "schema is up to date" in run("schema", "plan").output

    # Local rule: serial_no set but purchased_on missing must FAIL, then pass.
    (ledger_dir / "rules").mkdir()
    (ledger_dir / "rules" / "equipment.yaml").write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "EQ01",
                        "description": "serial numbers need a purchase date",
                        "severity": "fail",
                        "sql": (
                            "SELECT COUNT(*) FROM entity_equipment "
                            "WHERE serial_no IS NOT NULL AND purchased_on IS NULL"
                        ),
                        "expect": {"single_value": 0},
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = yaml.safe_load((ledger_dir / "ledger.yaml").read_text(encoding="utf-8"))
    config["rules_dir"] = "rules/"
    (ledger_dir / "ledger.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    run("schema", "apply")
    failing = run("check", "--group", "rules:local", expect=1)
    assert "EQ01" in failing.output
    run("entity", "edit", equipment["id"], "--set", "purchased_on=2026-01-01")
    assert "OK:" in run("check", "--group", "rules:local").output


# ---------------------------------------------------------------------------
# Migration recipe: explicit old IDs, aliases, CSV round-trip
# ---------------------------------------------------------------------------


def test_migration_recipe_explicit_ids_and_csv_roundtrip(
    docs_env: Path,
) -> None:
    """The zsqlab08 mapping in docs/guides/migrating-a-legacy-ledger.md runs end to end."""
    del docs_env
    zsqlab = new_ledger("zsqlab08", preset="molbio", id_prefix="08")

    run("box", "add", "grid9x9", "WFL-08-01")
    run("box", "add", "grid9x9", "WFL-08-02")

    construct_id = "cd0ae0ea-363f-4409-a432-bd50d5a2b7f3"
    plasmid_id = "5fe23f35-21fe-4274-bae8-168a3225ac3c"
    run_json(
        "entity", "add", "protein_construct", "--id", construct_id,
        "--name", "0801_4_Ori_6H.1@pZET043",
        "--set", "sequence=ATGC", "--set", "tag=6H",
    )
    run_json(
        "entity", "add", "plasmid", "--id", plasmid_id,
        "--name", "0801_5_Ori_6H.1@pZET043",
        "--set", "sequence=ATGC", "--set", "backbone=pZET043",
        "--set", f"construct={construct_id}", "--alias", "0801_5",
    )
    bacterial = run_json(
        "stock", "add", "bacterial_glycerol", "--id", "0826FP02",
        "--entity", plasmid_id,
        "--set", "strain=Top10", "--set", "batch=01",
        "--box", "WFL-08-01", "--slot", "A2",
    )
    prep = run_json(
        "stock", "add", "plasmid_prep", "--id", "0826FP21", "--entity", plasmid_id,
        "--set", "prep_method=miniprep",
        "--box", "WFL-08-02", "--slot", "A1",
    )
    assert bacterial["id"] == "0826FP02"
    assert prep["id"] == "0826FP21"

    # A non-date-code old UID becomes an alias (zsqlab10 pellet short code).
    pellet = run_json(
        "stock", "add", "bacterial_pellet", "--entity", plasmid_id,
        "--aliquots", "2",
        "--quantity", "500", "--unit", "ml",
        "--set", "culture_volume_ml=1000", "--set", "od600=2.2",
    )
    run("alias", "add", "stock", pellet["id"], "4a334ac2")
    found = run("find", "4a334ac2")
    assert pellet["id"] in found.output

    assert "WFL-08-01:A2" in run("find", "0826FP02").output
    shown = run("box", "show", "WFL-08-01")
    assert "0826FP02.1" in shown.output
    assert "1 of 81 slots used" in shown.output
    run("check")

    # Row counts per kind (header included in CSV).
    construct_count = len(
        run("--format", "csv", "entity", "ls", "protein_construct")
        .output.splitlines()
    )
    plasmid_count = len(
        run("--format", "csv", "entity", "ls", "plasmid").output.splitlines()
    )
    bacterial_count = len(
        run("--format", "csv", "stock", "ls", "bacterial_glycerol")
        .output.splitlines()
    )
    prep_count = len(
        run("--format", "csv", "stock", "ls", "plasmid_prep").output.splitlines()
    )
    assert (construct_count, plasmid_count, bacterial_count, prep_count) == (2, 2, 2, 2)

    # Export all, import into a fresh ledger, and verify it round-trips.
    exports = Path.cwd() / "exports"
    run("export", "all", "--out", str(exports))
    for name in (
        "protein_construct.csv",
        "plasmid.csv",
        "bacterial_glycerol.csv",
        "plasmid_prep.csv",
        "bacterial_pellet.csv",
        "boxes.csv",
        "aliquots.csv",
        "aliases.csv",
    ):
        assert (exports / name).is_file(), name

    new_ledger("lab42", preset="molbio", id_prefix="08")
    run("--dry-run", "import", "all", str(exports))
    run("import", "all", str(exports))
    assert "OK:" in run("check").output
    copied = run_json("find", "0826FP02")
    assert copied

    # Importing into the source ledger rejects existing IDs, all-or-nothing.
    use_ledger(zsqlab)
    rejected = run("import", "all", str(exports), expect=1)
    assert "already exists" in str(rejected.exception)


# ---------------------------------------------------------------------------
# Skill/documentation contract
# ---------------------------------------------------------------------------


def test_skills_have_frontmatter_triggers_and_disclosure() -> None:
    """Skills parse as frontmatter + body, with the promised triggers."""
    root = Path(__file__).resolve().parents[1]
    skill_file = root / "skills" / "tetramark" / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    front = yaml.safe_load(match.group(1))
    assert front["name"] == "tetramark"
    assert front["description"].startswith("Use when")
    for trigger in ("台账", "样品", "盒", "冻存", "库存", "管", "分装"):
        assert trigger in front["description"]
    assert len(text.splitlines()) <= 150

    for relative in (
        "skills/tetramark/references/commands.md",
        "skills/tetramark/references/recipes.md",
        "skills/tetramark-schema/SKILL.md",
        "docs/guides/migrating-a-legacy-ledger.md",
        "README.md",
    ):
        assert (root / relative).is_file(), relative

    schema_text = (root / "skills" / "tetramark-schema" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    schema_front = yaml.safe_load(
        re.match(r"^---\n(.*?)\n---\n", schema_text, re.DOTALL).group(1)
    )
    assert schema_front["name"] == "tetramark-schema"
    assert schema_front["description"].startswith("Use when")
    assert (root / "skills" / "tetramark-schema" / "references" / "presets.md").is_file()
    assert (root / "skills" / "tetramark-schema" / "references" / "evolution.md").is_file()
