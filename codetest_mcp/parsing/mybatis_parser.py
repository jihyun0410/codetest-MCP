"""
MyBatis Mapper XML 파서.

`<mapper namespace="com.demo.UserMapper">` 안의 select/insert/update/delete 를
각각 **SQL 노드**로 만든다. 노드의 논리 경로는 `mapper:<namespace>.<id>` 규칙을
따르며, Java Mapper 인터페이스 파서가 만든 Executes 간선의 target_hint 와
정확히 일치하도록 설계되어 있다.

  Java  : UserMapper#selectUser  --Executes--> hint "mapper:com.demo.UserMapper.selectUser"
  XML   : SQL 노드 qualified_name = "mapper:com.demo.UserMapper.selectUser"
  → GraphBuilder 의 심볼 해석 단계에서 두 노드가 연결된다.
"""

from __future__ import annotations

import re

from codetest_mcp.db import EdgeType, NodeType
from codetest_mcp.parsing.base import ParsedEdge, ParsedNode, ParseResult

LANGUAGE = "xml"

_STATEMENT_TAGS = {"select", "insert", "update", "delete"}
_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


class MyBatisXmlParser:
    language = LANGUAGE

    def parse(self, file_path: str, source: str) -> ParseResult:
        result = ParseResult()

        # MyBatis Mapper XML 인지 먼저 확인 — 그 외 XML(pom.xml 등)은 대상 아님
        if not is_mybatis_mapper(source):
            return result

        result.frameworks.add("MyBatis")

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
                signature=f"mybatis mapper {file_path}",
                body=source,
                meta={"kind": "mybatis-mapper"},
            )
        )

        try:
            statements = _parse_with_lxml(source)
        except Exception as exc:
            statements = _parse_with_regex(source)
            result.warnings.append(f"{file_path}: lxml 파싱 실패 → 정규식 폴백 ({exc})")

        namespace = statements.get("namespace", "")
        for stmt in statements.get("items", []):
            stmt_id = stmt["id"]
            sql_text = stmt["sql"]
            qname = f"mapper:{namespace}.{stmt_id}" if namespace else f"mapper:{file_path}.{stmt_id}"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.SQL,
                    name=stmt_id,
                    qualified_name=qname,
                    file_path=file_path,
                    language="sql",
                    start_line=stmt.get("line"),
                    end_line=stmt.get("line"),
                    signature=_signature(sql_text),
                    body=sql_text,
                    meta={
                        "origin": "mybatis",
                        "namespace": namespace,
                        "statement_type": stmt["tag"].upper(),
                        "operation": _operation(sql_text, stmt["tag"]),
                        "tables": _extract_tables(sql_text),
                        "parameter_type": stmt.get("parameterType"),
                        "result_type": stmt.get("resultType"),
                    },
                )
            )
            result.edges.append(
                ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=qname)
            )

        return result


def is_mybatis_mapper(source: str) -> bool:
    """DOCTYPE 또는 루트 태그로 MyBatis Mapper XML 여부를 판정한다."""
    head = source[:2000].lower()
    return ("mybatis" in head and "mapper" in head) or bool(
        re.search(r"<\s*mapper\s+[^>]*namespace\s*=", source[:4000], re.IGNORECASE)
    )


def _parse_with_lxml(source: str) -> dict:
    from lxml import etree  # 지역 import: lxml 미설치 환경에서도 폴백 가능

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(source.encode("utf-8", errors="ignore"), parser=parser)
    if root is None:
        return {"namespace": "", "items": []}

    namespace = root.get("namespace", "") or ""
    items: list[dict] = []
    for element in root.iter():
        tag = etree.QName(element).localname.lower() if element.tag is not etree.Comment else ""
        if tag not in _STATEMENT_TAGS:
            continue
        stmt_id = element.get("id")
        if not stmt_id:
            continue
        items.append(
            {
                "id": stmt_id,
                "tag": tag,
                "sql": _element_text(element),
                "line": element.sourceline,
                "parameterType": element.get("parameterType"),
                "resultType": element.get("resultType") or element.get("resultMap"),
            }
        )
    return {"namespace": namespace, "items": items}


def _element_text(element) -> str:
    """<if>, <foreach> 등 동적 태그를 포함한 전체 텍스트를 평문 SQL 로 합친다."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


_RE_STMT = re.compile(
    r"<\s*(select|insert|update|delete)\b[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_RE_NAMESPACE = re.compile(r"<\s*mapper\b[^>]*namespace\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _parse_with_regex(source: str) -> dict:
    namespace_match = _RE_NAMESPACE.search(source)
    items = []
    for match in _RE_STMT.finditer(source):
        body = re.sub(r"<[^>]+>", " ", match.group(3))
        items.append(
            {
                "id": match.group(2),
                "tag": match.group(1).lower(),
                "sql": re.sub(r"\s+", " ", body).strip(),
                "line": source[: match.start()].count("\n") + 1,
            }
        )
    return {"namespace": namespace_match.group(1) if namespace_match else "", "items": items}


def _signature(sql: str, limit: int = 160) -> str:
    one_line = re.sub(r"\s+", " ", sql).strip()
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


def _operation(sql: str, tag: str) -> str:
    match = _SQL_KEYWORD.search(sql)
    return match.group(1).upper() if match else tag.upper()


def _extract_tables(sql: str) -> list[str]:
    pattern = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE
    )
    seen: list[str] = []
    for name in pattern.findall(sql):
        if name.lower() not in {t.lower() for t in seen}:
            seen.append(name)
    return seen
