"""Render a self-contained, read-only HTML report with inline CSS only."""

from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path

from ..models import ScanResult

_TEMPLATE = Path(__file__).resolve().parent.parent / "template" / "report.html"
_CATEGORIES = (
    "missing_upstream",
    "unexpected_upstream",
    "type_mismatch",
    "assertion_mismatch",
    "unsupported_sql",
    "cycle_detected",
    "table_redefined",
)


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _cycle_section(result: ScanResult) -> list[str]:
    report = result.cycle_report
    if report is None:
        return []
    body = ['<section id="cycles"><h2>Cycle detection</h2>']
    if not report.cycles:
        body.append('<p class="empty">No cyclic strongly connected components.</p></section>')
        return body
    unvisited = (
        f", {len(report.unvisited_nodes)} node(s) not visited" if report.unvisited_nodes else ""
    )
    body.append(
        '<p class="muted">Budget '
        f"{_e(report.budget)} edge examination(s), used {_e(report.edges_examined)}; "
        f"truncated: <strong>{'yes' if report.truncated else 'no'}</strong>{_e(unvisited)}</p>"
    )
    body.append(
        "<table><thead><tr><th>#</th><th>Type</th><th>Members</th>"
        "<th>Representative path(s)</th><th>Truncated</th></tr></thead><tbody>"
    )
    for index, cycle in enumerate(report.cycles, start=1):
        routes = "<br>".join(
            ("<code>" + _e(" -> ".join(path.nodes)) + "</code>")
            + (' <span class="muted">(partial)</span>' if path.partial else "")
            for path in cycle.paths
        )
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><code>{_e(cycle.cycle_type)}</code></td>"
            f"<td>{len(cycle.members)}</td>"
            f"<td>{routes}</td>"
            f"<td>{'yes' if cycle.truncated else 'no'}</td>"
            "</tr>"
        )
    body.append("</tbody></table></section>")
    return body


