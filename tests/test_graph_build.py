from support import findings_for

from lineage_inquest.graph.build import edge_payload
from lineage_inquest.graph.query import (
    DEFAULT_CYCLE_BUDGET,
    detect_cycles,
    detect_cycles_report,
    downstream,
    upstream,
)
from lineage_inquest.graph.stable import edge_id, node_id
from lineage_inquest.models import (
    CYCLE_TYPE_BINARY,
    CYCLE_TYPE_MULTI,
    CYCLE_TYPE_SELF_LOOP,
    Edge,
    EdgeLocation,
    Graph,
    Node,
)


def test_nodes_and_edges_are_sorted_and_deduplicated(project_factory):
    p = project_factory("dedupe")
    p.sql(
        "01_schema.sql",
        """
        CREATE TABLE raw.s (a INT, b INT);
        CREATE TABLE mart.t (a INT, b INT);
        """,
    )
    p.sql("02_first.sql", "INSERT INTO mart.t (a, b) SELECT a, b FROM raw.s;")
    p.sql("03_second.sql", "INSERT INTO mart.t (a, b) SELECT a, b FROM raw.s;")
    result = p.scan()
    names = [node.name for node in result.graph.nodes]
    assert names == sorted(names)
    keys = [(edge.target, edge.source, edge.kind) for edge in result.graph.edges]
    assert keys == sorted(keys)
    edge = p.edge(result, "raw.s.a", "mart.t.a", "direct")
    assert [location.script for location in edge.locations] == ["02_first.sql", "03_second.sql"]
    payload = edge_payload(edge)
    assert payload["scripts"] == ["02_first.sql", "03_second.sql"]
    assert payload["id"] == edge_id("raw.s.a", "mart.t.a", "direct")
    assert node_id("raw.s.a").startswith("n-")
    assert len(payload["locations"]) == 2


def test_upstream_downstream_depth_and_truncation(project_factory):
    p = project_factory("depth")
    p.sql(
        "chain.sql",
        """
        CREATE TABLE raw.s (a INT);
        CREATE TABLE stage.s AS SELECT a FROM raw.s;
        CREATE TABLE mart.s AS SELECT a FROM stage.s;
        CREATE TABLE report.s AS SELECT a FROM mart.s;
        """,
    )
    result = p.scan()
    up_one = upstream(result.graph, "report.s.a", 1)
    assert [node["name"] for node in up_one["nodes"]] == ["report.s.a", "mart.s.a"]
    assert up_one["truncated"] is True
    up_all = upstream(result.graph, "report.s.a", 3)
    assert {node["name"] for node in up_all["nodes"]} == {"report.s.a", "mart.s.a", "stage.s.a", "raw.s.a"}
    assert up_all["truncated"] is False
    down_two = downstream(result.graph, "raw.s.a", 2)
    assert {node["name"] for node in down_two["nodes"]} == {"raw.s.a", "stage.s.a", "mart.s.a"}
    assert down_two["truncated"] is True
    down_all = downstream(result.graph, "raw.s.a", 3)
    assert "report.s.a" in {node["name"] for node in down_all["nodes"]}


def test_cycle_detection_and_cycle_finding(project_factory):
    p = project_factory("cycle")
    p.sql(
        "cycle.sql",
        """
        CREATE TABLE mart.a (id INT);
        CREATE TABLE mart.b (id INT);
        INSERT INTO mart.a (id) SELECT id FROM mart.b;
        INSERT INTO mart.b (id) SELECT id FROM mart.a;
        """,
    )
    result = p.scan()
    cycles = detect_cycles(result.graph)
    assert [cycle.members for cycle in cycles] == [("mart.a.id", "mart.b.id")]
    assert cycles[0].cycle_type == CYCLE_TYPE_BINARY
    # The representative path is a closed, deterministic, reviewable walk.
    path = cycles[0].paths[0]
    assert path.nodes == ("mart.a.id", "mart.b.id", "mart.a.id")
    assert len(path.hops) == 2
    assert [(hop.source, hop.target, hop.kind) for hop in path.hops] == [
        ("mart.a.id", "mart.b.id", "direct"),
        ("mart.b.id", "mart.a.id", "direct"),
    ]
    assert path.partial is False
    assert len(findings_for(result, "cycle_detected")) == 1
    finding = findings_for(result, "cycle_detected")[0]
    assert finding.context["cycle_type"] == CYCLE_TYPE_BINARY
    assert finding.context["paths"][0]["nodes"] == list(path.nodes)
    assert "mart.a.id -> mart.b.id -> mart.a.id" in finding.detail


