"""
Python 파서.

1차: Tree-sitter(python 문법)
2차: 표준 라이브러리 `ast` 폴백 — Python 소스에 한해 표준 AST 가 100% 정확하므로
     Tree-sitter 문법이 없는 환경에서도 품질 저하 없이 동작한다.

두 경로 모두 LLM 토큰을 사용하지 않는 순수 AST 분석이다.
"""

from __future__ import annotations

import ast as pyast
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

LANGUAGE = "python"

_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
_ROUTE_DECORATOR = re.compile(
    r"@\s*[\w.]*\.?(route|get|post|put|delete|patch|websocket)\s*\(", re.IGNORECASE
)
_FRAMEWORK_MODULES: list[tuple[str, str]] = [
    ("fastapi", "FastAPI"),
    ("flask", "Flask"),
    ("django", "Django"),
    ("sqlalchemy", "SQLAlchemy"),
    ("pydantic", "Pydantic"),
    ("celery", "Celery"),
]


class PythonParser:
    language = LANGUAGE

    def parse(self, file_path: str, source: str) -> ParseResult:
        parser = get_parser(LANGUAGE)
        if parser is not None:
            try:
                return _parse_with_tree_sitter(parser, file_path, source)
            except Exception as exc:
                result = _parse_with_stdlib_ast(file_path, source)
                result.warnings.append(f"{file_path}: Tree-sitter 실패 → stdlib ast 사용 ({exc})")
                return result
        return _parse_with_stdlib_ast(file_path, source)


# ---------------------------------------------------------------------------
#  Tree-sitter 경로
# ---------------------------------------------------------------------------
def _parse_with_tree_sitter(parser, file_path: str, source: str) -> ParseResult:
    src = source.encode("utf-8", errors="ignore")
    root = parser.parse(src).root_node
    result = ParseResult()

    module = _module_id(file_path)
    file_qname = f"file:{file_path}"
    result.nodes.append(_file_node(file_path, source, module))

    for node in iter_descendants(root):
        if node.type in {"import_statement", "import_from_statement"}:
            text = node_text(node, src).lower()
            for needle, label in _FRAMEWORK_MODULES:
                if needle in text:
                    result.frameworks.add(label)

    for node in iter_descendants(root):
        target = _unwrap_decorated(node)
        if target is None:
            continue
        real, decorators = target

        if real.type == "class_definition":
            _emit_class_ts(real, decorators, src, file_path, module, file_qname, result)
        elif real.type == "function_definition" and not _inside_class(real):
            name = field_text(real, "name", src) or "anonymous"
            _emit_function_ts(
                real, decorators, src, file_path, module,
                f"{module}::{name}", name, file_qname, result,
            )

    return result


def _unwrap_decorated(node):
    """decorated_definition 을 (실제 선언, 데코레이터 텍스트 목록) 으로 푼다."""
    if node.type == "decorated_definition":
        inner = child_by_field(node, "definition")
        if inner is None:
            return None
        decos = [c for c in node.children if c.type == "decorator"]
        return inner, decos
    if node.type in {"class_definition", "function_definition"}:
        if node.parent is not None and node.parent.type == "decorated_definition":
            return None  # 상위 decorated_definition 처리 시 이미 다룬다
        return node, []
    return None


def _emit_class_ts(node, decorators, src, file_path, module, file_qname, result) -> None:
    name = field_text(node, "name", src) or "AnonymousClass"
    qname = f"{module}::{name}"
    bases = field_text(node, "superclasses", src) or ""
    result.nodes.append(
        ParsedNode(
            node_type=NodeType.CLASS,
            name=name,
            qualified_name=qname,
            file_path=file_path,
            language=LANGUAGE,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"class {name}{bases}",
            body=node_text(node, src),
            meta={
                "module": module,
                "bases": bases,
                "decorators": [node_text(d, src) for d in decorators],
            },
        )
    )
    result.edges.append(ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=qname))

    body = child_by_field(node, "body")
    if body is None:
        return
    for member in body.children:
        inner = _unwrap_decorated(member)
        if inner is None:
            continue
        real, decos = inner
        if real.type != "function_definition":
            continue
        mname = field_text(real, "name", src) or "anonymous"
        _emit_function_ts(
            real, decos, src, file_path, module, f"{qname}.{mname}", mname, qname, result
        )


