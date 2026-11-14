"""Compare declared contracts with the statically reconstructed graph.

Two upstream comparison modes are supported and may coexist in one scan:

``direct`` (default)
    ``required_upstream`` is the allow-list of **one-hop** sources; every
    other direct source is unexpected.  This keeps the historical behaviour,
    including its finding detail text, byte-for-byte stable.

``transitive``
    ``required_upstream`` must be **reachable anywhere upstream**.  The
    reachable set itself is never used as an allow-list: declaring a column
    vouches for the path it sits on, and only the nearest undeclared
    ancestors of the remaining branches are reported.  Declared
    ``forbidden_upstream`` columns are matched against the whole reachable
    set, so a forbidden column buried deep in a path is still caught.

Transitive findings carry a stable ``mode=transitive;`` detail marker so a
finding can be attributed to its mode from the snapshot alone.  Direct
findings keep their original text; a target is bound to exactly one mode, so
the marker also disambiguates the two runs.
"""

from __future__ import annotations

import re

from ..graph.query import downstream_closure, upstream_distances
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
_TRANSITIVE_PREFIX = "mode=transitive; "


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
        if contract.upstream_mode == "transitive":
            reachable = _transitive_sources(graph, target) if node is not None else set()
        else:
            reachable = upstream.get(target, set())
        if node is None:
            missing = list(contract.required_upstream)
            detail = "target column is absent from the graph"
            if contract.upstream_mode == "transitive":
                detail = _TRANSITIVE_PREFIX + detail
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

        missing = sorted(set(contract.required_upstream) - reachable)
        if missing:
            message = (
                f"{len(missing)} required upstream column(s) are not reachable"
                if contract.upstream_mode == "transitive"
                else f"{len(missing)} required upstream column(s) are not direct sources"
            )
            detail = ("missing reachable: " if contract.upstream_mode == "transitive" else "missing: ") + ", ".join(missing)
            if contract.upstream_mode == "transitive":
                detail = _TRANSITIVE_PREFIX + detail
            drafts.append(FindingDraft("missing_upstream", target, message, detail, contract.source))
        unexpected, forbidden_hits = _unexpected_sources(graph, contract, reachable)
        if unexpected or forbidden_hits:
            if contract.upstream_mode == "transitive":
                parts = [_TRANSITIVE_PREFIX.rstrip("; ")]
                if unexpected:
                    parts.append("unexpected nearest undeclared: " + ", ".join(unexpected))
                if forbidden_hits:
                    parts.append("forbidden: " + ", ".join(forbidden_hits))
                detail = "; ".join(parts)
            else:
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


def _transitive_sources(graph: Graph, target: str) -> set[str]:
    """Every column reachable upstream of ``target`` (target excluded)."""
    return set(upstream_distances(graph, target))


def _unexpected_sources(graph: Graph, contract: Contract, reachable: set[str]) -> tuple[list[str], list[str]]:
    """Undeclared sources for one contract.

    Direct mode keeps the historical allow-list difference.  Transitive mode
    works on the reachable set instead of using it as a whitelist:

    * Declaring a column accounts for the whole path it sits on: the declared
      column itself plus every hop between it and the target.  That is what
      stops a deep declaration from flagging its own intermediate columns.
    * Everything else reachable belongs to an undeclared branch.  From those
      branches only the **nearest ancestors** are reported: an undeclared
      column is reported when no other undeclared column lies between it and
      the target.  Deeper columns behind that frontier are implied by the
      reported one and are not repeated.

    Taking the plain reachable-set difference would instead report every
    intermediate column of every declared path, which is why this walks the
    frontier instead.

    ``forbidden_upstream`` is checked against the *whole* reachable set, so
    a forbidden column buried behind a declared node still produces a hit;
    forbidden columns are reported separately and never double as ordinary
    unexpected sources.
    """
    if contract.upstream_mode != "transitive":
        return sorted(reachable - set(contract.required_upstream)), []

    declared = set(contract.required_upstream)
    forbidden = set(contract.forbidden_upstream)
    if not reachable:
        return [], []
    # The declared path: each declared column plus the hops leading from it
    # towards the target (its descendants that are also ancestors of the
    # target).  Ancestors of a declared column are deliberately not covered:
    # declaring a hop does not vouch for where that hop came from.
    accounted = downstream_closure(graph, declared) & reachable
    # Frontier over the whole undeclared region, forbidden columns included:
    # an undeclared column behind a forbidden one is not "nearest" either.
    branch = reachable - accounted
    nearest = sorted(
        name
        for name in branch
        if not (downstream_closure(graph, {name}) & branch - {name})
    )
    return [name for name in nearest if name not in forbidden], sorted(forbidden & reachable)
