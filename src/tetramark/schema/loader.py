"""Loading and resolving the declarative schema.

Responsibilities (spec §1.1, §2):

* read bundled presets (``tetramark/presets/<name>.yaml``) or presets from a path;
* resolve ``include`` chains (missing preset or cycle → error; a preset included
  twice through a diamond is merged once);
* merge local ``kinds_dir`` / ``rules_dir`` files and the ledger's ``ledger.yaml``;
* apply mixins to kinds (collisions → error);
* validate identifiers, reserved names and cross references;
* build an :class:`~tetramark.schema.model.EffectiveSchema` with a stable
  :meth:`~tetramark.schema.model.EffectiveSchema.fingerprint`.

Every error is a :class:`~tetramark.schema.model.SchemaError` carrying the YAML
file and dotted path, e.g. ``molbio.yaml: kinds.plasmid.fields.construct.to``.

Public entry points
-------------------
``load_preset(source)``
    One (include-expanded, not mixin-applied) preset document.
``load_ledger_config(path)``
    Parse ``ledger.yaml``.
``load_schema(config, base_dir=...)``
    Resolve a config into an :class:`EffectiveSchema`.
``load_ledger_schema(ledger_dir)``
    Both of the above for a ledger directory.
``list_bundled_presets()``
    Names of the presets shipped in the package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from pydantic import BaseModel, ValidationError

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
    "list_bundled_presets",
    "load_ledger_config",
    "load_ledger_schema",
    "load_preset",
    "load_schema",
]

YAML_SUFFIXES = (".yaml", ".yml")
_PRESETS_PACKAGE = "tetramark.presets"
_CORE_OBJECTS = frozenset({"entity", "stock", "aliquot", "box"})
_KIND_RESERVED = frozenset({"id", "kind"})
_ALIQUOT_RESERVED = frozenset({"id"})


# ---------------------------------------------------------------------------
# Filesystem / package-data helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Source:
    """A resolved preset location (directory or single YAML file)."""

    key: str
    root: Any  # Path or importlib.resources Traversable
    is_file: bool
    display_root: str
    include_base: Any
    bundled: bool = False

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


def _bundled_root() -> Any:
    return resources.files(_PRESETS_PACKAGE)


def _is_dir(item: Any) -> bool:
    try:
        return item.is_dir()
    except OSError:  # pragma: no cover - unlikely for package data
        return False


def _is_file(item: Any) -> bool:
    try:
        return item.is_file()
    except OSError:  # pragma: no cover
        return False


def _join(base: Any, *parts: str) -> Any:
    item = base
    for part in parts:
        item = item.joinpath(part)
    return item


def _read_text(item: Any) -> str:
    return item.read_text(encoding="utf-8")


def _iter_yaml(directory: Any) -> list[Any]:
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


def _bundled_source(raw: str) -> _Source | None:
    if "/" in raw or "\\" in raw:
        return None
    root = _bundled_root()
    for name in _candidate_names(raw):
        item = _join(root, name)
        if _is_dir(item) or _is_file(item):
            is_file = _is_file(item)
            display = name
            return _Source(
                key=f"bundled:{name}",
                root=item,
                is_file=is_file,
                display_root=display,
                include_base=root,
                bundled=True,
            )
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

    bundled = _bundled_source(raw)
    if bundled is not None:
        return bundled

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
        include_base = None if unit.source.bundled else unit.source.include_base
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
    """Load one preset by bundled name or path, resolving its includes.

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


def load_schema(
    config: LedgerConfig,
    *,
    base_dir: str | Path | None = None,
    search_paths: Sequence[str | Path] = (),
) -> EffectiveSchema:
    """Build the effective schema for ``config``.

    ``base_dir`` is the directory containing ``ledger.yaml``; relative
    ``presets``, ``kinds_dir`` and ``rules_dir`` entries are resolved against
    it.
    """
    root = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd()
    search = [Path(path).expanduser() for path in search_paths]

    units: list[_PresetUnit] = []
    seen_sources: set[str] = set()
    for preset_source in config.presets:
        for unit in _expand(preset_source, base_dir=root, search_paths=search):
            if unit.source.key in seen_sources:
                continue
            seen_sources.add(unit.source.key)
            units.append(unit)

    collector = _Collector()
    versions: dict[str, int] = {}
    for unit in units:
        name = unit.primary.preset or (
            unit.source.root.name
            if not unit.source.is_file
            else Path(unit.source.root.name).stem
        )
        versions.setdefault(name, unit.primary.version)
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
    """Load ``<ledger_dir>/ledger.yaml`` and its effective schema."""
    path = Path(ledger_dir)
    if path.is_file():
        path = path.parent
    config = load_ledger_config(path / "ledger.yaml")
    return config, load_schema(config, base_dir=path, search_paths=search_paths)


def list_bundled_presets() -> list[str]:
    """Names of bundled presets (top-level ``*.yaml``/``*.yml`` files)."""
    root = _bundled_root()
    names = [
        Path(item.name).stem
        for item in root.iterdir()
        if _is_file(item) and item.name.endswith(YAML_SUFFIXES)
    ]
    return sorted(names)
