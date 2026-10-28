"""Loading and resolving the declarative schema.

Responsibilities (spec §1.1, §2, §8):

* read presets from a path or from the ledger's own ``<ledger>/presets/`` copy;
* resolve ``include`` chains (missing preset or cycle → error; a preset included
  twice through a diamond is merged once);
* merge local ``kinds_dir`` / ``rules_dir`` files and the ledger's ``ledger.yaml``;
* apply mixins to kinds (collisions → error);
* validate identifiers, reserved names and cross references;
* build an :class:`~tetramark.schema.model.EffectiveSchema` with a stable
  :meth:`~tetramark.schema.model.EffectiveSchema.fingerprint`.

Presets live in the repository's ``presets/`` directory (they are not package
data).  ``tm init`` copies each selected preset and its include closure into
``<ledger>/presets/``; a ledger then resolves presets from that copy only, so its
schema cannot drift from the recorded ``preset_versions``.

Every error is a :class:`~tetramark.schema.model.SchemaError` carrying the YAML
file and dotted path, e.g. ``molbio.yaml: kinds.plasmid.fields.construct.to``.

Public entry points
------------------
``load_preset(source)``
    One (include-expanded, not mixin-applied) preset document.
``load_ledger_config(path)``
    Parse ``ledger.yaml``.
``load_schema(config, base_dir=...)``
    Resolve a config into an :class:`EffectiveSchema`.
``load_ledger_schema(ledger_dir)``
    Both of the above for a ledger directory, presets from its own copy.
``resolve_preset_spec(spec, presets_dir=...)``
    One ``--preset`` argument (a name or a path) to a concrete preset.
``expand_presets(sources)``
    The presets a ledger needs: every source plus its include closure.
``preset_copy_files(preset)``
    The files that copy one preset into ``<ledger>/presets/``.
``list_presets(directory)``
    Preset names in a presets directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from pydantic import BaseModel, ValidationError

from tetramark.errors import UsageError
from tetramark.schema.model import (
    IDENTIFIER_RE,
    RESERVED_SQL_WORDS,
    EffectiveSchema,
    Field,
    FieldType,
    Kind,
    LedgerConfig,
    Preset,
    Rule,
    SchemaError,
)

__all__ = [
    "PresetCopy",
    "PresetFile",
    "expand_presets",
    "list_presets",
    "load_ledger_config",
    "load_ledger_schema",
    "load_preset",
    "load_schema",
    "preset_copy_files",
    "resolve_ledger_presets",
    "resolve_preset_spec",
]

YAML_SUFFIXES = (".yaml", ".yml")
_CORE_OBJECTS = frozenset({"entity", "stock", "aliquot", "box"})
_KIND_RESERVED = frozenset({"id", "kind"})
_ALIQUOT_RESERVED = frozenset({"id"})


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetFile:
    """A concrete preset source: a YAML file or a directory with ``preset.yaml``."""

    name: str
    path: Path
    is_file: bool


@dataclass(frozen=True)
class PresetCopy:
    """One file to write into ``<ledger>/presets/`` (``destination`` is relative)."""

    name: str
    source: Path
    destination: Path


@dataclass(frozen=True)
class _Source:
    """A resolved preset location (directory or single YAML file)."""

    key: str
    root: Path
    is_file: bool
    display_root: str
    include_base: Path

    @property
    def primary_display(self) -> str:
        if self.is_file:
            return self.display_root
        return f"{self.display_root}/preset.yaml"


@dataclass(frozen=True)
class _PresetUnit:
    source: _Source
    primary: Preset
    extras: list[tuple[str, Preset]] = field(default_factory=list)


def _is_dir(item: Path) -> bool:
    try:
        return item.is_dir()
    except OSError:  # pragma: no cover - unlikely for a broken path
        return False


def _is_file(item: Path) -> bool:
    try:
        return item.is_file()
    except OSError:  # pragma: no cover
        return False


def _join(base: Path, *parts: str) -> Path:
    item = base
    for part in parts:
        item = item / part
    return item


def _read_text(item: Path) -> str:
    return item.read_text(encoding="utf-8")


def _iter_yaml(directory: Path) -> list[Path]:
    """Sorted ``*.yaml``/``*.yml`` files directly inside ``directory``."""
    files = []
    for child in directory.iterdir():
        if _is_file(child) and child.name.endswith(YAML_SUFFIXES):
            files.append(child)
    return sorted(files, key=lambda item: item.name)


def _candidate_names(raw: str) -> list[str]:
    if raw.endswith(YAML_SUFFIXES):
        return [raw]
    return [raw, f"{raw}.yaml", f"{raw}.yml"]


def _source_for(path: Path, display: str) -> _Source:
    """A :class:`_Source` for an already existing absolute path."""
    return _Source(
        key=str(path),
        root=path,
        is_file=path.is_file(),
        display_root=display,
        include_base=path.parent,
    )


def _find_preset_in_dir(directory: Path, name: str) -> Path | None:
    for candidate_name in _candidate_names(name):
        candidate = directory / candidate_name
        if candidate.exists():
            return candidate
    return None



def _resolve_source(
    source: str | Path,
    *,
    base_dir: Path | None,
    search_paths: Sequence[Path] = (),
    error_path: str = "include",
) -> _Source:
    raw = str(source)
    path = Path(raw).expanduser()

    candidates: list[Path] = []
    if path.is_absolute():
        for name in _candidate_names(raw):
            candidates.append(Path(name))
    else:
        bases = ([Path(base_dir)] if base_dir is not None else []) + list(search_paths)
        for base in bases:
            for name in _candidate_names(raw):
                candidates.append(base / name)
        if not bases:
            for name in _candidate_names(raw):
                candidates.append(Path(name))

    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            return _Source(
                key=str(resolved),
                root=resolved,
                is_file=resolved.is_file(),
                display_root=raw,
                include_base=resolved.parent,
            )

    tried = ", ".join(str(candidate) for candidate in candidates) or "(none)"
    raise SchemaError(
        f"preset {raw!r} not found (tried: {tried})",
        file=str(base_dir) if base_dir is not None else None,
        path=error_path,
    )


# ---------------------------------------------------------------------------
# YAML + pydantic helpers
# ---------------------------------------------------------------------------


def _read_yaml(item: Any, display: str) -> Any:
    try:
        text = _read_text(item)
    except OSError as exc:  # pragma: no cover - unreadable file
        raise SchemaError(f"cannot read file: {exc}", file=display) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = ""
        if mark is not None:
            where = f" (line {mark.line + 1}, column {mark.column + 1})"
        problem = getattr(exc, "problem", None) or str(exc)
        raise SchemaError(f"invalid YAML{where}: {problem}", file=display) from exc

    if data is None:
        data = {}
    return data


def _validation_error(exc: ValidationError, display: str, prefix: str = "") -> SchemaError:
    errors = exc.errors(include_url=False)
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return SchemaError("invalid schema document", file=display)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    path = ".".join(part for part in (prefix, location) if part) or None
    message = first.get("msg", "invalid value")
    if message.startswith("Value error, "):
        message = message[len("Value error, ") :]
    if len(errors) > 1:
        message = f"{message} (and {len(errors) - 1} more error(s))"
    return SchemaError(message, file=display, path=path)


def _validate(
    model: type[BaseModel], data: Any, display: str, prefix: str = ""
) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _validation_error(exc, display, prefix) from exc


def _validate_preset(data: Any, display: str, prefix: str = "") -> Preset:
    if not isinstance(data, dict):
        raise SchemaError("preset document must be a YAML mapping", file=display)
    return _validate(Preset, data, display, prefix)


# ---------------------------------------------------------------------------
# Loading preset sources
# ---------------------------------------------------------------------------


def _load_rules_document(data: Any, display: str, prefix: str = "") -> Preset:
    if isinstance(data, list):
        return _validate(Preset, {"rules": data}, display, prefix)
    if not isinstance(data, dict):
        raise SchemaError(
            "rules document must be a list or a YAML mapping", file=display
        )
    if "rules" in data or "preset" in data or "version" in data or "include" in data:
        return _validate_preset(data, display, prefix)
    if {"id", "sql", "expect"} <= set(data):
        return _validate(Preset, {"rules": [data]}, display, prefix)
    raise SchemaError(
        "rules document must contain a 'rules' list or a single rule", file=display
    )


def _load_kinds_document(data: Any, display: str, prefix: str = "") -> Preset:
    if not isinstance(data, dict):
        raise SchemaError("kinds document must be a YAML mapping", file=display)
    if any(key in data for key in ("kinds", "mixins", "rules", "preset", "version", "include")):
        return _validate_preset(data, display, prefix)
    # Bare mapping: the document *is* the kinds mapping.
    return _validate(Preset, {"kinds": data}, display, prefix)


def _load_unit(source: _Source) -> _PresetUnit:
    if source.is_file:
        data = _read_yaml(source.root, source.primary_display)
        return _PresetUnit(
            source=source,
            primary=_validate_preset(data, source.primary_display),
        )

    if not _is_dir(source.root):
        raise SchemaError("preset is neither a directory nor a YAML file", file=source.primary_display)

    preset_file = _join(source.root, "preset.yaml")
    if not _is_file(preset_file):
        raise SchemaError("preset directory has no preset.yaml", file=source.primary_display)
    primary = _validate_preset(
        _read_yaml(preset_file, source.primary_display), source.primary_display
    )

    extras: list[tuple[str, Preset]] = []
    kinds_dir = _join(source.root, "kinds")
    if _is_dir(kinds_dir):
        for item in _iter_yaml(kinds_dir):
            display = f"{source.display_root}/kinds/{item.name}"
            extras.append(
                (display, _load_kinds_document(_read_yaml(item, display), display))
            )
    rules_dir = _join(source.root, "rules")
    if _is_dir(rules_dir):
        for item in _iter_yaml(rules_dir):
            display = f"{source.display_root}/rules/{item.name}"
            extras.append(
                (display, _load_rules_document(_read_yaml(item, display), display))
            )
    return _PresetUnit(source=source, primary=primary, extras=extras)


def _expand(
    source: str | Path,
    *,
    base_dir: Path | None,
    search_paths: Sequence[Path],
) -> list[_PresetUnit]:
    """Load ``source`` and all its includes, includes first, each source once."""
    loaded: dict[str, _PresetUnit] = {}
    order: list[str] = []

    def visit(
        raw: str | Path,
        parent: _PresetUnit | None,
        chain: tuple[tuple[str, str], ...],
        *,
        top_level: bool = False,
        base_override: Path | None = None,
    ) -> None:
        try:
            src = _resolve_source(
                raw,
                base_dir=base_override if base_override is not None else base_dir,
                search_paths=search_paths,
                error_path="presets" if top_level else "include",
            )
        except SchemaError as exc:
            if parent is None:
                raise
            raise SchemaError(
                exc.message, file=parent.source.primary_display, path="include"
            ) from None
        keys = [key for key, _display in chain]
        if src.key in keys:
            start = keys.index(src.key)
            names = [display for _key, display in chain[start:]]
            names.append(src.primary_display)
            raise SchemaError(
                "include cycle: " + " -> ".join(names),
                file=parent.source.primary_display if parent else src.primary_display,
                path="include",
            )
        if src.key in loaded:
            return

        unit = _load_unit(src)
        include_base = unit.source.include_base
        for included in unit.primary.include:
            visit(
                included,
                unit,
                chain + ((src.key, src.primary_display),),
                base_override=include_base,
            )
        loaded[src.key] = unit
        order.append(src.key)

    visit(source, None, (), top_level=True)
    return [loaded[key] for key in order]


# ---------------------------------------------------------------------------
# Merging, mixins and cross references
# ---------------------------------------------------------------------------


@dataclass
class _KindEntry:
    kind: Kind
    display: str


@dataclass
class _MixinEntry:
    fields: dict[str, Field]
    display: str


@dataclass
class _RuleEntry:
    rule: Rule
    display: str
    source: str = "preset"


class _Collector:
    def __init__(self) -> None:
        self.kinds: dict[str, _KindEntry] = {}
        self.mixins: dict[str, _MixinEntry] = {}
        self.rules: dict[str, _RuleEntry] = {}

    def add(self, doc: Preset, display: str, *, source: str = "preset") -> None:
        for mixin_name, fields in doc.mixins.items():
            _check_identifier(mixin_name, display, f"mixins.{mixin_name}")
            if mixin_name in self.mixins:
                raise SchemaError(
                    f"duplicate mixin {mixin_name!r} (also defined in "
                    f"{self.mixins[mixin_name].display})",
                    file=display,
                    path=f"mixins.{mixin_name}",
                )
            for field_name in fields:
                _check_identifier(field_name, display, f"mixins.{mixin_name}.{field_name}")
                _check_not_reserved_column(
                    field_name,
                    _KIND_RESERVED,
                    display,
                    f"mixins.{mixin_name}.{field_name}",
                )
            self.mixins[mixin_name] = _MixinEntry(fields=fields, display=display)

        for kind_name, kind in doc.kinds.items():
            _check_identifier(kind_name, display, f"kinds.{kind_name}")
            if kind_name in self.kinds:
                raise SchemaError(
                    f"duplicate kind {kind_name!r} (also defined in "
                    f"{self.kinds[kind_name].display})",
                    file=display,
                    path=f"kinds.{kind_name}",
                )
            for field_name in kind.fields:
                _check_identifier(
                    field_name, display, f"kinds.{kind_name}.fields.{field_name}"
                )
                _check_not_reserved_column(
                    field_name,
                    _KIND_RESERVED,
                    display,
                    f"kinds.{kind_name}.fields.{field_name}",
                )
            for field_name in kind.aliquot_fields:
                _check_identifier(
                    field_name,
                    display,
                    f"kinds.{kind_name}.aliquot_fields.{field_name}",
                )
                _check_not_reserved_column(
                    field_name,
                    _ALIQUOT_RESERVED,
                    display,
                    f"kinds.{kind_name}.aliquot_fields.{field_name}",
                )
            for mixin_name in kind.mixins:
                _check_identifier(
                    mixin_name, display, f"kinds.{kind_name}.mixins"
                )
            if kind.renamed_from is not None:
                _check_identifier(
                    kind.renamed_from, display, f"kinds.{kind_name}.renamed_from"
                )
            self.kinds[kind_name] = _KindEntry(kind=kind, display=display)

        for index, rule in enumerate(doc.rules):
            if rule.id in self.rules:
                raise SchemaError(
                    f"duplicate rule id {rule.id!r} (also defined in "
                    f"{self.rules[rule.id].display})",
                    file=display,
                    path=f"rules[{index}].id",
                )
            self.rules[rule.id] = _RuleEntry(
                rule=rule, display=display, source=source
            )


def _check_identifier(name: Any, display: str, path: str) -> None:
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise SchemaError(
            f"identifier {name!r} must match [a-z][a-z0-9_]*",
            file=display,
            path=path,
        )
    if name in RESERVED_SQL_WORDS:
        raise SchemaError(
            f"{name!r} is a reserved SQL word", file=display, path=path
        )


def _check_not_reserved_column(
    name: str, reserved: frozenset[str], display: str, path: str
) -> None:
    if name in reserved:
        raise SchemaError(
            f"{name!r} is a generated core column and cannot be redefined",
            file=display,
            path=path,
        )


def _apply_mixins(collector: _Collector) -> dict[str, Kind]:
    resolved: dict[str, Kind] = {}
    for kind_name, entry in collector.kinds.items():
        kind = entry.kind
        fields: dict[str, Field] = {}
        for mixin_name in kind.mixins:
            mixin = collector.mixins.get(mixin_name)
            if mixin is None:
                raise SchemaError(
                    f"unknown mixin {mixin_name!r}",
                    file=entry.display,
                    path=f"kinds.{kind_name}.mixins",
                )
            for field_name, schema_field in mixin.fields.items():
                if field_name in fields:
                    raise SchemaError(
                        f"field {field_name!r} is defined by more than one mixin",
                        file=entry.display,
                        path=f"kinds.{kind_name}.fields.{field_name}",
                    )
                fields[field_name] = schema_field
        for field_name, schema_field in kind.fields.items():
            if field_name in fields:
                raise SchemaError(
                    f"field {field_name!r} collides with a mixin field",
                    file=entry.display,
                    path=f"kinds.{kind_name}.fields.{field_name}",
                )
            fields[field_name] = schema_field
        resolved[kind_name] = kind.model_copy(update={"fields": fields})
    return resolved


def _validate_references(kinds: dict[str, Kind], entries: dict[str, _KindEntry]) -> None:
    entity_kinds = {
        name for name, kind in kinds.items() if kind.object.value == "entity"
    }
    for kind_name, kind in kinds.items():
        display = entries[kind_name].display

        if kind.entity_kinds is not None:
            if len(set(kind.entity_kinds)) != len(kind.entity_kinds):
                raise SchemaError(
                    "'entity_kinds' must not contain duplicates",
                    file=display,
                    path=f"kinds.{kind_name}.entity_kinds",
                )
            for target in kind.entity_kinds:
                if target not in entity_kinds:
                    raise SchemaError(
                        f"{target!r} is not an entity kind",
                        file=display,
                        path=f"kinds.{kind_name}.entity_kinds",
                    )

        for scope_name, scope in (
            ("fields", kind.fields),
            ("aliquot_fields", kind.aliquot_fields),
        ):
            for field_name, schema_field in scope.items():
                if schema_field.type is not FieldType.ref:
                    continue
                target = schema_field.to
                if target not in kinds and target not in _CORE_OBJECTS:
                    raise SchemaError(
                        f"ref target {target!r} is neither a kind nor a core object "
                        f"({', '.join(sorted(_CORE_OBJECTS))})",
                        file=display,
                        path=f"kinds.{kind_name}.{scope_name}.{field_name}.to",
                    )

        for field_name, schema_field in kind.fields.items():
            if schema_field.renamed_from is not None:
                _check_identifier(
                    schema_field.renamed_from,
                    display,
                    f"kinds.{kind_name}.fields.{field_name}.renamed_from",
                )
        for field_name, schema_field in kind.aliquot_fields.items():
            if schema_field.renamed_from is not None:
                _check_identifier(
                    schema_field.renamed_from,
                    display,
                    f"kinds.{kind_name}.aliquot_fields.{field_name}.renamed_from",
                )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _merge_units(units: Iterable[_PresetUnit]) -> Preset:
    """Merge units into one raw preset (includes applied, mixins not inlined)."""
    merged = Preset()
    seen: dict[str, str] = {}
    for unit in units:
        for display, doc in [(unit.source.primary_display, unit.primary), *unit.extras]:
            for name in doc.mixins:
                if name in merged.mixins:
                    raise SchemaError(
                        f"duplicate mixin {name!r}", file=display, path=f"mixins.{name}"
                    )
                merged.mixins[name] = doc.mixins[name]
            for name, kind in doc.kinds.items():
                if name in merged.kinds:
                    raise SchemaError(
                        f"duplicate kind {name!r} (also defined in {seen[name]})",
                        file=display,
                        path=f"kinds.{name}",
                    )
                merged.kinds[name] = kind
                seen[name] = display
            for index, rule in enumerate(doc.rules):
                if any(existing.id == rule.id for existing in merged.rules):
                    raise SchemaError(
                        f"duplicate rule id {rule.id!r}",
                        file=display,
                        path=f"rules[{index}].id",
                    )
                merged.rules.append(rule)
    return merged


def load_preset(
    source: str | Path,
    *,
    base_dir: str | Path | None = None,
    search_paths: Sequence[str | Path] = (),
) -> Preset:
    """Load one preset by path, resolving its includes.

    Mixins are *not* inlined here; use :func:`load_schema` for a resolved
    schema.  Included presets are merged in include-first order.
    """
    units = _expand(
        source,
        base_dir=Path(base_dir) if base_dir is not None else None,
        search_paths=[Path(path) for path in search_paths],
    )
    merged = _merge_units(units)
    root = units[-1].primary
    merged.preset = root.preset or merged.preset
    merged.version = root.version
    merged.description = root.description
    merged.include = []
    return merged


def load_ledger_config(path: str | Path) -> LedgerConfig:
    """Parse a ``ledger.yaml`` file."""
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "ledger.yaml"
    display = str(candidate)
    if not candidate.is_file():
        raise SchemaError("ledger.yaml not found", file=display)
    data = _read_yaml(candidate, display)
    if not isinstance(data, dict):
        raise SchemaError("ledger.yaml must be a YAML mapping", file=display)
    return _validate(LedgerConfig, data, display)


def _load_local_dir(
    directory: Path,
    display_root: str,
    *,
    rules: bool,
) -> list[tuple[str, Preset]]:
    display_root = display_root.rstrip("/\\") or display_root
    if not directory.is_dir():
        raise SchemaError("directory not found", file=display_root)
    docs: list[tuple[str, Preset]] = []
    for item in _iter_yaml(directory):
        display = f"{display_root}/{item.name}"
        data = _read_yaml(item, display)
        if rules:
            docs.append((display, _load_rules_document(data, display)))
        else:
            docs.append((display, _load_kinds_document(data, display)))
    return docs


def _expand_many(
    sources: Iterable[str | Path],
    *,
    base_dir: Path | None,
    search_paths: Sequence[Path] = (),
) -> list[_PresetUnit]:
    """Expand several preset sources, deduplicated by resolved source path."""
    units: list[_PresetUnit] = []
    seen: set[str] = set()
    for source in sources:
        for unit in _expand(source, base_dir=base_dir, search_paths=search_paths):
            if unit.source.key in seen:
                continue
            seen.add(unit.source.key)
            units.append(unit)
    return units


def _unit_name(unit: _PresetUnit) -> str:
    """The preset's own ``preset:`` name, else its file stem / directory name."""
    if unit.primary.preset:
        return unit.primary.preset
    if unit.source.is_file:
        return unit.source.root.stem
    return unit.source.root.name


