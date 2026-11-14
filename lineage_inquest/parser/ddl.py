"""CREATE TABLE, CTAS, DROP TABLE, and ALTER TABLE extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlglot import exp

from ..models import (
    Assertion,
    Bound,
    ColumnDef,
    ColumnMapping,
    RangeAssertion,
    SqlUnit,
    TableDef,
    Unsupported,
    assertion_key,
)
from ..naming import canonical_table, clean_ident
from ..sqltext import locate_line, snippet
from .guard import issue as guard_issue
from .select_map import MappingIssue, analyze_select, data_type_text


@dataclass
class DdlResult:
    definition: TableDef | None = None
    mappings: list[ColumnMapping] = field(default_factory=list)
    issues: list[Unsupported] = field(default_factory=list)
    redefined: list[tuple[str, list[tuple[str, int]]]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def table_name(table: exp.Table, default_schema: str) -> str:
    return canonical_table(
        [clean_ident(table.catalog or ""), clean_ident(table.db or ""), clean_ident(table.name)],
        default_schema,
    )


def handle_ddl(
    unit: SqlUnit,
    ast: exp.Expression,
    schema: dict[str, TableDef],
    known: dict[str, TableDef],
    default_schema: str,
) -> DdlResult | None:
    if isinstance(ast, exp.Create):
        return _create(unit, ast, schema, known, default_schema)
    if isinstance(ast, exp.Drop):
        return _drop(ast, schema, default_schema)
    if isinstance(ast, exp.Alter):
        return _alter(unit, ast, schema, known, default_schema)
    return None


def _create(unit, ast: exp.Create, schema, known, default_schema) -> DdlResult:
    result = DdlResult()
    if str(ast.kind).upper() != "TABLE":
        result.issues.append(guard_issue(unit, "unsupported_statement", "only CREATE TABLE is supported"))
        return result
    target = ast.this
    explicit_columns: list[exp.Identifier] = []
    if isinstance(target, exp.Schema):
        explicit_columns = list(target.expressions)
        target = target.this
    name = table_name(target, default_schema)
    old = known.get(name)
    if old is not None and ast.args.get("exists"):
        result.definition = old
        return result

    if isinstance(ast.expression, exp.Query):
        analysis = analyze_select(ast.expression, schema, default_schema, outer_with=ast.args.get("with_"))
        result.issues.extend(_issues(unit, analysis.issues))
        definition = TableDef(name=name, origin="derived", locations=[(unit.script, unit.line)])
        for output in analysis.columns:
            definition.add(
                ColumnDef(
                    name=output.name,
                    data_type=output.data_type,
                    nullable=True,
                    derived=True,
                    assertions=(),
                )
            )
            if output.sources:
                result.mappings.append(
                    ColumnMapping(
                        target=f"{name}.{output.name}",
                        sources=output.sources,
                        kind=output.kind,
                        expression=output.expression,
                        script=unit.script,
                        line=unit.line,
                        statement=unit.index,
                    )
                )
    else:
        if not isinstance(target, exp.Table):
            result.issues.append(guard_issue(unit, "unsupported_statement", "unsupported CREATE TABLE target"))
            return result
        definition = TableDef(name=name, origin="ddl", locations=[(unit.script, unit.line)])
        for column in explicit_columns:
            if isinstance(column, exp.ColumnDef):
                definition.add(_column(column))
            elif isinstance(column, exp.PrimaryKey):
                _apply_table_primary_key(definition, column)
            elif isinstance(column, exp.Constraint):
                for primary in column.find_all(exp.PrimaryKey):
                    _apply_table_primary_key(definition, primary)
                for check in column.find_all(exp.CheckColumnConstraint):
                    _apply_table_check(definition, check.this)
            elif isinstance(column, exp.CheckColumnConstraint):
                _apply_table_check(definition, column.this)

    if old is not None:
        result.redefined.append((name, list(old.locations)))
    known[name] = definition
    schema[name] = definition
    result.definition = definition
    return result


def _column(column: exp.ColumnDef) -> ColumnDef:
    name = clean_ident(column.name)
    data_type = data_type_text(column.kind) if column.kind else "unknown"
    primary_key = False
    nullable = True
    assertions: set[Assertion] = set()
    ranges: list[RangeAssertion] = []
    for constraint in column.constraints or []:
        kind = constraint.kind
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            primary_key = True
            nullable = False
            assertions.update({"not_null", "unique"})
        elif isinstance(kind, exp.NotNullColumnConstraint):
            if getattr(kind, "allow_null", False):
                nullable = True
            else:
                nullable = False
                assertions.add("not_null")
        elif isinstance(kind, exp.UniqueColumnConstraint):
            assertions.add("unique")
        elif isinstance(kind, exp.CheckColumnConstraint):
            found = _check_range(kind.this, name)
            if found:
                ranges.append(found)
    if ranges:
        assertions.add(_combine(ranges))
    return ColumnDef(
        name=name,
        data_type=data_type,
        primary_key=primary_key,
        nullable=nullable,
        derived=False,
        assertions=_ordered(assertions),
    )


def _apply_table_primary_key(definition: TableDef, node: exp.PrimaryKey) -> None:
    for identifier in node.expressions:
        name = clean_ident(identifier.name)
        current = definition.columns.get(name)
        if current is None:
            continue
        definition.add(
            ColumnDef(
                current.name,
                current.data_type,
                True,
                False,
                current.derived,
                _merge_assertion(current.assertions, "not_null", "unique"),
            )
        )


def _apply_table_check(definition: TableDef, check: exp.Expression) -> None:
    """Apply a table-level CHECK to every column it constrains."""
    names = {clean_ident(node.name) for node in check.find_all(exp.Column)}
    for name in sorted(names):
        current = definition.columns.get(name)
        if current is None:
            continue
        found = _check_range(check, name)
        if found is None:
            continue
        definition.add(
            ColumnDef(
                current.name,
                current.data_type,
                current.primary_key,
                current.nullable,
                current.derived,
                _merge_assertion(current.assertions, found),
            )
        )


def _check_range(check: exp.Expression, column: str) -> RangeAssertion | None:
    """Normalize numeric comparisons against ``column`` into one range."""
    found: list[RangeAssertion] = []
    for node in _conjuncts(check):
        partial = _comparison_range(node, column)
        if partial is not None:
            found.append(partial)
    if not found:
        return None
    return _combine(found)


def _conjuncts(node: exp.Expression):
    """Yield the top-level AND operands, looking through parentheses."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    else:
        yield node


