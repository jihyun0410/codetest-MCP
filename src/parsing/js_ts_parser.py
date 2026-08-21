"""
JavaScript / TypeScript 파서 (Tree-sitter 기반).

추출 대상
  - Node : File / Class / Method(=function·method·arrow) / Variable / SQL(문자열 쿼리)
  - Edge : Contains / Calls / Uses / Executes

진입점(entrypoint) 판정
  - Express/Koa 스타일 : router.get("/path", handler) / app.post(...)
  - NestJS 데코레이터   : @Get() / @Post() / @Controller()
  - Next.js 라우트 파일 : pages/api/** , app/**/route.ts 의 export 된 핸들러
"""

from __future__ import annotations

import re

from src.db import EdgeType, NodeType
from src.parsing.base import ParsedEdge, ParsedNode, ParseResult
from src.parsing.tree_sitter_loader import (
    child_by_field,
    field_text,
    get_parser,
    iter_descendants,
    iter_descendants_until,
    node_text,
)

#: 파일 확장자 → tree-sitter 문법 코드
EXT_LANGUAGE = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_FUNC_DECLS = {
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "function_signature",
}
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "all", "use", "options", "head"}
_NEST_DECORATORS = {"Get", "Post", "Put", "Delete", "Patch", "All"}
_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)

_FRAMEWORK_IMPORTS: list[tuple[str, str]] = [
    ("@nestjs", "NestJS"),
    ("express", "Express"),
    ("next", "Next.js"),
    ("react", "React"),
    ("vue", "Vue"),
    ("@angular", "Angular"),
    ("typeorm", "TypeORM"),
    ("prisma", "Prisma"),
    ("axios", "Axios"),
]


class JsTsParser:
    """확장자에 따라 javascript / typescript / tsx 문법을 선택한다."""

    def __init__(self, language: str = "typescript") -> None:
        self.language = language

    def parse(self, file_path: str, source: str) -> ParseResult:
        parser = get_parser(self.language)
        if parser is None:
            return _regex_fallback(file_path, source, self.language)
        try:
            return _parse_with_tree_sitter(parser, file_path, source, self.language)
        except Exception as exc:
            result = _regex_fallback(file_path, source, self.language)
            result.warnings.append(f"{file_path}: Tree-sitter 파싱 실패 → 폴백 ({exc})")
            return result


def _parse_with_tree_sitter(parser, file_path: str, source: str, language: str) -> ParseResult:
    src = source.encode("utf-8", errors="ignore")
    root = parser.parse(src).root_node
    result = ParseResult()

    module = _module_id(file_path)
    file_qname = f"file:{file_path}"
    result.nodes.append(
        ParsedNode(
            node_type=NodeType.FILE,
            name=file_path.rsplit("/", 1)[-1],
            qualified_name=file_qname,
            file_path=file_path,
            language=language,
            start_line=1,
            end_line=source.count("\n") + 1,
            signature=f"file {file_path}",
            body=source,
            meta={"module": module},
        )
    )

    # --- import 로 프레임워크 판정 -----------------------------------------
    for node in iter_descendants(root):
        if node.type in {"import_statement", "call_expression"}:
            text = node_text(node, src)
            for needle, label in _FRAMEWORK_IMPORTS:
                if f'"{needle}' in text or f"'{needle}" in text:
                    result.frameworks.add(label)

    #: Next.js 라우트 규약 파일은 export 된 핸들러 전체를 진입점으로 본다.
    is_route_file = bool(
        re.search(r"(^|/)pages/api/", file_path) or re.search(r"(^|/)app/.*/route\.[tj]sx?$", file_path)
    )

    # --- 클래스 -------------------------------------------------------------
    for node in iter_descendants(root):
        if node.type in _CLASS_DECLS:
            _parse_class(node, src, file_path, module, file_qname, language, result)

    # --- 최상위 함수 / 화살표 함수 -------------------------------------------
    for node in iter_descendants(root):
        if node.type in _FUNC_DECLS and not _is_inside_class(node):
            name = field_text(node, "name", src) or "anonymous"
            _emit_function(
                node, src, file_path, module, f"{module}::{name}", name,
                file_qname, language, result, is_route_file,
            )
        elif node.type == "variable_declarator":
            value = child_by_field(node, "value")
            if value is None or value.type not in {"arrow_function", "function_expression", "function"}:
                continue
            name = field_text(node, "name", src) or "anonymous"
            _emit_function(
                value, src, file_path, module, f"{module}::{name}", name,
                file_qname, language, result, is_route_file,
            )

    return result


