"""
독립 SQL 파일(.sql) 파서 — DML 전용.

정의서의 지원 범위가 SQL(DML) 이므로 DDL(CREATE/ALTER/DROP)은 노드로 만들지 않고
SELECT / INSERT / UPDATE / DELETE / MERGE 구문만 SQL 노드로 승격한다.
"""

from __future__ import annotations

import re

from codetest_mcp.db import EdgeType, NodeType
from codetest_mcp.parsing.base import ParsedEdge, ParsedNode, ParseResult

LANGUAGE = "sql"

_DML = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|WITH)\b", re.IGNORECASE)
_OPERATION = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


class SqlParser:
    language = LANGUAGE

    def parse(self, file_path: str, source: str) -> ParseResult:
        result = ParseResult()

        file_qname = f"file:{file_path}"
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.FILE,
                name=file_path.rsplit("/", 1)[-1],
                qualified_name=file_qname,
                file_path=file_path,
                language=LANGUAGE,
                start_line=1,
                end_line=source.count("\n") + 1,
                signature=f"sql file {file_path}",
                body=source,
                meta={},
            )
        )

        for index, (statement, line_no) in enumerate(_split_statements(source)):
            if not _DML.match(statement):
                continue  # DDL/주석/설정문은 대상 외
            qname = f"sql:{file_path}@{index}"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.SQL,
                    name=f"{_operation(statement).lower()}@L{line_no}",
                    qualified_name=qname,
                    file_path=file_path,
                    language=LANGUAGE,
                    start_line=line_no,
                    end_line=line_no + statement.count("\n"),
                    signature=_signature(statement),
                    body=statement,
                    meta={
                        "origin": "sql-file",
                        "operation": _operation(statement),
                        "tables": _extract_tables(statement),
                    },
                )
            )
            result.edges.append(
                ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=qname)
            )

        return result


def _split_statements(source: str) -> list[tuple[str, int]]:
    """
    주석을 제거하고 세미콜론 기준으로 구문을 나눈다.

    문자열 리터럴 안의 세미콜론을 보호하기 위해 간단한 상태 머신을 사용한다.
    """
    cleaned = re.sub(r"--[^\n]*", "", source)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)

    statements: list[tuple[str, int]] = []
    buffer: list[str] = []
    line_no = 1
    start_line = 1
    in_string = False
    quote_char = ""

    for char in cleaned:
        if char == "\n":
            line_no += 1
        if in_string:
            buffer.append(char)
            if char == quote_char:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote_char = char
            buffer.append(char)
            continue
        if char == ";":
            text = "".join(buffer).strip()
            if text:
                statements.append((text, start_line))
            buffer = []
            start_line = line_no
            continue
        if not buffer and char.strip():
            start_line = line_no
        buffer.append(char)

    tail = "".join(buffer).strip()
    if tail:
        statements.append((tail, start_line))
    return statements


def _operation(sql: str) -> str:
    match = _OPERATION.search(sql)
    return match.group(1).upper() if match else "UNKNOWN"


def _signature(sql: str, limit: int = 160) -> str:
    one_line = re.sub(r"\s+", " ", sql).strip()
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


def _extract_tables(sql: str) -> list[str]:
    pattern = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE
    )
    seen: list[str] = []
    for name in pattern.findall(sql):
        if name.lower() not in {t.lower() for t in seen}:
            seen.append(name)
    return seen