def test_self_loop_is_a_cycle(project_factory):
    p = project_factory("self")
    p.sql(
        "self.sql",
        """
        CREATE TABLE mart.a (id INT);
        INSERT INTO mart.a (id) SELECT id FROM mart.a;
        """,
    )
    result = p.scan()
    cycles = detect_cycles(result.graph)
    assert [cycle.members for cycle in cycles] == [("mart.a.id",)]
    assert cycles[0].cycle_type == CYCLE_TYPE_SELF_LOOP
    assert cycles[0].paths[0].nodes == ("mart.a.id", "mart.a.id")
    finding = findings_for(result, "cycle_detected")
    assert finding
    assert finding[0].context["cycle_type"] == CYCLE_TYPE_SELF_LOOP


def test_multi_vertex_cycle_gets_directed_representative_path(project_factory):
    p = project_factory("multi")
    p.sql(
        "multi.sql",
        """
        CREATE TABLE mart.a (id INT);
        CREATE TABLE mart.b (id INT);
        CREATE TABLE mart.c (id INT);
        INSERT INTO mart.a (id) SELECT id FROM mart.b;
        INSERT INTO mart.b (id) SELECT id FROM mart.c;
        INSERT INTO mart.c (id) SELECT id FROM mart.a;
        """,
    )
    result = p.scan()
    cycles = detect_cycles(result.graph)
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.cycle_type == CYCLE_TYPE_MULTI
    assert cycle.members == ("mart.a.id", "mart.b.id", "mart.c.id")
    path = cycle.paths[0]
    # Closed walk: starts and ends at the same (smallest) member.
    assert path.nodes[0] == path.nodes[-1] == "mart.a.id"
    assert set(path.nodes[:-1]) == set(cycle.members)
    # Every hop corresponds to a real edge in the graph.
    edge_pairs = {(edge.source, edge.target) for edge in result.graph.edges}
    for hop in path.hops:
        assert (hop.source, hop.target) in edge_pairs


def test_representative_paths_are_bounded_and_deterministic(project_factory):
    p = project_factory("bounded")
    p.sql(
        "bounded.sql",
        """
        CREATE TABLE mart.a (id INT);
        CREATE TABLE mart.b (id INT);
        CREATE TABLE mart.c (id INT);
        INSERT INTO mart.a (id) SELECT id FROM mart.b;
        INSERT INTO mart.b (id) SELECT id FROM mart.a;
        INSERT INTO mart.a (id) SELECT id FROM mart.c;
        INSERT INTO mart.c (id) SELECT id FROM mart.a;
        INSERT INTO mart.b (id) SELECT id FROM mart.c;
        INSERT INTO mart.c (id) SELECT id FROM mart.b;
        """,
    )
    result = p.scan()
    cycle = detect_cycles(result.graph)[0]
    assert len(cycle.paths) <= 3
    again = detect_cycles(result.graph)[0]
    assert [path.nodes for path in again.paths] == [path.nodes for path in cycle.paths]


