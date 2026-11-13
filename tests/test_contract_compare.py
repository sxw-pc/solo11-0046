import pytest
from support import categories, findings_for

from lineage_inquest.contract.compare import types_match
from lineage_inquest.errors import ConfigError

SQL = """
CREATE TABLE raw.orders (
  order_id INT PRIMARY KEY,
  amount DECIMAL(10,2) NOT NULL,
  qty INT CHECK (qty >= 0 AND qty <= 10),
  note VARCHAR(20),
  legacy VARCHAR(20)
);
CREATE TABLE mart.summary (
  order_id INT,
  amount_copy DECIMAL(10,2),
  measure VARCHAR(20),
  nullable_note VARCHAR(20),
  qty_copy INT,
  exact_date DATE,
  checked_id INT NOT NULL,
  unique_note VARCHAR(20) NOT NULL UNIQUE
);
INSERT INTO mart.summary (
  order_id, amount_copy, measure, nullable_note, qty_copy, exact_date, checked_id, unique_note
)
SELECT order_id, amount, CAST(amount AS VARCHAR(20)) AS measure, note, qty,
       CAST('2024-01-01' AS DATE) AS exact_date, order_id AS checked_id, note AS unique_note
FROM raw.orders;
"""

CONTRACTS = """
version: 1
contracts:
  - target: mart.summary.amount_copy
    required_upstream: [raw.orders.legacy]
    forbidden_upstream: [raw.orders.amount]
    expected_type: numeric
    assertions: [not_null]
  - target: mart.summary.qty_copy
    required_upstream: [raw.orders.legacy]
    expected_type: decimal(10,2)
    assertions: ["range:[0,20]"]
  - target: mart.summary.measure
    required_upstream: [raw.orders.amount]
    expected_type: numeric
  - target: mart.summary.nullable_note
    required_upstream: [raw.orders.note]
    expected_type: integer
  - target: mart.summary.order_id
    required_upstream: [raw.orders.order_id]
    expected_type: integer
  - target: mart.summary.exact_date
    expected_type: date
  - target: mart.summary.checked_id
    required_upstream: [raw.orders.order_id]
    expected_type: integer
    assertions: [not_null]
  - target: mart.summary.unique_note
    required_upstream: [raw.orders.note]
    expected_type: string
    assertions: [not_null, unique]
"""


def _project(project_factory, name="contracts", **config):
    p = project_factory(name, **config)
    p.sql("schema.sql", SQL)
    p.contract("summary.yaml", CONTRACTS)
    return p


def test_two_positive_and_one_negative_for_each_contract_category(project_factory):
    result = _project(project_factory).scan()
    counts = categories(result)
    assert counts["missing_upstream"] == 2
    assert counts["unexpected_upstream"] == 2
    assert counts["type_mismatch"] == 3
    assert counts["assertion_mismatch"] == 2
    assert counts.get("cycle_detected", 0) == 0
    assert counts.get("unsupported_sql", 0) == 0
    missing = {item.target for item in findings_for(result, "missing_upstream")}
    assert missing == {"mart.summary.amount_copy", "mart.summary.qty_copy"}
    unexpected = {item.target for item in findings_for(result, "unexpected_upstream")}
    assert unexpected == {"mart.summary.amount_copy", "mart.summary.qty_copy"}
    typed = {item.target for item in findings_for(result, "type_mismatch")}
    assert typed == {"mart.summary.measure", "mart.summary.nullable_note", "mart.summary.qty_copy"}
    declared = {item.target for item in findings_for(result, "assertion_mismatch")}
    assert declared == {"mart.summary.amount_copy", "mart.summary.qty_copy"}
    clean_targets = {"mart.summary.order_id", "mart.summary.exact_date", "mart.summary.checked_id", "mart.summary.unique_note"}
    assert not clean_targets & {item.target for item in result.findings}


