"""Attach source locations to dialect guard results."""

from __future__ import annotations

from sqlglot import exp

from ..dialect import REASONS, detect_unsupported, unsupported_statement_reason
from ..models import SqlUnit, Unsupported
from ..sqltext import locate_line, snippet


def guard_ast(unit: SqlUnit, ast: exp.Expression | None) -> list[Unsupported]:
    if ast is None:
        return [
            Unsupported(
                script=unit.script,
                line=unit.line,
                construct="parse_error",
                reason="statement could not be parsed",
                snippet=snippet(unit.text),
            )
        ]
    issues: list[Unsupported] = []
    constructs = detect_unsupported(ast)
    for construct in constructs:
        issues.append(
            Unsupported(
                script=unit.script,
                line=locate_line(unit.text, unit.line, _pattern(construct)),
                construct=construct,
                reason=REASONS.get(construct, "construct is outside the supported subset"),
                snippet=snippet(unit.text, _pattern(construct)),
            )
        )
    reason = unsupported_statement_reason(ast)
    if reason and not constructs:
        issues.append(
            Unsupported(
                script=unit.script,
                line=unit.line,
                construct="unsupported_statement",
                reason=reason,
                snippet=snippet(unit.text),
            )
        )
    return issues


def _pattern(construct: str) -> str:
    from ..dialect import LINE_PATTERNS

    return LINE_PATTERNS.get(construct, r"\S")


def issue(unit: SqlUnit, construct: str, reason: str) -> Unsupported:
    pattern = _pattern(construct)
    return Unsupported(
        script=unit.script,
        line=locate_line(unit.text, unit.line, pattern),
        construct=construct,
        reason=reason,
        snippet=snippet(unit.text, pattern),
    )
