"""
AST 기반 문법 검증 (100% 팩트).

- Python : 표준 `compile()` 로 SyntaxError 를 정확히 검출
- 그 외  : Tree-sitter 파스 트리에서 `ERROR` / `MISSING` 노드를 찾아 보고

추론이 아니라 파서가 확정한 사실만 보고하므로 오탐이 없다.
"""

from __future__ import annotations

from functools import lru_cache

from codetest_mcp.models import FileInput, Finding

#: 파일당 보고 상한 (구문 오류 1개가 트리 전체를 오염시킬 수 있어 상한을 둔다)
MAX_FINDINGS_PER_FILE = 15

_GRAMMARS = {
    "java": "java",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "tsx",
    "python": "python",
    "xml": "xml",
    "sql": "sql",
}


@lru_cache(maxsize=16)
def _get_parser(language: str):
    grammar = _GRAMMARS.get(language)
    if grammar is None:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        return get_parser(grammar)
    except Exception:
        return None


def available_grammars() -> dict[str, bool]:
    """health 도구용 — 어떤 문법이 실제로 로드되는지 보고한다."""
    return {name: _get_parser(name) is not None for name in _GRAMMARS}


def run(files: list[FileInput]) -> list[Finding]:
    findings: list[Finding] = []
    for file_input in files:
        if file_input.language == "python":
            findings.extend(_check_python(file_input))
        else:
            findings.extend(_check_tree_sitter(file_input))
    return findings


def _check_python(file_input: FileInput) -> list[Finding]:
    try:
        compile(file_input.content, file_input.path, "exec")
        return []
    except SyntaxError as exc:
        return [
            Finding(
                file_path=file_input.path,
                line=exc.lineno,
                severity="error",
                rule="python-syntax-error",
                message=f"SyntaxError: {exc.msg}"
                + (f" (offset {exc.offset})" if exc.offset else ""),
                source="ast",
            )
        ]
    except (ValueError, MemoryError, RecursionError) as exc:
        return [
            Finding(
                file_path=file_input.path,
                severity="error",
                rule="python-compile-error",
                message=f"{type(exc).__name__}: {exc}",
                source="ast",
            )
        ]


def _check_tree_sitter(file_input: FileInput) -> list[Finding]:
    parser = _get_parser(file_input.language)
    if parser is None:
        return []

    try:
        tree = parser.parse(file_input.content.encode("utf-8", errors="ignore"))
    except Exception as exc:
        return [
            Finding(
                file_path=file_input.path,
                severity="error",
                rule="ast-parse-failure",
                message=f"AST 파싱 실패: {exc}",
                source="ast",
            )
        ]

    findings: list[Finding] = []
    stack = [tree.root_node]
    while stack and len(findings) < MAX_FINDINGS_PER_FILE:
        node = stack.pop()

        if node.type == "ERROR":
            findings.append(
                Finding(
                    file_path=file_input.path,
                    line=node.start_point[0] + 1,
                    severity="error",
                    rule="ast-parse-error",
                    message=(
                        "구문 오류로 파싱에 실패한 구간입니다: "
                        f"`{_snippet(file_input.content, node)}`"
                    ),
                    source="ast",
                )
            )
            continue  # ERROR 하위는 신뢰할 수 없으므로 더 내려가지 않는다

        if getattr(node, "is_missing", False):
            findings.append(
                Finding(
                    file_path=file_input.path,
                    line=node.start_point[0] + 1,
                    severity="error",
                    rule="ast-missing-token",
                    message=f"토큰 누락: '{node.type}' 이(가) 필요합니다.",
                    source="ast",
                )
            )
            continue

        # has_error 가 False 인 서브트리는 통째로 건너뛰어 순회를 크게 줄인다.
        stack.extend(child for child in node.children if getattr(child, "has_error", True))

    return findings


def _snippet(content: str, node, limit: int = 60) -> str:
    """오류 구간의 소스 조각을 한 줄로 잘라 보여준다."""
    raw = content.encode("utf-8", errors="ignore")[node.start_byte : node.end_byte]
    text = raw.decode("utf-8", errors="ignore").replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")
