"""SQLite schema and row/payload conversion helpers."""

from __future__ import annotations

import json
import sqlite3

from .models import Edge, EdgeLocation, Node

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  created_at TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  tables_count INTEGER NOT NULL,
  nodes_count INTEGER NOT NULL,
  edges_count INTEGER NOT NULL,
  findings_count INTEGER NOT NULL,
  unsupported_count INTEGER NOT NULL,
  statements_count INTEGER NOT NULL,
  scripts_count INTEGER NOT NULL,
  summary_json TEXT NOT NULL,
  severity_json TEXT NOT NULL,
  overrides_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node (
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  data_type TEXT NOT NULL,
  nullable INTEGER NOT NULL,
  primary_key INTEGER NOT NULL,
  origin TEXT NOT NULL,
  assertions_json TEXT NOT NULL,
  PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS edge (
  run_id TEXT NOT NULL,
  edge_id TEXT NOT NULL,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL,
  locations_json TEXT NOT NULL,
  PRIMARY KEY (run_id, edge_id)
);
CREATE TABLE IF NOT EXISTS finding (
  run_id TEXT NOT NULL,
  finding_id TEXT NOT NULL,
  category TEXT NOT NULL,
  severity TEXT NOT NULL,
  default_severity TEXT NOT NULL,
  overridden INTEGER NOT NULL,
  target TEXT NOT NULL,
  message TEXT NOT NULL,
  detail TEXT NOT NULL,
  script TEXT NOT NULL,
  line INTEGER NOT NULL,
  PRIMARY KEY (run_id, finding_id)
);
CREATE TABLE IF NOT EXISTS unsupported (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  script TEXT NOT NULL,
  line INTEGER NOT NULL,
  construct TEXT NOT NULL,
  reason TEXT NOT NULL,
  snippet TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_finding_severity ON finding(run_id, severity);
CREATE INDEX IF NOT EXISTS idx_edge_target ON edge(run_id, target);
"""


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def edge_from_row(row: sqlite3.Row) -> Edge:
    locations = tuple(
        EdgeLocation(item["script"], item["line"], item["statement"])
        for item in json.loads(row["locations_json"])
    )
    return Edge(row["source"], row["target"], row["kind"], locations)


def run_payload(row: sqlite3.Row) -> dict:
    return {
        "run_id": row["run_id"],
        "project": row["project"],
        "created_at": row["created_at"],
        "input_digest": row["input_digest"],
        "config_digest": row["config_digest"],
        "counts": {
            "tables": row["tables_count"],
            "nodes": row["nodes_count"],
            "edges": row["edges_count"],
            "findings": row["findings_count"],
            "unsupported": row["unsupported_count"],
            "statements": row["statements_count"],
            "scripts": row["scripts_count"],
        },
        "summary": json.loads(row["summary_json"]),
        "severity": json.loads(row["severity_json"]),
        "severity_overrides": json.loads(row["overrides_json"]),
    }


def finding_payload(row) -> dict:
    return {
        "id": row["finding_id"],
        "category": row["category"],
        "severity": row["severity"],
        "default_severity": row["default_severity"],
        "overridden": bool(row["overridden"]),
        "target": row["target"],
        "message": row["message"],
        "detail": row["detail"],
        "script": row["script"],
        "line": row["line"],
    }


def node_payload(node: Node) -> dict:
    from .graph.stable import node_id

    return {
        "id": node_id(node.name),
        "name": node.name,
        "table": node.table,
        "column": node.column,
        "data_type": node.data_type,
        "nullable": node.nullable,
        "primary_key": node.primary_key,
        "origin": node.origin,
        "assertions": list(node.assertions),
    }


def edge_payload(edge: Edge) -> dict:
    from .graph.stable import edge_id

    return {
        "id": edge_id(edge.source, edge.target, edge.kind),
        "source": edge.source,
        "target": edge.target,
        "kind": edge.kind,
        "scripts": sorted({loc.script for loc in edge.locations}),
        "locations": [
            {"script": loc.script, "line": loc.line, "statement": loc.statement}
            for loc in edge.locations
        ],
    }