# ---------------------------------------------------------------------------
# Presets of a ledger: --preset resolution, the include closure, and copies
# ---------------------------------------------------------------------------


def _reference_name(raw: str | Path) -> str:
    """The name a ``ledger.yaml`` ``presets:`` entry stands for.

    New ledgers record bare names; ``tm init`` accepts paths too, and old
    ``ledger.yaml`` files may still hold a path.  Either way the ledger's own
    copy is ``<ledger>/presets/<name>.yaml`` (or ``<name>/``), so the entry is
    reduced to its file stem / last path component.
    """
    text = str(raw).strip()
    path = Path(text)
    if path.suffix in YAML_SUFFIXES:
        return path.stem
    return path.name


def resolve_preset_spec(
    spec: str | Path,
    *,
    presets_dir: str | Path | None,
    base_dir: str | Path | None = None,
) -> PresetFile:
    """Resolve one ``tm init --preset`` value to a concrete preset.

    A value containing ``/`` (or ``\\``) or ending in ``.yaml``/``.yml`` is a
    path, resolved against ``base_dir`` (the caller's cwd).  Anything else is a
    bare name resolved as ``<presets_dir>/<name>.yaml``; without a configured
    ``presets_dir`` the error names that workspace-config key.
    """
    text = str(spec).strip()
    if not text:
        raise UsageError("preset names must not be empty")

    if "/" in text or "\\" in text or text.endswith(YAML_SUFFIXES):
        root = Path(base_dir).expanduser() if base_dir is not None else Path.cwd()
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            raise SchemaError(
                f"preset path {text!r} not found (tried: {candidate})",
                path="presets",
            )
        resolved = candidate.resolve()
        unit = _load_unit(_source_for(resolved, text))
        return PresetFile(name=_unit_name(unit), path=resolved, is_file=resolved.is_file())

    if presets_dir is None:
        raise UsageError(
            f"preset name {text!r} needs 'presets_dir' in workspace.yaml: set it "
            "to the directory that holds <name>.yaml (for example the Tetramark "
            "checkout's presets/), or pass --preset as a path instead"
        )
    directory = Path(presets_dir).expanduser()
    if not directory.is_absolute():
        directory = (Path(base_dir) if base_dir is not None else Path.cwd()) / directory
    found = _find_preset_in_dir(directory, text)
    if found is None:
        tried = ", ".join(str(directory / name) for name in _candidate_names(text))
        raise SchemaError(
            f"preset {text!r} not found in presets_dir {directory} (tried: {tried})",
            file=str(directory),
            path="presets",
        )
    resolved = found.resolve()
    unit = _load_unit(_source_for(resolved, text))
    return PresetFile(name=_unit_name(unit), path=resolved, is_file=resolved.is_file())


