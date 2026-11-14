from support import categories, findings_for


def test_ddl_columns_constraints_and_types(project_factory):
    p = project_factory("ddl")
    p.sql(
        "schema.sql",
        """
        CREATE TABLE raw.accounts (
          id INT PRIMARY KEY,
          amount DECIMAL(10,2) NOT NULL UNIQUE,
          qty INT CHECK (qty >= 0 AND qty <= 10),
          note VARCHAR(20),
          owner_id INT
        );
        """,
    )
    result = p.scan()
    account = p.node(result, "raw.accounts.amount")
    assert account.data_type == "DECIMAL(10,2)"
    assert account.nullable is False
    assert "not_null" in account.assertions
    assert "unique" in account.assertions
    primary = p.node(result, "raw.accounts.id")
    assert primary.primary_key is True
    assert primary.nullable is False
    assert p.node(result, "raw.accounts.qty").assertions == ("range:[0,10]",)
    assert p.node(result, "raw.accounts.note").nullable is True


def test_ctas_marks_derived_and_maps_expression(project_factory):
    p = project_factory("ctas")
    p.sql(
        "build.sql",
        """
        CREATE TABLE raw.accounts (id INT, amount DECIMAL(10,2));
        CREATE TABLE mart.copy AS
        SELECT id, amount * 2 AS doubled FROM raw.accounts;
        """,
    )
    result = p.scan()
    derived = p.node(result, "mart.copy.doubled")
    assert derived.origin == "derived"
    assert derived.data_type == "DECIMAL"
    direct = p.edge(result, "raw.accounts.id", "mart.copy.id", "direct")
    assert direct.locations[0].script == "build.sql"
    expression = p.edge(result, "raw.accounts.amount", "mart.copy.doubled")
    assert expression.kind == "expression"


def test_alter_then_drop_keeps_historical_source_and_blocks_later_insert(project_factory):
    p = project_factory("order")
    p.sql(
        "order.sql",
        """
        CREATE TABLE raw.source (id INT);
        ALTER TABLE raw.source ADD COLUMN note VARCHAR(10);
        CREATE TABLE mart.target (id INT, note VARCHAR(10));
        INSERT INTO mart.target (id, note) SELECT id, note FROM raw.source;
        DROP TABLE raw.source;
        INSERT INTO raw.source (id) SELECT id FROM mart.target;
        """,
    )
    result = p.scan()
    assert p.node(result, "raw.source.note").data_type == "VARCHAR(10)"
    p.edge(result, "raw.source.note", "mart.target.note", "direct")
    unknown = [item for item in result.unsupported if item.construct == "unknown_insert_target"]
    assert len(unknown) == 1
    assert unknown[0].script == "order.sql"


def test_redefinition_reports_old_and_new_locations(project_factory):
    p = project_factory("redef")
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.dup (id INT);
        CREATE TABLE raw.dup (id INT, note VARCHAR(5));
        """,
    )
    result = p.scan()
    findings = findings_for(result, "table_redefined")
    assert len(findings) == 1
    assert findings[0].target == "raw.dup"
    assert "a.sql:2" in findings[0].detail
    assert "a.sql:3" in findings[0].detail
    assert p.node(result, "raw.dup.note").data_type == "VARCHAR(5)"


def test_if_not_exists_does_not_report_redefinition(project_factory):
    p = project_factory("ifnot")
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.keep (id INT);
        CREATE TABLE IF NOT EXISTS raw.keep (id INT);
        """,
    )
    result = p.scan()
    assert categories(result).get("table_redefined", 0) == 0


def test_unknown_alter_target_is_reported(project_factory):
    p = project_factory("alter")
    p.sql("a.sql", "ALTER TABLE raw.missing ADD COLUMN note VARCHAR(5);")
    result = p.scan()
    assert any(item.construct == "unknown_alter_target" for item in result.unsupported)
    assert any(item.category == "unsupported_sql" for item in result.findings)


def test_table_level_primary_key_and_quoted_identifiers(project_factory):
    p = project_factory("primary")
    p.sql(
        "schema.sql",
        """
        CREATE TABLE "Raw"."Events" (
          "Tenant" INT NOT NULL,
          "Event_ID" BIGINT,
          note VARCHAR(12),
          PRIMARY KEY ("Tenant", "Event_ID")
        );
        """,
    )
    result = p.scan()
    tenant = p.node(result, "raw.events.tenant")
    event = p.node(result, "raw.events.event_id")
    assert tenant.primary_key and event.primary_key
    assert tenant.nullable is False
    assert "unique" in event.assertions and "not_null" in event.assertions


def test_standalone_select_is_inventoried_without_edges(project_factory):
    p = project_factory("select")
    p.sql("a.sql", "SELECT id FROM raw.unknown;")
    result = p.scan()
    assert [item.status for item in result.statements] == ["supported"]
    assert result.statements[0].kind == "select"
    assert result.graph.edges == ()
    assert result.graph.nodes == ()


def test_unsupported_create_view_is_never_silent(project_factory):
    p = project_factory("view")
    p.sql("a.sql", "CREATE VIEW mart.v AS SELECT id FROM raw.t;")
    result = p.scan()
    assert any(item.construct == "unsupported_statement" for item in result.unsupported)
    assert findings_for(result, "unsupported_sql")
