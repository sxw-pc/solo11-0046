"""Structured numeric CHECK bounds and interval-coverage contract comparison."""

from decimal import Decimal

import pytest
from support import findings_for

from lineage_inquest.contract.loader import normalize_assertion
from lineage_inquest.errors import ConfigError
from lineage_inquest.models import Bound, RangeAssertion, parse_range_text


def one_range(node) -> RangeAssertion:
    ranges = [item for item in node.assertions if isinstance(item, RangeAssertion)]
    assert len(ranges) == 1, node.assertions
    return ranges[0]


def node_named(result, name):
    return next(n for n in result.graph.nodes if n.name == name)


def text(value) -> str:
    return str(value)


CONTRACT = """
version: 1
contracts:
  - target: mart.t.out
    required_upstream: [raw.t.src]
    assertions: ["{assertion}"]
"""


def build(project_factory, name, ddl, assertion=None):
    """Build ``raw.t`` -> ``mart.t.out`` and optionally declare an assertion."""
    p = project_factory(name)
    p.sql("a.sql", ddl + "\nINSERT INTO mart.t (out) SELECT src FROM raw.t;")
    if assertion is not None:
        p.contract("c.yaml", CONTRACT.format(assertion=assertion))
    return p


def with_target_check(check: str, data_type: str = "DECIMAL(10,2)") -> str:
    """DDL whose *target* column carries the CHECK under test."""
    return (
        f"CREATE TABLE raw.t (src {data_type});\n"
        f"CREATE TABLE mart.t (out {data_type} CHECK ({check}));"
    )


def with_source_check(check: str, data_type: str = "DECIMAL(10,2)") -> str:
    """DDL whose *source* column carries the CHECK (target output is unchecked)."""
    return (
        f"CREATE TABLE raw.t (src {data_type} CHECK ({check}));\n"
        f"CREATE TABLE mart.t (out {data_type});"
    )


def assertion_findings(result):
    return findings_for(result, "assertion_mismatch")


# ---------------------------------------------------------------------------
# DDL normalization (read the raw.* column that owns the CHECK)
# ---------------------------------------------------------------------------

def test_between_is_normalized_with_value_precision(project_factory):
    result = build(
        project_factory,
        "between",
        with_source_check("src BETWEEN -12.50 AND 30.75"),
    ).scan()
    found = one_range(node_named(result, "raw.t.src"))
    assert found.lower == Bound(Decimal("-12.50"), True)
    assert found.upper == Bound(Decimal("30.75"), True)
    assert text(found) == "range:[-12.50,30.75]"


def test_two_sided_comparisons_preserve_open_and_closed_endpoints(project_factory):
    result = build(
        project_factory,
        "exclusive",
        with_source_check("src > -0.5 AND src < 10.25"),
    ).scan()
    found = one_range(node_named(result, "raw.t.src"))
    assert found.lower == Bound(Decimal("-0.5"), False)
    assert found.upper == Bound(Decimal("10.25"), False)
    assert text(found) == "range:(-0.5,10.25)"


@pytest.mark.parametrize(
    "check,expected",
    [
        ("src >= 0", "range:[0,+inf)"),
        ("src > 0.50", "range:(0.50,+inf)"),
        ("src <= 100.00", "range:(-inf,100.00]"),
        ("src < -1.5", "range:(-inf,-1.5)"),
    ],
)
def test_single_sided_bounds_are_kept(project_factory, check, expected):
    p = build(project_factory, f"one-sided-{check}", with_source_check(check))
    result = p.scan()
    assert text(one_range(node_named(result, "raw.t.src"))) == expected


def test_table_level_checks_apply_to_the_constrained_column(project_factory):
    p = build(
        project_factory,
        "table-level",
        """
        CREATE TABLE raw.t (
          untouched INT,
          src DECIMAL(10,2),
          CHECK (src >= -1.25 AND src <= 4.50)
        );
        CREATE TABLE mart.t (out DECIMAL(10,2));
        """,
    )
    result = p.scan()
    assert text(one_range(node_named(result, "raw.t.src"))) == "range:[-1.25,4.50]"
    assert node_named(result, "raw.t.untouched").assertions == ()


def test_reverse_operand_order_and_parentheses_are_handled(project_factory):
    p = build(
        project_factory,
        "reverse",
        with_source_check("(0.25 <= src) AND (src < 9.75)"),
    )
    result = p.scan()
    found = one_range(node_named(result, "raw.t.src"))
    assert found.lower == Bound(Decimal("0.25"), True)
    assert found.upper == Bound(Decimal("9.75"), False)


def test_non_numeric_checks_do_not_produce_ranges(project_factory):
    p = build(
        project_factory,
        "non-numeric",
        with_source_check("src <> 0"),
    )
    result = p.scan()
    assert node_named(result, "raw.t.src").assertions == ()


def test_or_check_does_not_produce_a_range(project_factory):
    p = build(project_factory, "or-check", with_source_check("src > 1 OR src < -1"))
    result = p.scan()
    assert node_named(result, "raw.t.src").assertions == ()


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("range:[0,50]", "range:[0,50]"),
        ("range:(0.00,50.75)", "range:(0.00,50.75)"),
        ("range:[-10.5,+inf)", "range:[-10.5,+inf)"),
        ("range:[10,5]", "range:[10,5]"),
        ("range:[-10,-5]", "range:[-10,-5]"),
        ("not_null", "not_null"),
    ],
)
def test_loader_normalizes_ranges_to_the_same_structure(value, expected):
    assert text(normalize_assertion(value)) == expected