def expand_presets(
    sources: Sequence[str | Path],
    *,
    base_dir: str | Path | None = None,
    search_paths: Sequence[str | Path] = (),
) -> list[PresetFile]:
    """Every preset a ledger needs: each source plus its include closure.

    Ordered include-first, each resolved source once.  Used by ``tm init`` to
    decide what to copy into ``<ledger>/presets/``.
    """
    units = _expand_many(
        sources,
        base_dir=Path(base_dir) if base_dir is not None else None,
        search_paths=[Path(path) for path in search_paths],
    )
    return [
        PresetFile(
            name=_unit_name(unit),
            path=unit.source.root,
            is_file=unit.source.is_file,
        )
        for unit in units
    ]


def preset_copy_files(preset: PresetFile) -> list[PresetCopy]:
    """The files that place ``preset`` inside ``<ledger>/presets/``.

    A YAML preset is copied as ``<name><suffix>``; a directory preset becomes
    ``<name>/preset.yaml`` plus its ``kinds/*.yaml`` and ``rules/*.yaml``.
    Includes are plain preset names, so they resolve inside the copy directory.
    """
    if preset.is_file:
        destination = Path(f"{preset.name}{preset.path.suffix}")
        return [PresetCopy(name=preset.name, source=preset.path, destination=destination)]

    files = [
        PresetCopy(
            name=preset.name,
            source=preset.path / "preset.yaml",
            destination=Path(preset.name) / "preset.yaml",
        )
    ]
    for sub in ("kinds", "rules"):
        directory = preset.path / sub
        if not directory.is_dir():
            continue
        for item in _iter_yaml(directory):
            files.append(
                PresetCopy(
                    name=preset.name,
                    source=item,
                    destination=Path(preset.name) / sub / item.name,
                )
            )
    return files


