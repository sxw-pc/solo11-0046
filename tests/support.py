"""Test fixture builder with no external dependencies."""

from __future__ import annotations

from pathlib import Path

import yaml

from lineage_inquest.config import load_config
from lineage_inquest.report.writer import write_artifacts
from lineage_inquest.scanner import scan
from lineage_inquest.store import LedgerStore


class Project:
    def __init__(self, root: Path, frozen: str = "2024-01-01T00:00:00Z", config: dict | None = None):
        self.root = root
        self.frozen = frozen
        self.sql_dir = root / "sql"
        self.contracts_dir = root / "contracts"
        self.out_dir = root / "out"
        self.sql_dir.mkdir(parents=True, exist_ok=True)
        self.contracts_dir.mkdir(parents=True, exist_ok=True)
        base = {
            "project": f"test-{root.name}",
            "sql_dir": "sql",
            "contracts_dir": "contracts",
            "out_dir": "out",
            "default_schema": "main",
            "frozen_time": frozen,
            "server": {"host": "127.0.0.1", "port": 5000},
            "max_query_depth": 8,
        }
        for key, value in (config or {}).items():
            if key == "frozen_time" and value is None:
                base.pop(key, None)
            else:
                base[key] = value
        self.config_path = root / "config.yaml"
        self._write(self.config_path, yaml.safe_dump(base, sort_keys=False))

    def _write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def sql(self, name: str, text: str) -> Path:
        return self._write(self.sql_dir / name, text)

    def contract(self, name: str, text: str) -> Path:
        return self._write(self.contracts_dir / name, text)

    def load_config(self):
        return load_config(self.config_path)

    def scan(self, frozen: str | None = None):
        return scan(self.load_config(), frozen)

    def scan_artifacts(self, out: Path | None = None):
        result = self.scan()
        out = out or self.out_dir
        artifacts = write_artifacts(result, out)
        LedgerStore(out / "ledger.sqlite").write_scan(result)
        return result, artifacts

    def node(self, result, name: str):
        for node in result.graph.nodes:
            if node.name == name:
                return node
        raise AssertionError(f"node not found: {name}")

    def edge(self, result, source: str, target: str, kind: str | None = None):
        for edge in result.graph.edges:
            if edge.source == source and edge.target == target and (kind is None or edge.kind == kind):
                return edge
        raise AssertionError(f"edge not found: {source} -> {target}")


def categories(result) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in result.findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
    return counts


def findings_for(result, category: str):
    return [finding for finding in result.findings if finding.category == category]
