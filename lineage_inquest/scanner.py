"""Deterministic scan orchestration: scripts -> graph -> contracts -> findings."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlglot import exp

from .config import Config, parse_frozen_time
from .contract.classify import FindingDraft, classify
from .contract.compare import compare_contracts
from .contract.loader import load_contracts
from .dialect import statement_kind
from .errors import ConfigError
from .graph.build import build_graph
from .graph.query import detect_cycles
from .graph.stable import content_hash
from .models import (
    ColumnMapping,
    Finding,
    ScanResult,
    StatementInfo,
    TableDef,
    Unsupported,
)
from .parser import parse_unit
from .parser.ddl import DdlResult, handle_ddl
from .parser.guard import guard_ast
from .parser.produce import handle_produce
from .sqltext import split_sql


def _files(folder: Path, suffix: str) -> list[Path]:
    return sorted(
        [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == suffix],
        key=lambda path: path.relative_to(folder).as_posix(),
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").lstrip("﻿")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read UTF-8 SQL file {path}: {exc}") from exc


def _timestamp(config: Config, frozen_time: str | None) -> str:
    fixed = parse_frozen_time(frozen_time) or config.frozen_time
    if fixed:
        return fixed
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan(config: Config, frozen_time: str | None = None) -> ScanResult:
    contracts = load_contracts(config.contracts_dir, config.default_schema)
    sql_files = _files(config.sql_dir, ".sql")
    contract_files = [
        path for path in _files(config.contracts_dir, ".yaml") + _files(config.contracts_dir, ".yml")
    ]
    digest_parts = []
    for path in sql_files:
        digest_parts.append(
            {"kind": "sql", "path": path.relative_to(config.sql_dir).as_posix(), "text": _read(path)}
        )
    for path in sorted(contract_files, key=lambda p: p.as_posix()):
        digest_parts.append(
            {"kind": "contract", "path": path.relative_to(config.contracts_dir).as_posix(), "text": _read(path)}
        )
    input_digest = content_hash(digest_parts)
    generated_at = _timestamp(config, frozen_time)
    run_id = "run-" + content_hash(
        {"input": input_digest, "config": config.digest, "at": generated_at}
    )[:12]

    schema: dict[str, TableDef] = {}
    known: dict[str, TableDef] = {}
    mappings: list[ColumnMapping] = []
    unsupported: list[Unsupported] = []
    statements: list[StatementInfo] = []
    drafts: list[FindingDraft] = []

    for path in sql_files:
        relative = path.relative_to(config.sql_dir).as_posix()
        for unit in split_sql(_read(path), relative):
            ast = parse_unit(unit)
            kind = statement_kind(ast)
            issues = guard_ast(unit, ast)
            if issues:
                unsupported.extend(issues)
                statements.append(
                    StatementInfo(relative, unit.line, unit.index, kind, "unsupported", issues[0].construct)
                )
                continue
            if isinstance(ast, (exp.Create, exp.Drop, exp.Alter)):
                outcome = handle_ddl(unit, ast, schema, known, config.default_schema)
            elif isinstance(ast, (exp.Insert, exp.Merge)):
                outcome = handle_produce(unit, ast, schema, config.default_schema)
            elif isinstance(ast, exp.Select):
                outcome = None
            else:
                outcome = None
                unsupported.append(_unsupported(unit, "unsupported_statement", "statement type is not supported"))
            if outcome is not None:
                mappings.extend(outcome.mappings)
                unsupported.extend(outcome.issues)
                if isinstance(outcome, DdlResult):
                    for table, old_locations in outcome.redefined:
                        previous = ", ".join(f"{script}:{line}" for script, line in old_locations)
                        drafts.append(
                            FindingDraft(
                                "table_redefined",
                                table,
                                "table is defined more than once",
                                f"redefined at {unit.script}:{unit.line}; previous: {previous}",
                                unit.script,
                                unit.line,
                            )
                        )
            parser_issues = outcome.issues if outcome is not None else []
            reason = "unsupported construct" if parser_issues else ""
            statements.append(StatementInfo(relative, unit.line, unit.index, kind, "unsupported" if parser_issues else "supported", reason))

    graph = build_graph(known, mappings)
    for cycle in detect_cycles(graph):
        drafts.append(
            FindingDraft(
                "cycle_detected",
                cycle[0],
                "a cycle was detected in the column lineage graph",
                "cycle: " + ", ".join(cycle),
                "",
                0,
            )
        )
    drafts.extend(compare_contracts(graph, contracts))
    for item in unsupported:
        drafts.append(
            FindingDraft(
                "unsupported_sql",
                f"{item.script}:{item.line}:{item.construct}",
                f"unsupported SQL construct: {item.construct}",
                item.reason,
                item.script,
                item.line,
            )
        )

    findings = _finalize_findings(drafts, config)
    unsupported.sort(key=lambda item: (item.script, item.line, item.construct, item.reason))
    unsupported = _dedupe_unsupported(unsupported)
    statements.sort(key=lambda item: (item.script, item.line, item.index))
    summary = _summary(graph, statements, unsupported, findings)
    return ScanResult(
        project=config.project,
        run_id=run_id,
        generated_at=generated_at,
        input_digest=input_digest,
        config_digest=config.digest,
        graph=graph,
        findings=tuple(findings),
        unsupported=tuple(unsupported),
        statements=tuple(statements),
        severity=dict(config.severity),
        severity_overrides=dict(config.severity_overrides),
        summary=summary,
    )


def _unsupported(unit, construct: str, reason: str) -> Unsupported:
    return Unsupported(unit.script, unit.line, construct, reason, unit.text.strip()[:160])


def _finalize_findings(drafts: list[FindingDraft], config: Config) -> list[Finding]:
    unique: dict[str, Finding] = {}
    for draft in drafts:
        finding = classify(draft, config.severity, config.severity_overrides)
        unique.setdefault(finding.finding_id, finding)
    rank = {"error": 0, "warn": 1, "info": 2}
    return sorted(
        unique.values(),
        key=lambda item: (rank.get(item.severity, 9), item.category, item.target, item.detail),
    )


def _dedupe_unsupported(items: list[Unsupported]) -> list[Unsupported]:
    seen = set()
    result = []
    for item in items:
        key = (item.script, item.line, item.construct, item.reason)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _summary(graph, statements, unsupported, findings) -> dict:
    file_names = sorted({item.script for item in statements})
    files = []
    for script in file_names:
        own_statements = [item for item in statements if item.script == script]
        own_edges = {
            edge.source + "->" + edge.target + ":" + edge.kind + ":" + edge.branch
            for edge in graph.edges
            if any(location.script == script for location in edge.locations)
        }
        own_findings = [item for item in findings if item.script == script]
        files.append(
            {
                "script": script,
                "statements": len(own_statements),
                "supported": sum(item.status == "supported" for item in own_statements),
                "unsupported": sum(item.status == "unsupported" for item in own_statements),
                "edges": len(own_edges),
                "findings": len(own_findings),
            }
        )
    by_severity = Counter(item.severity for item in findings)
    by_category = Counter(item.category for item in findings)
    return {
        "counts": {
            "tables": graph.tables,
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "findings": len(findings),
            "unsupported": len(unsupported),
            "scripts": len(file_names),
            "statements": len(statements),
        },
        "by_severity": dict(sorted(by_severity.items())),
        "by_category": dict(sorted(by_category.items())),
        "files": files,
    }