def resolve_ledger_presets(
    ledger_dir: str | Path, config: LedgerConfig
) -> list[PresetFile]:
    """Resolve ``config.presets`` to the ledger's own copies.

    A ledger uses **only** the presets under ``<ledger>/presets/``: the
    recorded ``preset_versions`` must describe exactly the schema the ledger is
    opened with, so there is no fallback to a workspace ``presets_dir`` or to a
    path recorded in ``ledger.yaml``.  A missing copy is a hard error naming the
    expected file.
    """
    root = Path(ledger_dir).expanduser()
    presets_dir = root / "presets"
    resolved: list[PresetFile] = []
    missing: list[tuple[str, Path]] = []
    for raw in config.presets:
        name = _reference_name(raw)
        found = _find_preset_in_dir(presets_dir, name)
        if found is None:
            suffix = Path(str(raw)).suffix
            expected_suffix = suffix if suffix in YAML_SUFFIXES else ".yaml"
            missing.append((name, presets_dir / f"{name}{expected_suffix}"))
            continue
        resolved.append(
            PresetFile(name=name, path=found.resolve(), is_file=found.is_file())
        )

    if missing:
        expected = "; ".join(str(path) for _name, path in missing)
        names = ", ".join(repr(name) for name, _path in missing)
        raise SchemaError(
            f"preset {names} is missing from this ledger's own copy: expected "
            f"{expected}. A ledger resolves presets only from <ledger>/presets/, "
            "never from a search path; copy the preset and its includes into "
            "that directory (a manual copy works), or run the matching "
            "migration/ script (see docs/migration.md)",
            file=str(root / "ledger.yaml"),
            path="presets",
        )
    return resolved


