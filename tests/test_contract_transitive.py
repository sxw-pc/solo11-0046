import pytest
from support import findings_for

from lineage_inquest.contract.loader import load_contracts
from lineage_inquest.errors import ConfigError
from lineage_inquest.report.writer import write_artifacts

# raw.orders.net_amount -> mart.customer_revenue.revenue
#   -> mart.customer_rollup.lifetime_revenue -> mart.customer_latest.lifetime_revenue
CHAIN_SQL = """
CREATE TABLE raw.orders (order_id INT PRIMARY KEY, net_amount DECIMAL(12,2), gross_amount DECIMAL(12,2), note VARCHAR(40));
CREATE TABLE mart.customer_revenue (customer_id INT, revenue DECIMAL(18,2));
INSERT INTO mart.customer_revenue (customer_id, revenue)
SELECT order_id, net_amount FROM raw.orders;
CREATE TABLE mart.customer_rollup AS
SELECT customer_id, SUM(revenue) AS lifetime_revenue FROM mart.customer_revenue GROUP BY customer_id;
CREATE TABLE mart.customer_latest (customer_id INT PRIMARY KEY, lifetime_revenue DECIMAL(18,2));
MERGE INTO mart.customer_latest AS t
USING mart.customer_rollup AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.lifetime_revenue = s.lifetime_revenue
WHEN NOT MATCHED THEN INSERT (customer_id, lifetime_revenue) VALUES (s.customer_id, s.lifetime_revenue);
"""

# Same spine, but revenue also consumes an undeclared raw.orders.gross_amount.
# The nearest undeclared ancestor of the latest target is therefore
# mart.customer_revenue.revenue (the branch root), not the deeper
# raw.orders.gross_amount or the intermediate rollup hop.
BRANCH_SQL = CHAIN_SQL.replace(
    "SELECT order_id, net_amount FROM raw.orders;",
    "SELECT order_id, net_amount + gross_amount FROM raw.orders;",
)


def _project(project_factory, name, contracts, sql=CHAIN_SQL):
    p = project_factory(name)
    p.sql("schema.sql", sql)
    p.contract("c.yaml", contracts)
    return p


