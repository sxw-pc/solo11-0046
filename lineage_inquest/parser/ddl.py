"""CREATE TABLE, CTAS, DROP TABLE, and ALTER TABLE extraction."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from ..models import ColumnDef, ColumnMapping, SqlUnit, TableDef, Unsupported
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
    assertions: set[str] = set()
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
                assertions.add(found)
    return ColumnDef(
        name=name,
        data_type=data_type,
        primary_key=primary_key,
        nullable=nullable,
        derived=False,
        assertions=tuple(sorted(assertions)),
    )


def _apply_table_primary_key(definition: TableDef, node: exp.PrimaryKey) -> None:
    for identifier in node.expressions:
        name = clean_ident(identifier.name)
        current = definition.columns.get(name)
        if current is None:
            continue
        assertions = sorted(set(current.assertions) | {"not_null", "unique"})
        definition.add(
            ColumnDef(
                current.name,
                current.data_type,
                True,
                False,
                current.derived,
                tuple(assertions),
            )
        )


def _check_range(check: exp.Expression, column: str) -> str | None:
    lower: str | None = None
    upper: str | None = None
    for node in check.find_all(exp.GTE, exp.GT, exp.LTE, exp.LT):
        left, right = node.this, node.expression
        if not isinstance(left, exp.Column) or clean_ident(left.name) != column:
            continue
        if not isinstance(right, exp.Literal) or right.is_string:
            continue
        if isinstance(node, (exp.GTE, exp.GT)):
            lower = right.this
        else:
            upper = right.this
    if lower is not None and upper is not None:
        return f"range:[{lower},{upper}]"
    return None


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
