from support import findings_for

from lineage_inquest.graph.build import edge_payload
from lineage_inquest.graph.query import detect_cycles, downstream, upstream
from lineage_inquest.graph.stable import edge_id, node_id
from lineage_inquest.models import Edge, EdgeLocation, Graph, Node


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
    assert detect_cycles(result.graph) == [("mart.a.id", "mart.b.id")]
    assert len(findings_for(result, "cycle_detected")) == 1


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
    assert detect_cycles(result.graph) == [("mart.a.id",)]
    assert findings_for(result, "cycle_detected")


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
    assert detect_cycles(result.graph) == []
    assert not findings_for(result, "cycle_detected")


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


def test_content_addressed_ids_ignore_locations_and_encode_identity():
    a = node_id("raw.s.a")
    assert a == node_id("raw.s.a")
    assert a != node_id("raw.s.b")
    direct = edge_id("raw.s.a", "mart.t.a", "direct")
    assert direct == edge_id("raw.s.a", "mart.t.a", "direct")
    assert direct != edge_id("raw.s.a", "mart.t.a", "expression")
    assert direct != edge_id("raw.s.b", "mart.t.a", "direct")
