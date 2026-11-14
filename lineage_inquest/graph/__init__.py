"""Graph construction, queries, and stable serialization."""

from .build import build_graph, edge_payload, node_payload
from .query import (
    detect_cycles,
    downstream,
    downstream_closure,
    upstream,
    upstream_closure,
    upstream_distances,
)

__all__ = [
    "build_graph",
    "detect_cycles",
    "downstream",
    "downstream_closure",
    "edge_payload",
    "node_payload",
    "upstream",
    "upstream_closure",
    "upstream_distances",
]
