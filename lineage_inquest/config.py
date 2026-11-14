"""Configuration loading with a small, handwritten YAML schema validator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .graph.stable import content_hash
from .naming import valid_ident

SEVERITIES = ("error", "warn", "info")
CATEGORIES = (
    "missing_upstream",
    "unexpected_upstream",
    "type_mismatch",
    "assertion_mismatch",
    "unsupported_sql",
    "cycle_detected",
    "table_redefined",
)
DEFAULT_SEVERITY = {
    "missing_upstream": "error",
    "unexpected_upstream": "warn",
    "type_mismatch": "error",
    "assertion_mismatch": "warn",
    "unsupported_sql": "warn",
    "cycle_detected": "error",
    "table_redefined": "warn",
}
_TOP_KEYS = {
    "project",
    "sql_dir",
    "contracts_dir",
    "out_dir",
    "default_schema",
    "server",
    "severity",
    "frozen_time",
    "expect_findings",
    "max_query_depth",
    "cycle_budget",
}
_SERVER_KEYS = {"host", "port"}


@dataclass(frozen=True)
class Config:
    project: str
    sql_dir: Path
    contracts_dir: Path
    out_dir: Path
    default_schema: str
    severity: dict[str, str]
    severity_overrides: dict[str, str]
    server_host: str
    server_port: int
    frozen_time: str | None
    expect_findings: Path | None
    max_query_depth: int
    cycle_budget: int
    source_path: Path
    raw: dict[str, Any]

    @property
    def digest(self) -> str:
        return content_hash(
            {
                "project": self.project,
                "default_schema": self.default_schema,
                "severity": self.severity,
                "max_query_depth": self.max_query_depth,
                "cycle_budget": self.cycle_budget,
            }
        )


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def _path(value: Any, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config key {key!r} must be a non-empty path string")
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _server(raw: Any) -> tuple[str, int]:
    if raw is None:
        return "127.0.0.1", 5000
    if not isinstance(raw, dict):
        raise ConfigError("config key 'server' must be a mapping")
    unknown = set(raw) - _SERVER_KEYS
    if unknown:
        raise ConfigError(f"unknown server key(s): {', '.join(sorted(unknown))}")
    host = raw.get("host", "127.0.0.1")
    port = raw.get("port", 5000)
    if not isinstance(host, str) or not host:
        raise ConfigError("server.host must be a string")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")
    return host, port


def _severity(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    overrides: dict[str, str] = {}
    if raw is not None:
        if not isinstance(raw, dict):
            raise ConfigError("config key 'severity' must be a mapping")
        for key, value in raw.items():
            if key not in CATEGORIES:
                raise ConfigError(f"unknown finding category in severity: {key!r}")
            if value not in SEVERITIES:
                raise ConfigError(
                    f"severity for {key!r} must be one of: {', '.join(SEVERITIES)}"
                )
            overrides[key] = value
    return {**DEFAULT_SEVERITY, **overrides}, overrides


def parse_frozen_time(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(
                f"invalid frozen time {text!r}; use ISO-8601 (e.g. 2024-01-01T00:00:00Z)"
            ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_host(host: str) -> None:
    if host != "127.0.0.1":
        raise ConfigError(
            f"refusing to bind to {host!r}: the read-only server only listens on 127.0.0.1"
        )


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    root = _read_yaml(cfg_path)
    if not isinstance(root, dict):
        raise ConfigError("config root must be a YAML mapping")
    unknown = set(root) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    base = cfg_path.parent

    if "sql_dir" not in root:
        raise ConfigError("config is missing required key 'sql_dir'")
    if "contracts_dir" not in root:
        raise ConfigError("config is missing required key 'contracts_dir'")
    sql_dir = _path(root["sql_dir"], "sql_dir", base)
    contracts_dir = _path(root["contracts_dir"], "contracts_dir", base)
    out_dir = _path(root.get("out_dir", "out"), "out_dir", base)
    for label, folder in (("sql_dir", sql_dir), ("contracts_dir", contracts_dir)):
        if not folder.is_dir():
            raise ConfigError(f"{label} is not a directory: {folder}")

    schema = str(root.get("default_schema", "main")).lower()
    if not valid_ident(schema):
        raise ConfigError("default_schema must be a simple SQL identifier")
    severity, overrides = _severity(root.get("severity"))
    host, port = _server(root.get("server"))
    project = root.get("project", "lineage-inquest")
    if not isinstance(project, str) or not project.strip():
        raise ConfigError("config key 'project' must be a non-empty string")
    max_depth = root.get("max_query_depth", 8)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 50:
        raise ConfigError("max_query_depth must be an integer between 1 and 50")
    cycle_budget = root.get("cycle_budget", 200000)
    if isinstance(cycle_budget, bool) or not isinstance(cycle_budget, int) or not 1 <= cycle_budget <= 100_000_000:
        raise ConfigError("cycle_budget must be an integer between 1 and 100000000")
    expect = root.get("expect_findings")
    expect_path = _path(expect, "expect_findings", base) if expect else None
    frozen = parse_frozen_time(root.get("frozen_time"))

    return Config(
        project=project.strip(),
        sql_dir=sql_dir,
        contracts_dir=contracts_dir,
        out_dir=out_dir,
        default_schema=schema,
        severity=severity,
        severity_overrides=overrides,
        server_host=host,
        server_port=port,
        frozen_time=frozen,
        expect_findings=expect_path,
        max_query_depth=max_depth,
        cycle_budget=cycle_budget,
        source_path=cfg_path,
        raw=root,
    )
