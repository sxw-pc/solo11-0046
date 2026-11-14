"""Command-line entry points for Lineage Inquest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, load_config, parse_frozen_time, validate_host
from .errors import ConfigError, NotFoundError
from .graph.query import downstream, upstream
from .graph.stable import stable_json
from .models import Replay, SliceState
from .report.json_out import findings_json, findings_payload, graph_json
from .report.writer import write_artifacts, write_text
from .scanner import (
    build_slices,
    load_scan_inputs,
    make_run_id,
    replay_one_slice,
    replay_slices,
    replay_to_scan_result,
    scan,
    scan_timestamp,
    snapshot_payload,
)
from .seed import seed
from .store import LedgerStore

EXIT_OK = 0
EXIT_VERIFY = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lineage-inquest", description="Offline static SQL lineage and contract inquest")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="write the synthetic demo fixtures")
    seed_parser.add_argument("--out", default=".work/demo", help="destination directory (default: .work/demo)")

    scan_parser = sub.add_parser("scan", help="scan SQL, build the graph, verify contracts, write reports")
    _config_arg(scan_parser)
    scan_parser.add_argument("--out", help="override output directory")
    _frozen_arg(scan_parser)

    replay_parser = sub.add_parser(
        "replay",
        help="replay the scan one SQL script slice at a time with state snapshots",
    )
    _config_arg(replay_parser)
    replay_parser.add_argument("--out", help="override output directory")
    _frozen_arg(replay_parser)
    replay_parser.add_argument(
        "--slices",
        type=int,
        nargs="*",
        help="only replay these zero-based slice indexes (default: the whole chain)",
    )

    verify_parser = sub.add_parser("verify", help="scan and compare findings with a snapshot")
    _config_arg(verify_parser)
    verify_parser.add_argument("--out", help="override output directory")
    verify_parser.add_argument("--expect", help="expected findings.json path")
    _frozen_arg(verify_parser)

    serve_parser = sub.add_parser("serve", help="start the read-only local viewer")
    _config_arg(serve_parser)
    serve_parser.add_argument("--out", help="override ledger/output directory")
    serve_parser.add_argument("--host", help="must be 127.0.0.1")
    serve_parser.add_argument("--port", type=int, help="port (default from config)")

    export_parser = sub.add_parser("export", help="export a stored run snapshot")
    _config_arg(export_parser)
    export_parser.add_argument("--run-id", required=True)
    export_parser.add_argument("--out", help="override output directory")
    export_parser.add_argument("--output", help="explicit JSON output path")

    query_parser = sub.add_parser("query", help="query a stored graph without rescanning")
    _config_arg(query_parser)
    query_parser.add_argument("--node", required=True)
    query_parser.add_argument("--direction", choices=("upstream", "downstream"), default="upstream")
    query_parser.add_argument("--depth", type=int, default=3)
    query_parser.add_argument("--run-id")
    query_parser.add_argument("--out", help="override output directory")
    query_parser.add_argument("--output", help="optional JSON output path")

    diff_parser = sub.add_parser("diff", help="show finding and edge changes between two runs")
    _config_arg(diff_parser)
    diff_parser.add_argument("--base", required=True)
    diff_parser.add_argument("--target", required=True)
    diff_parser.add_argument("--out", help="override output directory")
    diff_parser.add_argument("--output", help="optional JSON output path")
    return parser


def _config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="path to config.yaml")


def _frozen_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--frozen-time", help="ISO-8601 UTC timestamp used in report/ledger headers")


def _out(args, config: Config) -> Path:
    return Path(args.out).expanduser().resolve() if args.out else config.out_dir


def _print_scan(result, artifacts, ledger: Path) -> None:
    counts = result.summary["counts"]
    print(
        f"scan {result.run_id}: tables={counts['tables']} nodes={counts['nodes']} "
        f"edges={counts['edges']} findings={counts['findings']} unsupported={counts['unsupported']}"
    )
    for label in ("graph", "findings", "markdown", "html"):
        print(f"{label}: {artifacts[label]}")
    print(f"ledger: {ledger}")


def _run_scan(args, config: Config):
    frozen = parse_frozen_time(args.frozen_time) if args.frozen_time else None
    result = scan(config, frozen)
    out = _out(args, config)
    artifacts = write_artifacts(result, out)
    ledger = out / "ledger.sqlite"
    LedgerStore(ledger).write_scan(result)
    return result, artifacts, ledger


def _do_replay(args, config: Config) -> int:
    frozen = parse_frozen_time(args.frozen_time) if args.frozen_time else None
    inputs = load_scan_inputs(config)
    all_slices = build_slices(config, inputs.sql_files)
    if args.slices:
        indexes = set(args.slices)
        unknown = sorted(indexes - set(range(len(all_slices))))
        if unknown:
            raise ConfigError(
                f"unknown slice index(es): {', '.join(str(i) for i in unknown)}; "
                f"this config has {len(all_slices)} slice(s)"
            )
        slices = tuple(all_slices[index] for index in sorted(indexes))
        partial = indexes != set(range(len(all_slices)))
    else:
        slices = all_slices
        partial = False
    generated_at = scan_timestamp(config, frozen)
    run_id = make_run_id(inputs.input_digest, config, generated_at)

    # First pass: replay the ordered chain, threading state across slices.
    first = replay_slices(config, slices, SliceState())
    # Second pass: replay each slice independently from a fresh copy of the
    # state its predecessor produced. Identical per-slice state_digest values
    # prove every script boundary is individually reproducible.
    second_snapshots = []
    incoming = SliceState()
    for slice_item in slices:
        snapshot = replay_one_slice(config, slice_item, incoming)
        second_snapshots.append(snapshot)
        incoming = snapshot.state
    first_digests = [snap.state_digest for snap in first.snapshots]
    second_digests = [snap.state_digest for snap in second_snapshots]
    if first_digests != second_digests:
        raise ConfigError("replay is not deterministic: repeated slice states differ")

    out = _out(args, config) / "replay"
    write_text(
        out / "snapshots.json",
        stable_json(
            {
                "version": 1,
                "project": config.project,
                "input_digest": inputs.input_digest,
                "run_id": run_id,
                "generated_at": generated_at,
                "slices": [
                    {"index": item.index, "script": item.script, "path": item.path}
                    for item in first.slices
                ],
                "snapshots": [snapshot_payload(snap) for snap in first.snapshots],
            }
        ),
    )
    print(f"replay {run_id}: {len(first.slices)} slice(s), each replayed twice with identical state")
    for snap in first.snapshots:
        print(f"  slice {snap.index}: {snap.script} state={snap.state_digest[:12]}")
    if not partial:
        full_replay = Replay(
            input_digest=inputs.input_digest,
            run_id=run_id,
            slices=first.slices,
            snapshots=first.snapshots,
            state=first.state,
        )
        result = replay_to_scan_result(config, full_replay, frozen)
        write_text(out / "graph.json", graph_json(result))
        write_text(out / "findings.json", findings_json(result))
        print(f"replay artifacts: {out}")
    else:
        print(f"partial replay artifacts (snapshots only): {out}")
    return EXIT_OK


def _do_verify(args, config: Config) -> int:
    result, artifacts, ledger = _run_scan(args, config)
    expected_path = (
        Path(args.expect).expanduser().resolve()
        if args.expect
        else config.expect_findings or (config.source_path.parent / "expected" / "findings.json")
    )
    if not expected_path.is_file():
        raise ConfigError(f"expected findings snapshot not found: {expected_path}")
    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read expected findings {expected_path}: {exc}") from exc
    actual = findings_payload(result)
    if expected != actual:
        expected_categories = (expected.get("summary", {}).get("by_category", {}) if isinstance(expected, dict) else {})
        actual_categories = actual["summary"]["by_category"]
        print(f"verify FAILED for {result.run_id}", file=sys.stderr)
        print(f"expected categories: {expected_categories}", file=sys.stderr)
        print(f"actual categories:   {actual_categories}", file=sys.stderr)
        print(f"expected snapshot: {expected_path}", file=sys.stderr)
        return EXIT_VERIFY
    print(f"verify OK: {len(result.findings)} finding(s) match {expected_path}")
    _print_scan(result, artifacts, ledger)
    return EXIT_OK


def _load_store_and_run(args, config: Config):
    out = _out(args, config)
    store = LedgerStore(out / "ledger.sqlite")
    run_id = getattr(args, "run_id", None) or store.latest_run_id()
    if run_id is None:
        raise NotFoundError("no runs are available in the ledger")
    store.require_run(run_id)
    return store, run_id, out


def _do_export(args, config: Config) -> int:
    store, run_id, out = _load_store_and_run(args, config)
    snapshot = store.snapshot(run_id)
    target = (
        Path(args.output).expanduser().resolve()
        if args.output
        else out / "exports" / f"{run_id}.json"
    )
    write_text(target, stable_json(snapshot))
    print(f"exported {run_id} to {target}")
    return EXIT_OK


def _do_query(args, config: Config) -> int:
    if not 1 <= args.depth <= 50:
        raise ConfigError("--depth must be between 1 and 50")
    store, run_id, _ = _load_store_and_run(args, config)
    graph = store.load_graph(run_id)
    result = upstream(graph, args.node, args.depth) if args.direction == "upstream" else downstream(graph, args.node, args.depth)
    payload = {"run_id": run_id, **result}
    if args.output:
        target = Path(args.output).expanduser().resolve()
        write_text(target, stable_json(payload))
        print(f"query written to {target}")
    else:
        sys.stdout.write(stable_json(payload))
    return EXIT_OK


def _do_diff(args, config: Config) -> int:
    store = LedgerStore(_out(args, config) / "ledger.sqlite")
    store.require_run(args.base)
    store.require_run(args.target)
    payload = store.diff(args.base, args.target)
    if args.output:
        target = Path(args.output).expanduser().resolve()
        write_text(target, stable_json(payload))
        print(f"diff written to {target}")
    else:
        sys.stdout.write(stable_json(payload))
    return EXIT_OK


def _do_serve(args, config: Config) -> int:
    host = args.host or config.server_host
    validate_host(host)
    port = args.port if args.port is not None else config.server_port
    if not 1 <= port <= 65535:
        raise ConfigError("--port must be between 1 and 65535")
    from .server import create_app

    app = create_app(config, _out(args, config))
    print(f"serving read-only on http://{host}:{port} (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "seed":
            written = seed(Path(args.out))
            print(f"seeded {len(written)} files into {Path(args.out).resolve()}")
            return EXIT_OK
        config = load_config(args.config)
        if args.command == "scan":
            result, artifacts, ledger = _run_scan(args, config)
            _print_scan(result, artifacts, ledger)
            return EXIT_OK
        if args.command == "replay":
            return _do_replay(args, config)
        if args.command == "verify":
            return _do_verify(args, config)
        if args.command == "serve":
            return _do_serve(args, config)
        if args.command == "export":
            return _do_export(args, config)
        if args.command == "query":
            return _do_query(args, config)
        if args.command == "diff":
            return _do_diff(args, config)
        raise ConfigError(f"unknown command: {args.command}")
    except NotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except (ConfigError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
