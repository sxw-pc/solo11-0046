"""Core data structures shared by the scanner, graph, report, and store."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .contract.classify import FindingDraft


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


@dataclass(frozen=True)
class ScriptSlice:
    """One replayable scan boundary: a single SQL script and its text snapshot.

    ``text`` is the exact file content used by the scanner, so a slice can be
    replayed without touching the filesystem again. Slices are ordered by
    ``index`` and their ``script`` names are unique within a scan.
    """

    index: int
    script: str
    path: str
    text: str


@dataclass
class SliceState:
    """Mutable inter-stage state handed from one script slice to the next.

    A slice consumes only its own SQL text snapshot plus the state passed in
    here. The lists intentionally keep every entry in encounter order:
    unsupported items and statements are deduplicated/sorted once, globally,
    after the whole slice chain has run -- never inside an individual slice.
    """

    schema: dict[str, TableDef] = field(default_factory=dict)
    known: dict[str, TableDef] = field(default_factory=dict)
    mappings: list[ColumnMapping] = field(default_factory=list)
    unsupported: list[Unsupported] = field(default_factory=list)
    statements: list[StatementInfo] = field(default_factory=list)
    drafts: list[FindingDraft] = field(default_factory=list)

    def clone(self) -> SliceState:
        """Deep copy so a repeated replay starts from an identical state."""
        return SliceState(
            schema=deepcopy(self.schema),
            known=deepcopy(self.known),
            mappings=list(self.mappings),
            unsupported=list(self.unsupported),
            statements=list(self.statements),
            drafts=list(self.drafts),
        )


@dataclass(frozen=True)
class SliceSnapshot:
    """Immutable checkpoint of the accumulated state after one slice ran.

    ``state_digest`` hashes the full checkpoint contents (excluding itself),
    so re-executing the same slice against the same incoming state must yield
    the same digest.
    """

    index: int
    script: str
    state: SliceState
    state_digest: str

    @classmethod
    def capture(cls, index: int, script: str, state: SliceState) -> SliceSnapshot:
        from .graph.stable import content_hash

        return cls(
            index=index,
            script=script,
            state=state,
            state_digest=content_hash(_snapshot_payload(index, script, state)),
        )


@dataclass(frozen=True)
class Replay:
    """Result of replaying an ordered chain of script slices."""

    input_digest: str
    run_id: str
    slices: tuple[ScriptSlice, ...]
    snapshots: tuple[SliceSnapshot, ...]
    state: SliceState


def _snapshot_payload(index: int, script: str, state: SliceState) -> dict[str, Any]:
    """JSON-safe, key-order-independent representation of a state checkpoint."""
    return {
        "index": index,
        "script": script,
        "schema": [asdict(state.schema[name]) for name in sorted(state.schema)],
        "known": [asdict(state.known[name]) for name in sorted(state.known)],
        "mappings": [asdict(item) for item in state.mappings],
        "unsupported": [asdict(item) for item in state.unsupported],
        "statements": [asdict(item) for item in state.statements],
        "drafts": [asdict(item) for item in state.drafts],
    }