def list_presets(directory: str | Path) -> list[str]:
    """Names of the presets in ``directory`` (YAML files and preset dirs)."""
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise SchemaError(
            "presets directory not found", file=str(root), path="presets_dir"
        )
    names: set[str] = set()
    for item in root.iterdir():
        if item.name.startswith("."):
            continue
        if _is_file(item) and item.name.endswith(YAML_SUFFIXES):
            names.add(item.stem)
        elif _is_dir(item) and _is_file(item / "preset.yaml"):
            names.add(item.name)
    return sorted(names)


def load_schema(
    config: LedgerConfig,
    *,
    base_dir: str | Path | None = None,
    search_paths: Sequence[str | Path] = (),
) -> EffectiveSchema:
    """Build the effective schema for ``config``.

    ``base_dir`` is the directory containing ``ledger.yaml``; relative
    ``presets``, ``kinds_dir`` and ``rules_dir`` entries are resolved against
    it.  Callers that open an existing ledger should use
    :func:`load_ledger_schema`, which resolves presets from the ledger's own
    ``presets/`` copy only.
    """
    root = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd()
    search = [Path(path).expanduser() for path in search_paths]

    units = _expand_many(config.presets, base_dir=root, search_paths=search)

    collector = _Collector()
    versions: dict[str, int] = {}
    for unit in units:
        versions.setdefault(_unit_name(unit), unit.primary.version)
        collector.add(unit.primary, unit.source.primary_display)
        for display, doc in unit.extras:
            collector.add(doc, display)

    if config.kinds_dir:
        directory = Path(config.kinds_dir).expanduser()
        if not directory.is_absolute():
            directory = root / directory
        for display, doc in _load_local_dir(directory, config.kinds_dir, rules=False):
            collector.add(doc, display, source="local")

    if config.rules_dir:
        directory = Path(config.rules_dir).expanduser()
        if not directory.is_absolute():
            directory = root / directory
        for display, doc in _load_local_dir(directory, config.rules_dir, rules=True):
            collector.add(doc, display, source="local")

    resolved_kinds = _apply_mixins(collector)
    _validate_references(resolved_kinds, collector.kinds)

    return EffectiveSchema(
        kinds=resolved_kinds,
        rules=[entry.rule for entry in collector.rules.values()],
        rule_sources={
            rule_id: entry.source for rule_id, entry in collector.rules.items()
        },
        statuses=list(config.statuses),
        preset_versions=versions,
    )


def load_ledger_schema(
    ledger_dir: str | Path,
    *,
    search_paths: Sequence[str | Path] = (),
) -> tuple[LedgerConfig, EffectiveSchema]:
    """Load ``<ledger_dir>/ledger.yaml`` and its effective schema.

    Presets resolve only from ``<ledger_dir>/presets/`` (spec §8); when the
    ledger's copy is incomplete the error names the expected file.
    """
    path = Path(ledger_dir)
    if path.is_file():
        path = path.parent
    config = load_ledger_config(path / "ledger.yaml")
    presets = resolve_ledger_presets(path, config)
    effective = config.model_copy(update={"presets": [str(preset.path) for preset in presets]})
    return config, load_schema(
        effective, base_dir=path, search_paths=search_paths
    )
