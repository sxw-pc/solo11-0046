"""Stable identifiers and deterministic JSON serialization helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def node_id(name: str) -> str:
    return "n-" + content_hash({"name": name})[:16]


def edge_id(source: str, target: str, kind: str) -> str:
    return "e-" + content_hash(
        {"source": source, "target": target, "kind": kind}
    )[:16]


def finding_id(category: str, target: str, detail: str = "") -> str:
    return "f-" + content_hash(
        {"category": category, "target": target, "detail": detail}
    )[:16]


def stable_json(value: Any) -> str:
    """Canonical JSON used for byte-identical artifacts."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": ")
    ) + "\n"


def sorted_unique(values) -> list[str]:
    return sorted(set(values))
