"""Stable JSON artifacts (no timestamps)."""

from __future__ import annotations

from ..graph.build import edge_payload, node_payload
from ..graph.stable import stable_json
from ..models import ScanResult


def graph_payload(result: ScanResult) -> dict:
    return {
        "version": 1,
        "project": result.project,
        "input_digest": result.input_digest,
        "stats": {
            "tables": result.graph.tables,
            "nodes": len(result.graph.nodes),
            "edges": len(result.graph.edges),
        },
        "nodes": [node_payload(node) for node in result.graph.nodes],
        "edges": [edge_payload(edge) for edge in result.graph.edges],
    }


def findings_payload(result: ScanResult) -> dict:
    return {
        "version": 1,
        "project": result.project,
        "input_digest": result.input_digest,
        "summary": {
            "total": len(result.findings),
            "by_severity": result.summary.get("by_severity", {}),
            "by_category": result.summary.get("by_category", {}),
            "severity_overrides": dict(sorted(result.severity_overrides.items())),
        },
        "findings": [
            {
                "id": item.finding_id,
                "category": item.category,
                "severity": item.severity,
                "default_severity": item.default_severity,
                "overridden": item.overridden,
                "target": item.target,
                "message": item.message,
                "detail": item.detail,
                "script": item.script,
                "line": item.line,
            }
            for item in result.findings
        ],
    }


def graph_json(result: ScanResult) -> str:
    return stable_json(graph_payload(result))


def findings_json(result: ScanResult) -> str:
    return stable_json(findings_payload(result))
