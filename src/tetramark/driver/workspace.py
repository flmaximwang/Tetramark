"""Workspaces: one ``data_dir`` holding ledgers.

A workspace is a directory containing ``workspace.yaml``; every direct
subdirectory with a ``.dolt`` directory is a ledger.  This module loads and
saves the workspace configuration, lists its ledgers and hands out the
embedded driver for one of them.  Tetramark does not start, stop or track a
``dolt sql-server``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from tetramark.driver.base import Driver
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import LedgerError, UnreachableError, UsageError

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "WORKSPACE_FILE",
    "Workspace",
    "WorkspaceConfig",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 13308
WORKSPACE_FILE = "workspace.yaml"


class WorkspaceConfig(BaseModel):
    """Contents of ``workspace.yaml``."""

    model_config = ConfigDict(extra="allow")

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    default_ledger: str | None = None
    author: str | None = None
    #: Directory holding ``<name>.yaml`` presets for bare ``--preset NAME``.
    #: A relative path is resolved against ``workspace.yaml``.
    presets_dir: str | None = None


class Workspace:
    """A Dolt data directory plus its ``workspace.yaml``."""

    def __init__(
        self,
        data_dir: str | Path,
        config: WorkspaceConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.config = config or WorkspaceConfig()
        self._env = dict(env) if env is not None else None

    # -- construction -----------------------------------------------------
    @property
    def env(self) -> dict[str, str] | None:
        """The environment every ``dolt``/``git`` subprocess should inherit.

        Tests create workspaces with an isolated ``DOLT_ROOT_PATH``/``HOME``;
        production workspaces leave it ``None`` and inherit the process
        environment.  ``Ledger`` passes this to the embedded driver.
        """
        return self._env

    @property
    def config_path(self) -> Path:
        return self.data_dir / WORKSPACE_FILE

    @property
    def presets_dir_path(self) -> Path | None:
        """The workspace's ``presets_dir`` as an absolute path (``None`` if unset).

        Relative values are resolved against ``workspace.yaml`` (the data_dir),
        never against the caller's cwd.
        """
        raw = self.config.presets_dir
        if raw is None or not str(raw).strip():
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.data_dir / path
        return path

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        default_ledger: str | None = None,
        author: str | None = None,
        presets_dir: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Workspace:
        """Create ``workspace.yaml`` in ``path`` (the directory is made if needed)."""
        data_dir = Path(path).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        workspace = cls(
            data_dir,
            WorkspaceConfig(
                host=host,
                port=port,
                default_ledger=default_ledger,
                author=author,
                presets_dir=presets_dir,
            ),
            env=env,
        )
        if workspace.config_path.exists():
            raise LedgerError(f"a workspace already exists at {workspace.config_path}")
        workspace.save()
        return workspace

    def save(self) -> None:
        """Write ``workspace.yaml`` (UTF-8, stable key order)."""
        payload = self.config.model_dump(exclude_none=True)
        self.config_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path, *, env: Mapping[str, str] | None = None) -> Workspace:
        """Load a workspace from its directory or from the YAML file itself."""
        target = Path(path).expanduser()
        config_path = target / WORKSPACE_FILE if target.is_dir() else target
        if not config_path.is_file():
            raise UsageError(f"no {WORKSPACE_FILE} at {config_path}")
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise UsageError(f"invalid {config_path}: {error}") from error
        if not isinstance(payload, dict):
            raise UsageError(f"invalid {config_path}: expected a mapping")
        try:
            config = WorkspaceConfig(**payload)
        except ValidationError as error:
            raise UsageError(f"invalid {config_path}: {error}") from error
        return cls(config_path.parent, config, env=env)

    @classmethod
    def discover(
        cls,
        start: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Workspace | None:
        """Search upward from ``start`` (default cwd), then ``$TETRAMARK_WORKSPACE``."""
        environ = os.environ if env is None else env
        origin = Path(start) if start is not None else Path.cwd()
        origin = origin.expanduser().resolve()
        if origin.is_file():
            origin = origin.parent
        for candidate in (origin, *origin.parents):
            if (candidate / WORKSPACE_FILE).is_file():
                return cls.load(candidate, env=environ)
        fallback = environ.get("TETRAMARK_WORKSPACE")
        if fallback:
            return cls.load(fallback, env=environ)
        return None

    # -- ledgers ----------------------------------------------------------
    def list_ledgers(self) -> list[str]:
        """Names of direct subdirectories that are Dolt databases."""
        if not self.data_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.data_dir.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and (entry / ".dolt").is_dir()
        )

    def ledger_path(self, ledger: str) -> Path:
        return self.data_dir / ledger

    # -- drivers ----------------------------------------------------------
    def driver_for(self, ledger: str | None = None, *, branch: str | None = None) -> Driver:
        """The embedded driver for one ledger in this workspace.

        ``branch`` selects a session branch through a ``db/branch`` revision
        database; the repository's checked-out branch is left untouched.
        """
        name = ledger or self.config.default_ledger
        if not name:
            raise UsageError(
                "no ledger selected: pass --ledger or set default_ledger in workspace.yaml"
            )
        path = self.ledger_path(name)
        if not (path / ".dolt").is_dir():
            raise UnreachableError(f"ledger {name!r} not found in {self.data_dir}")
        return EmbeddedDriver(path, branch=branch, env=self._env)
