"""SQL parsing façade used by the scanner."""

from __future__ import annotations

import sqlglot
from sqlglot.errors import ParseError

from ..models import SqlUnit


def parse_unit(unit: SqlUnit):
    """Parse one statement; return None instead of leaking parser tracebacks."""
    try:
        return sqlglot.parse_one(unit.text, read=None)
    except (ParseError, ValueError):
        return None