def render_html(result: ScanResult) -> str:
    counts = result.summary["counts"]
    body: list[str] = []
    body.append('<section id="overview"><h2>Overview</h2><div class="metrics">')
    metrics = (
        ("Tables", counts["tables"]),
        ("Nodes", counts["nodes"]),
        ("Edges", counts["edges"]),
        ("Findings", counts["findings"]),
        ("Unsupported", counts["unsupported"]),
        ("Scripts", counts["scripts"]),
        ("Statements", counts["statements"]),
    )
    for label, value in metrics:
        body.append(f'<div class="metric"><strong>{_e(value)}</strong><span>{_e(label)}</span></div>')
    body.append("</div>")
    severity_text = ", ".join(f"{key}={value}" for key, value in result.summary["by_severity"].items())
    body.append(f'<p class="muted">By severity: {_e(severity_text or "none")}</p>')
    body.append("</section>")

    body.append('<section id="severity"><h2>Severity policy</h2><table><thead><tr>')
    body.append("<th>Category</th><th>Severity</th><th>Source</th><th>Default</th></tr></thead><tbody>")
    for category in _CATEGORIES:
        overridden = category in result.severity_overrides
        source = "config override" if overridden else "built-in default"
        row_class = ' class="overridden"' if overridden else ""
        default = _default_severity(category)
        body.append(
            f"<tr{row_class}><td><code>{_e(category)}</code></td>"
            f"<td>{_severity_badge(result.severity[category])}</td>"
            f"<td>{_e(source)}</td><td>{_severity_badge(default)}</td></tr>"
        )
    body.append("</tbody></table></section>")

    body.append('<section id="findings"><h2>Findings</h2>')
    if not result.findings:
        body.append('<p class="empty">No findings.</p>')
    else:
        body.append(
            "<table><thead><tr><th>Severity</th><th>Category</th><th>Target</th>"
            "<th>Message</th><th>Location</th></tr></thead><tbody>"
        )
        for item in result.findings:
            location = f"{item.script}:{item.line}" if item.script else "contract/global"
            body.append(
                "<tr>"
                f"<td>{_severity_badge(item.severity)}</td>"
                f"<td><code>{_e(item.category)}</code>"
                f"{' <span class=muted>(override)</span>' if item.overridden else ''}</td>"
                f"<td><code>{_e(item.target)}</code></td>"
                f"<td>{_e(item.message)}<details><summary>Explanation</summary>"
                f"<div><code>{_e(item.finding_id)}</code><br>{_e(item.detail)}</div></details></td>"
                f"<td><code>{_e(location)}</code></td>"
                "</tr>"
            )
        body.append("</tbody></table>")
    body.append("</section>")

    body.extend(_cycle_section(result))

    body.append('<section id="files"><h2>By script</h2><table><thead><tr>')
    body.append(
        "<th>Script</th><th>Statements</th><th>Supported</th><th>Unsupported</th>"
        "<th>Edges</th><th>Findings</th></tr></thead><tbody>"
    )
    for item in result.summary["files"]:
        body.append(
            f"<tr><td><code>{_e(item['script'])}</code></td><td>{item['statements']}</td>"
            f"<td>{item['supported']}</td><td>{item['unsupported']}</td>"
            f"<td>{item['edges']}</td><td>{item['findings']}</td></tr>"
        )
    body.append("</tbody></table></section>")

    body.append('<section id="statements"><h2>Statement inventory</h2>')
    for script in sorted({item.script for item in result.statements}):
        body.append(f"<h3><code>{_e(script)}</code></h3><ul class=\"plain\">")
        for statement in [item for item in result.statements if item.script == script]:
            reason = f" — {_e(statement.reason)}" if statement.reason else ""
            body.append(
                f'<li>line {statement.line}, #{statement.index}, <code>{_e(statement.kind)}</code>: '
                f'<span class="status-{_e(statement.status)}">{_e(statement.status)}</span>{reason}</li>'
            )
        body.append("</ul>")
    body.append("</section>")

    body.append('<section id="unsupported"><h2>Unsupported statements</h2>')
    if not result.unsupported:
        body.append('<p class="empty">None.</p>')
    else:
        body.append("<table><thead><tr><th>Location</th><th>Construct</th><th>Reason</th></tr></thead><tbody>")
        for item in result.unsupported:
            body.append(
                f"<tr><td><code>{_e(item.script)}:{item.line}</code></td>"
                f"<td><code>{_e(item.construct)}</code></td><td>{_e(item.reason)}</td></tr>"
            )
        body.append("</tbody></table>")
    body.append("</section>")

    body.append('<section id="topology"><h2>Topology summary</h2><table><thead><tr>')
    body.append("<th>Column</th><th>Upstream edges</th><th>Downstream edges</th></tr></thead><tbody>")
    incoming = Counter(edge.target for edge in result.graph.edges)
    outgoing = Counter(edge.source for edge in result.graph.edges)
    names = sorted(set(incoming) | set(outgoing), key=lambda name: (-(incoming[name] + outgoing[name]), name))[:15]
    for name in names:
        body.append(
            f"<tr><td><code>{_e(name)}</code></td><td>{incoming[name]}</td><td>{outgoing[name]}</td></tr>"
        )
    body.append("</tbody></table></section>")

    template = _TEMPLATE.read_text(encoding="utf-8")
    meta = (
        f"Run {result.run_id} · Generated {result.generated_at} · "
        f"Input {result.input_digest[:16]} · Config {result.config_digest[:16]}"
    )
    return (
        template.replace("{{TITLE}}", _e(f"Lineage Inquest · {result.project}"))
        .replace("{{PROJECT}}", _e(result.project))
        .replace("{{META}}", _e(meta))
        .replace("{{BODY}}", "\n".join(body))
    )


def _default_severity(category: str) -> str:
    from ..config import DEFAULT_SEVERITY

    return DEFAULT_SEVERITY[category]


def _severity_badge(value: str) -> str:
    safe = value if value in {"error", "warn", "info"} else "info"
    return f'<span class="sev sev-{safe}">{_e(value)}</span>'
