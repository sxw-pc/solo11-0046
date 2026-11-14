"""Read-only local Flask viewer. Every route is GET-only."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from flask import Flask, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from .config import Config
from .errors import NotFoundError
from .graph.build import edge_payload
from .graph.query import downstream, upstream
from .store import LedgerStore

_RUN = re.compile(r"^run-[0-9a-f]{12}$")
_FINDING = re.compile(r"^f-[0-9a-f]{16}$")
_NODE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}(?:\.[A-Za-z_][A-Za-z0-9_]{0,62}){2}$")
_STYLE = """
body{margin:0;background:#f6f7f9;color:#1f2933;font:15px/1.55 system-ui,sans-serif}
header{background:#10384a;color:#fff;padding:18px 28px}header h1{margin:0;font-size:21px}
nav{padding:10px 28px;background:#e8eef2}nav a{color:#145c7a;margin-right:18px;text-decoration:none}
main{max-width:1100px;margin:20px auto;padding:0 18px 40px}
section,table{background:#fff;border:1px solid #d9e0e7;border-radius:7px}
section{padding:16px 18px;margin-bottom:16px}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #d9e0e7;vertical-align:top}
th{background:#f3f6f8;color:#5c6b7a}code{font-family:Consolas,monospace;font-size:12px;word-break:break-word}
.metrics{display:flex;gap:10px;flex-wrap:wrap}.metric{border:1px solid #d9e0e7;border-radius:6px;padding:10px 14px;min-width:100px}
.metric strong{display:block;font-size:22px}.muted{color:#5c6b7a}.error{color:#a4262c}
a{color:#145c7a}.tag{display:inline-block;padding:1px 8px;border-radius:999px;color:#fff;background:#235c8a;font-size:11px}
.tag.error{background:#a4262c}.tag.warn{background:#8a5a00}.tag.info{background:#235c8a}
"""


def create_app(config: Config, out_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_SORT_KEYS=True, MAX_CONTENT_LENGTH=64 * 1024)
    store = LedgerStore((out_dir or config.out_dir) / "ledger.sqlite")
    app.extensions["ledger_store"] = store

    @app.before_request
    def read_only():
        if request.method != "GET":
            abort(405, description="this service is read-only; only GET is accepted")

    @app.get("/")
    def index():
        selected = request.args.get("run_id")
        latest = _run_id(store) if selected else store.latest_run_id()
        runs = store.list_runs()
        if latest is None:
            body = '<section><h2>No runs yet</h2><p class="muted">Run <code>scan</code> to create a ledger.</p></section>'
            return _page("Overview", body)
        run = store.get_run(latest)
        counts = run["counts"]
        metrics = "".join(
            f'<div class="metric"><strong>{escape(str(value))}</strong><span class="muted">{escape(key)}</span></div>'
            for key, value in counts.items()
        )
        findings = store.list_findings(latest)[:20]
        body = f'<section><h2>Latest run</h2><p><code>{escape(latest)}</code> · {escape(run["created_at"])}</p><div class="metrics">{metrics}</div></section>'
        body += '<section><h2>Recent findings</h2>' + _finding_table(findings, latest, detail_links=True) + "</section>"
        body += '<section><h2>Run history</h2><table><tr><th>Run</th><th>Created</th><th>Findings</th></tr>'
        for item in runs:
            body += f'<tr><td><code>{escape(item["run_id"])}</code></td><td>{escape(item["created_at"])}</td><td>{item["counts"]["findings"]}</td></tr>'
        body += "</table></section>"
        return _page("Overview", body, latest)

    @app.get("/findings")
    def findings():
        run_id = _run_id(store)
        severity = request.args.get("severity", "").strip()
        if severity and severity not in {"error", "warn", "info"}:
            abort(400, description="severity must be error, warn, or info")
        items = store.list_findings(run_id, severity or None)
        body = f'<section><h2>Findings</h2><p class="muted">Run <code>{escape(run_id)}</code> · {len(items)} row(s)</p>'
        body += _finding_table(items, run_id, detail_links=True) + "</section>"
        return _page("Findings", body, run_id)

    @app.get("/finding/<finding_id>")
    def finding(finding_id: str):
        if not _FINDING.match(finding_id):
            abort(400, description="invalid finding id")
        run_id = _run_id(store)
        item = store.get_finding(run_id, finding_id)
        if item is None:
            abort(404, description="finding not found")
        body = (
            f'<section><h2>Finding <code>{escape(item["id"])}</code></h2>'
            f'<p>{_badge(item["severity"])} <code>{escape(item["category"])}</code></p>'
            f'<p><strong>Target:</strong> <code>{escape(item["target"])}</code></p>'
            f'<p><strong>Message:</strong> {escape(item["message"])}</p>'
            f'<p><strong>Detail:</strong> {escape(item["detail"])}</p>'
            f'<p class="muted">Location: {escape(item["script"] or "contract/global")}:{item["line"]} · '
            f'Default severity: {escape(item["default_severity"])}</p></section>'
        )
        return _page("Finding", body, run_id)

    @app.get("/node/<path:node>")
    def node_page(node: str):
        run_id, graph, depth = _query_context(store, node)
        try:
            up = upstream(graph, node, min(depth, 3))
            down = downstream(graph, node, min(depth, 3))
        except NotFoundError:
            abort(404, description="node not found in this run")
        body = f'<section><h2>Node <code>{escape(node)}</code></h2><p class="muted">Run <code>{escape(run_id)}</code></p>'
        body += _traversal_table("Upstream", up, run_id) + _traversal_table("Downstream", down, run_id) + "</section>"
        return _page("Node", body, run_id)

    @app.get("/api/graph")
    def api_graph():
        run_id = _run_id(store)
        graph = store.load_graph(run_id)
        return jsonify(
            {
                "run_id": run_id,
                "nodes": [_node_for_api(node) for node in graph.nodes],
                "edges": [edge_payload(edge) for edge in graph.edges],
            }
        )

    @app.get("/api/query")
    def api_query():
        node = request.args.get("node", "").strip()
        run_id, graph, depth = _query_context(store, node)
        direction = request.args.get("direction", "upstream").strip()
        if direction not in {"upstream", "downstream"}:
            abort(400, description="direction must be upstream or downstream")
        try:
            result = upstream(graph, node, depth) if direction == "upstream" else downstream(graph, node, depth)
        except NotFoundError:
            abort(404, description="node not found in this run")
        return jsonify({"run_id": run_id, **result})

    @app.get("/api/upstream")
    def api_upstream():
        return _api_direction("upstream")

    @app.get("/api/downstream")
    def api_downstream():
        return _api_direction("downstream")

    def _api_direction(direction: str):
        node = request.args.get("node", "").strip()
        run_id, graph, depth = _query_context(store, node)
        try:
            result = upstream(graph, node, depth) if direction == "upstream" else downstream(graph, node, depth)
        except NotFoundError:
            abort(404, description="node not found in this run")
        return jsonify({"run_id": run_id, **result})

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.name, "message": error.description}), error.code
        return _page(error.name, f'<section><h2>{escape(error.name)}</h2><p>{escape(error.description or "")}</p></section>'), error.code

    @app.errorhandler(Exception)
    def unexpected(error):  # pragma: no cover - defensive boundary
        app.logger.exception("unhandled server error")
        return jsonify({"error": "internal_error", "message": "the request could not be served"}), 500

    return app


def _run_id(store: LedgerStore) -> str:
    raw = request.args.get("run_id")
    if raw is None or raw == "":
        run_id = store.latest_run_id()
        if run_id is None:
            abort(404, description="no runs are available")
        return run_id
    if len(raw) > 40 or not _RUN.match(raw):
        abort(400, description="invalid run id")
    if not store.run_exists(raw):
        abort(404, description="unknown run id")
    return raw


def _query_context(store: LedgerStore, node: str):
    if len(node) > 200 or not _NODE.match(node):
        abort(400, description="invalid node name")
    run_id = _run_id(store)
    raw_depth = request.args.get("depth", "3")
    if not raw_depth.isdigit() or not 1 <= int(raw_depth) <= 20:
        abort(400, description="depth must be an integer between 1 and 20")
    return run_id, store.load_graph(run_id), int(raw_depth)


def _page(title: str, body: str, run_id: str | None = None) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)} · Lineage Inquest</title><style>{_STYLE}</style></head><body>"
        "<header><h1>Lineage Inquest · read-only</h1></header>"
        f"<nav><a href=\"/\">Overview</a><a href=\"/findings\">Findings</a>"
        f"{'<a href=\"/api/graph\">Graph JSON</a>' if run_id else ''}</nav>"
        f"<main>{body}</main></body></html>"
    )


def _badge(severity: str) -> str:
    safe = severity if severity in {"error", "warn", "info"} else "info"
    return f'<span class="tag {safe}">{escape(severity)}</span>'


def _finding_table(items: list[dict], run_id: str, detail_links: bool) -> str:
    if not items:
        return '<p class="muted">No findings.</p>'
    rows = "<tr><th>Severity</th><th>Category</th><th>Target</th><th>Message</th></tr>"
    for item in items:
        label = f'<a href="/finding/{escape(item["id"])}">{escape(item["id"])}</a>' if detail_links else escape(item["id"])
        rows += f'<tr><td>{_badge(item["severity"])}</td><td><code>{escape(item["category"])}</code></td><td><code>{escape(item["target"])}</code></td><td>{escape(item["message"])}<br><span class="muted">{label}</span></td></tr>'
    return f"<table>{rows}</table>"


def _traversal_table(title: str, payload: dict, run_id: str) -> str:
    rows = f"<h2>{escape(title)}</h2><p class=\"muted\">depth {payload['depth']}, {payload['truncated'] and 'truncated' or 'complete'}</p>"
    rows += "<table><tr><th>Hop</th><th>Column</th><th>Type</th></tr>"
    for node in payload["nodes"]:
        rows += f'<tr><td>{node["hop"]}</td><td><code>{escape(node["name"])}</code></td><td>{escape(node["data_type"])}</td></tr>'
    return rows + "</table>"


def _node_for_api(node) -> dict:
    return {
        "id": node.node_id,
        "name": node.name,
        "table": node.table,
        "column": node.column,
        "data_type": node.data_type,
        "nullable": node.nullable,
        "primary_key": node.primary_key,
        "origin": node.origin,
        "assertions": list(node.assertions),
    }