def test_transitive_required_source_may_be_reachable_not_direct(project_factory):
    p = _project(
        project_factory,
        "trans-ok",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    assert result.findings == ()
    # Declaring the deep source also accounts for the whole spine in between.
    assert findings_for(result, "unexpected_upstream") == []


def test_direct_mode_still_requires_one_hop_sources(project_factory):
    p = _project(
        project_factory,
        "direct-still-dogs",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            required_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    missing = findings_for(result, "missing_upstream")
    assert len(missing) == 1
    assert "raw.orders.net_amount" in missing[0].detail
    assert "mode=transitive" not in missing[0].detail


def test_transitive_missing_reports_unreachable_source(project_factory):
    p = _project(
        project_factory,
        "trans-missing",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.note]
        """,
    )
    result = p.scan()
    missing = findings_for(result, "missing_upstream")
    assert len(missing) == 1
    assert missing[0].detail == "mode=transitive; missing reachable: raw.orders.note"


def test_transitive_unexpected_is_nearest_undeclared_ancestor(project_factory):
    # BRANCH_SQL: declared raw.orders.net_amount; the undeclared
    # raw.orders.gross_amount is the nearest undeclared ancestor of the
    # declared source.  The forbidden column sits on the declared path and
    # is caught separately.
    p = _project(
        project_factory,
        "trans-unexpected",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.net_amount]
            forbidden_upstream: [mart.customer_revenue.revenue]
        """,
        sql=BRANCH_SQL,
    )
    result = p.scan()
    unexpected = findings_for(result, "unexpected_upstream")
    assert len(unexpected) == 1
    detail = unexpected[0].detail
    assert detail.startswith("mode=transitive; ")
    assert "unexpected nearest undeclared: raw.orders.gross_amount" in detail
    assert "forbidden: mart.customer_revenue.revenue" in detail
    # Intermediate declared-path hops and the forbidden column are not
    # repeated as ordinary unexpected sources.
    frontier = detail.split("forbidden:")[0]
    assert "raw.orders.net_amount" not in frontier
    assert "mart.customer_revenue.revenue" not in frontier


def test_transitive_forbidden_hits_a_column_two_hops_below_declared_node(project_factory):
    # The direct check is satisfied by mart.customer_rollup.lifetime_revenue,
    # while raw.orders.net_amount is forbidden and buried two hops deeper.
    # Declaring the rollup hop does not vouch for where that hop came from,
    # so mart.customer_revenue.revenue is still the nearest undeclared
    # ancestor; the forbidden column is reported separately.
    p = _project(
        project_factory,
        "trans-forbidden-deep",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [mart.customer_rollup.lifetime_revenue]
            forbidden_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    unexpected = findings_for(result, "unexpected_upstream")
    assert len(unexpected) == 1
    detail = unexpected[0].detail
    assert (
        detail
        == "mode=transitive; unexpected nearest undeclared: mart.customer_revenue.revenue"
        "; forbidden: raw.orders.net_amount"
    )
    # The forbidden column is reported as forbidden, not also as an ordinary
    # unexpected source.
    assert "unexpected nearest undeclared: raw.orders.net_amount" not in detail


def test_direct_mode_does_not_see_forbidden_column_deeper_than_one_hop(project_factory):
    p = _project(
        project_factory,
        "direct-shallow-forbidden",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            required_upstream: [mart.customer_rollup.lifetime_revenue]
            forbidden_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    assert findings_for(result, "unexpected_upstream") == []


def test_two_modes_coexist_in_one_scan_without_cross_contamination(project_factory):
    p = _project(
        project_factory,
        "coexist",
        """
        version: 1
        contracts:
          - target: mart.customer_rollup.lifetime_revenue
            required_upstream: [mart.customer_revenue.revenue]
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    assert result.findings == ()
    by_target = {item.target: item for item in load_contracts(p.contracts_dir, "main")}
    assert by_target["mart.customer_rollup.lifetime_revenue"].upstream_mode == "direct"
    assert by_target["mart.customer_latest.lifetime_revenue"].upstream_mode == "transitive"


def test_mixed_mode_findings_carry_distinct_detail_markers(project_factory):
    p = _project(
        project_factory,
        "mixed-markers",
        """
        version: 1
        contracts:
          - target: mart.customer_rollup.lifetime_revenue
            required_upstream: [raw.orders.note]
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.note]
        """,
    )
    result = p.scan()
    missing = {item.target: item for item in findings_for(result, "missing_upstream")}
    direct = missing["mart.customer_rollup.lifetime_revenue"]
    transitive = missing["mart.customer_latest.lifetime_revenue"]
    assert direct.detail == "missing: raw.orders.note"
    assert transitive.detail == "mode=transitive; missing reachable: raw.orders.note"
    assert direct.finding_id != transitive.finding_id


def test_mixed_mode_unexpected_findings_carry_distinct_detail_markers(project_factory):
    # Same shape, two modes: one target declares the direct hop, the other
    # declares the deep source.  Both are happy, proving the modes are
    # evaluated independently on the same graph.
    p = _project(
        project_factory,
        "mixed-markers-ok",
        """
        version: 1
        contracts:
          - target: mart.customer_rollup.lifetime_revenue
            required_upstream: [mart.customer_revenue.revenue]
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.net_amount]
        """,
    )
    result = p.scan()
    assert result.findings == ()


def test_mixed_mode_unexpected_details_differ_for_same_inputs(project_factory):
    # Declaring nothing leaves every branch undeclared in both modes; the
    # detail text must still say which mode produced the finding.
    p = _project(
        project_factory,
        "mixed-unexpected",
        """
        version: 1
        contracts:
          - target: mart.customer_rollup.lifetime_revenue
            required_upstream: []
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: []
        """,
    )
    result = p.scan()
    rollup = next(
        item for item in findings_for(result, "unexpected_upstream")
        if item.target == "mart.customer_rollup.lifetime_revenue"
    )
    latest = next(
        item for item in findings_for(result, "unexpected_upstream")
        if item.target == "mart.customer_latest.lifetime_revenue"
    )
    assert rollup.detail.startswith("unexpected: ")
    assert "mode=" not in rollup.detail
    assert latest.detail.startswith("mode=transitive; ")
    assert rollup.finding_id != latest.finding_id


@pytest.mark.parametrize(
    "body,message",
    [
        (
            """version: 1
contracts:
  - target: raw.t.a
    upstream_mode: transitive
    transitive: true
""",
            "declared more than once",
        ),
        (
            """version: 1
contracts:
  - target: raw.t.a
    upstream_mode: closure
""",
            "upstream_mode must be one of",
        ),
        (
            """version: 1
contracts:
  - target: raw.t.a
    transitive: maybe
""",
            "transitive must be a boolean",
        ),
        (
            """version: 1
contracts:
  - target: raw.t.a
    upstream_mode: transitive
    upstream_mode: direct
""",
            "duplicate key",
        ),
        (
            """version: 1
contracts:
  - target: raw.t.a
    transitive_upstream: true
""",
            "unknown key",
        ),
    ],
)
def test_mode_configuration_errors_are_raised_at_load_time(project_factory, body, message):
    p = project_factory("mode-errors")
    p.sql("a.sql", "CREATE TABLE raw.t (a INT);")
    p.contract("bad.yaml", body)
    with pytest.raises(ConfigError) as caught:
        load_contracts(p.contracts_dir, "main")
    assert message in str(caught.value)


def test_one_target_cannot_mix_modes_across_files(project_factory):
    p = project_factory("mix-across-files")
    p.sql("a.sql", "CREATE TABLE raw.t (a INT);")
    p.contract(
        "a.yaml",
        """version: 1
contracts:
  - target: raw.t.a
    upstream_mode: direct
""",
    )
    p.contract(
        "b.yaml",
        """version: 1
contracts:
  - target: raw.t.a
    upstream_mode: transitive
""",
    )
    with pytest.raises(ConfigError, match="duplicate contract target"):
        load_contracts(p.contracts_dir, "main")


def test_transitive_boolean_shorthand_selects_mode(project_factory):
    p = project_factory("shorthand")
    p.contract(
        "c.yaml",
        """version: 1
contracts:
  - target: raw.t.a
    transitive: true
    required_upstream: [raw.t.a]
""",
    )
    p.contract(
        "d.yaml",
        """version: 1
contracts:
  - target: raw.t.b
    transitive: false
""",
    )
    by_target = {item.target: item for item in load_contracts(p.contracts_dir, "main")}
    assert by_target["raw.t.a"].upstream_mode == "transitive"
    assert by_target["raw.t.b"].upstream_mode == "direct"


def test_repeated_scan_is_byte_identical_with_transitive_contract(project_factory, tmp_path):
    p = _project(
        project_factory,
        "trans-stable",
        """
        version: 1
        contracts:
          - target: mart.customer_latest.lifetime_revenue
            upstream_mode: transitive
            required_upstream: [raw.orders.note]
""",
    )
    first = p.scan()
    second = p.scan()
    one = write_artifacts(first, tmp_path / "one")
    two = write_artifacts(second, tmp_path / "two")
    for key in ("graph", "findings", "markdown", "html"):
        assert one[key].read_bytes() == two[key].read_bytes()
