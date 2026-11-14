"""Deterministic scan orchestration: scripts -> graph -> contracts -> findings.

The scan is structured as an ordered chain of per-script slices. Each slice
consumes only its own SQL text snapshot (see :class:`~lineage_inquest.models.
ScriptSlice`) and the :class:`~lineage_inquest.models.SliceState` handed in by
the previous slice, which makes any individual script independently
replayable via :func:`replay_slices` / :func:`replay_scan` without rerunning
the whole scan. The regular :func:`scan` entry point is itself a single replay
of the full chain, so both paths share exactly one implementation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
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
    Finding,
    Replay,
    ScanResult,
    ScriptSlice,
    SliceSnapshot,
    SliceState,
    StatementInfo,
    Unsupported,
    _snapshot_payload,
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


def scan_timestamp(config: Config, frozen_time: str | None) -> str:
    fixed = parse_frozen_time(frozen_time) or config.frozen_time
    if fixed:
        return fixed
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ScanInputs:
    """Filesystem snapshot and content identity for one scan configuration."""

    sql_files: tuple[Path, ...]
    contract_files: tuple[Path, ...]
    digest_parts: tuple[dict[str, str], ...]
    input_digest: str


def load_scan_inputs(config: Config) -> ScanInputs:
    """Read every SQL/contract text once and derive the content digest.

    The digest covers the full ordered set of SQL and contract texts, so it is
    independent of how the scan is later sliced or replayed.
    """
    sql_files = tuple(_files(config.sql_dir, ".sql"))
    contract_files = tuple(
        sorted(
            _files(config.contracts_dir, ".yaml") + _files(config.contracts_dir, ".yml"),
            key=lambda path: path.as_posix(),
        )
    )
    digest_parts: list[dict[str, str]] = []
    for path in sql_files:
        digest_parts.append(
            {"kind": "sql", "path": path.relative_to(config.sql_dir).as_posix(), "text": _read(path)}
        )
    for path in contract_files:
        digest_parts.append(
            {
                "kind": "contract",
                "path": path.relative_to(config.contracts_dir).as_posix(),
                "text": _read(path),
            }
        )
    return ScanInputs(
        sql_files=sql_files,
        contract_files=contract_files,
        digest_parts=tuple(digest_parts),
        input_digest=content_hash(digest_parts),
    )


def build_slices(config: Config, sql_files: tuple[Path, ...] | list[Path]) -> tuple[ScriptSlice, ...]:
    """Turn the ordered SQL files into self-contained replayable slices."""
    slices: list[ScriptSlice] = []
    for index, path in enumerate(sql_files):
        relative = path.relative_to(config.sql_dir).as_posix()
        slices.append(ScriptSlice(index=index, script=relative, path=str(path), text=_read(path)))
    return tuple(slices)


def make_run_id(input_digest: str, config: Config, generated_at: str) -> str:
    """run_id is derived solely from input contents, config, and the timestamp."""
    return "run-" + content_hash(
        {"input": input_digest, "config": config.digest, "at": generated_at}
    )[:12]


def scan(config: Config, frozen_time: str | None = None) -> ScanResult:
    inputs = load_scan_inputs(config)
    generated_at = scan_timestamp(config, frozen_time)
    run_id = make_run_id(inputs.input_digest, config, generated_at)
    contracts = load_contracts(config.contracts_dir, config.default_schema)
    replay = replay_slices(config, build_slices(config, inputs.sql_files), SliceState())
    return assemble_scan_result(
        config, replay, contracts, inputs.input_digest, run_id, generated_at
    )


def replay_scan(config: Config, frozen_time: str | None = None) -> Replay:
    """Replay the whole slice chain from a pristine state.

    Independent calls always return equal per-slice snapshots and the same
    ``input_digest`` / ``run_id`` as a regular :func:`scan` of the same
    configuration.
    """
    inputs = load_scan_inputs(config)
    generated_at = scan_timestamp(config, frozen_time)
    run_id = make_run_id(inputs.input_digest, config, generated_at)
    replay = replay_slices(config, build_slices(config, inputs.sql_files), SliceState())
    return Replay(
        input_digest=inputs.input_digest,
        run_id=run_id,
        slices=replay.slices,
        snapshots=replay.snapshots,
        state=replay.state,
    )


def replay_slices(
    config: Config,
    slices: tuple[ScriptSlice, ...] | list[ScriptSlice],
    state: SliceState,
) -> Replay:
    """Replay an ordered chain of slices, threading state between them.

    Each slice only reads its embedded SQL text snapshot and the incoming
    ``state``; the filesystem is not touched. Snapshots are captured after
    every slice. Replaying the same slices against an equal incoming state is
    deterministic.
    """
    snapshots: list[SliceSnapshot] = []
    ordered = tuple(slices)
    for slice_item in ordered:
        _replay_slice(config, slice_item, state)
        # Capture a detached copy: snapshots are immutable checkpoints and
        # must keep observing later slices from mutating them.
        snapshots.append(
            SliceSnapshot.capture(slice_item.index, slice_item.script, state.clone())
        )
    return Replay(
        input_digest="",
        run_id="",
        slices=ordered,
        snapshots=tuple(snapshots),
        state=state,
    )


def _replay_slice(config: Config, slice_item: ScriptSlice, state: SliceState) -> None:
    for unit in split_sql(slice_item.text, slice_item.script):
        _replay_unit(config, unit, state)


def replay_one_slice(
    config: Config,
    slice_item: ScriptSlice,
    state: SliceState,
) -> SliceSnapshot:
    """Replay a single script slice against an incoming state.

    This is the smallest independently callable replay unit: pass the
    snapshot state produced by the upstream slice (or a fresh
    :class:`SliceState` for the first script) and get back the immutable
    checkpoint after this script. The incoming state is cloned first, so the
    caller's copy is never mutated. Repeating the call with equal inputs
    always yields an equal snapshot and state digest.
    """
    working = state.clone()
    _replay_slice(config, slice_item, working)
    return SliceSnapshot.capture(slice_item.index, slice_item.script, working)


def _replay_unit(config: Config, unit, state: SliceState) -> None:
    ast = parse_unit(unit)
    kind = statement_kind(ast)
    issues = guard_ast(unit, ast)
    if issues:
        state.unsupported.extend(issues)
        state.statements.append(
            StatementInfo(unit.script, unit.line, unit.index, kind, "unsupported", issues[0].construct)
        )
        return
    if isinstance(ast, (exp.Create, exp.Drop, exp.Alter)):
        outcome = handle_ddl(unit, ast, state.schema, state.known, config.default_schema)
    elif isinstance(ast, (exp.Insert, exp.Merge)):
        outcome = handle_produce(unit, ast, state.schema, config.default_schema)
    elif isinstance(ast, exp.Select):
        outcome = None
    else:
        outcome = None
        state.unsupported.append(
            _unsupported(unit, "unsupported_statement", "statement type is not supported")
        )
    if outcome is not None:
        state.mappings.extend(outcome.mappings)
        state.unsupported.extend(outcome.issues)
        if isinstance(outcome, DdlResult):
            for table, old_locations in outcome.redefined:
                previous = ", ".join(f"{script}:{line}" for script, line in old_locations)
                state.drafts.append(
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
    state.statements.append(
        StatementInfo(
            unit.script,
            unit.line,
            unit.index,
            kind,
            "unsupported" if parser_issues else "supported",
            reason,
        )
    )


def assemble_scan_result(
    config: Config,
    replay: Replay,
    contracts: list,
    input_digest: str,
    run_id: str,
    generated_at: str,
) -> ScanResult:
    """Graph build, contract comparison and finding finalization.

    Runs once, after the full slice chain. unsupported items and statements
    are therefore sorted/deduplicated globally -- never per slice.
    """
    state = replay.state
    drafts: list[FindingDraft] = list(state.drafts)
    graph = build_graph(state.known, state.mappings)
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
    for item in state.unsupported:
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
    unsupported = _dedupe_unsupported(
        sorted(state.unsupported, key=lambda item: (item.script, item.line, item.construct, item.reason))
    )
    statements = sorted(state.statements, key=lambda item: (item.script, item.line, item.index))
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


def replay_to_scan_result(
    config: Config,
    replay: Replay,
    frozen_time: str | None = None,
) -> ScanResult:
    """Finalize a replay (e.g. a partial one) into the standard ScanResult."""
    if not replay.input_digest:
        raise ValueError("replay must carry the scan-level input_digest before finalization")
    contracts = load_contracts(config.contracts_dir, config.default_schema)
    generated_at = scan_timestamp(config, frozen_time)
    run_id = replay.run_id or make_run_id(replay.input_digest, config, generated_at)
    return assemble_scan_result(
        config, replay, contracts, replay.input_digest, run_id, generated_at
    )


def snapshot_payload(snapshot: SliceSnapshot) -> dict:
    """Stable JSON representation of an intermediate state snapshot."""
    payload = _snapshot_payload(snapshot.index, snapshot.script, snapshot.state)
    payload["state_digest"] = snapshot.state_digest
    return payload


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
            edge.source + "->" + edge.target + ":" + edge.kind
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
