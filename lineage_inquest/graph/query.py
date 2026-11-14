"""Upstream/downstream traversal and cycle detection."""

from __future__ import annotations

from collections import deque

from ..errors import NotFoundError
from ..models import Edge, Graph
from .build import edge_payload, node_payload
from .stable import edge_id


def _adjacency(graph: Graph, direction: str) -> dict[str, list[Edge]]:
    result: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        origin = edge.target if direction == "upstream" else edge.source
        result.setdefault(origin, []).append(edge)
    for edges in result.values():
        edges.sort(key=lambda item: (item.target, item.source, item.kind))
    return result


def upstream(graph: Graph, node: str, depth: int = 8) -> dict:
    return _traverse(graph, node, "upstream", depth)


def downstream(graph: Graph, node: str, depth: int = 8) -> dict:
    return _traverse(graph, node, "downstream", depth)


def _traverse(graph: Graph, node: str, direction: str, depth: int) -> dict:
    by_name = {item.name: item for item in graph.nodes}
    if node not in by_name:
        raise NotFoundError(f"node not found in this run: {node}")
    depth = max(1, int(depth))
    adjacency = _adjacency(graph, direction)
    hops = {node: 0}
    edge_ids: set[str] = set()
    edges: list[Edge] = []
    truncated = False
    queue = deque([node])
    while queue:
        current = queue.popleft()
        current_hop = hops[current]
        neighbors = adjacency.get(current, [])
        if current_hop >= depth:
            if neighbors:
                truncated = True
            continue
        for edge in neighbors:
            other = edge.source if direction == "upstream" else edge.target
            identifier = edge_id(edge.source, edge.target, edge.kind)
            if identifier not in edge_ids:
                edge_ids.add(identifier)
                edges.append(edge)
            if other not in hops:
                hops[other] = current_hop + 1
                queue.append(other)
    nodes = []
    for name in sorted(hops, key=lambda item: (hops[item], item)):
        payload = node_payload(by_name[name])
        payload["hop"] = hops[name]
        nodes.append(payload)
    return {
        "root": node,
        "direction": direction,
        "depth": depth,
        "truncated": truncated,
        "nodes": nodes,
        "edges": [edge_payload(edge) for edge in sorted(edges, key=lambda e: (e.target, e.source, e.kind))],
    }


def impact(base_snapshot: dict, target_snapshot: dict) -> dict:
    """Propagate edge-level diffs downstream to terminal columns.

    Consumes only the ``nodes`` / ``edges`` payloads of two
    ``LedgerStore.snapshot`` results.  For every added or removed edge the
    edge's target end is the propagation start; enumeration then follows
    outgoing edges in the *target* graph only, down to columns with no
    outgoing edge.  A removed edge's start is judged from the base
    snapshot, but nodes absent from the target graph are unreachable and
    yield no chains.
    """
    base_edges = {item["id"]: item for item in base_snapshot["edges"]}
    target_edges = {item["id"]: item for item in target_snapshot["edges"]}
    target_nodes = {item["name"] for item in target_snapshot["nodes"]}
    adjacency = _outgoing(target_snapshot["edges"])
    added = [target_edges[key] for key in sorted(set(target_edges) - set(base_edges))]
    removed = [base_edges[key] for key in sorted(set(base_edges) - set(target_edges))]
    entries = {
        "added": [_impact_entry(edge, adjacency, target_nodes) for edge in added],
        "removed": [_impact_entry(edge, adjacency, target_nodes) for edge in removed],
    }
    terminals = {
        terminal["node"]
        for group in entries.values()
        for entry in group
        for terminal in entry["terminals"]
    }
    return {
        **entries,
        "summary": {
            "added_count": len(entries["added"]),
            "removed_count": len(entries["removed"]),
            "terminal_count": len(terminals),
        },
    }


def _outgoing(edges: list[dict]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for edge in edges:
        result.setdefault(edge["source"], set()).add(edge["target"])
    return {source: sorted(targets) for source, targets in result.items()}


def _impact_entry(edge: dict, adjacency: dict[str, list[str]], target_nodes: set[str]) -> dict:
    start = edge["target"]
    terminals = _terminal_paths(start, adjacency) if start in target_nodes else {}
    return {
        "edge": edge,
        "start": start,
        "terminals": [
            {"node": node, "paths": [list(path) for path in paths]}
            for node, paths in sorted(terminals.items())
        ],
    }


def _terminal_paths(start: str, adjacency: dict[str, list[str]]) -> dict[str, list[tuple[str, ...]]]:
    """All simple paths from ``start`` to no-outgoing-edge terminals.

    Nodes are deduplicated within a path so cycles cannot loop or blow up;
    a path that can only continue into itself simply never reaches a
    terminal.  Paths per terminal are deduplicated and sorted.
    """
    terminals: dict[str, set[tuple[str, ...]]] = {}
    stack: list[tuple[str, ...]] = [(start,)]
    while stack:
        path = stack.pop()
        node = path[-1]
        neighbors = adjacency.get(node, [])
        if not neighbors:
            terminals.setdefault(node, set()).add(path)
            continue
        visited = set(path)
        for neighbor in neighbors:
            if neighbor not in visited:
                stack.append(path + (neighbor,))
    return {node: sorted(paths) for node, paths in terminals.items()}


def detect_cycles(graph: Graph) -> list[tuple[str, ...]]:
    adjacency: dict[str, list[str]] = {node.name: [] for node in graph.nodes}
    self_loops: set[str] = set()
    for edge in graph.edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
        if edge.source == edge.target:
            self_loops.add(edge.source)
    for neighbors in adjacency.values():
        neighbors.sort()

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(vertex: str) -> None:
        nonlocal index
        indices[vertex] = low[vertex] = index
        index += 1
        stack.append(vertex)
        on_stack.add(vertex)
        for neighbor in adjacency.get(vertex, []):
            if neighbor not in indices:
                visit(neighbor)
                low[vertex] = min(low[vertex], low[neighbor])
            elif neighbor in on_stack:
                low[vertex] = min(low[vertex], indices[neighbor])
        if low[vertex] == indices[vertex]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == vertex:
                    break
            components.append(component)

    for vertex in sorted(adjacency):
        if vertex not in indices:
            visit(vertex)

    cycles = [
        tuple(sorted(component))
        for component in components
        if len(component) > 1 or component[0] in self_loops
    ]
    return sorted(cycles)


def node_summary(graph: Graph, node: str) -> dict:
    by_name = {item.name: item for item in graph.nodes}
    if node not in by_name:
        raise NotFoundError(f"node not found in this run: {node}")
    incoming = [edge for edge in graph.edges if edge.target == node]
    outgoing = [edge for edge in graph.edges if edge.source == node]
    payload = node_payload(by_name[node])
    payload["upstream_count"] = len(incoming)
    payload["downstream_count"] = len(outgoing)
    return payload
