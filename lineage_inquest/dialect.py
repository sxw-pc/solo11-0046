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

# Statement roots the scanner knows how to dispatch. Kept in one place so the
# guard layer and the scanner's allow chain cannot drift apart.
STATEMENT_TYPES = (exp.Create, exp.Insert, exp.Merge, exp.Drop, exp.Alter, exp.Select)
DDL_TYPES = (exp.Create, exp.Drop, exp.Alter)
PRODUCE_TYPES = (exp.Insert, exp.Merge)

# sqlglot >= 30.31 mounts every set operation under a common base class.
# Matching the base keeps UNION/INTERSECT/EXCEPT (and any future sibling)
# equivalent to the historical per-class checks.
SET_OPERATION_TYPE = exp.SetOperation


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


def _with_is_recursive(with_clause: exp.With) -> bool:
    """Read the RECURSIVE marker without depending on one storage shape.

    sqlglot stores the flag in ``With.args["recursive"]`` (``True``/``None``)
    and exposes it through the ``recursive`` property. 30.31 additionally parses
    the standard ``CYCLE`` clause (stored either in ``With.args["cycle"]`` or as
    a ``RecursiveWithSearch`` node with ``kind="CYCLE"`` inside ``search``).
    30.18 rejected ``CYCLE`` as a parse error, so it must surface as
    ``recursive_cte`` here instead of silently becoming a supported statement.
    Plain ``SEARCH`` clauses parsed in both releases and were not flagged, so
    they keep their existing classification.
    """
    if with_clause.args.get("recursive"):
        return True
    if with_clause.args.get("cycle"):
        return True
    search = with_clause.args.get("search")
    if isinstance(search, exp.RecursiveWithSearch) and str(search.args.get("kind")).upper() == "CYCLE":
        return True
    # Compatibility shim: follow the public property if a later release moves
    # the flag out of the ``recursive`` args key.
    return bool(getattr(with_clause, "recursive", False))


def _has_recursive_cte(ast: exp.Expression) -> bool:
    return any(
        isinstance(node, exp.With) and _with_is_recursive(node) for node in ast.walk()
    )


def detect_unsupported(ast: exp.Expression) -> list[str]:
    """Return unsupported construct names in stable order.

    The order always follows ``UNSUPPORTED_FEATURES``; nodes are collected
    into a set first so AST traversal order cannot leak into the result.
    """
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
        elif isinstance(node, SET_OPERATION_TYPE):
            found.add("set_operation")
    if _has_recursive_cte(ast):
        found.add("recursive_cte")
    return [name for name, _ in UNSUPPORTED_FEATURES if name in found]


def unsupported_statement_reason(ast: exp.Expression | None) -> str | None:
    if ast is None:
        return "statement could not be parsed"
    if isinstance(ast, STATEMENT_TYPES):
        return None
    return f"statement type {type(ast).__name__} is outside the supported subset"
