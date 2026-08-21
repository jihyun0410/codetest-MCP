"""
Graph 빌더 — 저장소 전체를 AST 로 파싱해 그래프를 적재한다.

정의서 "[상세] 1. Git Diff와 AST로 프로젝트 개요를 파악하고 DB에 저장함":
  · Project 최초 등록 시 Git URL 에서 전체 소스를 가져온다
  · 개요 파악은 LLM 토큰 소비 없이 AST 파싱(Tree-sitter)만으로 진행한다
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.config import get_logger
from src.db import Project
from src.graph.store import GraphStore
from src.parsing.base import ParseResult
from src.parsing.registry import detect_language, parse_source
from src.repo import RepoService

logger = get_logger(__name__)


@dataclass
class BuildStats:
    """전체 수집 결과 통계."""

    node_count: int = 0
    edge_count: int = 0
    file_count: int = 0
    frameworks: list[str] = field(default_factory=list)
    language_stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


#: 빌드 파일 내용 → 프레임워크 라벨 (프로젝트 단위 판정)
_BUILD_FILE_SIGNATURES: list[tuple[str, str]] = [
    (r"spring-boot-starter", "Spring Boot"),
    (r"spring-boot", "Spring Boot"),
    (r"spring-boot-starter-web|spring-webmvc", "Spring MVC"),
    (r"spring-boot-starter-security|spring-security", "Spring Security"),
    (r"spring-boot-starter-data-jpa|hibernate-core|jakarta\.persistence", "Spring JPA"),
    (r"mybatis", "MyBatis"),
    (r'"@nestjs/core"', "NestJS"),
    (r'"express"', "Express"),
    (r'"next"', "Next.js"),
    (r'"react"', "React"),
    (r"fastapi", "FastAPI"),
    (r"\bflask\b", "Flask"),
    (r"\bdjango\b", "Django"),
]

_BUILD_FILES = [
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
]


class GraphBuilder:
    """프로젝트 1개의 전체 그래프를 만든다."""

    def __init__(self, db: Session, project: Project) -> None:
        self.db = db
        self.project = project
        self.store = GraphStore(db, project.id)
        self.repo = RepoService(project.id, project.git_url, project.github_token)

    def build_full(self, reset: bool = True) -> BuildStats:
        """
        저장소를 clone(또는 최신화)하고 전체 소스를 파싱해 그래프를 재구축한다.

        :param reset: True 면 기존 그래프를 지우고 처음부터 다시 만든다.
        """
        started = time.perf_counter()
        stats = BuildStats()

        self.repo.ensure_clone(self.project.default_branch)
        sources = self.repo.iter_source_files()
        stats.file_count = len(sources)

        if reset:
            self.store.clear()

        aggregate = ParseResult()
        language_counter: dict[str, int] = defaultdict(int)

        for source_file in sources:
            language = detect_language(source_file.path) or "unknown"
            language_counter[language] += 1
            aggregate.merge(parse_source(source_file.path, source_file.content))

        # 빌드 파일로 프로젝트 단위 프레임워크를 보강한다.
        aggregate.frameworks |= self._detect_frameworks_from_build_files()

        # 1) 노드 저장 → 2) 심볼 인덱스 구성 → 3) 간선 해석/저장
        self.store.upsert_nodes(aggregate.nodes)
        index = self.store.build_index()
        stats.edge_count = self.store.persist_edges(aggregate.edges, index)

        stats.node_count = len(index.by_qname)
        stats.frameworks = sorted(aggregate.frameworks)
        stats.language_stats = dict(language_counter)
        stats.warnings = aggregate.warnings[:100]  # 과다 누적 방지
        stats.elapsed_seconds = round(time.perf_counter() - started, 2)

        logger.info(
            "[%s] 그래프 구축 완료 — 파일 %d, 노드 %d, 간선 %d (%.2fs)",
            self.project.name,
            stats.file_count,
            stats.node_count,
            stats.edge_count,
            stats.elapsed_seconds,
        )
        return stats

    def build_incremental(self, changed_files: list[str]) -> BuildStats:
        """
        변경된 파일만 다시 파싱해 그래프에 반영한다 (PR 동기화 경로).

        해당 파일의 기존 노드를 지우고 새로 파싱한 노드로 대체하므로
        '추가 / 수정 / 삭제' 가 자연스럽게 반영된다.
        """
        started = time.perf_counter()
        stats = BuildStats()

        targets = [path for path in changed_files if detect_language(path)]
        if not targets:
            stats.elapsed_seconds = round(time.perf_counter() - started, 2)
            return stats

        # 1) 변경 파일의 옛 노드/간선 제거
        self.store.delete_by_files(targets)

        # 2) 현재 작업본 기준으로 재파싱 (파일이 삭제되었으면 내용이 없으므로 건너뜀)
        aggregate = ParseResult()
        for path in targets:
            content = self.repo.read_file(path)
            if content is None:
                continue
            stats.file_count += 1
            aggregate.merge(parse_source(path, content))

        self.store.upsert_nodes(aggregate.nodes)
        index = self.store.build_index()
        stats.edge_count = self.store.persist_edges(aggregate.edges, index)
        stats.node_count = len(aggregate.nodes)
        stats.frameworks = sorted(aggregate.frameworks)
        stats.warnings = aggregate.warnings[:50]
        stats.elapsed_seconds = round(time.perf_counter() - started, 2)
        return stats

    # ------------------------------------------------------------------
    def _detect_frameworks_from_build_files(self) -> set[str]:
        """
        pom.xml / build.gradle / package.json 등에서 프레임워크를 판정한다.

        멀티모듈 프로젝트를 위해 2단계 하위까지만 훑는다.
        (rglob 로 전체를 뒤지면 node_modules 때문에 매우 느려진다.)
        """
        found: set[str] = set()
        for build_file in _BUILD_FILES:
            for candidate in self._locate_build_files(build_file):
                try:
                    content = candidate.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for pattern, label in _BUILD_FILE_SIGNATURES:
                    if re.search(pattern, content, re.IGNORECASE):
                        found.add(label)
        return found

    def _locate_build_files(self, filename: str, max_matches: int = 5) -> list:
        """루트 / 1단계 / 2단계 하위에서만 빌드 파일을 찾는다."""
        from src.parsing.registry import EXCLUDED_DIRS

        matches = []
        for pattern in (filename, f"*/{filename}", f"*/*/{filename}"):
            for candidate in self.repo.path.glob(pattern):
                if set(candidate.relative_to(self.repo.path).parts) & EXCLUDED_DIRS:
                    continue
                matches.append(candidate)
                if len(matches) >= max_matches:
                    return matches
        return matches
