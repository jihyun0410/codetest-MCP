"""
Tree-sitter 파서 로더.

tree-sitter-language-pack 이 설치되어 있으면 언어별 Parser 를 생성해 캐싱한다.
설치되지 않았거나 특정 문법이 없는 환경에서도 서버가 죽지 않도록
None 을 반환하고, 호출 측(각 언어 파서)이 정규식 기반 축약 파싱으로 폴백한다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from src.config import get_logger

logger = get_logger(__name__)

#: 내부 언어 코드 → tree-sitter 문법 이름
GRAMMAR_NAMES: dict[str, str] = {
    "java": "java",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "tsx",
    "python": "python",
    "xml": "xml",
    "sql": "sql",
}

_UNAVAILABLE: set[str] = set()


@lru_cache(maxsize=16)
def get_parser(language: str) -> Any | None:
    """
    언어 코드로 tree-sitter Parser 를 가져온다. 사용할 수 없으면 None.

    lru_cache 로 프로세스 당 1회만 문법을 로드한다(파서 생성 비용이 크다).
    """
    grammar = GRAMMAR_NAMES.get(language)
    if grammar is None or language in _UNAVAILABLE:
        return None
    try:
        from tree_sitter_language_pack import get_parser as _get_parser  # type: ignore

        return _get_parser(grammar)
    except Exception as exc:  # 문법 미설치 / 바이너리 로드 실패 등
        _UNAVAILABLE.add(language)
        logger.warning(
            "tree-sitter 문법 '%s' 로드 실패 — 정규식 폴백 파서를 사용합니다: %s",
            grammar,
            exc,
        )
        return None


def node_text(node: Any, source_bytes: bytes) -> str:
    """tree-sitter 노드가 가리키는 원본 텍스트를 추출한다."""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def child_by_field(node: Any, field_name: str) -> Any | None:
    """필드명으로 자식 노드를 조회한다 (없으면 None)."""
    try:
        return node.child_by_field_name(field_name)
    except Exception:
        return None


def field_text(node: Any, field_name: str, source_bytes: bytes) -> str | None:
    child = child_by_field(node, field_name)
    return node_text(child, source_bytes) if child is not None else None


def iter_descendants(node: Any):
    """노드의 모든 자손을 전위 순회한다."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        # 역순으로 push 해야 소스 순서대로 순회된다.
        stack.extend(reversed(current.children))


def iter_descendants_until(node: Any, stop_types: set[str]):
    """
    자손을 순회하되, stop_types 에 해당하는 노드를 만나면 그 하위로는 내려가지 않는다.

    예: 메서드 본문을 훑을 때 중첩 클래스/중첩 함수 내부를 제외하고 싶을 때 사용.
    """
    stack = list(reversed(node.children))
    while stack:
        current = stack.pop()
        yield current
        if current.type not in stop_types:
            stack.extend(reversed(current.children))
