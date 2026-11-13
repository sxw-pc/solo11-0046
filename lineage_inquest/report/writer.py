"""Write report artifacts with deterministic UTF-8/LF encoding."""

from __future__ import annotations

from pathlib import Path

from ..models import ScanResult
from .html import render_html
from .json_out import findings_json, graph_json
from .markdown import render_markdown


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def write_artifacts(result: ScanResult, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "graph": out_dir / "graph.json",
        "findings": out_dir / "findings.json",
        "markdown": out_dir / "report.md",
        "html": out_dir / "report.html",
    }
    write_text(artifacts["graph"], graph_json(result))
    write_text(artifacts["findings"], findings_json(result))
    write_text(artifacts["markdown"], render_markdown(result))
    write_text(artifacts["html"], render_html(result))
    return artifacts
