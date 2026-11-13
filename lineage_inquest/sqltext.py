"""Small, dependency-free SQL text helpers.

The scanner only needs statement boundaries and decent line numbers. It does
not try to be a lexer: sqlglot remains the sole SQL parser. This splitter
understands strings, quoted identifiers, line comments, and block comments so
semicolons in those constructs do not split a statement.
"""

from __future__ import annotations

import re

from .models import SqlUnit

_LINE_START = re.compile(r"^[ \t\r\n﻿]*")


def split_sql(text: str, script: str) -> list[SqlUnit]:
    units: list[SqlUnit] = []
    buf: list[str] = []
    started = False
    start_line = 1
    line = 1
    index = 0
    i = 0
    n = len(text)
    in_single = in_double = in_backtick = False
    in_block = in_line = False

    def finish() -> None:
        nonlocal buf, started, index
        chunk = "".join(buf).strip()
        if started and chunk:
            index += 1
            units.append(SqlUnit(script, index, start_line, chunk))
        buf = []
        started = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line:
            if ch == "\n":
                in_line = False
                line += 1
                if started:
                    buf.append("\n")
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                if started:
                    buf.append(" ")
                i += 2
                continue
            if ch == "\n":
                line += 1
                if started:
                    buf.append("\n")
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            if ch == "\n":
                line += 1
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"' and nxt == '"':
                buf.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_double = False
            if ch == "\n":
                line += 1
            i += 1
            continue
        if in_backtick:
            buf.append(ch)
            if ch == "`":
                in_backtick = False
            if ch == "\n":
                line += 1
            i += 1
            continue

        if ch == "-" and nxt == "-":
            if started and buf and not buf[-1].isspace():
                buf.append(" ")
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            if started and buf and not buf[-1].isspace():
                buf.append(" ")
            in_block = True
            i += 2
            continue
        if ch == "'":
            if not started:
                started, start_line = True, line
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            if not started:
                started, start_line = True, line
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "`":
            if not started:
                started, start_line = True, line
            in_backtick = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            finish()
            i += 1
            continue
        if not started and ch.isspace():
            if ch == "\n":
                line += 1
            i += 1
            continue
        if not started:
            started, start_line = True, line
        buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1

    finish()
    return units


def locate_line(text: str, start_line: int, pattern: str) -> int:
    """Return the first matching line inside a statement, or its start line."""
    regex = re.compile(pattern, re.IGNORECASE)
    for offset, line_text in enumerate(text.splitlines()):
        if regex.search(line_text):
            return start_line + offset
    return start_line


def snippet(text: str, pattern: str | None = None, limit: int = 160) -> str:
    for line_text in text.splitlines():
        stripped = line_text.strip()
        if not stripped:
            continue
        if pattern is None or re.search(pattern, stripped, re.IGNORECASE):
            return stripped[:limit]
    return text.strip().replace("\n", " ")[:limit]
