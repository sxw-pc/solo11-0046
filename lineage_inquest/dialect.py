"""The deliberately small SQL dialect accepted by Lineage Inquest.

Anything outside this list is reported as unsupported. The project never
guesses lineage for a construct it does not understand.
"""

from __future__ import annotations

from sqlglot import exp

SUPPORTED_STATEMENTS = (
    "CREATE TABLE",
    "CREATE TABLE AS SELECT",
    "INSERT INTO ... SELECT",
    "MERGE",
    "DROP TABLE",
    "ALTER TABLE ADD COLUMN",
    "SELECT (query block inside the statements above)",
)
UNSUPPORTED_FEATURES = (
    ("window_function", "window functions (OVER) are outside the supported subset"),
    ("recursive_cte", "recursive common table expressions are not supported"),
    ("lateral", "LATERAL sources are not supported"),
    ("pivot", "PIVOT/UNPIVOT is not supported"),
    ("qualify", "QUALIFY is not supported"),
    ("unnest", "UNNEST and other table functions are not supported"),
    ("set_operation", "UNION/INTERSECT/EXCEPT are not supported"),
    ("values_source", "VALUES as a row source is not supported"),
)

# Patterns are only used to attach a useful line number to an AST finding.
LINE_PATTERNS = {
    "window_function": r"\bOVER\s*\(",
    "recursive_cte": r"\bWITH\s+RECURSIVE\b",
    "lateral": r"\bLATERAL\b",
    "pivot": r"\b(PIVOT|UNPIVOT)\b",
    "qualify": r"\bQUALIFY\b",
    "unnest": r"\bUNNEST\b",
    "set_operation": r"\b(UNION|INTERSECT|EXCEPT)\b",
    "values_source": r"\bVALUES\b",
}
REASONS = dict(UNSUPPORTED_FEATURES)


def statement_kind(ast: exp.Expression | None) -> str:
    if isinstance(ast, exp.Create):
        return "ctas" if isinstance(ast.expression, exp.Query) else "create_table"
    if isinstance(ast, exp.Insert):
        return "insert_select"
    if isinstance(ast, exp.Merge):
        return "merge"
    if isinstance(ast, exp.Drop):
        return "drop_table"
    if isinstance(ast, exp.Alter):
        return "alter_table"
    if isinstance(ast, exp.Select):
        return "select"
    return "unknown"


def _has_recursive_cte(ast: exp.Expression) -> bool:
    return any(
        isinstance(node, exp.With) and bool(node.args.get("recursive"))
        for node in ast.walk()
    )


def detect_unsupported(ast: exp.Expression) -> list[str]:
    """Return unsupported construct names in stable order."""
    found: set[str] = set()
    for node in ast.walk():
        if isinstance(node, exp.Window):
            found.add("window_function")
        elif isinstance(node, exp.Lateral):
            found.add("lateral")
        elif isinstance(node, exp.Pivot):
            found.add("pivot")
        elif isinstance(node, exp.Qualify):
            found.add("qualify")
        elif isinstance(node, exp.Unnest):
            found.add("unnest")
        elif isinstance(node, exp.Values):
            found.add("values_source")
        elif isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            found.add("set_operation")
    if _has_recursive_cte(ast):
        found.add("recursive_cte")
    return [name for name, _ in UNSUPPORTED_FEATURES if name in found]


def unsupported_statement_reason(ast: exp.Expression | None) -> str | None:
    if ast is None:
        return "statement could not be parsed"
    if isinstance(ast, (exp.Create, exp.Insert, exp.Merge, exp.Drop, exp.Alter, exp.Select)):
        return None
    return f"statement type {type(ast).__name__} is outside the supported subset"
