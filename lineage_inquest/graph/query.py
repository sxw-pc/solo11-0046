"""Upstream/downstream traversal and explainable, budgeted cycle detection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..errors import NotFoundError
from ..models import (
    CYCLE_TYPE_BINARY,
    CYCLE_TYPE_MULTI,
    CYCLE_TYPE_SELF_LOOP,
    CycleHop,
    CycleInfo,
    CyclePath,
    CycleReport,
    Edge,
    Graph,
)
from .build import edge_payload, node_payload
from .stable import edge_id

# Default edge-examination budget for detect_cycles. The scan path always
# passes an explicit budget from config; this constant keeps ad-hoc callers
# deterministic and shields them from pathological inputs.
DEFAULT_CYCLE_BUDGET = 200_000
# Upper bound on representative paths emitted per strongly connected component.
MAX_CYCLE_PATHS = 3
# Representative paths are meant to be short and reviewable; cap how deep a
# DFS-tree back-edge walk may be reconstructed when a scan is truncated.
MAX_REPRESENTATIVE_PATH_HOPS = 100


def _edge_adjacency(graph: Graph) -> dict[str, list[Edge]]:
    """Outgoing edges per source vertex, ordered deterministically."""
    result: dict[str, list[Edge]] = {node.name: [] for node in graph.nodes}
    for edge in graph.edges:
        result.setdefault(edge.source, []).append(edge)
    for edges in result.values():
        edges.sort(key=lambda item: (item.target, item.kind))
    return result


def _node_adjacency(edge_adjacency: dict[str, list[Edge]]) -> dict[str, list[str]]:
    return {vertex: [edge.target for edge in edges] for vertex, edges in edge_adjacency.items()}


def _edge_lookup(edge_adjacency: dict[str, list[Edge]]) -> dict[tuple[str, str], Edge]:
    """O(1) (source, target) -> edge map; duplicates collapse deterministically
    because adjacency lists are already sorted by (target, kind)."""
    lookup: dict[tuple[str, str], Edge] = {}
    for source, edges in edge_adjacency.items():
        for edge in edges:
            lookup.setdefault((source, edge.target), edge)
    return lookup


def _path_from_vertices(
    edge_lookup: dict[tuple[str, str], Edge], vertices: list[str], partial: bool
) -> CyclePath:
    hops = tuple(
        CycleHop(vertices[index], vertices[index + 1], edge_lookup[(vertices[index], vertices[index + 1])].kind)
        for index in range(len(vertices) - 1)
    )
    return CyclePath(nodes=tuple(vertices), hops=hops, partial=partial)


@dataclass
class _TarjanFrame:
    vertex: str
    neighbors: list[str]
    cursor: int = 0


def _run_tarjan(
    adjacency: dict[str, list[str]],
    budget: int,
) -> tuple[list[list[str]], dict[str, list[str]], dict[str, str | None], list[str], list[str], int]:
    """Iterative Tarjan SCC with an edge-examination budget.

    Returns (completed components, confirmed back edges keyed by their on-stack
    target, DFS parent pointers, DFS stack left open when the budget ran out,
    unvisited roots, edges examined). Only genuine back edges (target still on
    the stack) are recorded, so an unfinished acyclic branch can never look
    like a cycle. Parent pointers let any back-edge walk be reconstructed in
    linear time without storing a full stack snapshot per back edge.
    """
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    components: list[list[str]] = []
    back_edges: dict[str, list[str]] = {}
    examined = 0
    exhausted = False

    for root in sorted(adjacency):
        if root in indices:
            continue
        work: list[_TarjanFrame] = [_TarjanFrame(root, adjacency.get(root, []))]
        indices[root] = low[root] = index
        index += 1
        stack.append(root)
        on_stack.add(root)
        parent[root] = None

        while work:
            frame = work[-1]
            if frame.cursor >= len(frame.neighbors):
                work.pop()
                if low[frame.vertex] == indices[frame.vertex]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == frame.vertex:
                            break
                    # Back edges are retained: completed SCCs consume theirs
                    # below via membership tests, while the partial loop only
                    # accepts edges whose endpoints remain on the open stack.
                    components.append(component)
                if work:
                    par = work[-1].vertex
                    low[par] = min(low[par], low[frame.vertex])
                continue

            neighbor = frame.neighbors[frame.cursor]
            frame.cursor += 1
            examined += 1
            over_budget = examined > budget
            if neighbor not in indices:
                indices[neighbor] = low[neighbor] = index
                index += 1
                stack.append(neighbor)
                on_stack.add(neighbor)
                parent[neighbor] = frame.vertex
                if over_budget:
                    # The edge was examined but its target cannot be expanded;
                    # stop immediately so the open DFS tree stays intact for
                    # partial cycle reconstruction.
                    exhausted = True
                    break
                work.append(_TarjanFrame(neighbor, adjacency.get(neighbor, [])))
            else:
                if neighbor in on_stack:
                    low[frame.vertex] = min(low[frame.vertex], indices[neighbor])
                    # Record the back edge before honoring the budget: this
                    # edge has already been examined and its cycle is proven,
                    # so it must survive truncation as a partial result.
                    back_edges.setdefault(neighbor, []).append(frame.vertex)
                if over_budget:
                    exhausted = True
                    break
        if exhausted:
            unvisited = sorted(vertex for vertex in adjacency if vertex not in indices)
            return components, back_edges, parent, list(stack), unvisited, examined

    return components, back_edges, parent, [], [], examined


def _tree_walk(parent: dict[str, str | None], target: str, source: str) -> list[str]:
    """Reconstruct the DFS tree path target -> ... -> source via parents."""
    walk = [source]
    current = source
    seen = {current}
    while current != target:
        current = parent[current]
        if current is None or current in seen:  # pragma: no cover - invariant
            raise KeyError("target is not a DFS ancestor of source")
        seen.add(current)
        walk.append(current)
    walk.reverse()
    return walk


def _tree_depth(parent: dict[str, str | None], target: str, source: str) -> int:
    """Number of DFS tree edges from target down to source."""
    depth = 0
    current = source
    seen = {current}
    while current != target:
        current = parent[current]
        if current is None or current in seen:  # pragma: no cover - invariant
            raise KeyError("target is not a DFS ancestor of source")
        seen.add(current)
        depth += 1
    return depth


def _representative_paths(
    component: set[str],
    start: str,
    edge_lookup: dict[tuple[str, str], Edge],
    adjacency: dict[str, list[str]],
    seed: CyclePath | None,
    budget_left: int,
) -> tuple[list[CyclePath], int, bool]:
    """Deterministically pick up to MAX_CYCLE_PATHS simple cycles through start.

    Neighbors are visited in sorted order, so the first paths discovered form
    a reproducible, reviewable sample instead of an exhaustive enumeration.
    A pre-confirmed Tarjan back-edge seed guarantees that a cyclic component
    always yields at least one path even when the budget is tight. The walk
    is iterative so deep components cannot hit Python's recursion limit.
    """
    chosen: list[CyclePath] = []
    seen: set[tuple[str, ...]] = set()
    if seed is not None:
        chosen.append(seed)
        seen.add(seed.nodes)

    spent = 0
    capped = False
    dfs_stack: list[str] = [start]
    on_stack: set[str] = {start}
    # Per-frame cursor into the frame vertex's in-component neighbors.
    cursors: dict[str, int] = {start: 0}

    def _neighbors(vertex: str) -> list[str]:
        # Global adjacency is already sorted; filter lazily so components that
        # are never entered under a tight budget pay no preprocessing cost.
        return [name for name in adjacency.get(vertex, ()) if name in component]

    while dfs_stack and not capped:
        current = dfs_stack[-1]
        neighbors = _neighbors(current)
        cursor = cursors[current]
        if cursor >= len(neighbors):
            on_stack.discard(dfs_stack.pop())
            continue
        neighbor = neighbors[cursor]
        cursors[current] = cursor + 1
        spent += 1
        if spent > budget_left:
            capped = True
            break
        if neighbor == start and len(dfs_stack) > 1:
            vertices = [*dfs_stack, start]
            key = tuple(vertices)
            if key not in seen:
                chosen.append(_path_from_vertices(edge_lookup, vertices, partial=False))
                seen.add(key)
                if len(chosen) >= MAX_CYCLE_PATHS:
                    capped = True
                    break
        elif neighbor not in on_stack:
            dfs_stack.append(neighbor)
            on_stack.add(neighbor)
            cursors[neighbor] = 0

    # A self-edge on the start vertex is a valid cycle, but it is only
    # kept as a representative when the DFS found no longer walk (the
    # pure self-loop SCC case); multi-vertex SCCs show member-to-member
    # cycles instead so the closing edge is visible in the primary path.
    if not chosen and start in adjacency.get(start, ()):
        vertices = [start, start]
        chosen.append(_path_from_vertices(edge_lookup, vertices, partial=False))

    paths = sorted(chosen, key=lambda path: (len(path.nodes), path.nodes))[:MAX_CYCLE_PATHS]
    return paths, spent, capped


def detect_cycles_report(graph: Graph, budget: int = DEFAULT_CYCLE_BUDGET) -> CycleReport:
    """Find cyclic SCCs with representative directed cycle paths.

    The budget counts directed-edge examinations across SCC discovery and
    representative path search. When it is exhausted, confirmed cycles (with
    the paths already proven) are still returned together with truncation
    metadata; vertices that were never reached are listed separately and are
    never reported as non-cyclic.
    """
    budget = max(1, int(budget))
    edge_adjacency = _edge_adjacency(graph)
    edge_lookup = _edge_lookup(edge_adjacency)
    adjacency = _node_adjacency(edge_adjacency)
    self_loops = {
        vertex
        for vertex, edges in edge_adjacency.items()
        if any(edge.target == vertex for edge in edges)
    }

    components, back_edges, parent, open_stack, unvisited, examined = _run_tarjan(adjacency, budget)
    discovery_truncated = bool(open_stack) or bool(unvisited)
    budget_left = max(0, budget - examined)
    open_vertices = set(open_stack)
    closed_members: set[str] = set()
    for component in components:
        closed_members.update(component)

    def _closing_path(back_target: str, back_source: str, partial: bool) -> CyclePath:
        """Build the closed walk for back edge source -> target via DFS tree."""
        if back_source == back_target:
            return CyclePath(
                nodes=(back_target, back_target),
                hops=(CycleHop(back_target, back_target, edge_lookup[(back_target, back_target)].kind),),
                partial=partial,
            )
        walk = _tree_walk(parent, back_target, back_source)
        vertices = [*walk, back_target]
        return _path_from_vertices(edge_lookup, vertices, partial=partial)

    cycles: list[CycleInfo] = []
    for raw_component in components:
        members = set(raw_component)
        members_sorted = tuple(sorted(members))
        if len(members) == 1:
            only = members_sorted[0]
            if only not in self_loops:
                continue
            seed = CyclePath(
                nodes=(only, only),
                hops=(CycleHop(only, only, edge_lookup[(only, only)].kind),),
            )
            paths, spent, _capped = _representative_paths(
                members, only, edge_lookup, adjacency, seed, budget_left
            )
            budget_left = max(0, budget_left - spent)
            cycles.append(
                CycleInfo(
                    members=members_sorted,
                    cycle_type=CYCLE_TYPE_SELF_LOOP,
                    paths=tuple(paths),
                )
            )
            continue

        # Cyclicity is already proven by the presence of an in-component back
        # edge. The representative paths themselves are produced by the
        # bounded DFS below; only a self-loop back edge (a constant-size seed)
        # is passed directly, so deep DFS-tree walks never dominate runtime.
        seed_path: CyclePath | None = None
        for back_target in sorted(back_edges):
            if back_target not in members:
                continue
            for back_source in back_edges[back_target]:
                if back_source != back_target or back_source not in members:
                    continue
                seed_path = _closing_path(back_target, back_source, partial=False)
                break
            if seed_path is not None:
                break

        start = min(members)
        paths, spent, capped = _representative_paths(
            members, start, edge_lookup, adjacency, seed_path, budget_left
        )
        budget_left = max(0, budget_left - spent)
        if capped:
            paths = [
                CyclePath(nodes=path.nodes, hops=path.hops, partial=True)
                for path in paths
            ]
        if not paths:
            # The bounded DFS could not start a walk (budget exhausted before
            # the first edge); fall back to the shortest proven back-edge walk
            # so the confirmed cycle is never emitted without a path.
            for back_target in sorted(back_edges):
                if back_target not in members:
                    continue
                for back_source in back_edges[back_target]:
                    if back_source not in members:
                        continue
                    try:
                        fallback = _closing_path(back_target, back_source, partial=True)
                    except KeyError:
                        continue
                    paths = [fallback]
                    capped = True
                    break
                if paths:
                    break
        cycle_type = CYCLE_TYPE_BINARY if len(members) == 2 else CYCLE_TYPE_MULTI
        cycles.append(
            CycleInfo(
                members=members_sorted,
                cycle_type=cycle_type,
                paths=tuple(paths),
                truncated=capped,
            )
        )

    # Back edges whose endpoints never made it into a completed component
    # prove cycles inside SCCs that were still open when the budget ran out.
    # Their DFS-tree walks give complete, hop-by-hop partial cycle paths.
    # Plain adjacent acyclic branches have no back edge and therefore can
    # never reach this code path. A back edge whose target was already popped
    # from the DFS stack belongs to a finished branch and is ignored.
    partial_groups: dict[str, dict[tuple[str, ...], CyclePath]] = {}
    partial_members: dict[str, set[str]] = {}
    for back_target in sorted(back_edges):
        if back_target in closed_members or back_target not in open_vertices:
            continue
        # Shallow back edges close short, readable cycles; rank them before
        # paying for any walk reconstruction.
        ranked: list[tuple[int, str]] = []
        for source in back_edges[back_target]:
            if source not in open_vertices:
                continue
            try:
                depth = _tree_depth(parent, back_target, source)
            except KeyError:
                # Stale entry whose DFS ancestry was already unwound.
                continue
            if depth <= MAX_REPRESENTATIVE_PATH_HOPS:
                ranked.append((depth, source))
        ranked.sort()
        bucket = partial_groups.setdefault(back_target, {})
        for _depth, back_source in ranked:
            # Bound reconstruction work: the component only exposes
            # MAX_CYCLE_PATHS representatives, so extra distinct walks would
            # be discarded anyway.
            if len(bucket) >= MAX_CYCLE_PATHS:
                break
            try:
                path = _closing_path(back_target, back_source, partial=True)
            except KeyError:  # pragma: no cover - defensive ancestry guard
                continue
            bucket.setdefault(path.nodes, path)
            partial_members.setdefault(back_target, set()).update(path.nodes[:-1])

    for back_target, bucket in partial_groups.items():
        if back_target not in partial_members:
            continue  # every reconstructed walk duplicated another group's
        ordered = sorted(bucket.values(), key=lambda path: (len(path.nodes), path.nodes))[:MAX_CYCLE_PATHS]
        members = tuple(sorted(partial_members[back_target]))
        if len(members) == 1:
            cycle_type = CYCLE_TYPE_SELF_LOOP
        elif len(members) == 2:
            cycle_type = CYCLE_TYPE_BINARY
        else:
            cycle_type = CYCLE_TYPE_MULTI
        cycles.append(
            CycleInfo(
                members=members,
                cycle_type=cycle_type,
                paths=tuple(ordered),
                truncated=True,
            )
        )

    cycles.sort(key=lambda info: info.members)
    return CycleReport(
        cycles=tuple(cycles),
        truncated=discovery_truncated or any(info.truncated for info in cycles),
        budget=budget,
        edges_examined=examined,
        unvisited_nodes=tuple(unvisited),
    )


def detect_cycles(graph: Graph, budget: int = DEFAULT_CYCLE_BUDGET) -> list[CycleInfo]:
    """Backward-compatible entry point returning one CycleInfo per cyclic SCC."""
    return list(detect_cycles_report(graph, budget).cycles)


def upstream(graph: Graph, node: str, depth: int = 8, budget: int | None = None) -> dict:
    return _traverse(graph, node, "upstream", depth, budget)


def downstream(graph: Graph, node: str, depth: int = 8, budget: int | None = None) -> dict:
    return _traverse(graph, node, "downstream", depth, budget)


def _traverse(graph: Graph, node: str, direction: str, depth: int, budget: int | None = None) -> dict:
    by_name = {item.name: item for item in graph.nodes}
    if node not in by_name:
        raise NotFoundError(f"node not found in this run: {node}")
    depth = max(1, int(depth))
    edge_budget = None if budget is None else max(1, int(budget))
    adjacency = _edge_traversal_adjacency(graph, direction)
    hops = {node: 0}
    edge_ids: set[str] = set()
    edges: list[Edge] = []
    truncated = False
    budget_exhausted = False
    examined = 0
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
            examined += 1
            if edge_budget is not None and examined > edge_budget:
                # Stop exploring; everything already discovered stays valid,
                # but the caller must know the reachable set is incomplete.
                truncated = True
                budget_exhausted = True
                queue.clear()
                break
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
        "budget_exhausted": budget_exhausted,
        "edges_examined": examined,
        "nodes": nodes,
        "edges": [edge_payload(edge) for edge in sorted(edges, key=lambda e: (e.target, e.source, e.kind))],
    }


def _edge_traversal_adjacency(graph: Graph, direction: str) -> dict[str, list[Edge]]:
    result: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        origin = edge.target if direction == "upstream" else edge.source
        result.setdefault(origin, []).append(edge)
    for edges in result.values():
        edges.sort(key=lambda item: (item.target, item.source, item.kind))
    return result


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