def _parse_class(node, src, file_path, module, file_qname, language, result: ParseResult) -> None:
    class_name = field_text(node, "name", src) or "AnonymousClass"
    class_qname = f"{module}::{class_name}"
    decorators = _collect_decorators(node, src)
    class_route = _decorator_route(decorators, {"Controller"})
    if any(d["name"] == "Controller" for d in decorators):
        result.frameworks.add("NestJS")

    result.nodes.append(
        ParsedNode(
            node_type=NodeType.CLASS,
            name=class_name,
            qualified_name=class_qname,
            file_path=file_path,
            language=language,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"class {class_name}",
            body=node_text(node, src),
            meta={
                "module": module,
                "decorators": [d["name"] for d in decorators],
                "route": class_route,
            },
        )
    )
    result.edges.append(ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=class_qname))

    body = child_by_field(node, "body")
    if body is None:
        return
    for member in body.children:
        if member.type == "method_definition":
            mname = field_text(member, "name", src) or "anonymous"
            _emit_function(
                member, src, file_path, module, f"{class_qname}.{mname}", mname,
                class_qname, language, result, False, class_route=class_route,
            )
        elif member.type in {"public_field_definition", "field_definition", "property_signature"}:
            pname = field_text(member, "name", src)
            if not pname:
                continue
            pq = f"{class_qname}.{pname}"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.VARIABLE,
                    name=pname,
                    qualified_name=pq,
                    file_path=file_path,
                    language=language,
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    signature=node_text(member, src)[:120],
                    body=node_text(member, src),
                    meta={"owner": class_qname, "scope": "field"},
                )
            )
            result.edges.append(ParsedEdge(class_qname, EdgeType.CONTAINS, target_qname=pq))


def _emit_function(
    node, src, file_path, module, qname, name, parent_qname, language,
    result: ParseResult, is_route_file: bool, class_route: str = "",
) -> None:
    """함수/메서드 노드 1개와 그 본문에서 파생되는 간선을 만든다."""
    params_node = child_by_field(node, "parameters")
    params = node_text(params_node, src) if params_node is not None else "()"
    decorators = _collect_decorators(node, src)
    http_method = next(
        (d["name"].upper() for d in decorators if d["name"] in _NEST_DECORATORS), None
    )
    route = _join_route(class_route, _decorator_route(decorators, _NEST_DECORATORS))

    is_entrypoint = bool(http_method) or (
        is_route_file and name.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HANDLER", "DEFAULT"}
    )

    result.nodes.append(
        ParsedNode(
            node_type=NodeType.METHOD,
            name=name,
            qualified_name=qname,
            file_path=file_path,
            language=language,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"{name}{params}",
            body=node_text(node, src),
            meta={
                "owner": parent_qname,
                "decorators": [d["name"] for d in decorators],
                "entrypoint": is_entrypoint,
                "http_method": http_method,
                "route": route,
            },
        )
    )
    result.edges.append(ParsedEdge(parent_qname, EdgeType.CONTAINS, target_qname=qname))

    body = child_by_field(node, "body")
    if body is None:
        return

    for child in iter_descendants_until(body, {"class_declaration"}):
        if child.type == "call_expression":
            fn = child_by_field(child, "function")
            if fn is None:
                continue
            callee_text = node_text(fn, src)
            simple = callee_text.split(".")[-1].strip()
            result.edges.append(
                ParsedEdge(
                    qname,
                    EdgeType.CALLS,
                    target_hint=simple,
                    meta={
                        "callee": callee_text,
                        "line": child.start_point[0] + 1,
                    },
                )
            )
            # Express 라우트 등록: app.get("/path", handler)
            if "." in callee_text and simple.lower() in _HTTP_VERBS:
                args = child_by_field(child, "arguments")
                route_literal = _first_string_literal(args, src) if args is not None else None
                if route_literal:
                    result.frameworks.add("Express")
                    # 등록을 수행하는 함수 자체를 라우트 진입점으로 승격
                    for existing in result.nodes:
                        if existing.qualified_name == qname:
                            existing.meta["entrypoint"] = True
                            existing.meta["http_method"] = simple.upper()
                            existing.meta["route"] = route_literal

        elif child.type in {"string", "template_string"}:
            literal = node_text(child, src).strip("`'\"")
            if len(literal) > 20 and _SQL_KEYWORD.search(literal):
                sql_qname = f"sql:{qname}@line{child.start_point[0] + 1}"
                result.nodes.append(
                    ParsedNode(
                        node_type=NodeType.SQL,
                        name=f"inline@{child.start_point[0] + 1}",
                        qualified_name=sql_qname,
                        file_path=file_path,
                        language="sql",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=re.sub(r"\s+", " ", literal)[:160],
                        body=literal,
                        meta={"origin": "inline", "operation": _sql_operation(literal)},
                    )
                )
                result.edges.append(
                    ParsedEdge(qname, EdgeType.EXECUTES, target_qname=sql_qname)
                )


