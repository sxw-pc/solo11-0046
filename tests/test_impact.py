import json

from lineage_inquest.cli import main
from lineage_inquest.graph.query import impact


def _diamond_project(project_factory):
    project = project_factory("impact")
    project.sql(
        "01_schema.sql",
        """
        CREATE TABLE raw.src (v INT NOT NULL);
        CREATE TABLE mid.hub (v INT NOT NULL);
        CREATE TABLE mid.left (v INT NOT NULL);
        CREATE TABLE mid.right (v INT NOT NULL);
        CREATE TABLE mart.out (v INT NOT NULL);
        """,
    )
    project.sql(
        "02_build.sql",
        """
        INSERT INTO mid.left (v) SELECT v FROM mid.hub;
        INSERT INTO mid.right (v) SELECT v FROM mid.hub;
        INSERT INTO mart.out (v) SELECT l.v + r.v FROM mid.left l JOIN mid.right r ON l.v = r.v;
        """,
    )
    return project


def _extend_diamond(project):
    project.sql(
        "03_extra.sql",
        """
        INSERT INTO mid.hub (v) SELECT v FROM raw.src;
        CREATE TABLE mart.leaf (v INT NOT NULL);
        INSERT INTO mart.leaf (v) SELECT v FROM raw.src;
        """,
    )


def _run_impact(project, out, base_id, target_id, capsys, *extra):
    code = main(
        [
            "impact",
            "--config",
            str(project.config_path),
            "--out",
            str(out),
            "--base",
            base_id,
            "--target",
            target_id,
            *extra,
        ]
    )
    return code, capsys.readouterr()


def test_impact_added_edges_reach_terminal_columns(project_factory, capsys):
    project = _diamond_project(project_factory)
    out = project.root / "out"
    base = project.scan_artifacts(out)[0]
    _extend_diamond(project)
    target = project.scan_artifacts(out)[0]
    assert base.run_id != target.run_id

    code, captured = _run_impact(project, out, base.run_id, target.run_id, capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["base"] == base.run_id
    assert payload["target"] == target.run_id
    assert payload["summary"] == {"added_count": 2, "removed_count": 0, "terminal_count": 2}
    assert payload["removed"] == []

    entries = {entry["start"]: entry for entry in payload["added"]}
    assert set(entries) == {"mid.hub.v", "mart.leaf.v"}

    hub = entries["mid.hub.v"]
    assert hub["edge"]["source"] == "raw.src.v"
    assert hub["edge"]["target"] == "mid.hub.v"
    # diamond: two deduplicated paths to the same terminal, sorted
    assert hub["terminals"] == [
        {
            "node": "mart.out.v",
            "paths": [
                ["mid.hub.v", "mid.left.v", "mart.out.v"],
                ["mid.hub.v", "mid.right.v", "mart.out.v"],
            ],
        }
    ]

    # the added edge's target end is itself a terminal (no outgoing edges)
    leaf = entries["mart.leaf.v"]
    assert leaf["terminals"] == [{"node": "mart.leaf.v", "paths": [["mart.leaf.v"]]}]


def test_impact_removed_edge_with_missing_start_has_empty_impact(project_factory, capsys):
    project = project_factory("impact-removed")
    project.sql(
        "01_chain.sql",
        """
        CREATE TABLE raw.keep (v INT NOT NULL);
        CREATE TABLE mid.doomed (v INT NOT NULL);
        CREATE TABLE mart.down (v INT NOT NULL);
        INSERT INTO mid.doomed (v) SELECT v FROM raw.keep;
        INSERT INTO mart.down (v) SELECT v FROM mid.doomed;
        """,
    )
    out = project.root / "out"
    base = project.scan_artifacts(out)[0]
    project.sql(
        "01_chain.sql",
        """
        CREATE TABLE raw.keep (v INT NOT NULL);
        CREATE TABLE mart.down (v INT NOT NULL);
        """,
    )
    target = project.scan_artifacts(out)[0]

    code, captured = _run_impact(project, out, base.run_id, target.run_id, capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["summary"]["added_count"] == 0
    assert payload["summary"]["removed_count"] == 2

    entries = {entry["start"]: entry for entry in payload["removed"]}
    # start node exists only in the base graph: unreachable, no fake chain
    doomed = entries["mid.doomed.v"]
    assert doomed["edge"]["source"] == "raw.keep.v"
    assert doomed["terminals"] == []
    # start node survives in the target graph and is itself a terminal there
    down = entries["mart.down.v"]
    assert down["edge"]["source"] == "mid.doomed.v"
    assert down["terminals"] == [{"node": "mart.down.v", "paths": [["mart.down.v"]]}]


def test_impact_output_is_byte_stable(project_factory, capsys):
    project = _diamond_project(project_factory)
    out = project.root / "out"
    base = project.scan_artifacts(out)[0]
    _extend_diamond(project)
    target = project.scan_artifacts(out)[0]

    first = project.root / "impact-1.json"
    second = project.root / "impact-2.json"
    code, captured = _run_impact(project, out, base.run_id, target.run_id, capsys, "--output", str(first))
    assert code == 0
    assert f"impact written to {first}" in captured.out
    code, _ = _run_impact(project, out, base.run_id, target.run_id, capsys, "--output", str(second))
    assert code == 0
    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["summary"]["added_count"] == 2


def test_impact_unknown_run_returns_not_found(project_factory, capsys):
    project = project_factory("impact-unknown")
    project.sql("01.sql", "CREATE TABLE raw.a (v INT NOT NULL);")
    out = project.root / "out"
    run = project.scan_artifacts(out)[0]

    code, captured = _run_impact(project, out, "run-000000000000", run.run_id, capsys)
    assert code == 3
    assert "unknown run id" in captured.err
    code, captured = _run_impact(project, out, run.run_id, "run-000000000000", capsys)
    assert code == 3
    assert "unknown run id" in captured.err


def test_impact_cycles_do_not_loop_or_invent_terminals():
    base = {"nodes": [], "edges": []}
    target = {
        "nodes": [{"name": name} for name in ("a", "b", "t")],
        "edges": [
            {"id": "e-ab", "source": "a", "target": "b", "kind": "direct"},
            {"id": "e-ba", "source": "b", "target": "a", "kind": "direct"},
            {"id": "e-bt", "source": "b", "target": "t", "kind": "direct"},
        ],
    }
    payload = impact(base, target)
    assert payload["summary"]["added_count"] == 3
    entries = {entry["edge"]["id"]: entry for entry in payload["added"]}
    # a <-> b cycle: only the path that escapes to the real terminal survives
    assert entries["e-ab"]["terminals"] == [{"node": "t", "paths": [["b", "t"]]}]
    assert entries["e-ba"]["terminals"] == [{"node": "t", "paths": [["a", "b", "t"]]}]
    assert entries["e-bt"]["terminals"] == [{"node": "t", "paths": [["t"]]}]
