from support import findings_for

_TEXT = """-- guard fixtures
SELECT 1 + ROW_NUMBER() OVER () AS r FROM raw.t;

WITH RECURSIVE seq(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 3
)
SELECT n FROM seq;

SELECT o.id, x.note
FROM raw.orders AS o,
LATERAL (SELECT o.note AS note) AS x;

SELECT id
FROM raw.orders
PIVOT (SUM(amount) FOR currency IN ('USD')) AS p;
"""


def test_four_unsupported_constructs_have_locations(project_factory):
    p = project_factory("guard")
    p.sql("guard.sql", _TEXT)
    result = p.scan()
    by_construct = {item.construct: item for item in result.unsupported}
    assert {"window_function", "recursive_cte", "lateral", "pivot"} <= set(by_construct)
    assert by_construct["window_function"].line == 2
    assert by_construct["recursive_cte"].line == 4
    assert by_construct["lateral"].line == 13
    assert by_construct["pivot"].line == 17
    assert by_construct["set_operation"].line == 6
    statement_statuses = [item.status for item in result.statements]
    assert statement_statuses == ["unsupported"] * 4
    assert len(findings_for(result, "unsupported_sql")) == 5


def test_unsupported_statements_are_never_silent(project_factory):
    p = project_factory("silent")
    p.sql("a.sql", "SELECT SUM(x) OVER () FROM raw.t;")
    result = p.scan()
    assert result.summary["counts"]["unsupported"] == 1
    finding = findings_for(result, "unsupported_sql")[0]
    assert "window_function" in finding.target
    assert finding.script == "a.sql"


def test_parse_failure_is_reported_as_unsupported(project_factory):
    p = project_factory("parse")
    p.sql("broken.sql", "SELECT FROM WHERE;")
    result = p.scan()
    assert any(item.construct == "parse_error" for item in result.unsupported)
    assert any(item.category == "unsupported_sql" for item in result.findings)


def test_insert_values_is_outside_supported_subset(project_factory):
    p = project_factory("values")
    p.sql("schema.sql", "CREATE TABLE raw.t (id INT);")
    p.sql("load.sql", "INSERT INTO raw.t (id) VALUES (1);")
    result = p.scan()
    assert any(item.construct == "values_source" for item in result.unsupported)


def test_supported_non_recursive_cte_still_maps(project_factory):
    p = project_factory("cte")
    p.sql(
        "all.sql",
        """
        CREATE TABLE raw.t (id INT, amount INT);
        CREATE TABLE mart.t (id INT, amount INT);
        WITH picked AS (SELECT id, amount FROM raw.t)
        INSERT INTO mart.t (id, amount) SELECT id, amount FROM picked;
        """,
    )
    result = p.scan()
    assert result.unsupported == ()
    p.edge(result, "raw.t.id", "mart.t.id", "direct")
    p.edge(result, "raw.t.amount", "mart.t.amount", "direct")


def test_unsupported_line_survives_interleaved_comments(project_factory):
    p = project_factory("comment-lines")
    p.sql(
        "a.sql",
        """-- leading comment
SELECT id
-- the next line is unsupported
, ROW_NUMBER() OVER () AS r
FROM raw.t /* trailing */
;
""",
    )
    result = p.scan()
    item = next(entry for entry in result.unsupported if entry.construct == "window_function")
    assert item.line == 4
