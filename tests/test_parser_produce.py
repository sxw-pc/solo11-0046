from support import findings_for


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
    amount_edge = p.edge(result, "raw.changes.amount", "mart.target.amount", "direct")
    assert len(amount_edge.locations) == 1
    p.edge(result, "raw.changes.id", "mart.target.id", "direct")
    assert not any(edge.target == "mart.target.label" for edge in result.graph.edges)
    assert amount_edge.locations[0].script == "merge.sql"


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
