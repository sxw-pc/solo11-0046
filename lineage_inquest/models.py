"""Core data structures shared by the scanner, graph, report, and store."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, TypeAlias


@dataclass(frozen=True, order=True)
class Bound:
    """One endpoint of a numeric range, with open/closed semantics."""

    value: Decimal
    inclusive: bool = True

    @property
    def text(self) -> str:
        # ``format(..., "f")`` keeps trailing zeros (DECIMAL precision).
        return format(self.value, "f")


@dataclass(frozen=True)
class RangeAssertion:
    """A structured numeric CHECK constraint, possibly single-sided."""

    lower: Bound | None = None
    upper: Bound | None = None

    def __str__(self) -> str:
        lower = "-inf" if self.lower is None else self.lower.text
        upper = "+inf" if self.upper is None else self.upper.text
        left = "[" if self.lower is not None and self.lower.inclusive else "("
        right = "]" if self.upper is not None and self.upper.inclusive else ")"
        return f"range:{left}{lower},{upper}{right}"

    def covers_lower(self, target: Bound) -> bool:
        if self.lower is None:
            return False
        if self.lower.value < target.value:
            return True
        if self.lower.value > target.value:
            return False
        return self.lower.inclusive or not target.inclusive

    def covers_upper(self, target: Bound) -> bool:
        if self.upper is None:
            return False
        if self.upper.value > target.value:
            return True
        if self.upper.value < target.value:
            return False
        return self.upper.inclusive or not target.inclusive

    def covers(self, other: RangeAssertion) -> bool:
        """True when every value allowed by ``other`` is allowed by ``self``."""
        lower_ok = other.lower is None or self.covers_lower(other.lower)
        upper_ok = other.upper is None or self.covers_upper(other.upper)
        return lower_ok and upper_ok

    def intersect(self, other: RangeAssertion) -> RangeAssertion:
        return RangeAssertion(
            lower=_tightest_lower(self.lower, other.lower),
            upper=_tightest_upper(self.upper, other.upper),
        )


Assertion: TypeAlias = str | RangeAssertion

_RANGE_TEXT = re.compile(
    r"^range:([\[\(])\s*([+-]?(?:\d+(?:\.\d+)?|inf))?\s*,\s*([+-]?(?:\d+(?:\.\d+)?|inf))?\s*([\]\)])$"
)


def parse_range_text(value: str) -> RangeAssertion | None:
    """Parse ``range:[min,max]`` (brackets express open/closed endpoints)."""
    match = _RANGE_TEXT.match(value.strip())
    if not match:
        return None
    left, lower_text, upper_text, right = match.groups()
    lower, lower_ok = _bound_text(lower_text, left == "[")
    upper, upper_ok = _bound_text(upper_text, right == "]")
    if not lower_ok or not upper_ok:
        return None
    if lower is None and upper is None:
        return None
    return RangeAssertion(lower, upper)


def _bound_text(text: str | None, inclusive: bool) -> tuple[Bound | None, bool]:
    """Return ``(bound, parsed)``; infinities and blanks mean no bound."""
    if text is None or not text.strip():
        return None, True
    normalized = text.strip().lower()
    if normalized in {"inf", "+inf", "-inf"}:
        return None, True
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None, False
    if not value.is_finite():
        return None, True
    return Bound(value, inclusive), True


def assertion_key(value: Assertion) -> tuple[int, str]:
    return (1 if isinstance(value, RangeAssertion) else 0, str(value))


def assertion_payload(value: Assertion) -> str | dict[str, Any]:
    if not isinstance(value, RangeAssertion):
        return value
    return {
        "kind": "range",
        "lower": _bound_payload(value.lower),
        "upper": _bound_payload(value.upper),
    }


def assertion_from_payload(value: str | dict[str, Any]) -> Assertion:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("kind") == "range":
        return RangeAssertion(
            lower=_bound_from_payload(value.get("lower")),
            upper=_bound_from_payload(value.get("upper")),
        )
    raise ValueError(f"invalid assertion payload: {value!r}")


def _bound_payload(bound: Bound | None) -> dict[str, Any] | None:
    if bound is None:
        return None
    return {"value": bound.text, "inclusive": bound.inclusive}


def _bound_from_payload(value: dict[str, Any] | None) -> Bound | None:
    if value is None:
        return None
    return Bound(Decimal(str(value["value"])), bool(value["inclusive"]))


def _tightest_lower(first: Bound | None, second: Bound | None) -> Bound | None:
    if first is None:
        return second
    if second is None:
        return first
    if first.value > second.value:
        return first
    if second.value > first.value:
        return second
    chosen = first if len(first.text) >= len(second.text) else second
    return Bound(chosen.value, first.inclusive and second.inclusive)


def _tightest_upper(first: Bound | None, second: Bound | None) -> Bound | None:
    if first is None:
        return second
    if second is None:
        return first
    if first.value < second.value:
        return first
    if second.value < first.value:
        return second
    chosen = first if len(first.text) >= len(second.text) else second
    return Bound(chosen.value, first.inclusive and second.inclusive)


@dataclass(frozen=True)
class SqlUnit:
    script: str
    index: int
    line: int
    text: str
    kind: str = "unknown"


@dataclass(frozen=True)
class ColumnDef:
    name: str
    data_type: str = "unknown"
    primary_key: bool = False
    nullable: bool = True
    derived: bool = False
    assertions: tuple[Assertion, ...] = ()


@dataclass
class TableDef:
    name: str
    columns: dict[str, ColumnDef] = field(default_factory=dict)
    origin: str = "ddl"
    locations: list[tuple[str, int]] = field(default_factory=list)

    def add(self, column: ColumnDef) -> None:
        self.columns[column.name] = column

    def ordered_columns(self) -> list[ColumnDef]:
        return list(self.columns.values())


@dataclass(frozen=True)
class ColumnMapping:
    target: str
    sources: tuple[str, ...]
    kind: str
    expression: str
    script: str
    line: int
    statement: int


@dataclass(frozen=True)
class Node:
    name: str
    table: str
    column: str
    data_type: str = "unknown"
    nullable: bool = True
    primary_key: bool = False
    origin: str = "referenced"
    assertions: tuple[Assertion, ...] = ()

    @property
    def node_id(self) -> str:
        # Populated by stable.node_id at serialization time; property is a
        # convenience for callers that build transient nodes.
        from .graph.stable import node_id

        return node_id(self.name)


@dataclass(frozen=True, order=True)
class EdgeLocation:
    script: str
    line: int
    statement: int


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    locations: tuple[EdgeLocation, ...]


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    tables: int = 0

    @property
    def node_names(self) -> set[str]:
        return {n.name for n in self.nodes}


@dataclass(frozen=True)
class Contract:
    target: str
    required_upstream: tuple[str, ...] = ()
    forbidden_upstream: tuple[str, ...] = ()
    expected_type: str | None = None
    assertions: tuple[Assertion, ...] = ()
    description: str = ""
    source: str = ""


@dataclass(frozen=True)
class Finding:
    finding_id: str
    category: str
    severity: str
    default_severity: str
    overridden: bool
    target: str
    message: str
    detail: str
    script: str = ""
    line: int = 0


@dataclass(frozen=True)
class Unsupported:
    script: str
    line: int
    construct: str
    reason: str
    snippet: str = ""


@dataclass(frozen=True)
class StatementInfo:
    script: str
    line: int
    index: int
    kind: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class ScanResult:
    project: str
    run_id: str
    generated_at: str
    input_digest: str
    config_digest: str
    graph: Graph
    findings: tuple[Finding, ...]
    unsupported: tuple[Unsupported, ...]
    statements: tuple[StatementInfo, ...]
    severity: dict[str, str]
    severity_overrides: dict[str, str]
    summary: dict[str, Any]
