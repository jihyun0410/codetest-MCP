"""
영향도 분석.

정의서:
  · "Git PR 생성 시 변경된 Diff와 Graph DB 상에서 직접적으로 연결된 연관 Flow를
     추적하여 분석함."
  · "영향도 등급: Low / Medium / High 3단계로 구분하며, 반드시 어떠한 근거로
     영향도를 표시하는지(Rationale) 명확히 제시해야 함."

이 모듈은 **그래프 사실(fact)** 만으로 사전 점수와 등급을 계산한다.
LLM 은 이 결과를 근거 입력으로 받아 최종 Rationale 을 작성한다.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from src.db import EdgeType, GraphEdge, GraphNode, NodeType, RiskLevel
from src.graph.store import GraphStore

#: 영향 전파를 추적할 최대 깊이. Tier 3 컨텍스트 경계와 맞춘다.
DEFAULT_MAX_DEPTH = 3

#: 깊이별 가중치 — 가까울수록 위험이 크다.
_DEPTH_WEIGHT = {0: 10.0, 1: 6.0, 2: 3.0, 3: 1.5}

#: 노드 종류별 가중치 — SQL/엔드포인트 변경은 파급이 크다.
_NODE_TYPE_WEIGHT = {
    NodeType.SQL.value: 3.0,
    NodeType.METHOD.value: 2.0,
    NodeType.CLASS.value: 1.5,
    NodeType.VARIABLE.value: 1.0,
    NodeType.FILE.value: 0.5,
}


@dataclass
class ImpactNodeInfo:
    node_id: str
    qualified_name: str
    node_type: str
    file_path: str
    depth: int
    via: str | None = None
    name: str = ""
    signature: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "qualified_name": self.qualified_name,
            "node_type": self.node_type,
            "file_path": self.file_path,
            "depth": self.depth,
            "via": self.via,
        }


@dataclass
class ImpactReport:
    """영향도 계산 결과."""

    changed: list[ImpactNodeInfo] = field(default_factory=list)
    impacted: list[ImpactNodeInfo] = field(default_factory=list)
    score: int = 0
    risk: RiskLevel = RiskLevel.LOW
    #: 등급 산정 근거 (LLM 프롬프트와 UI 양쪽에서 사용)
    reasons: list[str] = field(default_factory=list)
    #: 영향받은 파일 목록
    affected_files: list[str] = field(default_factory=list)

    @property
    def all_nodes(self) -> list[ImpactNodeInfo]:
        return self.changed + self.impacted


# ---------------------------------------------------------------------------
#  Diff → 변경 라인 추출
# ---------------------------------------------------------------------------
_DIFF_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_DIFF_FILE_REMOVED = re.compile(r"^--- a/(.+)$")
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """
    unified diff 를 파싱해 {파일경로: [(시작줄, 끝줄), ...]} 를 만든다.

    줄 번호는 **변경 후(new) 파일 기준**이다. 삭제만 발생한 파일은 hunk 시작 줄
    주변을 범위로 잡아 인접 노드가 잡히도록 한다.
    """
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current_file: str | None = None
    fallback_file: str | None = None

    for line in diff.splitlines():
        removed = _DIFF_FILE_REMOVED.match(line)
        if removed:
            fallback_file = None if removed.group(1) == "dev/null" else removed.group(1)
            continue

        header = _DIFF_FILE_HEADER.match(line)
        if header:
            path = header.group(1)
            current_file = fallback_file if path == "dev/null" else path
            continue

        hunk = _DIFF_HUNK.match(line)
        if hunk and current_file:
            start = int(hunk.group(1))
            length = int(hunk.group(2) or 1)
            ranges[current_file].append((start, start + max(length, 1) - 1))

    return dict(ranges)


# ---------------------------------------------------------------------------
#  영향도 계산
# ---------------------------------------------------------------------------
class ImpactAnalyzer:
    """그래프를 역방향으로 순회하며 변경의 파급 범위를 구한다."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self._nodes: dict[str, GraphNode] = {}
        self._incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        self._outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        for node in self.store.all_nodes():
            self._nodes[node.id] = node
        for edge in self.store.all_edges():
            self._incoming[edge.target_id].append(edge)
            self._outgoing[edge.source_id].append(edge)
        self._loaded = True

    # ------------------------------------------------------------------
    def find_changed_nodes(
        self, diff_ranges: dict[str, list[tuple[int, int]]]
    ) -> list[ImpactNodeInfo]:
        """
        변경 라인과 겹치는 노드를 찾는다.

        - 라인 정보가 있으면 [start_line, end_line] 이 겹치는 노드만 선택
        - 겹치는 세부 노드가 없으면(파일 전체 신규/삭제 등) File 노드를 대표로 사용
        """
        self._load()
        selected: dict[str, ImpactNodeInfo] = {}

        by_file: dict[str, list[GraphNode]] = defaultdict(list)
        for node in self._nodes.values():
            by_file[node.file_path].append(node)

        for file_path, line_ranges in diff_ranges.items():
            candidates = by_file.get(file_path, [])
            if not candidates:
                continue

            matched_detail = False
            for node in candidates:
                if node.node_type == NodeType.FILE.value:
                    continue
                if node.start_line is None:
                    continue
                node_end = node.end_line or node.start_line
                for start, end in line_ranges:
                    if node.start_line <= end and start <= node_end:
                        selected[node.id] = _to_info(node, depth=0, via="changed")
                        matched_detail = True
                        break

            if not matched_detail:
                for node in candidates:
                    if node.node_type == NodeType.FILE.value:
                        selected[node.id] = _to_info(node, depth=0, via="changed-file")

        return list(selected.values())

    def traverse(
        self,
        changed: list[ImpactNodeInfo],
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = 400,
    ) -> list[ImpactNodeInfo]:
        """
        변경 노드에서 시작해 영향 범위를 BFS 로 확장한다.

        - **역방향(incoming)**: 나를 호출/사용하는 쪽 → 진짜 파급 대상
        - **정방향(outgoing) 중 Executes/Contains**: 내가 실행하는 SQL, 내가 품은 멤버
          (변경 지점의 의미를 이해하는 데 필요)
        """
        self._load()
        visited: set[str] = {info.node_id for info in changed}
        results: list[ImpactNodeInfo] = []

        queue: deque[tuple[str, int]] = deque((info.node_id, 0) for info in changed)
        while queue and len(results) < max_nodes:
            node_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            neighbours: list[tuple[GraphEdge, str]] = [
                (edge, edge.source_id) for edge in self._incoming.get(node_id, [])
            ]
            for edge in self._outgoing.get(node_id, []):
                if edge.edge_type in {EdgeType.EXECUTES.value, EdgeType.CONTAINS.value}:
                    neighbours.append((edge, edge.target_id))

            for edge, neighbour_id in neighbours:
                if neighbour_id in visited:
                    continue
                node = self._nodes.get(neighbour_id)
                if node is None:
                    continue
                visited.add(neighbour_id)
                info = _to_info(node, depth=depth + 1, via=edge.edge_type)
                results.append(info)
                queue.append((neighbour_id, depth + 1))

        return results

    def analyze(
        self, diff_ranges: dict[str, list[tuple[int, int]]], max_depth: int = DEFAULT_MAX_DEPTH
    ) -> ImpactReport:
        """변경 라인 → 변경 노드 → 파급 노드 → 점수/등급/근거를 한 번에 산출한다."""
        changed = self.find_changed_nodes(diff_ranges)
        impacted = self.traverse(changed, max_depth=max_depth)
        report = ImpactReport(changed=changed, impacted=impacted)

        score, reasons = _score(changed, impacted, diff_ranges)
        report.score = score
        report.risk = _to_risk(score)
        report.reasons = reasons
        report.affected_files = sorted(
            {info.file_path for info in changed + impacted if info.file_path}
        )
        return report

    def neighbours_of(self, node_id: str, depth: int = 1) -> list[ImpactNodeInfo]:
        """특정 노드의 N-Depth 이웃 조회 (Context Slicer 가 Tier 구성에 사용)."""
        self._load()
        node = self._nodes.get(node_id)
        if node is None:
            return []
        return self.traverse([_to_info(node, 0, "seed")], max_depth=depth)

    def get_node(self, node_id: str) -> GraphNode | None:
        self._load()
        return self._nodes.get(node_id)


