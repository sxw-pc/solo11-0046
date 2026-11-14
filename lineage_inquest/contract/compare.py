"""Compare declared contracts with the statically reconstructed graph."""

from __future__ import annotations

import re

from ..models import Contract, Graph, RangeAssertion
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
        failures: list[str] = []
        missing_scalar = sorted(
            str(item)
            for item in set(contract.assertions) - set(node.assertions)
            if not isinstance(item, RangeAssertion)
        )
        if missing_scalar:
            failures.append("missing assertions: " + ", ".join(missing_scalar))
        declared_ranges = [item for item in contract.assertions if isinstance(item, RangeAssertion)]
        actual_ranges = [item for item in node.assertions if isinstance(item, RangeAssertion)]
        if declared_ranges:
            # Several CHECK constraints on one column all hold, so the actual
            # interval is their intersection.
            actual = actual_ranges[0] if actual_ranges else None
            for item in actual_ranges[1:]:
                actual = actual.intersect(item)
            uncovered = [item for item in declared_ranges if actual is None or not actual.covers(item)]
            for declared_range in uncovered:
                gaps = _coverage_gaps(declared_range, actual)
                failures.append(
                    f"range not covered; declared {str(declared_range)}; "
                    f"actual {_actual_range_text(actual)}; {gaps}"
                )
        if failures:
            drafts.append(
                FindingDraft(
                    "assertion_mismatch",
                    target,
                    "declared assertions are not present in DDL/static metadata",
                    "; ".join(failures),
                    contract.source,
                )
            )
    return drafts


def _actual_range_text(actual: RangeAssertion | None) -> str:
    if actual is None:
        return "no numeric CHECK"
    return str(actual)


def _coverage_gaps(declared: RangeAssertion, actual: RangeAssertion | None) -> str:
    """Describe the endpoints the actual CHECK does not guarantee."""
    if actual is None:
        return "no CHECK constrains this column"
    gaps = []
    if declared.lower is not None and not actual.covers_lower(declared.lower):
        if actual.lower is None:
            gaps.append(f"missing lower bound {declared.lower.text}")
        else:
            gaps.append(
                f"lower bound {actual.lower.text} is tighter"
                f"{'' if actual.lower.inclusive else ' (open)'} than declared {declared.lower.text}"
            )
    if declared.upper is not None and not actual.covers_upper(declared.upper):
        if actual.upper is None:
            gaps.append(f"missing upper bound {declared.upper.text}")
        else:
            gaps.append(
                f"upper bound {actual.upper.text} is tighter"
                f"{'' if actual.upper.inclusive else ' (open)'} than declared {declared.upper.text}"
            )
    return "; ".join(gaps)
