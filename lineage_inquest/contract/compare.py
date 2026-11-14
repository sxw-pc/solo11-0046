"""Compare declared contracts with the statically reconstructed graph.

Branch semantics: MERGE statements may produce several branch-tagged edges
(``merge_update`` / ``merge_insert``) into the same target column. Contract
comparison is deliberately branch-agnostic: ``required_upstream`` is an
allow-list over source *column identities*, and every direct upstream source
counts no matter which branch carries it. A contract that lists only one
branch's source therefore reports the other branch's source as
``unexpected_upstream`` (never silently ignored), and a required source is
satisfied when it appears on any branch.
"""

from __future__ import annotations

import re

from ..models import Contract, Graph
from .classify import FindingDraft

_FAMILIES = {
    "integer": {"tinyint", "smallint", "int", "integer", "bigint"},
    "numeric": {"decimal", "numeric", "number", "float", "double", "real"},
    "string": {"varchar", "char", "text", "string", "nvarchar", "varchar2"},
    "boolean": {"boolean", "bool"},
    "date": {"date"},
    "timestamp": {"timestamp", "datetime"},
}
_TYPE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)")


def type_family(value: str) -> str:
    text = value.strip().upper()
    match = _TYPE.match(text)
    if not match:
        return "unknown"
    base = match.group(1).lower()
    for family, members in _FAMILIES.items():
        if base in members:
            return family
    return base


def types_match(expected: str, actual: str) -> bool:
    expected = expected.strip().lower()
    if expected in {"any", "*"}:
        return True
    if actual.strip().upper() == "NULL":
        return expected in {"null", "any"}
    family = type_family(expected)
    if expected in _FAMILIES:
        return type_family(actual) == family
    normalized_actual = re.sub(r"\s+", "", actual.strip().upper()).replace(",", ",")
    normalized_expected = re.sub(r"\s+", "", expected.upper())
    return normalized_expected == normalized_actual


def _direct_sources(graph: Graph) -> dict[str, set[str]]:
    # Branch-agnostic on purpose: both MERGE branches physically feed the
    # target column, so each branch's sources join the same allow-list check.
    sources: dict[str, set[str]] = {}
    for edge in graph.edges:
        sources.setdefault(edge.target, set()).add(edge.source)
    return sources


def compare_contracts(graph: Graph, contracts: list[Contract]) -> list[FindingDraft]:
    nodes = {node.name: node for node in graph.nodes}
    upstream = _direct_sources(graph)
    drafts: list[FindingDraft] = []
    for contract in contracts:
        target = contract.target
        node = nodes.get(target)
        actual_sources = upstream.get(target, set())
        if node is None:
            missing = list(contract.required_upstream)
            detail = "target column is absent from the graph"
            if missing:
                detail += f"; missing required upstream: {', '.join(missing)}"
            drafts.append(
                FindingDraft(
                    "missing_upstream",
                    target,
                    "contract target is not produced by any supported statement",
                    detail,
                    contract.source,
                )
            )
            continue

        missing = sorted(set(contract.required_upstream) - actual_sources)
        if missing:
            drafts.append(
                FindingDraft(
                    "missing_upstream",
                    target,
                    f"{len(missing)} required upstream column(s) are not direct sources",
                    "missing: " + ", ".join(missing),
                    contract.source,
                )
            )
        unexpected = sorted(actual_sources - set(contract.required_upstream))
        if unexpected:
            forbidden = sorted(set(unexpected) & set(contract.forbidden_upstream))
            detail = "unexpected: " + ", ".join(unexpected)
            if forbidden:
                detail += "; forbidden: " + ", ".join(forbidden)
            drafts.append(
                FindingDraft(
                    "unexpected_upstream",
                    target,
                    "column has upstream sources outside the declared allow-list",
                    detail,
                    contract.source,
                )
            )
        if contract.expected_type and not types_match(contract.expected_type, node.data_type):
            drafts.append(
                FindingDraft(
                    "type_mismatch",
                    target,
                    "declared type does not match the statically extracted type",
                    f"expected: {contract.expected_type}; actual: {node.data_type}",
                    contract.source,
                )
            )
        missing_assertions = sorted(set(contract.assertions) - set(node.assertions))
        if missing_assertions:
            drafts.append(
                FindingDraft(
                    "assertion_mismatch",
                    target,
                    "declared assertions are not present in DDL/static metadata",
                    "missing assertions: " + ", ".join(missing_assertions),
                    contract.source,
                )
            )
    return drafts
