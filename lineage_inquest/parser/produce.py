"""INSERT ... SELECT and MERGE output-to-source column mappings."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from ..models import (
    BRANCH_MERGE_INSERT,
    BRANCH_MERGE_UPDATE,
    ColumnMapping,
    SqlUnit,
    TableDef,
    Unsupported,
)
from ..naming import clean_ident
from ..sqltext import snippet
from .ddl import table_name
from .select_map import (
    MappingIssue,
    Scope,
    analyze_select,
    append_source,
    map_expression,
)


@dataclass
class ProduceResult:
    mappings: list[ColumnMapping] = field(default_factory=list)
    issues: list[Unsupported] = field(default_factory=list)


def handle_produce(
    unit: SqlUnit,
    ast: exp.Expression,
    schema: dict[str, TableDef],
    default_schema: str,
) -> ProduceResult | None:
    if isinstance(ast, exp.Insert):
        return _insert(unit, ast, schema, default_schema)
    if isinstance(ast, exp.Merge):
        return _merge(unit, ast, schema, default_schema)
    return None


def _target(ast, default_schema: str) -> tuple[str, list[str], exp.Table | None]:
    node = ast.this
    explicit: list[str] = []
    if isinstance(node, exp.Schema):
        explicit = [clean_ident(item.name) for item in node.expressions]
        node = node.this
    if not isinstance(node, exp.Table):
        return "", explicit, None
    return table_name(node, default_schema), explicit, node


def _insert(unit, ast: exp.Insert, schema, default_schema) -> ProduceResult:
    result = ProduceResult()
    target_name, explicit, _ = _target(ast, default_schema)
    definition = schema.get(target_name)
    if definition is None:
        result.issues.append(_issue(unit, "unknown_insert_target", f"INSERT target {target_name or '<unknown>'} has no CREATE TABLE"))
        return result
    if not isinstance(ast.expression, exp.Query):
        result.issues.append(_issue(unit, "unsupported_insert_source", "INSERT requires a SELECT query source"))
        return result
    analysis = analyze_select(ast.expression, schema, default_schema, outer_with=ast.args.get("with_"))
    result.issues.extend(_mapping_issues(unit, analysis.issues))
    targets = explicit or [column.name for column in definition.ordered_columns()]
    if len(targets) != len(analysis.columns):
        result.issues.append(
            _issue(
                unit,
                "column_count_mismatch",
                f"INSERT has {len(targets)} target columns but SELECT returns {len(analysis.columns)}",
            )
        )
        return result
    for name, output in zip(targets, analysis.columns):
        if name not in definition.columns:
            result.issues.append(_issue(unit, "unknown_target_column", f"target column {target_name}.{name} is not defined"))
            continue
        if output.sources:
            result.mappings.append(
                ColumnMapping(
                    f"{target_name}.{name}",
                    output.sources,
                    output.kind,
                    output.expression,
                    unit.script,
                    unit.line,
                    unit.index,
                )
            )
    return result


def _merge(unit, ast: exp.Merge, schema, default_schema) -> ProduceResult:
    result = ProduceResult()
    target_name, _, target_table = _target(ast, default_schema)
    definition = schema.get(target_name)
    if definition is None or target_table is None:
        result.issues.append(_issue(unit, "unknown_merge_target", f"MERGE target {target_name or '<unknown>'} has no CREATE TABLE"))
        return result
    using = ast.args.get("using")
    if using is None:
        result.issues.append(_issue(unit, "unsupported_merge", "MERGE without a USING source is not supported"))
        return result

    scopes: list[Scope] = []
    mapping_issues: list[MappingIssue] = []
    append_source(using, scopes, schema, default_schema, {}, mapping_issues)
    append_source(target_table, scopes, schema, default_schema, {}, mapping_issues)
    whens = ast.args.get("whens")
    for when in (whens.expressions if isinstance(whens, exp.Whens) else []):
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            _merge_update(unit, then, target_name, definition, scopes, mapping_issues, result)
        elif isinstance(then, exp.Insert):
            _merge_insert(unit, then, target_name, definition, scopes, mapping_issues, result)
    result.issues.extend(_mapping_issues(unit, mapping_issues))
    return result


def _merge_update(unit, then, target_name, definition, scopes, issues, result) -> None:
    for assignment in then.expressions:
        if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
            issues.append(MappingIssue("unsupported_merge", "MERGE UPDATE assignment must target a column"))
            continue
        name = clean_ident(assignment.this.name)
        if name not in definition.columns:
            issues.append(MappingIssue("unknown_target_column", f"target column {target_name}.{name} is not defined"))
            continue
        output = map_expression(assignment.expression, name, scopes, issues)
        _record(unit, result, target_name, name, output, BRANCH_MERGE_UPDATE)


def _merge_insert(unit, then, target_name, definition, scopes, issues, result) -> None:
    columns_node = then.this
    values_node = then.expression
    columns = (
        [clean_ident(item.name) for item in columns_node.expressions]
        if isinstance(columns_node, exp.Tuple)
        else []
    )
    values = (
        list(values_node.expressions)
        if isinstance(values_node, (exp.Tuple, exp.Array))
        else []
    )
    if not columns:
        columns = [column.name for column in definition.ordered_columns()]
    if len(columns) != len(values):
        issues.append(MappingIssue("column_count_mismatch", "MERGE INSERT columns and values differ in length"))
        return
    for name, value in zip(columns, values):
        if name not in definition.columns:
            issues.append(MappingIssue("unknown_target_column", f"target column {target_name}.{name} is not defined"))
            continue
        output = map_expression(value, name, scopes, issues)
        _record(unit, result, target_name, name, output, BRANCH_MERGE_INSERT)


def _record(unit, result, table: str, name: str, output, branch: str = "") -> None:
    if not output.sources:
        return
    result.mappings.append(
        ColumnMapping(
            f"{table}.{name}",
            output.sources,
            output.kind,
            output.expression,
            unit.script,
            unit.line,
            unit.index,
            branch,
        )
    )


def _issue(unit: SqlUnit, construct: str, reason: str) -> Unsupported:
    return Unsupported(
        script=unit.script,
        line=unit.line,
        construct=construct,
        reason=reason,
        snippet=snippet(unit.text),
    )


def _mapping_issues(unit: SqlUnit, items: list[MappingIssue]) -> list[Unsupported]:
    return [_issue(unit, item.construct, item.reason) for item in items]
