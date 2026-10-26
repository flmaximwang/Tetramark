"""Ledger init, open and the one-commit write session (ticket 06).

Everything runs against a real ``dolt`` binary; the workspace, Dolt root and
Git config all live under ``tmp_path``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tetramark import Ledger, __version__, init_ledger, open as open_ledger
from tetramark.core.ledger import InitPlan, WriteSession
from tetramark.core.ids import DatecodeIds, UuidIds
from tetramark.driver.workspace import Workspace
from tetramark.errors import EXIT_USAGE, LedgerError, UnreachableError, UsageError

from conftest import FACTORY_AUTHOR, PRESETS

CLI_AUTHOR = "CLI User <cli@tetramark.invalid>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path, dolt_env: dict[str, str], **overrides: object) -> Workspace:
    return Workspace.create(tmp_path / "data", env=dolt_env, **overrides)


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def latest_commit(ledger: Ledger) -> dict[str, object]:
    return ledger.driver.query(
        "SELECT message, author, author_email FROM dolt_log "
        "ORDER BY date DESC, commit_order DESC LIMIT 1"
    )[0]


def table_names(ledger: Ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


def read_config(ledger: Ledger) -> dict[str, object]:
    return yaml.safe_load((ledger.path / "ledger.yaml").read_text(encoding="utf-8"))


def write_config(ledger: Ledger, payload: dict[str, object]) -> None:
    (ledger.path / "ledger.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_database_schema_and_one_commit(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(
        workspace,
        "lab101",
        [PRESETS / "full"],
        id_prefix="12",
        author="Init Author <init@tetramark.invalid>",
    )

    assert isinstance(ledger, Ledger)
    assert ledger.path == workspace.ledger_path("lab101")
    assert (ledger.path / ".dolt").is_dir()
    assert (ledger.path / "ledger.yaml").is_file()

    tables = table_names(ledger)
    for name in (
        "ledger_meta",
        "entity",
        "stock",
        "box",
        "slot",
        "aliquot",
        "aliquot_event",
        "alias",
        "entity_everything",
        "stock_prep",
        "aliquot_prep",
        "v_entity_everything",
        "v_stock_status",
    ):
        assert name in tables, name

    # dolt init committed once; tm init made exactly one more commit.
    assert commit_count(ledger) == 2
    message = latest_commit(ledger)
    assert message["message"] == "init(ledger): lab101"
    assert message["author"] == "Init Author"
    assert message["author_email"] == "init@tetramark.invalid"
    # A completed init leaves no uncommitted changes.
    assert ledger.driver.query("SELECT * FROM dolt_status") == []


def test_init_writes_ledger_yaml_and_ledger_meta(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(
        workspace,
        "meta",
        [PRESETS / "full"],
        id_prefix="08",
        author=FACTORY_AUTHOR,
    )
    config = read_config(ledger)

    assert config["name"] == "meta"
    assert config["presets"] == [str((PRESETS / "full").resolve())]
    assert config["author"] == FACTORY_AUTHOR
    assert config["ids"] == {
        "entity": {"strategy": "uuid"},
        "stock": {"strategy": "datecode", "prefix": "08"},
    }
    assert config["statuses"] == ["pending", "stored", "missing", "consumed"]

    meta = ledger.meta
    assert set(meta) == {
        "spec_version",
        "tetramark_version",
        "created_with",
        "fingerprint",
        "preset_versions",
        "ledger_yaml",
    }
    assert meta["spec_version"] == "1"
    assert meta["tetramark_version"] == __version__
    assert meta["created_with"] == __version__
    # Only ``tm init --adopt`` records the pre-adoption commit (TM-04).
    assert "adopted_from_commit" not in meta
    assert meta["fingerprint"] == ledger.schema.fingerprint() == ledger.fingerprint
    assert json.loads(meta["preset_versions"]) == {
        "base": 2,
        "chain": 3,
        "full": 1,
    }
    # The DB is self-describing: the stored copy is byte-identical.
    assert meta["ledger_yaml"] == (ledger.path / "ledger.yaml").read_text(
        encoding="utf-8"
    )


def test_init_custom_statuses_reach_the_ddl(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(
        workspace,
        "statuses",
        [PRESETS / "base"],
        author=FACTORY_AUTHOR,
        statuses=["pending", "stored", "quarantined"],
    )
    assert ledger.schema.statuses == ["pending", "stored", "quarantined"]
    assert read_config(ledger)["statuses"] == ["pending", "stored", "quarantined"]
    create = ledger.driver.query("SHOW CREATE TABLE aliquot")[0]["Create Table"]
    assert "'quarantined'" in create
    assert "'consumed'" not in create


def test_init_without_id_prefix_uses_uuid_stock_ids(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(
        workspace, "noprefix", [PRESETS / "base"], author=FACTORY_AUTHOR
    )
    assert isinstance(ledger.ids["entity"], UuidIds)
    assert isinstance(ledger.ids["stock"], UuidIds)
    assert read_config(ledger)["ids"]["stock"] == {"strategy": "uuid"}


def test_init_refuses_an_existing_ledger(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    init_ledger(workspace, "twice", [PRESETS / "base"], author=FACTORY_AUTHOR)
    with pytest.raises(LedgerError, match="already exists"):
        init_ledger(workspace, "twice", [PRESETS / "base"], author=FACTORY_AUTHOR)


def test_init_requires_a_preset(tmp_path: Path, dolt_env: dict[str, str]) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(UsageError, match="at least one preset"):
        init_ledger(workspace, "nopreset", author=FACTORY_AUTHOR)
    assert not workspace.ledger_path("nopreset").exists()


def test_init_without_author_or_git_config_is_a_usage_error(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(UsageError, match="author"):
        init_ledger(workspace, "noauthor", [PRESETS / "base"])
    # Nothing half-created is left behind.
    assert not workspace.ledger_path("noauthor").exists()


def test_init_rejects_an_invalid_ledger_name(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(UsageError, match="invalid ledger name"):
        init_ledger(workspace, "../escape", [PRESETS / "base"], author=FACTORY_AUTHOR)


def test_init_bad_preset_leaves_nothing_behind(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(LedgerError):
        init_ledger(
            workspace,
            "broken",
            [tmp_path / "missing-preset"],
            author=FACTORY_AUTHOR,
        )
    assert not workspace.ledger_path("broken").exists()


def test_init_dry_run_returns_a_plan_and_writes_nothing(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    plan = init_ledger(
        workspace,
        "planned",
        [PRESETS / "full"],
        id_prefix="12",
        author=FACTORY_AUTHOR,
        dry_run=True,
    )

    assert isinstance(plan, InitPlan)
    assert not plan.path.exists()
    assert "CREATE TABLE `ledger_meta`" in plan.sql
    assert "CREATE VIEW `v_stock_status`" in plan.sql
    assert plan.config.name == "planned"
    assert plan.author == FACTORY_AUTHOR


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def test_open_reopens_with_the_same_schema_and_strategies(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "full"], name="reopen", id_prefix="77")
    reopened = open_ledger(ledger.workspace, "reopen")

    assert reopened.name == "reopen"
    assert reopened.config.name == "reopen"
    assert reopened.schema.fingerprint() == ledger.schema.fingerprint()
    assert reopened.fingerprint == ledger.fingerprint
    assert reopened.author == FACTORY_AUTHOR
    assert isinstance(reopened.ids["entity"], UuidIds)
    assert isinstance(reopened.ids["stock"], DatecodeIds)
    assert reopened.ids["stock"].prefix == "77"
    assert set(reopened.meta) == {
        "spec_version",
        "tetramark_version",
        "created_with",
        "fingerprint",
        "preset_versions",
        "ledger_yaml",
    }


def test_open_uses_the_default_ledger_and_discovers_the_workspace(
    tmp_path: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.create(
        tmp_path / "data", default_ledger="default", env=dolt_env
    )
    init_ledger(workspace, "default", [PRESETS / "base"], author=FACTORY_AUTHOR)

    monkeypatch.chdir(workspace.data_dir)
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    discovered = open_ledger()
    assert discovered.name == "default"


def test_open_missing_ledger_points_at_the_workspace(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(UnreachableError, match="not found"):
        open_ledger(workspace, "absent")


def test_open_prefers_ledger_author_then_workspace_author(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = Workspace.create(
        tmp_path / "data", author="Workspace <ws@tetramark.invalid>", env=dolt_env
    )
    ledger = init_ledger(workspace, "authors", [PRESETS / "base"])
    assert ledger.author == "Workspace <ws@tetramark.invalid>"

    config = read_config(ledger)
    config["author"] = "Ledger <led@tetramark.invalid>"
    write_config(ledger, config)
    assert open_ledger(workspace, "authors").author == "Ledger <led@tetramark.invalid>"

    config.pop("author")
    write_config(ledger, config)
    assert open_ledger(workspace, "authors").author == "Workspace <ws@tetramark.invalid>"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_open_falls_back_to_git_config(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    for key, value in (
        ("user.name", "Git User"),
        ("user.email", "git@tetramark.invalid"),
    ):
        subprocess.run(
            ["git", "config", "--global", "--add", key, value],
            env=dolt_env,
            check=True,
            capture_output=True,
        )
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(workspace, "git", [PRESETS / "base"])

    assert ledger.author == "Git User <git@tetramark.invalid>"
    assert read_config(ledger)["author"] == "Git User <git@tetramark.invalid>"
    assert open_ledger(workspace, "git").author == "Git User <git@tetramark.invalid>"


def test_open_warns_when_the_live_fingerprint_differs(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "full"], name="stale")
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = 'deadbeef' WHERE `key` = 'fingerprint'"
    )

    with pytest.warns(UserWarning, match="tm schema plan"):
        reopened = open_ledger(ledger.workspace, "stale")
    assert reopened.warnings and "tm schema plan" in reopened.warnings[0]
    assert reopened.check_fingerprint(warn=False) is False


def test_open_is_quiet_when_the_fingerprint_matches(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="fresh")
    import warnings as warnings_module

    with warnings_module.catch_warnings(record=True) as caught:
        warnings_module.simplefilter("always")
        reopened = open_ledger(ledger.workspace, "fresh")
    assert not [w for w in caught if "schema plan" in str(w.message)]
    assert reopened.warnings == []
    assert reopened.check_fingerprint(warn=False) is True


# ---------------------------------------------------------------------------
# Ledger.write
# ---------------------------------------------------------------------------


def test_write_is_one_transaction_and_one_commit(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "full"])
    before = commit_count(ledger)

    with ledger.write("create probe", verb="add", object="box") as session:
        session.execute("CREATE TABLE write_probe (id BIGINT)")
        session.execute("INSERT INTO write_probe (id) VALUES (%s)", (1,))
        assert session.query("SELECT COUNT(*) AS n FROM write_probe") == [{"n": 1}]

    assert commit_count(ledger) == before + 1
    assert session.commit_hash
    assert ledger.driver.query("SELECT id FROM write_probe") == [{"id": 1}]
    latest = latest_commit(ledger)
    assert latest["message"] == "add(box): create probe"
    assert latest["author_email"] == "factory@tetramark.invalid"


def test_write_rolls_back_when_the_body_raises(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)
    ledger.driver.execute("CREATE TABLE rollback_probe (id BIGINT)")

    with pytest.raises(RuntimeError):
        with ledger.write("abort", verb="add", object="box") as session:
            session.execute("INSERT INTO rollback_probe (id) VALUES (%s)", (1,))
            raise RuntimeError("boom")

    assert ledger.driver.query("SELECT * FROM rollback_probe") == []
    assert commit_count(ledger) == before


def test_write_dry_run_collects_sql_and_executes_nothing(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)

    with ledger.write("create probe", verb="add", object="box", dry_run=True) as session:
        session.execute("CREATE TABLE dry_probe (id BIGINT)")
        session.execute("INSERT INTO dry_probe (id) VALUES (%s)", (1,))

    assert "CREATE TABLE dry_probe" in session.sql
    assert "INSERT INTO dry_probe" in session.sql
    assert "dry_probe" not in table_names(ledger)
    assert commit_count(ledger) == before


def test_write_no_commit_executes_but_does_not_commit(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)

    with ledger.write("create probe", verb="add", object="box", no_commit=True) as session:
        session.execute("CREATE TABLE uncommitted (id BIGINT)")

    assert "uncommitted" in table_names(ledger)
    assert commit_count(ledger) == before
    assert session.commit_hash is None


def test_open_carries_default_dry_run_and_no_commit(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"], name="flags")
    opened = open_ledger(ledger.workspace, "flags", dry_run=True)
    with opened.write("probe", verb="add", object="box") as session:
        session.execute("CREATE TABLE never_created (id BIGINT)")
    assert "never_created" not in table_names(opened)
    assert session.dry_run is True

    # An explicit argument wins over the ledger default.
    with opened.write("probe", verb="add", object="box", dry_run=False) as session:
        session.execute("CREATE TABLE really_created (id BIGINT)")
    assert "really_created" in table_names(opened)
    assert session.dry_run is False


# ---------------------------------------------------------------------------
# Branch selection (spec A3)
# ---------------------------------------------------------------------------


def _create_branch(ledger: Ledger, name: str) -> None:
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", (name,))


def _entity_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM `entity`")[0]["n"])


def test_open_on_a_branch_writes_there_and_leaves_main_unchanged(
    ledger_factory,
) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="branchy")
    _create_branch(ledger, "feature")
    before = commit_count(ledger)

    branch = open_ledger(ledger.workspace, "branchy", branch="feature")
    try:
        assert branch.branch == "feature"
        created = branch.entities.add("item", name="on-branch")
        assert created["name"] == "on-branch"

        # The write and its commit are on the branch only.
        assert _entity_count(branch) == 1
        assert _entity_count(ledger) == 0
        assert commit_count(ledger) == before
        assert ledger.driver.query(
            "SELECT 1 AS `found` FROM dolt_log WHERE `message` LIKE %s",
            ("add(entity)%",),
        ) == []
        assert ledger.driver.query(
            "SELECT COUNT(*) AS n FROM `entity` AS OF %s", ("feature",)
        ) == [{"n": 1}]
        # Neither session moved the repository's checkout; the branch session's
        # own checkout is the branch it selected.
        assert ledger.driver.query("SELECT active_branch() AS `b`") == [
            {"b": "main"}
        ]
        assert branch.driver.query("SELECT active_branch() AS `b`") == [
            {"b": "feature"}
        ]
    finally:
        branch.close()


def test_ledger_yaml_branch_is_the_default_for_open(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="branchdefault")
    _create_branch(ledger, "config-branch")
    config = read_config(ledger)
    config["branch"] = "config-branch"
    write_config(ledger, config)
    assert read_config(ledger)["branch"] == "config-branch"

    opened = open_ledger(ledger.workspace, "branchdefault")
    try:
        assert opened.branch == "config-branch"
        opened.entities.add("item", name="from-config")
        assert _entity_count(opened) == 1
        assert _entity_count(ledger) == 0
    finally:
        opened.close()


def test_missing_branch_raises_usage_and_creates_nothing(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="nobranch")
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="does not exist"):
        open_ledger(ledger.workspace, "nobranch", branch="absent")
    assert ledger.driver.query(
        "SELECT 1 AS `found` FROM dolt_branches WHERE `name` = %s", ("absent",)
    ) == []
    assert commit_count(ledger) == before


def test_open_with_the_checked_out_branch_uses_the_revision_database(
    ledger_factory,
) -> None:
    ledger = ledger_factory([PRESETS / "base"], name="samebranch")
    opened = open_ledger(ledger.workspace, "samebranch", branch="main")
    try:
        opened.entities.add("item", name="on-main")
        assert opened.branch == "main"
        assert _entity_count(ledger) == 1
        assert opened.driver.query("SELECT active_branch() AS `b`") == [
            {"b": "main"}
        ]
    finally:
        opened.close()


def test_write_requires_a_context_manager(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    session = ledger.write("probe", verb="add", object="box")
    assert isinstance(session, WriteSession)
    with pytest.raises(LedgerError, match="inside 'with"):
        session.execute("SELECT 1")


def test_write_refuses_non_ascii_commit_messages(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    with pytest.raises(LedgerError, match="ASCII"):
        ledger.write("加了一支管", verb="add", object="aliquot")


def test_write_refuses_a_malformed_verb_or_object(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    with pytest.raises(UsageError):
        ledger.write("x", verb="Add It", object="box")
    with pytest.raises(UsageError):
        ledger.write("x", verb="add", object="Box!")


def test_next_id_uses_the_configured_strategies(server_ledger_factory) -> None:
    ledger = server_ledger_factory([PRESETS / "base"], id_prefix="08")
    entity_id = ledger.next_id("entity")
    assert isinstance(entity_id, str) and len(entity_id) == 36
    stock_id = ledger.next_id("stock")
    assert isinstance(ledger.ids["stock"], DatecodeIds)
    assert ledger.ids["stock"].validate(stock_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_isolated(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> None:
    for key in (
        "DOLT_ROOT_PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.setenv(key, dolt_env[key])
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)


def test_cli_init_creates_a_ledger(
    cli_isolated: None, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    runner = CliRunner()
    data = tmp_path / "data"
    workspace = Workspace.create(data)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(data),
            "init",
            "cliled",
            "--preset",
            str(PRESETS / "full"),
            "--id-prefix",
            "12",
            "--author",
            CLI_AUTHOR,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created ledger cliled" in result.output
    assert (workspace.ledger_path("cliled") / ".dolt").is_dir()

    ledger = open_ledger(workspace, "cliled")
    assert latest_commit(ledger)["message"] == "init(ledger): cliled"
    assert ledger.author == CLI_AUTHOR


def test_cli_init_dry_run_prints_sql_and_writes_nothing(
    cli_isolated: None, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    runner = CliRunner()
    data = tmp_path / "data"
    workspace = Workspace.create(data)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(data),
            "--dry-run",
            "init",
            "planned",
            "--preset",
            str(PRESETS / "base"),
            "--author",
            CLI_AUTHOR,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Would create ledger planned" in result.output
    assert "CREATE TABLE `ledger_meta`" in result.output
    assert not workspace.ledger_path("planned").exists()


def test_cli_init_requires_a_preset(
    cli_isolated: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import sys

    from tetramark.cli.app import main

    data = tmp_path / "data"
    Workspace.create(data)
    monkeypatch.setattr(
        sys,
        "argv",
        ["tm", "--workspace", str(data), "init", "bare", "--author", CLI_AUTHOR],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == EXIT_USAGE
    assert "at least one --preset" in capsys.readouterr().err


def test_cli_presets_list_and_show(cli_isolated: None) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app
    from tetramark.schema.loader import list_bundled_presets

    runner = CliRunner()
    listed = runner.invoke(app, ["presets", "list"])
    assert listed.exit_code == 0, listed.output
    bundled = list_bundled_presets()
    if bundled:
        for name in bundled:
            assert name in listed.output
    else:
        assert "no bundled presets" in listed.output

    shown = runner.invoke(app, ["presets", "show", str(PRESETS / "chain")])
    assert shown.exit_code == 0, shown.output
    payload = yaml.safe_load(shown.output)
    assert payload["preset"] == "chain"
    assert {"plasmid", "bacterial", "item"} <= set(payload["kinds"])
    assert [rule["id"] for rule in payload["rules"]] == ["CH01", "CH02"]


def test_cli_schema_show(cli_isolated: None, server_ledger_factory) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    ledger = server_ledger_factory([PRESETS / "full"], name="shown")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            "shown",
            "schema",
            "show",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.output)
    # Spec A1 header: format version and tool provenance.
    assert payload["spec_version"] == "1"
    assert payload["tetramark_version"] == __version__
    assert payload["created_with"] == __version__
    assert payload["preset_versions"] == {"base": 2, "chain": 3, "full": 1}
    assert payload["fingerprint"] == ledger.fingerprint
    assert set(payload["kinds"]) >= {"item", "plasmid", "grid9x9", "everything"}
    assert payload["statuses"] == ["pending", "stored", "missing", "consumed"]

    as_json = runner.invoke(
        app,
        [
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            "shown",
            "schema",
            "show",
            "--json",
        ],
    )
    assert as_json.exit_code == 0, as_json.output
    json_payload = json.loads(as_json.output)
    assert json_payload["spec_version"] == "1"
    assert json_payload["tetramark_version"] == __version__
    assert json_payload["created_with"] == __version__

    one = runner.invoke(
        app,
        [
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            "shown",
            "schema",
            "show",
            "plasmid",
            "--json",
        ],
    )
    assert one.exit_code == 0, one.output
    single = json.loads(one.output)
    assert single["kind"] == "plasmid"
    assert single["object"] == "entity"
    assert "sequence" in single["fields"]
