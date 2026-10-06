"""
파서 공통 자료구조.

모든 언어 파서는 소스 1개를 입력받아 ParseResult(노드/간선/프레임워크 힌트)를
반환한다. 이 단계는 **LLM 토큰을 전혀 사용하지 않는다** — 순수 AST 기반이다.
"""

from __future__ import annotations

import re

import hashlib
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Protocol

from codetest_mcp.db import EdgeType, NodeType


def line_finder(source: str):
    """문자 오프셋 → 1부터 시작하는 줄 번호를 돌려주는 함수.

    정규식 폴백에서 매치마다 `source[:start].count("\\n")` 을 세면 메서드가 많은 파일에서
    제곱으로 느려진다. 줄바꿈 위치를 한 번만 모아 두고 이분 탐색한다.
    """
    newlines = [m.start() for m in re.finditer("\n", source)]
    return lambda offset: bisect_left(newlines, offset) + 1


def fingerprint(text: str) -> str:
    """본문 해시. 재파싱 시 '수정' 여부를 판정하는 기준."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


@dataclass
class ParsedNode:
    """AST 에서 추출한 코드 구성요소 1개."""

    node_type: NodeType
    name: str
    #: 프로젝트 내 유일 논리 식별자.
    #: 예) com.demo.user.UserService#getUser(Long)
    #:     src/api/user.ts::UserApi.fetch
    #:     mapper:com.demo.UserMapper.selectUser
    qualified_name: str
    file_path: str            # 물리 경로 (저장소 루트 기준 상대)
    language: str
    start_line: int | None = None
    end_line: int | None = None
    #: Tier 3 컨텍스트에서 본문 대신 제공되는 AST 시그니처 요약
    signature: str | None = None
    #: 원본 본문. DB 에는 저장하지 않고 Tier 2 컨텍스트 구성에만 사용한다.
    body: str | None = None
    meta: dict = field(default_factory=dict)

    def compute_fingerprint(self) -> str:
        return fingerprint(self.body or self.signature or self.qualified_name)


@dataclass
class ParsedEdge:
    """
    AST 에서 추출한 관계 1개.

    호출 대상이 같은 파일 안에 있으면 target_qname 이 확정되지만,
    타 파일/타 클래스 호출은 파싱 시점에 알 수 없으므로 target_hint(이름)만 남기고
    GraphBuilder 의 심볼 해석 단계에서 실제 노드로 연결한다.
    """

    source_qname: str
    edge_type: EdgeType
    target_qname: str | None = None
    target_hint: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    nodes: list[ParsedNode] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)
    #: 파일에서 감지한 프레임워크 (예: {"Spring Boot", "MyBatis"})
    frameworks: set[str] = field(default_factory=set)
    #: 파싱 실패/부분 실패 사유
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: ParseResult) -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.frameworks |= other.frameworks
        self.warnings.extend(other.warnings)


#: FROM / JOIN / INTO / UPDATE 뒤의 테이블명
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE
)


def extract_tables(sql: str) -> list[str]:
    """SQL 에서 참조하는 테이블명을 등장 순서대로, 중복 없이 뽑는다.

    Java 인라인 SQL · MyBatis XML · .sql 파일 세 파서가 똑같이 쓰던 함수다.
    """
    seen: list[str] = []
    lowered: set[str] = set()
    for name in _TABLE_RE.findall(sql):
        if name.lower() not in lowered:
            lowered.add(name.lower())
            seen.append(name)
    return seen


def join_route(base: str, sub: str) -> str:
    """클래스 수준 경로와 메서드 수준 경로를 이어 붙인다 (`/a` + `b` → `/a/b`)."""
    if not base:
        return sub
    if not sub:
        return base
    return f"{base.rstrip('/')}/{sub.lstrip('/')}"


class LanguageParser(Protocol):
    """언어별 파서 인터페이스."""

    language: str

    def parse(self, file_path: str, source: str) -> ParseResult:
        ...
