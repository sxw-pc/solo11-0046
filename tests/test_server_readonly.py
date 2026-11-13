import pytest

from lineage_inquest.server import create_app


@pytest.fixture
def server(demo_project, tmp_path):
    out = tmp_path / "server"
    result = demo_project.scan_artifacts(out)[0]
    app = create_app(demo_project.load_config(), out)
    app.testing = True
    return app.test_client(), result


def test_home_findings_and_node_pages(server):
    client, _ = server
    assert client.get("/").status_code == 200
    assert client.get("/findings").status_code == 200
    assert client.get("/findings?severity=warn").status_code == 200
    page = client.get("/node/mart.daily.total?depth=2")
    assert page.status_code == 200
    assert b"Upstream" in page.data and b"Downstream" in page.data


def test_unknown_run_is_404_not_a_traceback(server):
    client, _ = server
    response = client.get("/?run_id=run-ffffffffffff")
    assert response.status_code == 404
    assert b"Traceback" not in response.data
    api = client.get("/api/graph?run_id=run-ffffffffffff")
    assert api.status_code == 404
    assert "unknown run id" in api.get_json()["message"]


def test_mutations_are_405(server):
    client, _ = server
    assert client.post("/").status_code == 405
    assert client.put("/findings").status_code == 405
    assert client.delete("/api/graph").status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/findings?severity=fatal",
        "/api/upstream?node=mart.daily.total&depth=0",
        "/api/upstream?node=mart.daily.total&depth=21",
        "/api/upstream?node=bad",
        "/api/graph?run_id=not-a-run",
        "/finding/bogus",
    ],
)
def test_invalid_parameters_are_rejected(server, path):
    client, _ = server
    assert client.get(path).status_code == 400


def test_long_node_name_is_rejected(server):
    client, _ = server
    node = "a" * 70 + "." + "b" * 70 + "." + "c" * 70
    assert client.get(f"/api/upstream?node={node}").status_code == 400


def test_unknown_node_and_finding_are_404(server):
    client, _ = server
    assert client.get("/api/upstream?node=mart.nope.id").status_code == 404
    assert client.get("/finding/f-0000000000000000").status_code == 404


def test_finding_detail_and_graph_api(server):
    client, result = server
    if result.findings:
        finding = result.findings[0]
        assert client.get(f"/finding/{finding.finding_id}").status_code == 200
    graph = client.get("/api/graph").get_json()
    assert len(graph["nodes"]) == len(result.graph.nodes)
    assert len(graph["edges"]) == len(result.graph.edges)
    query = client.get("/api/query?node=mart.daily.total&direction=upstream&depth=1").get_json()
    assert query["root"] == "mart.daily.total"
    assert query["direction"] == "upstream"


def test_pages_have_no_external_resources(server):
    client, _ = server
    html = client.get("/").data.decode("utf-8")
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()
    assert "<style>" in html


def test_empty_ledger_home_is_200(tmp_path, demo_project):
    app = create_app(demo_project.load_config(), tmp_path / "empty")
    app.testing = True
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"No runs yet" in response.data


def test_finding_detail_with_real_finding(project_factory, tmp_path):
    p = project_factory("server-finding")
    p.sql("a.sql", "CREATE TABLE raw.t (id INT); CREATE TABLE mart.t (id INT); INSERT INTO mart.t (id) SELECT id FROM raw.t;")
    p.contract(
        "c.yaml",
        """version: 1
contracts:
  - target: mart.t.id
    required_upstream: [raw.t.missing]
""",
    )
    result = p.scan_artifacts(tmp_path / "finding-out")[0]
    app = create_app(p.load_config(), tmp_path / "finding-out")
    app.testing = True
    client = app.test_client()
    finding = result.findings[0]
    response = client.get(f"/finding/{finding.finding_id}")
    assert response.status_code == 200
    assert finding.category.encode() in response.data
    filtered = client.get("/findings?severity=error")
    assert b"missing_upstream" in filtered.data


def test_unknown_node_page_is_404_and_head_is_not_allowed(server):
    client, _ = server
    assert client.get("/node/mart.nope.id").status_code == 404
    response = client.head("/")
    assert response.status_code == 405