def test_dag_has_no_cycle_finding(project_factory):
    p = project_factory("dag")
    p.sql(
        "dag.sql",
        """
        CREATE TABLE raw.a (id INT);
        CREATE TABLE mart.a (id INT);
        INSERT INTO mart.a (id) SELECT id FROM raw.a;
        """,
    )
    result = p.scan()
    report = detect_cycles_report(result.graph)
    assert report.cycles == ()
    assert report.truncated is False
    assert not findings_for(result, "cycle_detected")


def test_budget_truncation_keeps_confirmed_cycles_and_flags_unvisited():
    # A long chain that closes into a ring at the far end.
    pairs = [(f"s.t.n{i}", f"s.t.n{i + 1}") for i in range(6)]
    pairs.append(("s.t.n6", "s.t.n0"))
    names = sorted({name for pair in pairs for name in pair})
    nodes = tuple(Node(name, "s.t", name.split(".")[-1]) for name in names)
    edges = tuple(
        Edge(source, target, "direct", (EdgeLocation("x.sql", 1, 1),)) for source, target in pairs
    )
    graph = Graph(nodes, edges, tables=1)

    full = detect_cycles_report(graph, budget=DEFAULT_CYCLE_BUDGET)
    assert full.truncated is False
    assert len(full.cycles) == 1

    tight = detect_cycles_report(graph, budget=3)
    assert tight.truncated is True
    assert tight.unvisited_nodes
    # The ring is beyond the remaining budget here, so it cannot be confirmed
    # yet and must not be fabricated either.
    assert tight.cycles == ()

    # Budget big enough to reach the closing back edge: the ring is reported
    # as a confirmed partial cycle and the truncation marker stays visible.
    reachable = detect_cycles_report(graph, budget=7)
    assert reachable.truncated is True
    assert reachable.cycles
    cycle = reachable.cycles[0]
    assert cycle.truncated is True
    assert all(path.partial for path in cycle.paths)
    assert cycle.paths[0].nodes[0] == cycle.paths[0].nodes[-1]


def test_budget_truncation_does_not_fabricate_cycles_from_acyclic_branches():
    # Adjacent branches that never close a back edge must stay cycle-free
    # even when the budget runs out mid-traversal.
    pairs = [
        ("s.t.a", "s.t.b"),
        ("s.t.a", "s.t.c"),
        ("s.t.b", "s.t.c"),
        ("s.t.c", "s.t.d"),
        ("s.t.d", "s.t.e"),
    ]
    names = sorted({name for pair in pairs for name in pair})
    nodes = tuple(Node(name, "s.t", name.split(".")[-1]) for name in names)
    edges = tuple(
        Edge(source, target, "direct", (EdgeLocation("x.sql", 1, 1),)) for source, target in pairs
    )
    graph = Graph(nodes, edges, tables=1)
    for budget in (1, 2, 3, 4, 5, 50):
        report = detect_cycles_report(graph, budget=budget)
        assert report.cycles == ()
        if budget < 5:
            assert report.truncated is True
        else:
            assert report.truncated is False


def _ring_graph(width: int) -> Graph:
    pairs = [(f"s.t.n{i}", f"s.t.n{i + 1}") for i in range(width - 1)]
    pairs.append((f"s.t.n{width - 1}", "s.t.n0"))
    names = sorted({name for pair in pairs for name in pair})
    nodes = tuple(Node(name, "s.t", name.split(".")[-1]) for name in names)
    edges = tuple(
        Edge(source, target, "direct", (EdgeLocation("x.sql", 1, 1),)) for source, target in pairs
    )
    return Graph(nodes, edges, tables=1)


def test_deep_ring_representative_path_is_complete_without_budget_pressure():
    graph = _ring_graph(300)
    report = detect_cycles_report(graph)
    assert report.truncated is False
    cycle = report.cycles[0]
    assert cycle.cycle_type == CYCLE_TYPE_MULTI
    path = cycle.paths[0]
    assert path.nodes[0] == path.nodes[-1] == "s.t.n0"
    assert len(path.hops) == 300
    assert all(hop.kind == "direct" for hop in path.hops)


