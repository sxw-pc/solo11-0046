from support import findings_for

from lineage_inquest.graph.query import upstream
from lineage_inquest.graph.stable import edge_id
from lineage_inquest.models import BRANCH_MERGE_INSERT, BRANCH_MERGE_UPDATE
from lineage_inquest.store import LedgerStore


def _base(project_factory, name="produce"):
    p = project_factory(name)
    p.sql(
        "00_schema.sql",
        """
        CREATE TABLE raw.src (a INT, b INT, c INT);
        CREATE TABLE raw.customers (customer_id BIGINT, region VARCHAR(10));
        CREATE TABLE mart.out (x INT, y INT, z INT, w INT);
        """,
    )
    return p


def test_insert_select_direct_expression_aggregate_and_constant(project_factory):
    p = _base(project_factory)
    p.sql(
        "01_load.sql",
        """
        INSERT INTO mart.out (x, y, z, w)
        SELECT a, a + b AS y, SUM(c) AS z, 42 AS w
        FROM raw.src
        GROUP BY a, b;
        """,
    )
    result = p.scan()
    p.edge(result, "raw.src.a", "mart.out.x", "direct")
    assert p.edge(result, "raw.src.a", "mart.out.y").kind == "expression"
    p.edge(result, "raw.src.b", "mart.out.y", "expression")
    p.edge(result, "raw.src.c", "mart.out.z", "aggregate")
    assert not any(edge.target == "mart.out.w" for edge in result.graph.edges)
    assert p.node(result, "mart.out.w").origin == "ddl"


def test_star_expands_all_known_source_columns(project_factory):
    p = _base(project_factory, "star")
    p.sql("02_copy.sql", "CREATE TABLE mart.copy AS SELECT * FROM raw.src;")
    result = p.scan()
    assert p.edge(result, "raw.src.a", "mart.copy.a", "direct")
    assert p.edge(result, "raw.src.b", "mart.copy.b", "direct")
    assert p.edge(result, "raw.src.c", "mart.copy.c", "direct")
    assert not result.unsupported


def test_qualified_star_and_join_alias(project_factory):
    p = _base(project_factory, "join")
    p.sql(
        "03_join.sql",
        """
        CREATE TABLE mart.joined AS
        SELECT s.a AS source_a, c.*
        FROM raw.src AS s
        JOIN raw.customers AS c ON s.a = c.customer_id;
        """,
    )
    result = p.scan()
    p.edge(result, "raw.src.a", "mart.joined.source_a", "direct")
    p.edge(result, "raw.customers.region", "mart.joined.region", "direct")
    assert "mart.joined.customer_id" in result.graph.node_names


def test_unresolvable_star_is_unsupported_not_guessed(project_factory):
    p = project_factory("badstar")
    p.sql("a.sql", "CREATE TABLE mart.copy AS SELECT * FROM missing.source;")
    result = p.scan()
    assert any(item.construct == "star_unresolvable" for item in result.unsupported)
    assert not any(edge.target.startswith("mart.copy.") for edge in result.graph.edges)
    assert findings_for(result, "unsupported_sql")


def test_merge_extracts_update_and_insert_mappings(project_factory):
    p = project_factory("merge")
    p.sql(
        "00_schema.sql",
        """
        CREATE TABLE raw.changes (id INT, amount INT);
        CREATE TABLE mart.target (id INT, amount INT, label VARCHAR(10));
        """,
    )
    p.sql(
        "merge.sql",
        """
        MERGE INTO mart.target AS t
        USING raw.changes AS s
        ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.amount = s.amount, t.label = 'matched'
        WHEN NOT MATCHED THEN INSERT (id, amount, label) VALUES (s.id, s.amount, 'new');
        """,
    )
    result = p.scan()
    # The same source column feeding the same target column through two MERGE
    # branches stays two distinct, branch-tagged edges instead of collapsing.
    update_edge = p.edge(result, "raw.changes.amount", "mart.target.amount", "direct", BRANCH_MERGE_UPDATE)
    insert_edge = p.edge(result, "raw.changes.amount", "mart.target.amount", "direct", BRANCH_MERGE_INSERT)
    assert update_edge != insert_edge
    assert update_edge.locations == insert_edge.locations
    assert len(update_edge.locations) == 1
    assert update_edge.locations[0].script == "merge.sql"
    id_edge = p.edge(result, "raw.changes.id", "mart.target.id", "direct")
    assert id_edge.branch == BRANCH_MERGE_INSERT
    assert not any(edge.target == "mart.target.label" for edge in result.graph.edges)


