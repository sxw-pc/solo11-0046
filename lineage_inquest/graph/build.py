"""Build the column-level graph and its deterministic payloads."""

from __future__ import annotations

from ..models import (
    ColumnMapping,
    Edge,
    EdgeLocation,
    Graph,
    Node,
    TableDef,
)
from ..naming import column_of, table_of
from .stable import edge_id, node_id


def build_graph(tables: dict[str, TableDef], mappings: list[ColumnMapping]) -> Graph:
    nodes: dict[str, Node] = {}
    for table in tables.values():
        for column in table.ordered_columns():
            origin = "derived" if column.derived else "ddl"
            nodes[column_name(table.name, column.name)] = Node(
                name=column_name(table.name, column.name),
                table=table.name,
                column=column.name,
                data_type=column.data_type,
                nullable=column.nullable,
                primary_key=column.primary_key,
                origin=origin,
                assertions=tuple(column.assertions),
            )

    grouped: dict[tuple[str, str, str], set[EdgeLocation]] = {}
    for mapping in mappings:
        for source in mapping.sources:
            target = mapping.target
            if source not in nodes:
                nodes[source] = referenced_node(source)
            if target not in nodes:
                nodes[target] = referenced_node(target)
            key = (source, target, mapping.kind)
            grouped.setdefault(key, set()).add(
                EdgeLocation(mapping.script, mapping.line, mapping.statement)
            )

    edges = [
        Edge(source=source, target=target, kind=kind, locations=tuple(sorted(locations)))
        for (source, target, kind), locations in grouped.items()
    ]
    edges.sort(key=lambda edge: (edge.target, edge.source, edge.kind))
    ordered_nodes = tuple(nodes[name] for name in sorted(nodes))
    return Graph(nodes=ordered_nodes, edges=tuple(edges), tables=len(tables))


def referenced_node(name: str) -> Node:
    return Node(
        name=name,
        table=table_of(name),
        column=column_of(name),
        data_type="unknown",
        nullable=True,
        origin="referenced",
    )


def column_name(table: str, column: str) -> str:
    return f"{table}.{column}"


def node_payload(node: Node) -> dict:
    return {
        "id": node_id(node.name),
        "name": node.name,
        "table": node.table,
        "column": node.column,
        "data_type": node.data_type,
        "nullable": node.nullable,
        "primary_key": node.primary_key,
        "origin": node.origin,
        "assertions": list(node.assertions),
    }


def edge_payload(edge: Edge) -> dict:
    scripts = sorted({location.script for location in edge.locations})
    return {
        "id": edge_id(edge.source, edge.target, edge.kind),
        "source": edge.source,
        "target": edge.target,
        "kind": edge.kind,
        "scripts": scripts,
        "locations": [
            {
                "script": location.script,
                "line": location.line,
                "statement": location.statement,
            }
            for location in edge.locations
        ],
    }