def test_loader_accepts_open_and_single_sided_ranges():
    lowered = normalize_assertion("range:(-3.50,5.25]")
    assert isinstance(lowered, RangeAssertion)
    assert lowered.lower == Bound(Decimal("-3.50"), False)
    assert lowered.upper == Bound(Decimal("5.25"), True)


@pytest.mark.parametrize("value", ["range:[,]", "range:5,10", "range:[a,b]", "bogus", "range:[nan,inf]"])
def test_loader_rejects_malformed_ranges(value):
    with pytest.raises(ConfigError):
        normalize_assertion(value)


def test_parse_range_text_round_trips_text():
    parsed = parse_range_text("range:[0.00,10)")
    assert parsed is not None
    assert str(parsed) == "range:[0.00,10)"


# ---------------------------------------------------------------------------
# Coverage comparison (target column carries the actual CHECK)
# ---------------------------------------------------------------------------

def test_actual_check_covering_the_declared_range_is_clean(project_factory):
    p = build(
        project_factory,
        "covered",
        with_target_check("out BETWEEN -5.00 AND 50.00"),
        assertion="range:[-2.50,20.00]",
    )
    result = p.scan()
    assert assertion_findings(result) == []


def test_tighter_actual_check_is_an_assertion_mismatch(project_factory):
    p = build(
        project_factory,
        "tighter",
        with_target_check("out BETWEEN -2.50 AND 20.00"),
        assertion="range:[-5.00,50.00]",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "range not covered" in finding.detail
    assert "declared range:[-5.00,50.00]" in finding.detail
    assert "actual range:[-2.50,20.00]" in finding.detail
    assert "lower bound -2.50 is tighter" in finding.detail
    assert "upper bound 20.00 is tighter" in finding.detail


def test_open_endpoint_actual_does_not_cover_closed_declared(project_factory):
    p = build(
        project_factory,
        "open-endpoint",
        with_target_check("out >= 0 AND out < 10", "INT"),
        assertion="range:[0,10]",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "upper bound 10 is tighter (open)" in finding.detail


def test_closed_endpoint_actual_covers_open_declared(project_factory):
    p = build(
        project_factory,
        "closed-covers-open",
        with_target_check("out >= 0 AND out <= 10", "INT"),
        assertion="range:(0,10)",
    )
    result = p.scan()
    assert assertion_findings(result) == []


def test_single_sided_actual_cannot_cover_two_sided_declared(project_factory):
    p = build(
        project_factory,
        "single-sided-actual",
        with_target_check("out <= 100.00"),
        assertion="range:[0,100.00]",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "missing lower bound 0" in finding.detail
    assert "actual range:(-inf,100.00]" in finding.detail


def test_no_check_at_all_is_an_assertion_mismatch(project_factory):
    p = build(
        project_factory,
        "no-check",
        "CREATE TABLE raw.t (src DECIMAL(10,2));\nCREATE TABLE mart.t (out DECIMAL(10,2));",
        assertion="range:[0,50]",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "actual no numeric CHECK" in finding.detail
    assert "no CHECK constrains this column" in finding.detail


def test_negative_decimal_precision_is_preserved_in_the_detail(project_factory):
    p = build(
        project_factory,
        "precision",
        with_target_check(
            "out >= -99999999.9999 AND out <= 0.0100",
            "DECIMAL(18,4)",
        ),
        assertion="range:[-99999999.9999,0.0101]",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "actual range:[-99999999.9999,0.0100]" in finding.detail
    assert "declared range:[-99999999.9999,0.0101]" in finding.detail


def test_not_null_and_unique_still_use_strict_string_equality(project_factory):
    p = build(
        project_factory,
        "scalar-strict",
        with_target_check("out >= 0"),
        assertion="not_null",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert finding.detail == "missing assertions: not_null"


def test_derived_ctas_column_keeps_empty_assertions(project_factory):
    p = project_factory("derived-empty")
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.t (src DECIMAL(10,2) CHECK (src >= 0));
        CREATE TABLE mart.clone AS SELECT src FROM raw.t;
        """,
    )
    result = p.scan()
    assert node_named(result, "mart.clone.src").assertions == ()
    assert assertion_findings(result) == []


def test_mixed_scalar_and_range_failures_share_one_finding(project_factory):
    p = project_factory("mixed")
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.t (src DECIMAL(10,2));
        CREATE TABLE mart.t (out DECIMAL(10,2) CHECK (out > 0 AND out < 10));
        INSERT INTO mart.t (out) SELECT src FROM raw.t;
        """,
    )
    p.contract(
        "c.yaml",
        """
version: 1
contracts:
  - target: mart.t.out
    required_upstream: [raw.t.src]
    assertions: [not_null, "range:[-1.00,10.00]"]
""",
    )
    result = p.scan()
    finding = assertion_findings(result)[0]
    assert "missing assertions: not_null" in finding.detail
    assert "range not covered" in finding.detail
    assert "declared range:[-1.00,10.00]" in finding.detail
