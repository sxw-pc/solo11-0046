"""Migration guards for the sqlglot major-version bump.

These tests pin the AST access layer (not a specific sqlglot build): they feed
local SQL through the real parser and assert the *behaviour* the scanner
contract promises -- recursive CTEs, VALUES row sources, set operations and
non-SELECT roots keep producing the same unsupported constructs and findings.
"""

from sqlglot import exp

from lineage_inquest.dialect import (
    UNSUPPORTED_FEATURES,
    detect_unsupported,
    statement_kind,
    unsupported_statement_reason,
)
from lineage_inquest.parser import parse_unit
from lineage_inquest.models import SqlUnit
from support import findings_for

DECLARED_ORDER = [name for name, _ in UNSUPPORTED_FEATURES]


def _parse(sql: str):
    return parse_unit(SqlUnit("a.sql", 1, 1, sql))


def test_recursive_cte_still_reports_recursive_cte_construct():
    ast = _parse(
        "WITH RECURSIVE seq(n) AS ("
        "  SELECT 1"
        "  UNION ALL"
        "  SELECT n + 1 FROM seq WHERE n < 3"
        ") SELECT n FROM seq"
    )
    assert ast is not None
    names = detect_unsupported(ast)
    assert "recursive_cte" in names
    # The recursive body also uses UNION ALL; both must be reported.
    assert "set_operation" in names


def test_recursive_marker_is_read_from_the_real_with_node():
    ast = _parse("WITH RECURSIVE seq(n) AS (SELECT 1) SELECT n FROM seq")
    with_node = next(ast.find_all(exp.With))
    # Whatever the storage shape, the access layer must see the marker.
    assert detect_unsupported(ast) == ["recursive_cte"]
    assert bool(with_node.args.get("recursive")) or bool(getattr(with_node, "recursive", False))


def test_cycle_clause_never_silently_becomes_supported():
    """30.18 rejected standard CYCLE clauses outright; 30.31 parses them.

    A now-parseable statement must still be reported as recursive_cte rather
    than passing the guard as supported. On a build that cannot parse CYCLE at
    all the parser returns None, which the guard reports as parse_error.
    """
    ast = _parse(
        "WITH RECURSIVE s(n) AS (SELECT 1)"
        " CYCLE n SET cyc TO 1 DEFAULT 0"
        " SELECT n FROM s"
    )
    if ast is None:
        assert unsupported_statement_reason(ast) == "statement could not be parsed"
        return
    assert detect_unsupported(ast) == ["recursive_cte"]


def test_non_recursive_cte_is_not_flagged():
    ast = _parse("WITH picked AS (SELECT id FROM raw.t) SELECT id FROM picked")
    assert detect_unsupported(ast) == []


def test_values_row_source_still_reports_values_source():
    ast = _parse("SELECT * FROM (VALUES (1, 2), (3, 4)) AS v(a, b)")
    assert ast is not None
    assert detect_unsupported(ast) == ["values_source"]


def test_values_in_insert_position_still_reports_values_source(project_factory):
    p = project_factory("mig-values")
    p.sql("schema.sql", "CREATE TABLE raw.t (id INT);")
    p.sql("load.sql", "INSERT INTO raw.t (id) VALUES (1), (2);")
    result = p.scan()
    assert [item.construct for item in result.unsupported] == ["values_source"]
    assert findings_for(result, "unsupported_sql")


def test_set_operation_constructs_share_one_detection_path():
    for sql in (
        "SELECT a FROM t UNION SELECT b FROM u",
        "SELECT a FROM t INTERSECT SELECT b FROM u",
        "SELECT a FROM t EXCEPT SELECT b FROM u",
    ):
        ast = _parse(sql)
        assert type(ast).__name__ in {"Union", "Intersect", "Except"}
        assert detect_unsupported(ast) == ["set_operation"], sql


def test_set_operation_class_hierarchy_is_matched_via_base_type():
    ast = _parse("SELECT a FROM t UNION SELECT b FROM u")
    assert isinstance(ast, exp.SetOperation)


def test_detect_unsupported_order_follows_declared_feature_order():
    ast = _parse(
        "WITH RECURSIVE r(n) AS (SELECT 1)"
        " SELECT *"
        " FROM (SELECT * FROM raw.t PIVOT (SUM(a) FOR b IN ('x')) AS p) AS pv,"
        "      LATERAL (SELECT 1 AS z) AS l,"
        "      UNNEST(pv.arr) AS u"
        " QUALIFY ROW_NUMBER() OVER () = 1"
        " UNION ALL"
        " VALUES (1)"
    )
    names = detect_unsupported(ast)
    assert names == [name for name in DECLARED_ORDER if name in set(names)]
    # Sanity: every declared family is present exactly once.
    assert names == list(DECLARED_ORDER)