# ---------------------------------------------------------------------------
#  보조 함수
# ---------------------------------------------------------------------------
def _collect_decorators(node, src) -> list[dict]:
    """@Get('/x') 형태 데코레이터를 [{name, args}] 로 수집한다."""
    decorators: list[dict] = []
    # tree-sitter 는 데코레이터를 형제(이전 노드) 또는 자식으로 둘 수 있다.
    candidates = list(node.children)
    prev = node.prev_sibling
    while prev is not None and prev.type == "decorator":
        candidates.append(prev)
        prev = prev.prev_sibling
    for child in candidates:
        if child.type != "decorator":
            continue
        text = node_text(child, src).lstrip("@")
        name = re.split(r"[(\s]", text, maxsplit=1)[0].split(".")[-1]
        decorators.append({"name": name, "args": text})
    return decorators


def _decorator_route(decorators: list[dict], names: set[str]) -> str:
    for d in decorators:
        if d["name"] in names:
            match = re.search(r"""['"`]([^'"`]*)['"`]""", d["args"])
            if match:
                return match.group(1)
    return ""


def _first_string_literal(node, src) -> str | None:
    for child in node.children:
        if child.type in {"string", "template_string"}:
            return node_text(child, src).strip("`'\"")
    return None


def _join_route(base: str, sub: str) -> str:
    if not base:
        return sub
    if not sub:
        return base
    return f"{base.rstrip('/')}/{sub.lstrip('/')}"


def _module_id(file_path: str) -> str:
    """확장자를 제거한 모듈 식별자 (논리 경로의 접두사)."""
    return re.sub(r"\.(tsx?|jsx?|mjs|cjs)$", "", file_path)


def _is_inside_class(node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in {"class_body", "class_declaration", "abstract_class_declaration"}:
            return True
        parent = parent.parent
    return False


def _sql_operation(sql: str) -> str:
    match = _SQL_KEYWORD.search(sql)
    return match.group(1).upper() if match else "UNKNOWN"


# ---------------------------------------------------------------------------
#  정규식 폴백
# ---------------------------------------------------------------------------
_RE_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)
_RE_JS_FUNC = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()",
    re.MULTILINE,
)


def _regex_fallback(file_path: str, source: str, language: str) -> ParseResult:
    result = ParseResult()
    module = _module_id(file_path)
    file_qname = f"file:{file_path}"
    result.nodes.append(
        ParsedNode(
            node_type=NodeType.FILE,
            name=file_path.rsplit("/", 1)[-1],
            qualified_name=file_qname,
            file_path=file_path,
            language=language,
            start_line=1,
            end_line=source.count("\n") + 1,
            signature=f"file {file_path}",
            body=source,
            meta={"module": module, "degraded": True},
        )
    )
    for match in _RE_JS_CLASS.finditer(source):
        name = match.group(1)
        qname = f"{module}::{name}"
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.CLASS,
                name=name,
                qualified_name=qname,
                file_path=file_path,
                language=language,
                start_line=source[: match.start()].count("\n") + 1,
                signature=f"class {name}",
                meta={"degraded": True},
            )
        )
        result.edges.append(ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=qname))
    for match in _RE_JS_FUNC.finditer(source):
        name = match.group(1) or match.group(2)
        if not name:
            continue
        qname = f"{module}::{name}"
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.METHOD,
                name=name,
                qualified_name=qname,
                file_path=file_path,
                language=language,
                start_line=source[: match.start()].count("\n") + 1,
                signature=f"{name}(...)",
                meta={"degraded": True},
            )
        )
        result.edges.append(ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=qname))
    return result