def _comparison_range(node: exp.Expression, column: str) -> RangeAssertion | None:
    if isinstance(node, exp.Between):
        if not _is_column(node.this, column):
            return None
        low = _numeric_value(node.args.get("low"))
        high = _numeric_value(node.args.get("high"))
        if low is None or high is None:
            return None
        return RangeAssertion(Bound(low, True), Bound(high, True))
    upper_ops = (exp.GTE, exp.GT)
    if not isinstance(node, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
        return None
    inclusive = isinstance(node, (exp.GTE, exp.LTE))
    left, right = node.this, node.expression
    if _is_column(left, column):
        value = _numeric_value(right)
        if value is None:
            return None
        if isinstance(node, upper_ops):
            return RangeAssertion(lower=Bound(value, inclusive))
        return RangeAssertion(upper=Bound(value, inclusive))
    if _is_column(right, column):
        value = _numeric_value(left)
        if value is None:
            return None
        # ``literal < col`` is a lower bound (and vice versa).
        if isinstance(node, upper_ops):
            return RangeAssertion(upper=Bound(value, inclusive))
        return RangeAssertion(lower=Bound(value, inclusive))
    return None


def _is_column(node: exp.Expression | None, column: str) -> bool:
    return isinstance(node, exp.Column) and clean_ident(node.name) == column


def _numeric_value(node: exp.Expression | None) -> Decimal | None:
    sign = 1
    if isinstance(node, exp.Neg):
        sign = -1
        node = node.this
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        return sign * Decimal(node.this)
    except ArithmeticError:
        return None


def _combine(ranges: list[RangeAssertion]) -> RangeAssertion:
    combined = ranges[0]
    for item in ranges[1:]:
        combined = combined.intersect(item)
    return combined


def _ordered(values) -> tuple[Assertion, ...]:
    return tuple(sorted(set(values), key=assertion_key))


def _merge_assertion(assertions: tuple[Assertion, ...], *values: Assertion) -> tuple[Assertion, ...]:
    merged = list(assertions)
    for value in values:
        if isinstance(value, RangeAssertion):
            existing = [item for item in merged if isinstance(item, RangeAssertion)]
            merged = [item for item in merged if not isinstance(item, RangeAssertion)]
            merged.append(_combine(existing + [value]))
        else:
            merged.append(value)
    return _ordered(merged)


def _drop(ast: exp.Drop, schema, default_schema) -> DdlResult:
    result = DdlResult()
    for table in ast.args.get("tables") or []:
        name = table_name(table, default_schema)
        result.dropped.append(name)
        schema.pop(name, None)
    return result


def _alter(unit, ast: exp.Alter, schema, known, default_schema) -> DdlResult:
    result = DdlResult()
    if not isinstance(ast.this, exp.Table):
        result.issues.append(guard_issue(unit, "unsupported_statement", "unsupported ALTER target"))
        return result
    name = table_name(ast.this, default_schema)
    definition = schema.get(name) or known.get(name)
    if definition is None:
        result.issues.append(
            Unsupported(
                script=unit.script,
                line=locate_line(unit.text, unit.line, r"\bALTER\b"),
                construct="unknown_alter_target",
                reason=f"ALTER TABLE target {name} is not defined",
                snippet=snippet(unit.text, r"\bALTER\b"),
            )
        )
        return result
    for action in ast.args.get("actions") or []:
        if isinstance(action, exp.ColumnDef):
            definition.add(_column(action))
        else:
            result.issues.append(
                guard_issue(unit, "alter_unsupported", f"unsupported ALTER action {type(action).__name__}")
            )
    known[name] = definition
    if name in schema:
        schema[name] = definition
    result.definition = definition
    return result


def _issues(unit: SqlUnit, mapping_issues: list[MappingIssue]) -> list[Unsupported]:
    return [
        Unsupported(
            script=unit.script,
            line=unit.line,
            construct=item.construct,
            reason=item.reason,
            snippet=snippet(unit.text, r"SELECT|\*"),
        )
        for item in mapping_issues
    ]
