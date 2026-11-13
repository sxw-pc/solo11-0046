import json
from pathlib import Path

from lineage_inquest.cli import main
from lineage_inquest.store import LedgerStore


def test_scan_command_writes_all_artifacts(demo_project, capsys):
    out = demo_project.root / "cli-out"
    code = main(["scan", "--config", str(demo_project.config_path), "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 0
    assert "scan run-" in captured.out
    for name in ("graph.json", "findings.json", "report.md", "report.html", "ledger.sqlite"):
        assert (out / name).is_file()


def test_invalid_config_and_missing_directory_return_usage_error(project_factory, capsys):
    bad = project_factory("badcfg", unexpected_key=True)
    assert main(["scan", "--config", str(bad.config_path)]) == 2
    assert "unknown config key" in capsys.readouterr().err

    missing = project_factory("missing")
    text = missing.config_path.read_text(encoding="utf-8").replace("sql_dir: sql", "sql_dir: nowhere")
    missing.config_path.write_text(text, encoding="utf-8")
    assert main(["scan", "--config", str(missing.config_path)]) == 2
    assert "sql_dir is not a directory" in capsys.readouterr().err


def test_missing_config_path_is_readable(project_factory, capsys):
    assert main(["scan", "--config", str(project_factory("x").root / "nope.yaml")]) == 2
    assert "config file not found" in capsys.readouterr().err


def test_non_loopback_host_is_rejected_before_serving(project_factory, capsys):
    p = project_factory("host", server={"host": "localhost", "port": 5000})
    code = main(["serve", "--config", str(p.config_path)])
    assert code == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_unknown_run_export_returns_not_found(project_factory, capsys):
    p = project_factory("unknown")
    code = main(["export", "--config", str(p.config_path), "--run-id", "run-000000000000"])
    assert code == 3
    assert "unknown run id" in capsys.readouterr().err


def test_export_and_query_and_diff(demo_project, capsys):
    out = demo_project.root / "ledger"
    first = demo_project.scan_artifacts(out)[0]
    Path(demo_project.sql_dir / "03_extra.sql").write_text(
        "CREATE TABLE mart.extra AS SELECT id, amount FROM raw.orders;\n", encoding="utf-8"
    )
    second = demo_project.scan_artifacts(out)[0]
    assert first.run_id != second.run_id

    code = main(["export", "--config", str(demo_project.config_path), "--out", str(out), "--run-id", first.run_id])
    assert code == 0
    exported = json.loads((out / "exports" / f"{first.run_id}.json").read_text(encoding="utf-8"))
    assert exported["run"]["run_id"] == first.run_id
    capsys.readouterr()

    code = main(
        [
            "query",
            "--config",
            str(demo_project.config_path),
            "--out",
            str(out),
            "--node",
            "mart.daily.total",
            "--direction",
            "upstream",
            "--depth",
            "2",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["direction"] == "upstream"
    assert any(node["name"] == "raw.orders.amount" for node in payload["nodes"])

    code = main(
        [
            "diff",
            "--config",
            str(demo_project.config_path),
            "--out",
            str(out),
            "--base",
            first.run_id,
            "--target",
            second.run_id,
        ]
    )
    assert code == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["edges"]["added_count"] >= 2
    assert diff["findings"]["added_count"] == 0


def test_query_rejects_bad_depth_and_unknown_node(demo_project, capsys):
    out = demo_project.root / "q"
    demo_project.scan_artifacts(out)
    assert main(["query", "--config", str(demo_project.config_path), "--out", str(out), "--node", "raw.orders.id", "--depth", "0"]) == 2
    assert "--depth" in capsys.readouterr().err
    assert main(["query", "--config", str(demo_project.config_path), "--out", str(out), "--node", "mart.nope.id"]) == 3
    assert "node not found" in capsys.readouterr().err


def test_verify_mismatch_returns_one(demo_project, tmp_path, capsys):
    expected = tmp_path / "wrong.json"
    expected.write_text(json.dumps({"version": 1, "findings": []}), encoding="utf-8")
    code = main(["verify", "--config", str(demo_project.config_path), "--expect", str(expected)])
    assert code == 1
    assert "verify FAILED" in capsys.readouterr().err


def test_verify_broken_fixture_succeeds(tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]
    code = main(
        [
            "verify",
            "--config",
            str(root / "fixtures" / "broken" / "config.yaml"),
            "--out",
            str(tmp_path / "pytest-verify"),
            "--expect",
            str(root / "fixtures" / "broken" / "expected" / "findings.json"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "verify OK" in captured.out


def test_ledger_store_round_trip(demo_project):
    result = demo_project.scan()
    out = demo_project.root / "roundtrip"
    from lineage_inquest.report.writer import write_artifacts

    write_artifacts(result, out)
    store = LedgerStore(out / "ledger.sqlite")
    store.write_scan(result)
    assert store.run_exists(result.run_id)
    snapshot = store.snapshot(result.run_id)
    assert len(snapshot["nodes"]) == len(result.graph.nodes)
    assert len(snapshot["edges"]) == len(result.graph.edges)
    graph = store.load_graph(result.run_id)
    assert [node.name for node in graph.nodes] == [node.name for node in result.graph.nodes]
