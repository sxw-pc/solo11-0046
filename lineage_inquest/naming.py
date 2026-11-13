"""Identifier canonicalization and validation."""

from __future__ import annotations

import re

from .errors import ConfigError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def clean_ident(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in '"`' and value[-1] == value[0]:
        value = value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.lower()


def valid_ident(value: str) -> bool:
    return bool(_IDENT.match(value))


def canonical_table(parts: list[str], default_schema: str) -> str:
    clean = [clean_ident(p) for p in parts if p]
    if len(clean) == 1:
        clean = [default_schema.lower(), clean[0]]
    elif len(clean) >= 3:
        clean = clean[-2:]
    if len(clean) != 2:
        raise ValueError(f"invalid table name: {'.'.join(parts)}")
    return ".".join(clean)


def canonical_ref(value: str, default_schema: str) -> str:
    parts = [p for p in str(value).strip().split(".") if p != ""]
    if len(parts) == 2:
        parts = [default_schema] + parts
    if len(parts) != 3 or not all(valid_ident(clean_ident(p)) for p in parts):
        raise ConfigError(
            f"invalid column reference {value!r}; expected schema.table.column"
        )
    return ".".join(clean_ident(p) for p in parts)


def table_of(ref: str) -> str:
    parts = ref.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else ref


def column_of(ref: str) -> str:
    return ref.split(".")[-1]
