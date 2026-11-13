"""Idempotent demo fixture generator."""

from __future__ import annotations

from pathlib import Path

from .demo_data import DEMO_FILES


def seed(out_dir: Path) -> list[Path]:
    target = Path(out_dir).expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise ValueError(f"seed output exists and is not a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for relative in sorted(DEMO_FILES):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = DEMO_FILES[relative]
        path.write_bytes(content.encode("utf-8"))
        written.append(path)
    return written