def test_values_synonym_never_silently_becomes_supported(project_factory):
    """A parser that accepts ``VALUE`` as an INSERT synonym must still flag it.

    Older builds rejected this spelling as a parse error (also unsupported);
    the construct must stay ``values_source`` rather than slip through.
    """
    p = project_factory("mig-value-synonym")
    p.sql("schema.sql", "CREATE TABLE raw.t (id INT);")
    p.sql("load.sql", "INSERT INTO raw.t (id) VALUE (1);")
    result = p.scan()
    constructs = [item.construct for item in result.unsupported]
    assert constructs, "INSERT ... VALUE must never become silently supported"
    assert constructs == ["values_source"] or constructs == ["parse_error"]


def test_non_select_statement_is_never_silent(project_factory):
    p = project_factory("mig-roots")
    p.sql(
        "script.sql",
        """
        UPDATE raw.t SET id = 1;
        DELETE FROM raw.t;
        CREATE VIEW mart.v AS SELECT id FROM raw.t;
        """,
    )
    result = p.scan()
    constructs = [item.construct for item in result.unsupported]
    assert constructs.count("unsupported_statement") == 3
    assert [item.status for item in result.statements] == ["unsupported", "unsupported", "unsupported"]
    assert len(findings_for(result, "unsupported_sql")) == 3


def test_root_classification_covers_every_supported_statement():
    cases = {
        "CREATE TABLE raw.t (id INT)": "create_table",
        "CREATE TABLE raw.t AS SELECT 1 AS id": "ctas",
        "INSERT INTO raw.t (id) SELECT id FROM raw.s": "insert_select",
        "MERGE INTO raw.t USING raw.s ON raw.t.id = raw.s.id WHEN MATCHED THEN DELETE": "merge",
        "DROP TABLE raw.t": "drop_table",
        "ALTER TABLE raw.t ADD COLUMN note VARCHAR(4)": "alter_table",
        "SELECT id FROM raw.t": "select",
        "UPDATE raw.t SET id = 1": "unknown",
        "DELETE FROM raw.t": "unknown",
        "CREATE VIEW mart.v AS SELECT 1 AS id": "ctas",
    }
    for sql, expected in cases.items():
        ast = _parse(sql)
        assert ast is not None, sql
        assert statement_kind(ast) == expected, sql


def test_unsupported_statement_reason_tracks_the_same_allow_chain():
    for sql in (
        "UPDATE raw.t SET id = 1",
        "DELETE FROM raw.t",
        "SHOW TABLES",
        "BEGIN",
    ):
        ast = _parse(sql)
        assert unsupported_statement_reason(ast) is not None, sql
    for sql in (
        "SELECT id FROM raw.t",
        "CREATE TABLE raw.t (id INT)",
        "INSERT INTO raw.t (id) SELECT id FROM raw.s",
    ):
        ast = _parse(sql)
        assert unsupported_statement_reason(ast) is None, sql


def test_recursive_cte_and_values_row_source_lines_are_stable(project_factory):
    p = project_factory("mig-lines")
    p.sql(
        "a.sql",
        """-- migration line fixture
WITH RECURSIVE seq(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 3
)
SELECT n FROM seq;

SELECT *
FROM (VALUES (1, 2)) AS v(a, b);
""",
    )
    result = p.scan()
    by_construct = {item.construct: item for item in result.unsupported}
    assert by_construct["recursive_cte"].line == 2
    assert by_construct["values_source"].line == 10


def test_end_to_end_migration_smoke(project_factory):
    """One local script exercises every migrated access path at once."""
    p = project_factory("mig-smoke")
    p.sql(
        "migrate.sql",
        """CREATE TABLE raw.t (id INT);
WITH RECURSIVE seq(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 3
)
SELECT n FROM seq;
SELECT *
FROM (VALUES (1, 2)) AS v(a, b);
SELECT id FROM raw.t;
UPDATE raw.t SET id = 2;
""",
    )
    result = p.scan()
    constructs = [item.construct for item in result.unsupported]
    assert "recursive_cte" in constructs
    assert "values_source" in constructs
    assert "unsupported_statement" in constructs
    # The recursive CTE's UNION ALL must still count as a set operation.
    assert "set_operation" in constructs
    # Findings mirror every unsupported entry.
    targets = {finding.target for finding in findings_for(result, "unsupported_sql")}
    assert any(target.endswith(":recursive_cte") for target in targets)
    assert any(target.endswith(":values_source") for target in targets)
    assert any(target.endswith(":unsupported_statement") for target in targets)
    # The plain SELECT and DDL remain supported.
    statuses = {(item.line, item.status) for item in result.statements}
    assert (1, "supported") in statuses
    assert (10, "supported") in statuses
