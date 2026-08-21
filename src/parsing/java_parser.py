"""
Java 파서 (Tree-sitter 기반).

추출 대상
  - Node : File / Class(=class·interface·enum·record) / Method / Variable(=field) / SQL(@Query)
  - Edge : Contains(File→Class→Method/Field) / Calls(Method→Method)
           / Uses(Method→Variable·Class) / Executes(Method→SQL)

프레임워크 인식
  - Spring Boot / Spring MVC / Spring Security / Spring Data JPA / MyBatis
  - @RestController·@Controller + @GetMapping 계열을 만나면 해당 Method 를
    **워크플로우 진입점(entrypoint)** 으로 표시한다.
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

LANGUAGE = "java"

#: 클래스로 취급하는 선언 노드
_TYPE_DECLS = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}
_METHOD_DECLS = {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}

#: import 문자열 → 프레임워크 라벨
_FRAMEWORK_IMPORTS: list[tuple[str, str]] = [
    ("org.springframework.boot", "Spring Boot"),
    ("org.springframework.web", "Spring MVC"),
    ("org.springframework.security", "Spring Security"),
    ("org.springframework.data.jpa", "Spring JPA"),
    ("jakarta.persistence", "Spring JPA"),
    ("javax.persistence", "Spring JPA"),
    ("org.apache.ibatis", "MyBatis"),
    ("org.mybatis", "MyBatis"),
    ("org.springframework", "Spring"),
]

#: 애노테이션 → 프레임워크 라벨
_FRAMEWORK_ANNOTATIONS: dict[str, str] = {
    "SpringBootApplication": "Spring Boot",
    "RestController": "Spring MVC",
    "Controller": "Spring MVC",
    "RequestMapping": "Spring MVC",
    "EnableWebSecurity": "Spring Security",
    "PreAuthorize": "Spring Security",
    "Secured": "Spring Security",
    "Entity": "Spring JPA",
    "Repository": "Spring JPA",
    "Query": "Spring JPA",
    "Mapper": "MyBatis",
    "Select": "MyBatis",
    "Insert": "MyBatis",
    "Update": "MyBatis",
    "Delete": "MyBatis",
}

#: HTTP 매핑 애노테이션 → HTTP 메서드
_HTTP_MAPPINGS: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
}

_ENTRYPOINT_CLASS_ANNOTATIONS = {"RestController", "Controller"}
_SQL_ANNOTATIONS = {"Query", "Select", "Insert", "Update", "Delete", "NativeQuery"}
_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


class JavaParser:
    language = LANGUAGE

    def parse(self, file_path: str, source: str) -> ParseResult:
        parser = get_parser(LANGUAGE)
        if parser is None:
            return _regex_fallback(file_path, source)
        try:
            return _parse_with_tree_sitter(parser, file_path, source)
        except Exception as exc:  # 문법 오류가 있는 파일도 수집이 멈추면 안 된다
            result = _regex_fallback(file_path, source)
            result.warnings.append(f"{file_path}: Tree-sitter 파싱 실패 → 폴백 사용 ({exc})")
            return result


# ---------------------------------------------------------------------------
#  Tree-sitter 본 파싱
# ---------------------------------------------------------------------------
def _parse_with_tree_sitter(parser, file_path: str, source: str) -> ParseResult:
    src = source.encode("utf-8", errors="ignore")
    tree = parser.parse(src)
    root = tree.root_node

    result = ParseResult()

    # --- 1) 패키지 / import 수집 ------------------------------------------
    package = ""
    for child in root.children:
        if child.type == "package_declaration":
            package = _strip_semicolon(node_text(child, src).replace("package", "", 1)).strip()
        elif child.type == "import_declaration":
            imported = _strip_semicolon(node_text(child, src).replace("import", "", 1)).strip()
            for prefix, label in _FRAMEWORK_IMPORTS:
                if imported.startswith(prefix):
                    result.frameworks.add(label)
                    break

    # --- 2) File 노드 ------------------------------------------------------
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
            signature=f"file {file_path}",
            body=source,
            meta={"package": package},
        )
    )

    # --- 3) 타입(클래스/인터페이스/enum/record) 순회 ------------------------
    for type_node in iter_descendants(root):
        if type_node.type not in _TYPE_DECLS:
            continue
        _parse_type(type_node, src, file_path, package, file_qname, result)

    return result


def _parse_type(
    type_node,
    src: bytes,
    file_path: str,
    package: str,
    file_qname: str,
    result: ParseResult,
) -> None:
    """클래스 1개와 그 내부 멤버(필드/메서드)를 노드/간선으로 변환한다."""
    class_name = field_text(type_node, "name", src) or "<anonymous>"
    class_qname = f"{package}.{class_name}" if package else class_name

    annotations = _collect_annotations(type_node, src)
    for ann in annotations:
        if ann["name"] in _FRAMEWORK_ANNOTATIONS:
            result.frameworks.add(_FRAMEWORK_ANNOTATIONS[ann["name"]])

    superclass = field_text(type_node, "superclass", src)
    interfaces = field_text(type_node, "interfaces", src)
    # Spring Data JPA Repository 상속 판정
    inherits = f"{superclass or ''} {interfaces or ''}"
    if re.search(r"\b(JpaRepository|CrudRepository|PagingAndSortingRepository)\b", inherits):
        result.frameworks.add("Spring JPA")

    #: 클래스 레벨 기본 라우트 (@RequestMapping("/api/users"))
    class_route = ""
    for ann in annotations:
        if ann["name"] == "RequestMapping":
            class_route = _extract_route(ann["args"])
    is_endpoint_class = any(
        ann["name"] in _ENTRYPOINT_CLASS_ANNOTATIONS for ann in annotations
    )

    class_signature = (
        f"{type_node.type.replace('_declaration', '')} {class_qname}"
        f"{' extends ' + superclass if superclass else ''}"
        f"{' ' + interfaces if interfaces else ''}"
    )

    result.nodes.append(
        ParsedNode(
            node_type=NodeType.CLASS,
            name=class_name,
            qualified_name=class_qname,
            file_path=file_path,
            language=LANGUAGE,
            start_line=type_node.start_point[0] + 1,
            end_line=type_node.end_point[0] + 1,
            signature=class_signature,
            body=node_text(type_node, src),
            meta={
                "package": package,
                "annotations": [a["name"] for a in annotations],
                "superclass": superclass,
                "interfaces": interfaces,
                "route": class_route,
                "endpoint_class": is_endpoint_class,
                "kind": type_node.type.replace("_declaration", ""),
            },
        )
    )
    result.edges.append(
        ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=class_qname)
    )

    body = child_by_field(type_node, "body")
    if body is None:
        return

    #: 필드명 → 타입명. 메서드 호출 대상 추론(this.userService.getUser())에 사용
    field_types: dict[str, str] = {}

    # --- 3-1) 필드(Variable) ----------------------------------------------
    for member in body.children:
        if member.type != "field_declaration":
            continue
        decl_type = field_text(member, "type", src) or "var"
        for declarator in member.children:
            if declarator.type != "variable_declarator":
                continue
            fname = field_text(declarator, "name", src)
            if not fname:
                continue
            field_types[fname] = _base_type(decl_type)
            fq = f"{class_qname}.{fname}"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.VARIABLE,
                    name=fname,
                    qualified_name=fq,
                    file_path=file_path,
                    language=LANGUAGE,
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    signature=f"{decl_type} {fname}",
                    body=node_text(member, src),
                    meta={"owner": class_qname, "type": decl_type, "scope": "field"},
                )
            )
            result.edges.append(
                ParsedEdge(class_qname, EdgeType.CONTAINS, target_qname=fq)
            )
            # 필드 타입이 프로젝트 내 클래스라면 Uses 간선 후보
            result.edges.append(
                ParsedEdge(
                    class_qname,
                    EdgeType.USES,
                    target_hint=_base_type(decl_type),
                    meta={"reason": "field-type"},
                )
            )

    # --- 3-2) 메서드 --------------------------------------------------------
    for member in body.children:
        if member.type not in _METHOD_DECLS:
            continue
        _parse_method(
            member,
            src,
            file_path,
            class_qname,
            class_route,
            is_endpoint_class,
            field_types,
            result,
        )


def _parse_method(
    method_node,
    src: bytes,
    file_path: str,
    class_qname: str,
    class_route: str,
    is_endpoint_class: bool,
    field_types: dict[str, str],
    result: ParseResult,
) -> None:
    method_name = field_text(method_node, "name", src) or "<init>"
    params_node = child_by_field(method_node, "parameters")
    param_types, param_vars = _parse_parameters(params_node, src)
    return_type = field_text(method_node, "type", src) or "void"

    method_qname = f"{class_qname}#{method_name}({','.join(param_types)})"
    signature = f"{return_type} {method_name}({', '.join(param_types)})"

    annotations = _collect_annotations(method_node, src)
    ann_names = [a["name"] for a in annotations]
    for name in ann_names:
        if name in _FRAMEWORK_ANNOTATIONS:
            result.frameworks.add(_FRAMEWORK_ANNOTATIONS[name])

    # HTTP 엔드포인트 여부 판정 → 워크플로우 진입점
    http_method: str | None = None
    route = ""
    for ann in annotations:
        if ann["name"] in _HTTP_MAPPINGS:
            http_method = _HTTP_MAPPINGS[ann["name"]]
            route = _join_route(class_route, _extract_route(ann["args"]))
    is_entrypoint = bool(http_method) or (
        is_endpoint_class and method_name.lower() in {"handle", "index"}
    )
    #: 스케줄러/이벤트 리스너도 실행 진입점으로 취급
    if {"Scheduled", "EventListener", "KafkaListener", "PostConstruct"} & set(ann_names):
        is_entrypoint = True

    result.nodes.append(
        ParsedNode(
            node_type=NodeType.METHOD,
            name=method_name,
            qualified_name=method_qname,
            file_path=file_path,
            language=LANGUAGE,
            start_line=method_node.start_point[0] + 1,
            end_line=method_node.end_point[0] + 1,
            signature=signature,
            body=node_text(method_node, src),
            meta={
                "owner": class_qname,
                "annotations": ann_names,
                "return_type": return_type,
                "params": param_types,
                "entrypoint": is_entrypoint,
                "http_method": http_method,
                "route": route,
            },
        )
    )
    result.edges.append(ParsedEdge(class_qname, EdgeType.CONTAINS, target_qname=method_qname))

    # --- SQL(@Query / MyBatis 애노테이션) 추출 → Executes 간선 ---------------
    for idx, ann in enumerate(annotations):
        if ann["name"] not in _SQL_ANNOTATIONS:
            continue
        sql_text = _extract_string_literal(ann["args"])
        if not sql_text or not _SQL_KEYWORD.search(sql_text):
            continue
        sql_qname = f"sql:{method_qname}@{idx}"
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.SQL,
                name=f"{method_name}@{ann['name']}",
                qualified_name=sql_qname,
                file_path=file_path,
                language="sql",
                start_line=method_node.start_point[0] + 1,
                end_line=method_node.start_point[0] + 1,
                signature=_sql_signature(sql_text),
                body=sql_text,
                meta={
                    "origin": "annotation",
                    "annotation": ann["name"],
                    "tables": _extract_tables(sql_text),
                    "operation": _sql_operation(sql_text),
                },
            )
        )
        result.edges.append(
            ParsedEdge(method_qname, EdgeType.EXECUTES, target_qname=sql_qname)
        )

    # --- MyBatis Mapper 인터페이스: 본문 없는 메서드 → XML statement 연결 ----
    body_node = child_by_field(method_node, "body")
    if body_node is None:
        # 추상/인터페이스 메서드. MyBatis Mapper 라면 XML statement 를 실행한다.
        result.edges.append(
            ParsedEdge(
                method_qname,
                EdgeType.EXECUTES,
                target_hint=f"mapper:{class_qname}.{method_name}",
                meta={"reason": "mybatis-mapper"},
            )
        )
        return

    # --- 메서드 본문: 호출/변수 사용 추출 -----------------------------------
    local_types = dict(field_types)
    local_types.update(param_vars)

    for child in iter_descendants_until(body_node, {"class_declaration", "lambda_expression"}):
        if child.type == "local_variable_declaration":
            decl_type = field_text(child, "type", src) or "var"
            for declarator in child.children:
                if declarator.type == "variable_declarator":
                    vname = field_text(declarator, "name", src)
                    if vname:
                        local_types[vname] = _base_type(decl_type)

        elif child.type == "method_invocation":
            callee = field_text(child, "name", src)
            if not callee:
                continue
            receiver = field_text(child, "object", src)
            hint = _resolve_call_hint(callee, receiver, local_types)
            result.edges.append(
                ParsedEdge(
                    method_qname,
                    EdgeType.CALLS,
                    target_hint=hint,
                    meta={
                        "callee": callee,
                        "receiver": receiver,
                        "line": child.start_point[0] + 1,
                    },
                )
            )

        elif child.type == "object_creation_expression":
            created = field_text(child, "type", src)
            if created:
                result.edges.append(
                    ParsedEdge(
                        method_qname,
                        EdgeType.USES,
                        target_hint=_base_type(created),
                        meta={"reason": "new", "line": child.start_point[0] + 1},
                    )
                )

        elif child.type == "field_access":
            accessed = field_text(child, "field", src)
            if accessed and accessed in field_types:
                result.edges.append(
                    ParsedEdge(
                        method_qname,
                        EdgeType.USES,
                        target_qname=f"{class_qname}.{accessed}",
                        meta={"reason": "field-access"},
                    )
                )

        elif child.type == "identifier":
            name = node_text(child, src)
            if name in field_types:
                result.edges.append(
                    ParsedEdge(
                        method_qname,
                        EdgeType.USES,
                        target_qname=f"{class_qname}.{name}",
                        meta={"reason": "identifier"},
                    )
                )

        elif child.type == "string_literal":
            literal = node_text(child, src).strip('"')
            # 코드에 직접 박힌 SQL 문자열도 SQL 노드로 승격한다.
            if len(literal) > 20 and _SQL_KEYWORD.search(literal):
                sql_qname = f"sql:{method_qname}@line{child.start_point[0] + 1}"
                result.nodes.append(
                    ParsedNode(
                        node_type=NodeType.SQL,
                        name=f"inline@{child.start_point[0] + 1}",
                        qualified_name=sql_qname,
                        file_path=file_path,
                        language="sql",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=_sql_signature(literal),
                        body=literal,
                        meta={
                            "origin": "inline",
                            "tables": _extract_tables(literal),
                            "operation": _sql_operation(literal),
                        },
                    )
                )
                result.edges.append(
                    ParsedEdge(method_qname, EdgeType.EXECUTES, target_qname=sql_qname)
                )


# ---------------------------------------------------------------------------
#  보조 함수
# ---------------------------------------------------------------------------
def _collect_annotations(node, src: bytes) -> list[dict]:
    """선언 노드에 붙은 애노테이션을 [{name, args}] 로 수집한다."""
    result: list[dict] = []
    for child in node.children:
        if child.type != "modifiers":
            continue
        for mod in child.children:
            if mod.type in {"annotation", "marker_annotation"}:
                name = field_text(mod, "name", src) or ""
                args_node = child_by_field(mod, "arguments")
                args = node_text(args_node, src) if args_node is not None else ""
                result.append({"name": name.lstrip("@").split(".")[-1], "args": args})
    return result


def _parse_parameters(params_node, src: bytes) -> tuple[list[str], dict[str, str]]:
    """formal_parameters → (타입 목록, {파라미터명: 타입}) 로 변환."""
    types: list[str] = []
    variables: dict[str, str] = {}
    if params_node is None:
        return types, variables
    for param in params_node.children:
        if param.type not in {"formal_parameter", "spread_parameter", "receiver_parameter"}:
            continue
        ptype = field_text(param, "type", src) or "Object"
        pname = field_text(param, "name", src)
        types.append(_base_type(ptype))
        if pname:
            variables[pname] = _base_type(ptype)
    return types, variables


def _resolve_call_hint(
    callee: str, receiver: str | None, known_types: dict[str, str]
) -> str:
    """
    호출 대상 힌트를 만든다.

    - receiver 가 알려진 변수/필드면 그 타입을 붙여 `Type#method` 로 좁힌다.
    - receiver 가 대문자로 시작하면 정적 호출로 보고 그대로 사용한다.
    - 그 외에는 메서드 단순명만 남겨 GraphBuilder 가 후보를 탐색하게 한다.
    """
    if not receiver:
        return callee
    receiver = receiver.replace("this.", "").strip()
    if receiver in known_types:
        return f"{known_types[receiver]}#{callee}"
    if receiver and receiver[0].isupper():
        return f"{receiver.split('.')[-1]}#{callee}"
    return callee


def _base_type(type_text: str) -> str:
    """제네릭/배열 표기를 제거한 기본 타입명 (List<UserDto> → UserDto)."""
    text = type_text.strip()
    inner = re.search(r"<\s*([A-Za-z0-9_.]+)", text)
    if inner and text.split("<")[0].strip() in {
        "List", "Set", "Collection", "Optional", "Iterable", "Page", "Flux", "Mono",
    }:
        text = inner.group(1)
    text = text.split("<")[0].replace("[]", "").strip()
    return text.split(".")[-1]


def _strip_semicolon(text: str) -> str:
    return text.replace(";", "").strip()


def _extract_route(args: str) -> str:
    """@RequestMapping("/api/users") 또는 (value = "/x") 에서 경로를 추출."""
    match = re.search(r'"([^"]*)"', args or "")
    return match.group(1) if match else ""


def _join_route(base: str, sub: str) -> str:
    if not base:
        return sub
    if not sub:
        return base
    return f"{base.rstrip('/')}/{sub.lstrip('/')}"


def _extract_string_literal(args: str) -> str | None:
    """애노테이션 인자에서 (여러 조각으로 나뉜) 문자열 리터럴을 이어 붙인다."""
    if not args:
        return None
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', args)
    if not parts:
        return None
    return " ".join(p.replace('\\"', '"') for p in parts).strip()


def _sql_operation(sql: str) -> str:
    match = _SQL_KEYWORD.search(sql)
    return match.group(1).upper() if match else "UNKNOWN"


def _extract_tables(sql: str) -> list[str]:
    """FROM / JOIN / INTO / UPDATE 뒤의 테이블명을 추출한다."""
    pattern = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE
    )
    seen: list[str] = []
    for name in pattern.findall(sql):
        lowered = name.lower()
        if lowered not in {t.lower() for t in seen}:
            seen.append(name)
    return seen


def _sql_signature(sql: str, limit: int = 160) -> str:
    """SQL 을 한 줄로 정규화한 시그니처 (Tier 3 컨텍스트용)."""
    one_line = re.sub(r"\s+", " ", sql).strip()
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


# ---------------------------------------------------------------------------
#  Tree-sitter 미설치 환경용 정규식 폴백
# ---------------------------------------------------------------------------
_RE_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_RE_TYPE = re.compile(
    r"^\s*(?:public|protected|private|abstract|final|static|\s)*"
    r"(class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)
_RE_METHOD = re.compile(
    r"^[ \t]*(?:@\w+[^\n]*\n[ \t]*)*"
    r"(?:public|protected|private|static|final|synchronized|abstract|native|\s)+"
    r"[\w<>\[\],.?\s]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws [\w,\s.]+)?\s*[{;]",
    re.MULTILINE,
)


def _regex_fallback(file_path: str, source: str) -> ParseResult:
    """
    Tree-sitter 없이도 최소한의 File/Class/Method 골격을 만든다.

    호출 관계(Calls)까지는 신뢰할 수 없으므로 Contains 만 생성한다.
    """
    result = ParseResult()
    package_match = _RE_PACKAGE.search(source)
    package = package_match.group(1) if package_match else ""

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
            signature=f"file {file_path}",
            body=source,
            meta={"package": package, "degraded": True},
        )
    )

    lines = source.splitlines()
    for type_match in _RE_TYPE.finditer(source):
        class_name = type_match.group(2)
        class_qname = f"{package}.{class_name}" if package else class_name
        line_no = source[: type_match.start()].count("\n") + 1
        result.nodes.append(
            ParsedNode(
                node_type=NodeType.CLASS,
                name=class_name,
                qualified_name=class_qname,
                file_path=file_path,
                language=LANGUAGE,
                start_line=line_no,
                end_line=len(lines),
                signature=f"{type_match.group(1)} {class_qname}",
                body=None,
                meta={"package": package, "degraded": True},
            )
        )
        result.edges.append(
            ParsedEdge(file_qname, EdgeType.CONTAINS, target_qname=class_qname)
        )

        for method_match in _RE_METHOD.finditer(source):
            mname = method_match.group(1)
            if mname in {"if", "for", "while", "switch", "catch", "return", "new"}:
                continue
            mline = source[: method_match.start()].count("\n") + 1
            mqname = f"{class_qname}#{mname}()"
            result.nodes.append(
                ParsedNode(
                    node_type=NodeType.METHOD,
                    name=mname,
                    qualified_name=mqname,
                    file_path=file_path,
                    language=LANGUAGE,
                    start_line=mline,
                    end_line=mline,
                    signature=f"{mname}({method_match.group(2)})",
                    body=None,
                    meta={"owner": class_qname, "degraded": True},
                )
            )
            result.edges.append(
                ParsedEdge(class_qname, EdgeType.CONTAINS, target_qname=mqname)
            )
        break  # 폴백에서는 파일당 최상위 타입 1개만 처리

    return result
