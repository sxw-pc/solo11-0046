"""Deterministic Markdown report."""

from __future__ import annotations

from collections import Counter

from ..models import ScanResult

CATEGORIES = (
    "missing_upstream",
    "unexpected_upstream",
    "type_mismatch",
    "assertion_mismatch",
    "unsupported_sql",
    "cycle_detected",
    "table_redefined",
)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _severity_table(result: ScanResult) -> list[str]:
    lines = ["| Category | Severity | Source |", "|---|---|---|"]
    for category in CATEGORIES:
        source = "override" if category in result.severity_overrides else "default"
        lines.append(f"| `{category}` | {result.severity[category]} | {source} |")
    return lines


def render_markdown(result: ScanResult) -> str:
    counts = result.summary["counts"]
    lines = [
        "# Lineage Inquest report",
        "",
        f"- Project: `{result.project}`",
        f"- Run: `{result.run_id}`",
        f"- Generated (UTC): `{result.generated_at}`",
        f"- Input digest: `{result.input_digest}`",
        f"- Config digest: `{result.config_digest}`",
        "",
        "## Overview",
        "",
        "| Tables | Nodes | Edges | Findings | Unsupported | Scripts | Statements |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {counts['tables']} | {counts['nodes']} | {counts['edges']} | "
            f"{counts['findings']} | {counts['unsupported']} | {counts['scripts']} | {counts['statements']} |"
        ),
        "",
        "By severity: "
        + (", ".join(f"{key}={value}" for key, value in result.summary["by_severity"].items()) or "none"),
        "",
        "## Severity policy",
        "",
        *_severity_table(result),
        "",
        "## Findings",
        "",
    ]
    if not result.findings:
        lines.extend(["No findings.", ""])
    else:
        lines.extend(
            [
                "| Severity | Category | Target | Message |",
                "|---|---|---|---|",
            ]
        )
        for finding in result.findings:
            lines.append(
                f"| {finding.severity} | `{finding.category}` | `{_cell(finding.target)}` | {_cell(finding.message)} |"
            )
        lines.append("")
        lines.append("### Finding details")
        lines.append("")
        for finding in result.findings:
            location = f"{finding.script}:{finding.line}" if finding.script else "contract/global"
            lines.extend(
                [
                    f"<details><summary><code>{finding.finding_id}</code> {_cell(finding.category)}</summary>",
                    "",
                    f"- Target: `{finding.target}`",
                    f"- Location: `{location}`",
                    f"- Detail: {_cell(finding.detail)}",
                    "",
                    "</details>",
                    "",
                ]
            )

    lines.extend(
        [
            "## Files",
            "",
            "| Script | Statements | Supported | Unsupported | Edges | Findings |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for item in result.summary["files"]:
        lines.append(
            f"| `{item['script']}` | {item['statements']} | {item['supported']} | "
            f"{item['unsupported']} | {item['edges']} | {item['findings']} |"
        )
    lines.extend(["", "## Statement inventory", ""])
    for script in sorted({item.script for item in result.statements}):
        lines.append(f"### `{script}`")
        lines.append("")
        for statement in [item for item in result.statements if item.script == script]:
            reason = f" ({statement.reason})" if statement.reason else ""
            lines.append(
                f"- line {statement.line}, #{statement.index}, `{statement.kind}`: "
                f"**{statement.status}**{reason}"
            )
        lines.append("")

    lines.extend(["## Unsupported statements", ""])
    if not result.unsupported:
        lines.extend(["None.", ""])
    else:
        for item in result.unsupported:
            lines.append(
                f"- `{item.script}:{item.line}` `{item.construct}` — {_cell(item.reason)}"
            )
        lines.append("")

    out_counts = Counter(edge.source for edge in result.graph.edges)
    in_counts = Counter(edge.target for edge in result.graph.edges)
    busy = sorted(
        set(out_counts) | set(in_counts),
        key=lambda name: (-(out_counts[name] + in_counts[name]), name),
    )[:10]
    lines.extend(["## Topology summary", "", "| Column | Upstream edges | Downstream edges |", "|---|---:|---:|"])
    for name in busy:
        lines.append(f"| `{name}` | {in_counts[name]} | {out_counts[name]} |")
    lines.append("")
    return "\n".join(lines)
