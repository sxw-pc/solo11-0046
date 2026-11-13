"""Graph construction, queries, and stable serialization."""

from .build import build_graph, edge_payload, node_payload
from .query import detect_cycles, downstream, upstream

__all__ = [
    "build_graph",
    "detect_cycles",
    "downstream",
    "edge_payload",
    "node_payload",
    "upstream",
]