def test_forbidden_source_is_called_out(project_factory):
    result = _project(project_factory, "forbidden").scan()
    finding = next(
        item for item in findings_for(result, "unexpected_upstream")
        if item.target == "mart.summary.amount_copy"
    )
    assert "forbidden" in finding.detail
    assert "raw.orders.amount" in finding.detail


def test_assertion_comparison_uses_static_ddl_metadata(project_factory):
    result = _project(project_factory, "assertions").scan()
    amount = next(
        item for item in findings_for(result, "assertion_mismatch")
        if item.target == "mart.summary.amount_copy"
    )
    assert amount.detail == "missing assertions: not_null"
    qty = next(
        item for item in findings_for(result, "assertion_mismatch")
        if item.target == "mart.summary.qty_copy"
    )
    assert "range:[0,20]" in qty.detail


def test_type_families_and_parametrized_types():
    assert types_match("numeric", "DECIMAL(10,2)")
    assert types_match("integer", "BIGINT")
    assert types_match("string", "VARCHAR(20)")
    assert types_match("date", "DATE")
    assert types_match("decimal(10,2)", "DECIMAL(10,2)")
    assert not types_match("numeric", "VARCHAR(20)")
    assert not types_match("integer", "unknown")


def test_severity_override_is_recorded(project_factory):
    p = _project(project_factory, "override", severity={"type_mismatch": "info"})
    result = p.scan()
    typed = findings_for(result, "type_mismatch")
    assert typed
    assert all(item.severity == "info" for item in typed)
    assert all(item.default_severity == "error" for item in typed)
    assert all(item.overridden for item in typed)
    assert result.severity_overrides == {"type_mismatch": "info"}


def test_contract_missing_target_reports_missing_upstream(project_factory):
    p = project_factory("ghost")
    p.sql("a.sql", "CREATE TABLE raw.a (id INT);")
    p.contract(
        "ghost.yaml",
        """
        version: 1
        contracts:
          - target: mart.ghost.amount
            required_upstream: [raw.a.id]
        """,
    )
    result = p.scan()
    finding = findings_for(result, "missing_upstream")[0]
    assert "absent from the graph" in finding.detail


@pytest.mark.parametrize(
    "body,message",
    [
        ("version: 1\ncontracts:\n  - target: bad\n", "invalid column reference"),
        ("version: 9\ncontracts: []\n", "unsupported contract version"),
        ("version: 1\nunknown: true\n", "unknown root key"),
        (
            "version: 1\ncontracts:\n  - target: raw.t.a\n    assertions: [bogus]\n",
            "invalid assertion",
        ),
        (
            "version: 1\ncontracts:\n  - target: raw.t.a\n    required_upstream: [raw.t.a]\n    forbidden_upstream: [raw.t.a]\n",
            "both required and forbidden",
        ),
    ],
)
def test_invalid_contract_is_a_readable_config_error(project_factory, body, message):
    p = project_factory("badcontract")
    p.sql("a.sql", "CREATE TABLE raw.t (a INT);")
    p.contract("bad.yaml", body)
    with pytest.raises(ConfigError) as caught:
        p.scan()
    assert message in str(caught.value)


def test_two_part_contract_references_use_default_schema_and_normalize_case(project_factory):
    p = project_factory("twopart")
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.t (id INT);
        CREATE TABLE mart.t (id INT);
        INSERT INTO mart.t (id) SELECT id FROM raw.t;
        """,
    )
    p.contract(
        "c.yaml",
        """
        version: 1
        contracts:
          - target: MART.T.ID
            required_upstream: [RAW.T.ID]
            expected_type: integer
        """,
    )
    result = p.scan()
    assert result.findings == ()


def test_duplicate_contract_targets_across_files_are_rejected(project_factory):
    p = project_factory("dupe-contract")
    p.sql("a.sql", "CREATE TABLE raw.t (id INT);")
    body = """version: 1
contracts:
  - target: raw.t.id
"""
    p.contract("a.yaml", body)
    p.contract("b.yaml", body)
    with pytest.raises(ConfigError, match="duplicate contract target"):
        p.scan()
