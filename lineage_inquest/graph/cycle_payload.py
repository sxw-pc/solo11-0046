"""Deterministic JSON payloads for explainable cycle structures."""

from __future__ import annotations

from ..models import CycleInfo, CycleReport


def cycle_hop_payload(hop) -> dict:
    return {"source": hop.source, "target": hop.target, "kind": hop.kind}


def cycle_path_payload(path) -> dict:
    return {
        "nodes": list(path.nodes),
        "hops": [cycle_hop_payload(hop) for hop in path.hops],
        "partial": path.partial,
    }


def cycle_payload(cycle: CycleInfo) -> dict:
    return {
        "members": list(cycle.members),
        "cycle_type": cycle.cycle_type,
        "truncated": cycle.truncated,
        "paths": [cycle_path_payload(path) for path in cycle.paths],
    }


def cycle_report_payload(report: CycleReport) -> dict:
    return {
        "truncated": report.truncated,
        "budget": report.budget,
        "edges_examined": report.edges_examined,
        "unvisited_nodes": list(report.unvisited_nodes),
        "cycles": [cycle_payload(cycle) for cycle in report.cycles],
    }
