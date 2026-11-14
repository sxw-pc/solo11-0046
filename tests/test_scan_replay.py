"""Regression tests for per-script slice replay.

The scanner is cut into ordered slices, one per SQL script. These tests pin
down the contracts of that decomposition:

* replaying the same slice twice yields the same intermediate state snapshot;
* a chain of slices reproduces the regular scan exactly (same run_id,
  input_digest, graph, findings, summary and byte-identical artifacts);
* input_digest / run_id are decided by the full text contents, never by slice
  order or replay count;
* unsupported items and statements keep encounter order inside slices and are
  only sorted/deduplicated globally after the whole chain;
* the ``replay`` CLI entry point exposes the same guarantees.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from lineage_inquest.cli import main
from lineage_inquest.config import load_config
from lineage_inquest.models import SliceState
from lineage_inquest.report.json_out import findings_payload, graph_payload
from lineage_inquest.report.writer import write_artifacts
from lineage_inquest.scanner import (
    assemble_scan_result,
    build_slices,
    load_scan_inputs,
    make_run_id,
    replay_one_slice,
    replay_scan,
    replay_slices,
    replay_to_scan_result,
    scan,
    scan_timestamp,
    snapshot_payload,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _two_script_project(project_factory):
    p = project_factory("replay")
    p.sql(
        "01_schema.sql",
        """
        CREATE TABLE raw.orders (id INT PRIMARY KEY, amount DECIMAL(10,2) NOT NULL);
        CREATE TABLE mart.daily (day DATE NOT NULL, total DECIMAL(12,2) NOT NULL);
        """,
    )
    p.sql(
        "02_build.sql",
        """
        INSERT INTO mart.daily (day, total)
        SELECT CAST('2024-01-01' AS DATE), SUM(amount) FROM raw.orders;
        SELECT ROW_NUMBER() OVER (ORDER BY id) FROM raw.orders;
        """,
    )
    p.contract(
        "daily.yaml",
        """
        version: 1
        contracts:
          - target: mart.daily.total
            required_upstream: [raw.orders.amount]
            expected_type: numeric
        """,
    )
    return p


def test_slices_match_ordered_sql_files_and_carry_text_snapshot(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)

    assert [item.script for item in slices] == ["01_schema.sql", "02_build.sql"]
    assert [item.index for item in slices] == [0, 1]
    # The slice is self-contained: its text equals the file content stripped
    # of a BOM, so replay never has to reopen the file.
    for item in slices:
        assert item.text == Path(item.path).read_text(encoding="utf-8").lstrip("﻿")
        assert item.text  # non-empty snapshot


def test_repeated_replay_of_each_slice_gives_identical_snapshots(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)

    first = replay_slices(config, slices, SliceState())
    second = replay_slices(config, slices, SliceState())

    assert len(first.snapshots) == len(slices)
    assert [snap.script for snap in first.snapshots] == [s.script for s in slices]
    assert [snap.state_digest for snap in first.snapshots] == [
        snap.state_digest for snap in second.snapshots
    ]
    # And the accumulated states themselves compare equal field by field.
    for left, right in zip(first.snapshots, second.snapshots):
        assert left.state == right.state


def test_snapshots_advance_and_reflect_only_upstream_slices(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)

    # Replay just the first slice: the second script's INSERT/window statement
    # must not be visible yet.
    first_only = replay_slices(config, slices[:1], SliceState())
    snapshot = first_only.snapshots[0]
    assert snapshot.script == "01_schema.sql"
    assert set(snapshot.state.schema) == {"raw.orders", "mart.daily"}
    assert set(snapshot.state.known) == {"raw.orders", "mart.daily"}
    assert snapshot.state.mappings == []
    assert snapshot.state.unsupported == []
    assert [item.script for item in snapshot.state.statements] == ["01_schema.sql"] * 2

    # Continue the chain from that state: the second slice adds mappings and
    # one unsupported construct on top of the inherited state.
    continued = replay_slices(config, slices[1:], snapshot.state.clone())
    final = continued.snapshots[-1]
    assert final.state.schema == snapshot.state.schema
    # Only mart.daily.total carries source mappings; day is a CAST literal.
    assert len(final.state.mappings) == 1
    assert final.state.mappings[0].target == "mart.daily.total"
    assert [item.construct for item in final.state.unsupported] == ["window_function"]
    assert {item.script for item in final.state.statements} == {
        "01_schema.sql",
        "02_build.sql",
    }


def test_slice_state_is_not_mutated_by_a_second_replay(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)

    first = replay_slices(config, slices, SliceState())
    digest_before = [snap.state_digest for snap in first.snapshots]
    # Re-run every slice again; the earlier snapshots stay unchanged.
    replay_slices(config, slices, SliceState())
    assert [snap.state_digest for snap in first.snapshots] == digest_before


def test_full_slice_chain_reproduces_scan_result_identically(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    scanned = scan(config)

    replay = replay_scan(config)
    assert replay.input_digest == scanned.input_digest
    assert replay.run_id == scanned.run_id
    assert len(replay.snapshots) == 2

    finalized = replay_to_scan_result(config, replay)
    assert finalized == scanned


def test_replay_assembly_keeps_global_unsupported_and_statement_ordering(project_factory):
    p = project_factory("ordering")
    # Two scripts each contribute unsupported constructs. The two statements
    # in the first file sit on the same line, producing an identical
    # (script, line, construct, reason) key that global dedup must collapse.
    p.sql(
        "01_a.sql",
        "SELECT ROW_NUMBER() OVER () FROM raw.t; SELECT ROW_NUMBER() OVER () FROM raw.t;\n",
    )
    p.sql("02_b.sql", "WITH RECURSIVE q(n) AS (SELECT 1) SELECT n FROM q;")
    config = p.load_config()
    scanned = scan(config)

    replay = replay_scan(config)
    finalized = replay_to_scan_result(config, replay)
    # Global sort/dedupe happens once, after the whole chain, so the finalized
    # result matches scan() exactly.
    assert finalized.unsupported == scanned.unsupported
    assert finalized.statements == scanned.statements

    # But inside the slice state nothing has been deduped early: the first
    # slice keeps both identical window_function entries in encounter order.
    first_state = replay.snapshots[0].state
    assert [item.construct for item in first_state.unsupported] == [
        "window_function",
        "window_function",
    ]
    assert [item.script for item in first_state.statements] == ["01_a.sql", "01_a.sql"]
    # The finalized view collapses the duplicate key once, globally.
    assert len(finalized.unsupported) == 2
    assert [item.script for item in finalized.unsupported] == ["01_a.sql", "02_b.sql"]
    # Statements are sorted by (script, line, index) but never deduped.
    assert [item.script for item in finalized.statements] == [
        "01_a.sql",
        "01_a.sql",
        "02_b.sql",
    ]
    # And the recursive CTE only appears once the second slice has run.
    second_state = replay.snapshots[1].state
    assert [item.construct for item in second_state.unsupported] == [
        "window_function",
        "window_function",
        "recursive_cte",
    ]


def test_input_digest_and_run_id_ignore_replay_count_and_slice_order(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)
    generated_at = scan_timestamp(config, None)
    run_id = make_run_id(inputs.input_digest, config, generated_at)

    once = replay_slices(config, slices, SliceState())
    again = replay_slices(config, slices, SliceState())
    # Replay count changes nothing.
    assert once.snapshots[-1].state_digest == again.snapshots[-1].state_digest

    # Reversing slice order cannot change the content-derived identity values.
    reversed_chain = replay_slices(config, tuple(reversed(slices)), SliceState())
    from lineage_inquest.contract.loader import load_contracts

    reversed_result = assemble_scan_result(
        config,
        reversed_chain,
        load_contracts(config.contracts_dir, config.default_schema),
        inputs.input_digest,
        run_id,
        generated_at,
    )
    assert reversed_result.input_digest == scan(config).input_digest
    assert reversed_result.run_id == run_id
    # The reversed chain does produce different lineage (upstream DDL is
    # missing when the build script runs first), proving the digest itself is
    # independent of execution order.
    assert reversed_result.graph.edges == ()


def test_snapshot_digest_changes_when_incoming_state_changes(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    inputs = load_scan_inputs(config)
    slices = build_slices(config, inputs.sql_files)

    from_scratch = replay_slices(config, slices[1:], SliceState())
    with_upstream = replay_slices(
        config,
        slices[1:],
        replay_slices(config, slices[:1], SliceState()).state,
    )
    # Same slice, different incoming state -> different snapshot.
    assert (
        from_scratch.snapshots[0].state_digest
        != with_upstream.snapshots[0].state_digest
    )


def test_snapshot_payload_is_json_serializable_and_stable(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    replay = replay_scan(config)
    payloads = [snapshot_payload(snap) for snap in replay.snapshots]
    encoded_once = json.dumps(payloads, sort_keys=True)
    encoded_twice = json.dumps(
        [snapshot_payload(snap) for snap in replay_scan(config).snapshots], sort_keys=True
    )
    assert encoded_once == encoded_twice
    assert all("state_digest" in item for item in payloads)


def test_replay_chain_byte_identical_artifacts_for_both_fixtures(tmp_path):
    for name in ("demo", "broken"):
        root = FIXTURES / name
        config = load_config(root / "config.yaml")
        scanned = scan(config)

        replay = replay_scan(config)
        assert replay.run_id == scanned.run_id
        assert replay.input_digest == scanned.input_digest
        finalized = replay_to_scan_result(config, replay)

        scan_dir = tmp_path / name / "scan"
        replay_dir = tmp_path / name / "replay"
        scan_artifacts = write_artifacts(scanned, scan_dir)
        replay_artifacts = write_artifacts(finalized, replay_dir)
        for key in ("graph", "findings", "markdown", "html"):
            assert scan_artifacts[key].read_bytes() == replay_artifacts[key].read_bytes()
        assert graph_payload(finalized) == graph_payload(scanned)
        assert findings_payload(finalized) == findings_payload(scanned)
        assert finalized.summary == scanned.summary


def test_replay_cli_writes_deterministic_snapshots_and_matching_artifacts(
    project_factory, tmp_path, capsys
):
    p = _two_script_project(project_factory)
    out = tmp_path / "cli"
    code = main(["replay", "--config", str(p.config_path), "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 0
    assert "replayed twice with identical state" in captured.out

    replay_dir = out / "replay"
    snapshots = json.loads((replay_dir / "snapshots.json").read_text(encoding="utf-8"))
    assert [item["script"] for item in snapshots["slices"]] == [
        "01_schema.sql",
        "02_build.sql",
    ]
    assert snapshots["run_id"] == p.scan().run_id
    # Full chain also emits the same graph/findings as the scan command.
    scanned = scan(p.load_config())
    assert (replay_dir / "graph.json").read_bytes() == write_artifacts(
        scanned, tmp_path / "scan"
    )["graph"].read_bytes()
    assert (replay_dir / "findings.json").read_bytes() == (
        tmp_path / "scan" / "findings.json"
    ).read_bytes()

    # Running the command again overwrites with byte-identical snapshots.
    again = tmp_path / "cli-again"
    assert main(["replay", "--config", str(p.config_path), "--out", str(again)]) == 0
    capsys.readouterr()
    assert (replay_dir / "snapshots.json").read_bytes() == (
        again / "replay" / "snapshots.json"
    ).read_bytes()


def test_replay_cli_partial_chain_skips_final_artifacts(project_factory, tmp_path, capsys):
    p = _two_script_project(project_factory)
    code = main(
        [
            "replay",
            "--config",
            str(p.config_path),
            "--out",
            str(tmp_path / "partial"),
            "--slices",
            "0",
        ]
    )
    assert code == 0
    assert "partial replay" in capsys.readouterr().out
    replay_dir = tmp_path / "partial" / "replay"
    assert (replay_dir / "snapshots.json").is_file()
    assert not (replay_dir / "graph.json").exists()
    assert not (replay_dir / "findings.json").exists()


def test_replay_cli_rejects_unknown_slice_index(project_factory, tmp_path, capsys):
    p = _two_script_project(project_factory)
    code = main(
        [
            "replay",
            "--config",
            str(p.config_path),
            "--out",
            str(tmp_path / "bad"),
            "--slices",
            "9",
        ]
    )
    assert code == 2
    assert "unknown slice index" in capsys.readouterr().err


def test_demo_and_broken_fixture_replay_matches_expected_snapshots():
    demo = load_config(FIXTURES / "demo" / "config.yaml")
    demo_replay = replay_scan(demo, "2024-06-01T00:00:00Z")
    demo_result = replay_to_scan_result(demo, demo_replay, "2024-06-01T00:00:00Z")
    demo_expected = json.loads(
        (FIXTURES / "demo" / "expected" / "findings.json").read_text(encoding="utf-8")
    )
    assert findings_payload(demo_result) == demo_expected
    assert demo_result.run_id == scan(demo, "2024-06-01T00:00:00Z").run_id

    broken = load_config(FIXTURES / "broken" / "config.yaml")
    broken_replay = replay_scan(broken, "2024-06-02T00:00:00Z")
    broken_result = replay_to_scan_result(broken, broken_replay, "2024-06-02T00:00:00Z")
    broken_expected = json.loads(
        (FIXTURES / "broken" / "expected" / "findings.json").read_text(encoding="utf-8")
    )
    assert findings_payload(broken_result) == broken_expected
    # Every broken script appears as its own slice.
    assert [item.script for item in broken_replay.slices] == [
        "01_schema.sql",
        "02_revenue.sql",
        "03_cycle.sql",
        "04_unsupported.sql",
    ]


def test_replay_one_slice_is_independent_and_does_not_mutate_incoming_state(
    project_factory,
):
    p = _two_script_project(project_factory)
    config = p.load_config()
    slices = build_slices(config, load_scan_inputs(config).sql_files)

    pristine = SliceState()
    first = replay_one_slice(config, slices[0], pristine)
    # Repeating the same single slice gives the identical checkpoint.
    again = replay_one_slice(config, slices[0], SliceState())
    assert first.state_digest == again.state_digest
    assert first.state == again.state
    # The incoming state object is untouched (the API clones before running).
    assert pristine.schema == {} and pristine.known == {}

    # Continue one slice at a time, chaining snapshots manually.
    second = replay_one_slice(config, slices[1], first.state)
    chain = replay_slices(config, slices, SliceState())
    assert second.state_digest == chain.snapshots[1].state_digest
    # And double-executing the second slice from the same upstream matches.
    second_again = replay_one_slice(config, slices[1], first.state)
    assert second.state_digest == second_again.state_digest


def test_empty_sql_directory_replays_as_empty_chain(project_factory):
    p = project_factory("empty")
    config = p.load_config()
    inputs = load_scan_inputs(config)
    assert build_slices(config, inputs.sql_files) == ()
    replay = replay_slices(config, (), SliceState())
    assert replay.snapshots == ()
    result = replay_to_scan_result(config, replay_scan(config))
    assert result.graph.nodes == ()
    assert result.statements == ()
    assert result.unsupported == ()
    assert result.run_id == scan(config).run_id


def test_slices_are_frozen_and_state_clone_is_independent(project_factory):
    p = _two_script_project(project_factory)
    config = p.load_config()
    slices = build_slices(config, load_scan_inputs(config).sql_files)
    # ScriptSlice is immutable.
    with pytest.raises(dataclasses.FrozenInstanceError):
        slices[0].text = "SELECT 1;"

    state = replay_slices(config, slices[:1], SliceState()).state
    pristine = state.clone()
    clone = state.clone()
    clone.mappings.append("tampered")
    clone.schema["raw.orders"].columns.pop("id")
    assert "tampered" not in state.mappings
    assert "id" in state.schema["raw.orders"].columns
    # The original state and a fresh clone are untouched by the mutation.
    assert state == pristine
    assert clone != state
