"""SQLite run ledger (local, read-only in the server)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import NotFoundError
from .graph.cycle_payload import cycle_payload
from .graph.stable import edge_id
from .models import Edge, Graph, Node, ScanResult
from .store_schema import (
    SCHEMA,
    apply_migrations,
    dumps,
    edge_from_row,
    edge_payload,
    finding_payload,
    node_payload,
    run_payload,
)


class LedgerStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _open(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as connection:
            connection.executescript(SCHEMA)
            apply_migrations(connection)
            connection.commit()

    def write_scan(self, result: ScanResult) -> None:
        self.initialize()
        counts = result.summary["counts"]
        with self._open() as connection:
            connection.execute("DELETE FROM node WHERE run_id = ?", (result.run_id,))
            connection.execute("DELETE FROM edge WHERE run_id = ?", (result.run_id,))
            connection.execute("DELETE FROM finding WHERE run_id = ?", (result.run_id,))
            connection.execute("DELETE FROM unsupported WHERE run_id = ?", (result.run_id,))
            connection.execute("DELETE FROM cycle WHERE run_id = ?", (result.run_id,))
            connection.execute(
                """
                INSERT OR REPLACE INTO run (
                  run_id, project, created_at, input_digest, config_digest,
                  tables_count, nodes_count, edges_count, findings_count,
                  unsupported_count, statements_count, scripts_count,
                  summary_json, severity_json, overrides_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.project,
                    result.generated_at,
                    result.input_digest,
                    result.config_digest,
                    counts["tables"],
                    counts["nodes"],
                    counts["edges"],
                    counts["findings"],
                    counts["unsupported"],
                    counts["statements"],
                    counts["scripts"],
                    dumps(result.summary),
                    dumps(result.severity),
                    dumps(result.severity_overrides),
                ),
            )
            connection.executemany(
                "INSERT INTO node VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        result.run_id,
                        node.name,
                        node.table,
                        node.column,
                        node.data_type,
                        int(node.nullable),
                        int(node.primary_key),
                        node.origin,
                        dumps(list(node.assertions)),
                    )
                    for node in result.graph.nodes
                ],
            )
            connection.executemany(
                "INSERT INTO edge VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        result.run_id,
                        _edge_key(edge),
                        edge.source,
                        edge.target,
                        edge.kind,
                        dumps(
                            [
                                {"script": loc.script, "line": loc.line, "statement": loc.statement}
                                for loc in edge.locations
                            ]
                        ),
                    )
                    for edge in result.graph.edges
                ],
            )
            connection.executemany(
                "INSERT INTO finding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        result.run_id,
                        item.finding_id,
                        item.category,
                        item.severity,
                        item.default_severity,
                        int(item.overridden),
                        item.target,
                        item.message,
                        item.detail,
                        item.script,
                        item.line,
                        dumps(item.context),
                    )
                    for item in result.findings
                ],
            )
            if result.cycle_report is not None:
                connection.executemany(
                    "INSERT INTO cycle VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            result.run_id,
                            seq,
                            dumps(list(cycle.members)),
                            cycle.cycle_type,
                            int(cycle.truncated),
                            dumps(cycle_payload(cycle)),
                        )
                        for seq, cycle in enumerate(result.cycle_report.cycles)
                    ],
                )
            connection.executemany(
                "INSERT INTO unsupported VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (result.run_id, index, item.script, item.line, item.construct, item.reason, item.snippet)
                    for index, item in enumerate(result.unsupported)
                ],
            )
            connection.commit()

    def run_exists(self, run_id: str) -> bool:
        if not self.path.exists():
            return False
        try:
            with self._open() as connection:
                row = connection.execute("SELECT 1 FROM run WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def latest_run_id(self) -> str | None:
        if not self.path.exists():
            return None
        with self._open() as connection:
            row = connection.execute(
                "SELECT run_id FROM run ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return row["run_id"] if row else None

    def list_runs(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM run ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [run_payload(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        if not self.path.exists():
            return None
        with self._open() as connection:
            row = connection.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
        return run_payload(row) if row else None

    def require_run(self, run_id: str) -> str:
        if not self.run_exists(run_id):
            raise NotFoundError(f"unknown run id: {run_id}")
        return run_id

    def list_findings(self, run_id: str, severity: str | None = None) -> list[dict]:
        self.require_run(run_id)
        query = "SELECT * FROM finding WHERE run_id = ?"
        params: list = [run_id]
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        query += " ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, category, target, finding_id"
        with self._open() as connection:
            rows = connection.execute(query, params).fetchall()
        return [finding_payload(row) for row in rows]

    def list_cycles(self, run_id: str) -> list[dict]:
        self.require_run(run_id)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM cycle WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_finding(self, run_id: str, finding_id: str) -> dict | None:
        self.require_run(run_id)
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM finding WHERE run_id = ? AND finding_id = ?",
                (run_id, finding_id),
            ).fetchone()
        return finding_payload(row) if row else None

    def load_graph(self, run_id: str) -> Graph:
        self.require_run(run_id)
        with self._open() as connection:
            node_rows = connection.execute(
                "SELECT * FROM node WHERE run_id = ? ORDER BY name", (run_id,)
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT * FROM edge WHERE run_id = ? ORDER BY target, source, kind", (run_id,)
            ).fetchall()
        nodes = tuple(
            Node(
                name=row["name"],
                table=row["table_name"],
                column=row["column_name"],
                data_type=row["data_type"],
                nullable=bool(row["nullable"]),
                primary_key=bool(row["primary_key"]),
                origin=row["origin"],
                assertions=tuple(json.loads(row["assertions_json"])),
            )
            for row in node_rows
        )
        edges = tuple(edge_from_row(row) for row in edge_rows)
        return Graph(nodes=nodes, edges=edges)

    def snapshot(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise NotFoundError(f"unknown run id: {run_id}")
        graph = self.load_graph(run_id)
        with self._open() as connection:
            findings = connection.execute(
                "SELECT * FROM finding WHERE run_id = ? ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, category, target, finding_id",
                (run_id,),
            ).fetchall()
            unsupported = connection.execute(
                "SELECT * FROM unsupported WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
            cycle_rows = connection.execute(
                "SELECT * FROM cycle WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return {
            "run": run,
            "nodes": [node_payload(node) for node in graph.nodes],
            "edges": [edge_payload(edge) for edge in graph.edges],
            "findings": [finding_payload(row) for row in findings],
            "cycles": [json.loads(row["payload_json"]) for row in cycle_rows],
            "unsupported": [
                {
                    "script": row["script"],
                    "line": row["line"],
                    "construct": row["construct"],
                    "reason": row["reason"],
                    "snippet": row["snippet"],
                }
                for row in unsupported
            ],
        }

    def diff(self, base_run: str, target_run: str) -> dict:
        base = self.snapshot(base_run)
        target = self.snapshot(target_run)
        base_findings = {item["id"]: item for item in base["findings"]}
        target_findings = {item["id"]: item for item in target["findings"]}
        base_edges = {item["id"]: item for item in base["edges"]}
        target_edges = {item["id"]: item for item in target["edges"]}
        added_findings = [target_findings[key] for key in sorted(set(target_findings) - set(base_findings))]
        removed_findings = [base_findings[key] for key in sorted(set(base_findings) - set(target_findings))]
        added_edges = [target_edges[key] for key in sorted(set(target_edges) - set(base_edges))]
        removed_edges = [base_edges[key] for key in sorted(set(base_edges) - set(target_edges))]
        return {
            "base": base_run,
            "target": target_run,
            "findings": {
                "added": added_findings,
                "removed": removed_findings,
                "added_count": len(added_findings),
                "removed_count": len(removed_findings),
            },
            "edges": {
                "added": added_edges,
                "removed": removed_edges,
                "added_count": len(added_edges),
                "removed_count": len(removed_edges),
            },
        }


def _edge_key(edge: Edge):
    return edge_id(edge.source, edge.target, edge.kind)
