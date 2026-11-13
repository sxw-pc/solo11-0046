"""Map SELECT output columns to fully qualified source columns."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from ..models import TableDef
from ..naming import canonical_table, clean_ident


@dataclass(frozen=True)
class MappingIssue:
    construct: str
    reason: str


@dataclass(frozen=True)
class OutputColumn:
    name: str
    sources: tuple[str, ...]
    kind: str
    data_type: str
    expression: str


@dataclass
class Scope:
    alias: str
    table: str | None
    columns: dict[str, OutputColumn] | None = None
    order: list[str] = field(default_factory=list)


@dataclass
class SelectAnalysis:
    columns: list[OutputColumn]
    issues: list[MappingIssue]


def data_type_text(node: exp.DataType) -> str:
    if not isinstance(node, exp.DataType):
        return "unknown"
    return node.sql(dialect="").upper().replace(" ,", ",").replace(", ", ",")


def analyze_select(
    select: exp.Expression,
    schema: dict[str, TableDef],
    default_schema: str,
    cte_map: dict[str, list[OutputColumn]] | None = None,
    outer_with: exp.With | None = None,
) -> SelectAnalysis:
    if isinstance(select, exp.Subquery):
        select = select.this
    issues: list[MappingIssue] = []
    if not isinstance(select, exp.Select):
        return SelectAnalysis([], [MappingIssue("unsupported_query", "only SELECT query blocks are supported")])

    local_ctes: dict[str, list[OutputColumn]] = dict(cte_map or {})
    for with_clause in (outer_with, select.args.get("with_")):
        if isinstance(with_clause, exp.With):
            for cte in with_clause.expressions:
                name = clean_ident(cte.alias)
                analysis = analyze_select(cte.this, schema, default_schema, local_ctes)
                local_ctes[name] = analysis.columns
                issues.extend(analysis.issues)

    scopes: list[Scope] = []
    from_clause = select.args.get("from_")
    if isinstance(from_clause, exp.From):
        append_source(from_clause.this, scopes, schema, default_schema, local_ctes, issues)
    for join in select.args.get("joins") or []:
        append_source(join.this, scopes, schema, default_schema, local_ctes, issues)

    outputs: list[OutputColumn] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or _is_star_column(projection):
            outputs.extend(_expand_star(projection, scopes, issues))
        elif isinstance(projection, exp.Alias):
            alias = clean_ident(projection.alias)
            outputs.append(map_expression(projection.this, alias, scopes, issues))
        else:
            name = clean_ident(projection.alias_or_name)
            if not name:
                name = f"column_{len(outputs) + 1}"
            outputs.append(map_expression(projection, name, scopes, issues))

    seen: set[str] = set()
    for output in outputs:
        if output.name in seen:
            issues.append(MappingIssue("duplicate_output_column", f"duplicate output column {output.name!r}"))
        seen.add(output.name)
    return SelectAnalysis(outputs, issues)


def _is_star_column(node: exp.Expression) -> bool:
    return isinstance(node, exp.Column) and isinstance(node.this, exp.Star)


def append_source(node, scopes, schema, default_schema, cte_map, issues) -> None:
    if isinstance(node, exp.Table):
        raw_name = clean_ident(node.name)
        alias = clean_ident(node.alias or node.name)
        if raw_name in cte_map and not (node.db or node.catalog):
            columns = {col.name: col for col in cte_map[raw_name]}
            scopes.append(Scope(alias, None, columns, [col.name for col in cte_map[raw_name]]))
            return
        table_name = canonical_table(
            [clean_ident(node.catalog or ""), clean_ident(node.db or ""), raw_name], default_schema
        )
        definition = schema.get(table_name)
        columns = None
        order: list[str] = []
        if definition is not None:
            names = [col.name for col in definition.ordered_columns()]
            columns = {
                col.name: OutputColumn(
                    col.name,
                    (f"{table_name}.{col.name}",),
                    "direct",
                    col.data_type,
                    col.name,
                )
                for col in definition.ordered_columns()
            }
            order = names
        scopes.append(Scope(alias, table_name, columns, order))
        return
    if isinstance(node, exp.Subquery):
        inner = analyze_select(node.this, schema, default_schema, cte_map)
        issues.extend(inner.issues)
        alias = clean_ident(node.alias or f"subquery_{len(scopes) + 1}")
        columns = {col.name: col for col in inner.columns}
        scopes.append(Scope(alias, None, columns, [col.name for col in inner.columns]))
        return
    issues.append(MappingIssue("unsupported_source", f"unsupported FROM source {type(node).__name__}"))


def _resolve(name: str, qualifier: str, scopes: list[Scope], issues: list[MappingIssue]):
    qualifier = clean_ident(qualifier) if qualifier else ""
    name = clean_ident(name)
    candidates = scopes
    if qualifier:
        candidates = [s for s in scopes if s.alias == qualifier or (s.table and s.table.split(".")[-1] == qualifier)]
        if not candidates:
            issues.append(MappingIssue("unresolved_column", f"unknown table alias {qualifier!r} for column {name!r}"))
            return None, None
    known = [s for s in candidates if s.columns is not None and name in s.columns]
    if len(known) == 1:
        return known[0], known[0].columns[name]
    if len(known) > 1:
        issues.append(MappingIssue("ambiguous_column", f"column {name!r} exists in multiple sources"))
        return None, None
    if len(candidates) == 1:
        scope = candidates[0]
        if scope.columns is not None:
            issues.append(MappingIssue("unresolved_column", f"column {name!r} not found in {scope.alias!r}"))
            return None, None
        if scope.table:
            return scope, OutputColumn(name, (f"{scope.table}.{name}",), "direct", "unknown", name)
    issues.append(MappingIssue("unresolved_column", f"cannot resolve column {name!r} to a source table"))
    return None, None


def _expand_star(projection: exp.Expression, scopes: list[Scope], issues: list[MappingIssue]) -> list[OutputColumn]:
    qualifier = ""
    if isinstance(projection, exp.Column) and projection.table:
        qualifier = clean_ident(projection.table)
    selected = scopes
    if qualifier:
        selected = [s for s in scopes if s.alias == qualifier or (s.table and s.table.split(".")[-1] == qualifier)]
        if not selected:
            issues.append(MappingIssue("star_unresolvable", f"cannot expand * for unknown source {qualifier!r}"))
            return []
    if not selected:
        issues.append(MappingIssue("star_unresolvable", "cannot expand * without a known FROM source"))
        return []
    outputs: list[OutputColumn] = []
    for scope in selected:
        if scope.columns is None or not scope.columns:
            issues.append(MappingIssue("star_unresolvable", f"cannot expand * for source {scope.alias!r}: columns unknown"))
            return []
        for name in scope.order:
            col = scope.columns[name]
            outputs.append(OutputColumn(col.name, col.sources, col.kind, col.data_type, f"{scope.alias}.*"))
    return outputs


def map_expression(expr: exp.Expression, name: str, scopes: list[Scope], issues: list[MappingIssue]) -> OutputColumn:
    if isinstance(expr, exp.Column) and not isinstance(expr.this, exp.Star):
        _scope, source = _resolve(expr.name, expr.table, scopes, issues)
        if source is not None:
            return OutputColumn(name, source.sources, source.kind, source.data_type, expr.sql(dialect=""))
        return OutputColumn(name, (), "unresolved", "unknown", expr.sql(dialect=""))

    sources: list[str] = []
    types: list[str] = []
    for column in expr.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        _scope, source = _resolve(column.name, column.table, scopes, issues)
        if source is not None:
            for source_name in source.sources:
                if source_name not in sources:
                    sources.append(source_name)
            if source.data_type != "unknown":
                types.append(source.data_type)
    kind = "aggregate" if any(expr.find_all(exp.AggFunc)) else ("expression" if sources else "constant")
    return OutputColumn(name, tuple(sources), kind, _infer_type(expr, types), expr.sql(dialect=""))


def _infer_type(expr: exp.Expression, source_types: list[str]) -> str:
    if isinstance(expr, exp.Cast):
        return data_type_text(expr.to)
    if isinstance(expr, exp.Literal):
        if expr.is_string:
            return "VARCHAR"
        return "BIGINT" if "." not in expr.this else "DECIMAL"
    if isinstance(expr, exp.Boolean):
        return "BOOLEAN"
    if isinstance(expr, exp.Null):
        return "NULL"
    if isinstance(expr, exp.Count):
        return "BIGINT"
    if isinstance(expr, (exp.Sum, exp.Avg, exp.Round)):
        return "DECIMAL"
    if isinstance(expr, (exp.Min, exp.Max, exp.Coalesce)) and source_types:
        return source_types[0]
    if isinstance(expr, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
        return "DECIMAL"
    if isinstance(expr, (exp.Date, exp.DateTrunc, exp.CurrentDate)):
        return "DATE"
    if isinstance(expr, (exp.Timestamp, exp.CurrentTimestamp)):
        return "TIMESTAMP"
    if isinstance(expr, (exp.Concat, exp.Lower, exp.Upper, exp.Substring)):
        return "VARCHAR"
    return source_types[0] if len(set(source_types)) == 1 else "unknown"