def test_merge_branches_with_different_sources_stay_separate(project_factory):
    p = project_factory("merge-branches")
    p.sql(
        "00_schema.sql",
        """
        CREATE TABLE raw.updates (id INT, amount INT);
        CREATE TABLE raw.catalog (id INT, default_amount INT);
        CREATE TABLE mart.target (id INT, amount INT);
        """,
    )
    p.sql(
        "merge.sql",
        """
        MERGE INTO mart.target AS t
        USING (
          SELECT u.id, u.amount, c.default_amount
          FROM raw.updates AS u
          JOIN raw.catalog AS c ON u.id = c.id
        ) AS s
        ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.amount = s.amount
        WHEN NOT MATCHED THEN INSERT (id, amount) VALUES (s.id, s.default_amount);
        """,
    )
    result = p.scan()
    update_edge = p.edge(result, "raw.updates.amount", "mart.target.amount", "direct", BRANCH_MERGE_UPDATE)
    insert_edge = p.edge(result, "raw.catalog.default_amount", "mart.target.amount", "direct", BRANCH_MERGE_INSERT)
    assert [loc.script for loc in update_edge.locations] == ["merge.sql"]
    assert [loc.script for loc in insert_edge.locations] == ["merge.sql"]
    # Branch-tagged edges get distinct content-addressed ids.
    assert edge_id("raw.updates.amount", "mart.target.amount", "direct", BRANCH_MERGE_UPDATE) != edge_id(
        "raw.catalog.default_amount", "mart.target.amount", "direct", BRANCH_MERGE_INSERT
    )
    # Deterministic ordering: edges sort by (target, source, kind, branch).
    keys = [(edge.target, edge.source, edge.kind, edge.branch) for edge in result.graph.edges]
    assert keys == sorted(keys)
    # The query layer exposes both branches with their labels.
    payload = upstream(result.graph, "mart.target.amount", 1)
    branches = {
        (edge["source"], edge["branch"])
        for edge in payload["edges"]
        if edge["target"] == "mart.target.amount"
    }
    assert branches == {
        ("raw.updates.amount", BRANCH_MERGE_UPDATE),
        ("raw.catalog.default_amount", BRANCH_MERGE_INSERT),
    }
    ids = [edge["id"] for edge in payload["edges"]]
    assert len(ids) == len(set(ids))


def test_merge_branch_edges_survive_ledger_round_trip(project_factory):
    p = project_factory("merge-ledger")
    p.sql(
        "00_schema.sql",
        """
        CREATE TABLE raw.changes (id INT, amount INT);
        CREATE TABLE mart.target (id INT, amount INT);
        """,
    )
    p.sql(
        "merge.sql",
        """
        MERGE INTO mart.target AS t
        USING raw.changes AS s
        ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.amount = s.amount
        WHEN NOT MATCHED THEN INSERT (id, amount) VALUES (s.id, s.amount);
        """,
    )
    result, _ = p.scan_artifacts()
    store = LedgerStore(p.out_dir / "ledger.sqlite")
    graph = store.load_graph(result.run_id)
    branches = {
        (edge.source, edge.branch)
        for edge in graph.edges
        if edge.target == "mart.target.amount"
    }
    assert branches == {
        ("raw.changes.amount", BRANCH_MERGE_UPDATE),
        ("raw.changes.amount", BRANCH_MERGE_INSERT),
    }
    snapshot = store.snapshot(result.run_id)
    exported = {
        (edge["source"], edge["branch"])
        for edge in snapshot["edges"]
        if edge["target"] == "mart.target.amount"
    }
    assert exported == branches


def test_insert_column_count_mismatch_is_reported(project_factory):
    p = _base(project_factory, "mismatch")
    p.sql(
        "bad.sql",
        """
        INSERT INTO mart.out (x, y)
        SELECT a, b, c FROM raw.src;
        """,
    )
    result = p.scan()
    assert any(item.construct == "column_count_mismatch" for item in result.unsupported)
    assert any("column_count_mismatch" == item.target.split(":")[-1] for item in findings_for(result, "unsupported_sql"))


def test_unknown_target_column_is_reported(project_factory):
    p = _base(project_factory, "targetcol")
    p.sql(
        "bad.sql",
        """
        INSERT INTO mart.out (x, missing)
        SELECT a, b FROM raw.src;
        """,
    )
    result = p.scan()
    assert any(item.construct == "unknown_target_column" for item in result.unsupported)
