"""Core data structures shared by the scanner, graph, report, and store."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    assertions: tuple[str, ...] = ()


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
    assertions: tuple[str, ...] = ()

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
    upstream_mode: str = "direct"
    required_upstream: tuple[str, ...] = ()
    forbidden_upstream: tuple[str, ...] = ()
    expected_type: str | None = None
    assertions: tuple[str, ...] = ()
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
