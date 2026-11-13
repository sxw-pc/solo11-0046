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