# ---------------------------------------------------------------------------
#  점수/등급 산정
# ---------------------------------------------------------------------------
def _score(
    changed: list[ImpactNodeInfo],
    impacted: list[ImpactNodeInfo],
    diff_ranges: dict[str, list[tuple[int, int]]],
) -> tuple[int, list[str]]:
    """
    0~100 점수와 산정 근거 문장을 만든다.

    가중 요소
      1. 변경 노드의 종류 (SQL/Method 가중치가 높음)
      2. 파급 노드 수 × 깊이 가중치
      3. 엔드포인트(Controller) 도달 여부 — 사용자 노출 경로
      4. SQL 도달 여부 — 데이터 정합성 위험
      5. 변경 파일 수 / 변경 라인 규모
    """
    reasons: list[str] = []
    raw = 0.0

    # 1) 변경 노드 자체
    for info in changed:
        raw += _NODE_TYPE_WEIGHT.get(info.node_type, 1.0) * _DEPTH_WEIGHT[0] / 10.0
    if changed:
        types = sorted({info.node_type for info in changed})
        reasons.append(
            f"직접 변경된 그래프 노드 {len(changed)}개 (유형: {', '.join(types)})"
        )

    # 2) 파급 노드
    depth_buckets: dict[int, int] = defaultdict(int)
    for info in impacted:
        depth_buckets[info.depth] += 1
        raw += _NODE_TYPE_WEIGHT.get(info.node_type, 1.0) * _DEPTH_WEIGHT.get(info.depth, 1.0) / 10.0
    if impacted:
        detail = ", ".join(f"{d}-Depth {count}개" for d, count in sorted(depth_buckets.items()))
        reasons.append(f"그래프 상 연관 노드 {len(impacted)}개 도달 ({detail})")

    # 3) 엔드포인트 도달
    endpoints = [
        info
        for info in changed + impacted
        if info.node_type == NodeType.METHOD.value and (info.meta or {}).get("entrypoint")
    ]
    if endpoints:
        raw += 8.0 + min(len(endpoints), 5) * 2.0
        sample = ", ".join(
            f"{(e.meta or {}).get('http_method') or 'ENTRY'} {(e.meta or {}).get('route') or e.name}"
            for e in endpoints[:3]
        )
        reasons.append(f"사용자 노출 진입점 {len(endpoints)}개에 영향 ({sample})")

    # 4) SQL 도달
    sql_nodes = [i for i in changed + impacted if i.node_type == NodeType.SQL.value]
    if sql_nodes:
        raw += 6.0 + min(len(sql_nodes), 5) * 2.0
        tables: set[str] = set()
        for node in sql_nodes:
            tables.update((node.meta or {}).get("tables") or [])
        table_text = f" / 대상 테이블: {', '.join(sorted(tables)[:5])}" if tables else ""
        reasons.append(f"SQL 실행 지점 {len(sql_nodes)}개 연관{table_text}")

    # 5) 변경 규모
    changed_lines = sum(
        end - start + 1 for spans in diff_ranges.values() for start, end in spans
    )
    file_count = len(diff_ranges)
    raw += min(file_count, 20) * 0.8 + min(changed_lines, 800) * 0.02
    reasons.append(f"변경 파일 {file_count}개 / 변경 라인 약 {changed_lines}줄")

    score = int(max(0, min(100, round(raw))))
    return score, reasons


def _to_risk(score: int) -> RiskLevel:
    """
    점수 → 3단계 등급.

    임계값은 운영 데이터로 조정할 수 있도록 한 곳에 모아 둔다.
    """
    if score >= 55:
        return RiskLevel.HIGH
    if score >= 25:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _to_info(node: GraphNode, depth: int, via: str | None) -> ImpactNodeInfo:
    return ImpactNodeInfo(
        node_id=node.id,
        qualified_name=node.qualified_name,
        node_type=node.node_type,
        file_path=node.file_path,
        depth=depth,
        via=via,
        name=node.name,
        signature=node.signature,
        start_line=node.start_line,
        end_line=node.end_line,
        meta=node.meta or {},
    )