def test_truncated_ring_reports_confirmed_cycle_with_truncation_marker():
    graph = _ring_graph(300)
    # The closing back edge is reached exactly on the last funded edge; the
    # component never fully closes, so the confirmed cycle stays marked
    # truncated even though its full walk can be reconstructed.
    report = detect_cycles_report(graph, budget=300)
    assert report.truncated is True
    assert report.cycles
    cycle = report.cycles[0]
    assert cycle.truncated is True
    assert all(path.partial for path in cycle.paths)
    edge_pairs = {(edge.source, edge.target) for edge in graph.edges}
    for path in cycle.paths:
        assert path.nodes[0] == path.nodes[-1]
        assert all((hop.source, hop.target) in edge_pairs for hop in path.hops)


def test_self_loop_survives_tight_budget_as_partial_cycle():
    pairs = [("s.t.a", "s.t.b"), ("s.t.b", "s.t.c"), ("s.t.c", "s.t.c")]
    names = sorted({name for pair in pairs for name in pair})
    nodes = tuple(Node(name, "s.t", name.split(".")[-1]) for name in names)
    edges = tuple(
        Edge(source, target, "direct", (EdgeLocation("x.sql", 1, 1),)) for source, target in pairs
    )
    report = detect_cycles_report(Graph(nodes, edges, tables=1), budget=2)
    assert report.truncated is True
    assert len(report.cycles) == 1
    cycle = report.cycles[0]
    assert cycle.cycle_type == CYCLE_TYPE_SELF_LOOP
    assert cycle.paths[0].nodes == ("s.t.c", "s.t.c")
    assert cycle.paths[0].partial is True


def test_traversal_is_safe_in_a_cycle():
    nodes = (
        Node("s.t.a", "s.t", "a"),
        Node("s.t.b", "s.t", "b"),
    )
    edges = (
        Edge("s.t.a", "s.t.b", "direct", (EdgeLocation("x.sql", 1, 1),)),
        Edge("s.t.b", "s.t.a", "direct", (EdgeLocation("x.sql", 2, 2),)),
    )
    graph = Graph(nodes, edges, tables=1)
    result = upstream(graph, "s.t.a", 5)
    assert {node["name"] for node in result["nodes"]} == {"s.t.a", "s.t.b"}
    assert result["truncated"] is False
    assert result["budget_exhausted"] is False


def test_traversal_budget_marks_truncation_without_losing_discovered_nodes():
    nodes = tuple(Node(f"s.t.n{i}", "s.t", f"n{i}") for i in range(5))
    edges = tuple(
        Edge(f"s.t.n{i}", f"s.t.n{i + 1}", "direct", (EdgeLocation("x.sql", i, i),))
        for i in range(4)
    )
    graph = Graph(nodes, edges, tables=1)
    full = downstream(graph, "s.t.n0", 8)
    assert full["truncated"] is False and len(full["nodes"]) == 5

    capped = downstream(graph, "s.t.n0", 8, budget=2)
    assert capped["budget_exhausted"] is True
    assert capped["truncated"] is True
    # Discovered nodes remain valid and ordered by hop; nothing is fabricated.
    assert [node["name"] for node in capped["nodes"]] == ["s.t.n0", "s.t.n1", "s.t.n2"]
    assert all(node["hop"] <= 2 for node in capped["nodes"])


def test_content_addressed_ids_ignore_locations_and_encode_identity():
    a = node_id("raw.s.a")
    assert a == node_id("raw.s.a")
    assert a != node_id("raw.s.b")
    direct = edge_id("raw.s.a", "mart.t.a", "direct")
    assert direct == edge_id("raw.s.a", "mart.t.a", "direct")
    assert direct != edge_id("raw.s.a", "mart.t.a", "expression")
    assert direct != edge_id("raw.s.b", "mart.t.a", "direct")