def _emit_function_ts(
    node, decorators, src, file_path, module, qname, name, parent_qname, result
) -> None:
    params = field_text(node, "parameters", src) or "()"
    deco_texts = [node_text(d, src) for d in decorators]
    http_method, route = _route_from_decorators(deco_texts)
    is_entrypoint = bool(http_method) or name == "main"

    result.nodes.append(
        ParsedNode(
            node_type=NodeType.METHOD,
            name=name,
            qualified_name=qname,
            file_path=file_path,
            language=LANGUAGE,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"def {name}{params}",
            body=node_text(node, src),
            meta={
                "owner": parent_qname,
                "decorators": deco_texts,
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
    for child in iter_descendants_until(body, {"class_definition", "function_definition"}):
        if child.type == "call":
            fn = child_by_field(child, "function")
            if fn is None:
                continue
            callee = node_text(fn, src)
            result.edges.append(
                ParsedEdge(
                    qname,
                    EdgeType.CALLS,
                    target_hint=callee.split(".")[-1].strip(),
                    meta={"callee": callee, "line": child.start_point[0] + 1},
                )
            )
        elif child.type == "string":
            literal = node_text(child, src).strip("'\"")
            if len(literal) > 20 and _SQL_KEYWORD.search(literal):
                _emit_sql(result, qname, file_path, literal, child.start_point[0] + 1)


# ---------------------------------------------------------------------------
#  stdlib ast 폴백 경로
# ---------------------------------------------------------------------------
def _parse_with_stdlib_ast(file_path: str, source: str) -> ParseResult:
    result = ParseResult()
    module = _module_id(file_path)
    file_qname = f"file:{file_path}"
    result.nodes.append(_file_node(file_path, source, module))

    try:
        tree = pyast.parse(source)
    except SyntaxError as exc:
        result.warnings.append(f"{file_path}: SyntaxError — 파일 노드만 생성 ({exc})")
        return result

    lines = source.splitlines()

    def source_of(node) -> str:
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", start + 1)
        return "\n".join(lines[start:end])

    for node in pyast.walk(tree):
        if isinstance(node, (pyast.Import, pyast.ImportFrom)):
            text = (getattr(node, "module", "") or "") + " ".join(
                a.name for a in getattr(node, "names", [])
            )
            for needle, label in _FRAMEWORK_MODULES:
                if needle in text.lower():
                    result.frameworks.add(label)

    def handle_function(node, parent_qname: str, name_prefix: str) -> None:
        qname = f"{name_prefix}{node.name}"
        deco_texts = [_deco_text(d) for d in node.decorator_list]
        http_method, route = _route_from_decorators(deco_texts)
        args = ", ".join(a.arg for a in node.args.args)
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.METHOD,
                name=node.name,
                qualified_name=qname,
                file_path=file_path,
                language=LANGUAGE,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=f"def {node.name}({args})",
                body=source_of(node),
                meta={
                    "owner": parent_qname,
                    "decorators": deco_texts,
                    "entrypoint": bool(http_method) or node.name == "main",
                    "http_method": http_method,
                    "route": route,
                },
            )
        )
        result.edges.append(ParsedEdge(parent_qname, EdgeType.CONTAINS, target_qname=qname))

        for sub in pyast.walk(node):
            if isinstance(sub, pyast.Call):
                callee = _call_name(sub.func)
                if callee:
                    result.edges.append(
                        ParsedEdge(
                            qname,
                            EdgeType.CALLS,
                            target_hint=callee.split(".")[-1],
                            meta={"callee": callee, "line": getattr(sub, "lineno", None)},
                        )
                    )
            elif isinstance(sub, pyast.Constant) and isinstance(sub.value, str):
                if len(sub.value) > 20 and _SQL_KEYWORD.search(sub.value):
                    _emit_sql(result, qname, file_path, sub.value, getattr(sub, "lineno", 1))

    for node in tree.body:
        if isinstance(node, pyast.ClassDef):
            cq = f"{module}::{node.name}"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.CLASS,
                    name=node.name,
                    qualified_name=cq,
                    file_path=file_path,
                    language=LANGUAGE,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    signature=f"class {node.name}",
                    body=source_of(node),
                    meta={"module": module, "bases": [_call_name(b) for b in node.bases]},
                )
            )
            result.edges.append(ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=cq))
            for member in node.body:
                if isinstance(member, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
                    handle_function(member, cq, f"{cq}.")
                elif isinstance(member, pyast.AnnAssign) and isinstance(member.target, pyast.Name):
                    vq = f"{cq}.{member.target.id}"
                    result.nodes.append(
                        ParsedNode(
                            node_type=NodeType.VARIABLE,
                            name=member.target.id,
                            qualified_name=vq,
                            file_path=file_path,
                            language=LANGUAGE,
                            start_line=member.lineno,
                            signature=source_of(member)[:120],
                            body=source_of(member),
                            meta={"owner": cq, "scope": "field"},
                        )
                    )
                    result.edges.append(ParsedEdge(cq, EdgeType.CONTAINS, target_qname=vq))
        elif isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            handle_function(node, file_qname, f"{module}::")

    return result


# ---------------------------------------------------------------------------
#  공통 보조
# ---------------------------------------------------------------------------
def _file_node(file_path: str, source: str, module: str) -> ParsedNode:
    return ParsedNode(
        node_type=NodeType.FILE,
        name=file_path.rsplit("/", 1)[-1],
        qualified_name=f"file:{file_path}",
        file_path=file_path,
        language=LANGUAGE,
        start_line=1,
        end_line=source.count("\n") + 1,
        signature=f"file {file_path}",
        body=source,
        meta={"module": module},
    )


def _emit_sql(result: ParseResult, owner_qname: str, file_path: str, sql: str, line: int) -> None:
    sql_qname = f"sql:{owner_qname}@line{line}"
    result.nodes.append(
        ParsedNode(
            node_type=NodeType.SQL,
            name=f"inline@{line}",
            qualified_name=sql_qname,
            file_path=file_path,
            language="sql",
            start_line=line,
            end_line=line,
            signature=re.sub(r"\s+", " ", sql)[:160],
            body=sql,
            meta={"origin": "inline", "operation": _sql_operation(sql)},
        )
    )
    result.edges.append(ParsedEdge(owner_qname, EdgeType.EXECUTES, target_qname=sql_qname))


def _route_from_decorators(decorators: list[str]) -> tuple[str | None, str]:
    """@app.get("/users") 같은 데코레이터에서 (HTTP 메서드, 경로) 를 추출."""
    for deco in decorators:
        match = _ROUTE_DECORATOR.search(deco)
        if not match:
            continue
        verb = match.group(1).upper()
        path_match = re.search(r"""['"]([^'"]*)['"]""", deco)
        return ("ANY" if verb == "ROUTE" else verb), (path_match.group(1) if path_match else "")
    return None, ""


def _deco_text(node) -> str:
    try:
        return "@" + pyast.unparse(node)
    except Exception:
        return "@<decorator>"


def _call_name(node) -> str:
    """ast 노드에서 점 표기 호출명을 복원한다."""
    if isinstance(node, pyast.Name):
        return node.id
    if isinstance(node, pyast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}" if node.value else node.attr
    if isinstance(node, pyast.Call):
        return _call_name(node.func)
    return ""


def _module_id(file_path: str) -> str:
    return re.sub(r"\.py$", "", file_path).replace("/", ".")


def _inside_class(node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "class_definition":
            return True
        parent = parent.parent
    return False


def _sql_operation(sql: str) -> str:
    match = _SQL_KEYWORD.search(sql)
    return match.group(1).upper() if match else "UNKNOWN"
