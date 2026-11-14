import json
from pathlib import Path

from lineage_inquest.report.html import render_html
from lineage_inquest.report.markdown import render_markdown
from lineage_inquest.report.writer import write_artifacts


def test_artifacts_are_written_and_parseable(demo_project, tmp_path):
    result = demo_project.scan()
    out = tmp_path / "reports"
    artifacts = write_artifacts(result, out)
    assert all(path.is_file() for path in artifacts.values())
    graph = json.loads(Path(artifacts["graph"]).read_text(encoding="utf-8"))
    findings = json.loads(Path(artifacts["findings"]).read_text(encoding="utf-8"))
    assert "generated_at" not in graph
    assert "generated_at" not in findings
    assert findings["summary"]["total"] == len(result.findings)
    assert graph["nodes"] == sorted(graph["nodes"], key=lambda item: item["name"])
    assert graph["edges"] == sorted(graph["edges"], key=lambda item: (item["target"], item["source"], item["kind"], item["branch"]))


def test_html_is_self_contained_and_has_collapsible_details(project_factory):
    p = project_factory("html-details")
    p.sql("a.sql", "CREATE TABLE raw.t (id INT); CREATE TABLE mart.t (id INT); INSERT INTO mart.t (id) SELECT id FROM raw.t;")
    p.contract(
        "c.yaml",
        """version: 1
contracts:
  - target: mart.t.id
    required_upstream: [raw.t.missing]
""",
    )
    result = p.scan()
    html = render_html(result)
    assert "<style>" in html
    assert "<details>" in html
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html
    assert "Statement inventory" in html
    assert "Unsupported statements" in html
    assert "By script" in html


def test_markdown_contains_required_sections(demo_project):
    result = demo_project.scan()
    markdown = render_markdown(result)
    for heading in ("# Lineage Inquest report", "## Overview", "## Severity policy", "## Findings", "## Files", "## Statement inventory", "## Unsupported statements"):
        assert heading in markdown
    assert result.run_id in markdown
    assert result.generated_at in markdown


def test_severity_overrides_are_visible_in_reports(project_factory):
    p = project_factory("report-override", severity={"missing_upstream": "info"})
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
          - target: mart.t.id
            required_upstream: [raw.t.missing]
        """,
    )
    result = p.scan()
    html = render_html(result)
    markdown = render_markdown(result)
    assert "config override" in html
    assert "| `missing_upstream` | info | override |" in markdown
    assert findings_override(result)


def findings_override(result):
    return all(item.overridden for item in result.findings if item.category == "missing_upstream")


def test_artifact_bytes_are_identical_for_frozen_runs(demo_project, tmp_path):
    first = demo_project.scan()
    second = demo_project.scan()
    one = write_artifacts(first, tmp_path / "one")
    two = write_artifacts(second, tmp_path / "two")
    for key in ("graph", "findings", "markdown", "html"):
        assert Path(one[key]).read_bytes() == Path(two[key]).read_bytes()
    assert first.run_id == second.run_id


def test_unsupported_script_is_grouped_in_reports(project_factory):
    p = project_factory("report-unsupported")
    p.sql("bad.sql", "SELECT ROW_NUMBER() OVER () FROM raw.t;")
    result = p.scan()
    markdown = render_markdown(result)
    assert "bad.sql" in markdown
    assert "window_function" in markdown
    html = render_html(result)
    assert "window_function" in html
