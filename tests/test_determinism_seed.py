import hashlib
import json
import socket
from pathlib import Path

import pytest

from lineage_inquest.config import load_config
from lineage_inquest.demo_data import DEMO_FILES
from lineage_inquest.report.json_out import findings_payload, graph_payload
from lineage_inquest.report.writer import write_artifacts
from lineage_inquest.scanner import scan
from lineage_inquest.seed import seed


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_seed_is_idempotent_and_complete(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    seed(first)
    seed(second)
    assert tree_digest(first) == tree_digest(second)
    assert set(DEMO_FILES) == set(tree_digest(first))
    before = tree_digest(first)
    seed(first)
    assert tree_digest(first) == before


def test_seeded_demo_scans_cleanly(tmp_path):
    seed(tmp_path / "demo")
    config = load_config(tmp_path / "demo" / "config.yaml")
    first = scan(config)
    second = scan(config)
    assert first.findings == ()
    assert first.unsupported == ()
    assert first.run_id == second.run_id
    assert first.summary["counts"]["nodes"] == 30
    assert first.summary["counts"]["edges"] == 18


def test_graph_and_findings_are_byte_stable_without_timestamps(project_factory, tmp_path):
    p = project_factory("stable", frozen=None)
    p.sql(
        "a.sql",
        """
        CREATE TABLE raw.t (id INT, amount INT);
        CREATE TABLE mart.t (id INT, amount INT);
        INSERT INTO mart.t (id, amount) SELECT id, amount FROM raw.t;
        """,
    )
    first = p.scan()
    second = p.scan()
    one = write_artifacts(first, tmp_path / "a")
    two = write_artifacts(second, tmp_path / "b")
    assert Path(one["graph"]).read_bytes() == Path(two["graph"]).read_bytes()
    assert Path(one["findings"]).read_bytes() == Path(two["findings"]).read_bytes()


def test_demo_expected_snapshot_matches(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "fixtures" / "demo" / "config.yaml")
    result = scan(config, "2024-06-01T00:00:00Z")
    expected = root / "fixtures" / "demo" / "expected"
    assert findings_payload(result) == json.loads((expected / "findings.json").read_text(encoding="utf-8"))
    assert graph_payload(result) == json.loads((expected / "graph.json").read_text(encoding="utf-8"))


def test_broken_expected_snapshot_has_all_structural_categories():
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "fixtures" / "broken" / "config.yaml")
    result = scan(config, "2024-06-02T00:00:00Z")
    expected = json.loads((root / "fixtures" / "broken" / "expected" / "findings.json").read_text(encoding="utf-8"))
    assert findings_payload(result) == expected
    assert set(expected["summary"]["by_category"]) >= {
        "missing_upstream",
        "unexpected_upstream",
        "type_mismatch",
        "assertion_mismatch",
        "unsupported_sql",
        "cycle_detected",
        "table_redefined",
    }


def test_external_network_is_rejected_while_tests_run():
    with pytest.raises(AssertionError, match="external network access is disabled"):
        socket.create_connection(("offline-host", 9), timeout=0.1)


def test_seeded_demo_reports_are_self_contained_and_stable(tmp_path):
    from lineage_inquest.report.html import render_html
    from lineage_inquest.report.markdown import render_markdown
    from lineage_inquest.report.writer import write_artifacts

    seed(tmp_path / "demo")
    config = load_config(tmp_path / "demo" / "config.yaml")
    result = scan(config)
    first = write_artifacts(result, tmp_path / "r1")
    second = write_artifacts(scan(config), tmp_path / "r2")
    for key in ("markdown", "html"):
        assert Path(first[key]).read_bytes() == Path(second[key]).read_bytes()
    html = render_html(result)
    markdown = render_markdown(result)
    assert "http://" not in html and "https://" not in html
    assert result.run_id in markdown and result.run_id in html
    assert "Statement inventory" in html and "Statement inventory" in markdown
