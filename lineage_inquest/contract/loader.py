"""Load and validate declarative contract YAML files."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..errors import ConfigError
from ..models import Contract
from ..naming import canonical_ref

_ASSERTION = re.compile(r"^range:\[(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\]$")
_ALLOWED = {
    "target",
    "required_upstream",
    "forbidden_upstream",
    "expected_type",
    "assertions",
    "description",
}


def normalize_assertion(value: str) -> str:
    value = value.strip()
    if value in {"not_null", "unique"}:
        return value
    match = _ASSERTION.match(value)
    if not match:
        raise ConfigError(
            f"invalid assertion {value!r}; expected not_null, unique, or range:[min,max]"
        )
    return f"range:[{match.group(1)},{match.group(2)}]"


def _reference_list(raw, key: str, default_schema: str, source: Path) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{source}: {key} must be a list of column references")
    refs: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(f"{source}: entries in {key} must be strings")
        refs.append(canonical_ref(item, default_schema))
    return tuple(sorted(set(refs)))


def load_contracts(contracts_dir: Path, default_schema: str) -> list[Contract]:
    files = sorted(
        [path for path in contracts_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}],
        key=lambda path: path.relative_to(contracts_dir).as_posix(),
    )
    contracts: list[Contract] = []
    seen: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(contracts_dir).as_posix()
        try:
            with path.open("r", encoding="utf-8") as handle:
                root = yaml.safe_load(handle)
        except OSError as exc:
            raise ConfigError(f"cannot read contract {relative}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in contract {relative}: {exc}") from exc
        if not isinstance(root, dict):
            raise ConfigError(f"{relative}: contract file root must be a mapping")
        unknown = set(root) - {"version", "contracts"}
        if unknown:
            raise ConfigError(f"{relative}: unknown root key(s): {', '.join(sorted(unknown))}")
        version = root.get("version", 1)
        if version != 1:
            raise ConfigError(f"{relative}: unsupported contract version {version!r}")
        items = root.get("contracts", [])
        if not isinstance(items, list):
            raise ConfigError(f"{relative}: 'contracts' must be a list")
        for index, item in enumerate(items, start=1):
            contract = _contract(item, default_schema, relative, index)
            if contract.target in seen:
                raise ConfigError(
                    f"{relative}: duplicate contract target {contract.target} (also in {seen[contract.target]})"
                )
            seen[contract.target] = relative
            contracts.append(contract)
    contracts.sort(key=lambda item: (item.target, item.source))
    return contracts


def _contract(item, default_schema: str, source: str, index: int) -> Contract:
    where = f"{source} contract #{index}"
    if not isinstance(item, dict):
        raise ConfigError(f"{where}: contract must be a mapping")
    unknown = set(item) - _ALLOWED
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    target = item.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ConfigError(f"{where}: target is required")
    target_ref = canonical_ref(target, default_schema)
    required = _reference_list(item.get("required_upstream"), "required_upstream", default_schema, Path(where))
    forbidden = _reference_list(item.get("forbidden_upstream"), "forbidden_upstream", default_schema, Path(where))
    overlap = set(required) & set(forbidden)
    if overlap:
        raise ConfigError(f"{where}: fields cannot be both required and forbidden: {', '.join(sorted(overlap))}")
    expected_type = item.get("expected_type")
    if expected_type is not None and (not isinstance(expected_type, str) or not expected_type.strip()):
        raise ConfigError(f"{where}: expected_type must be a non-empty string")
    raw_assertions = item.get("assertions", [])
    if not isinstance(raw_assertions, list):
        raise ConfigError(f"{where}: assertions must be a list")
    assertions: list[str] = []
    for assertion in raw_assertions:
        if not isinstance(assertion, str):
            raise ConfigError(f"{where}: assertions must be strings")
        try:
            assertions.append(normalize_assertion(assertion))
        except ConfigError as exc:
            raise ConfigError(f"{where}: {exc}") from exc
    description = item.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{where}: description must be a string")
    return Contract(
        target=target_ref,
        required_upstream=required,
        forbidden_upstream=forbidden,
        expected_type=expected_type.strip() if isinstance(expected_type, str) else None,
        assertions=tuple(sorted(set(assertions))),
        description=description,
        source=source,
    )
