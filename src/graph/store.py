"""
Graph 영속화 계층.

파서가 만든 ParsedNode/ParsedEdge 를 DB(GraphNode/GraphEdge)에 반영하고,
파싱 시점에 이름만 알던 호출 대상(target_hint)을 실제 노드로 해석한다.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.config import get_logger
from src.db import EdgeType, GraphEdge, GraphNode, NodeType
from src.parsing.base import ParsedEdge, ParsedNode

logger = get_logger(__name__)

#: 단순명 힌트 1개가 이보다 많은 후보에 매칭되면 노이즈로 보고 연결하지 않는다.
MAX_AMBIGUOUS_CANDIDATES = 3


class SymbolIndex:
    """
    호출 대상 해석용 인덱스.

    파싱 단계에서 얻은 힌트는 세 가지 형태다.
      1) 정확한 논리 경로            : "com.demo.UserService#getUser(Long)"
      2) 타입 한정 호출              : "UserService#getUser"
      3) 단순 메서드/클래스 이름     : "getUser", "UserService"
      4) MyBatis statement 참조      : "mapper:com.demo.UserMapper.selectUser"
    """

    def __init__(self) -> None:
        self.by_qname: dict[str, str] = {}
        #: (클래스 단순명, 메서드명) → [node_id]
        self.by_owner_method: dict[tuple[str, str], list[str]] = defaultdict(list)
        #: 메서드 단순명 → [node_id]
        self.by_method_name: dict[str, list[str]] = defaultdict(list)
        #: 클래스 단순명 → [node_id]
        self.by_class_name: dict[str, list[str]] = defaultdict(list)

    def add(self, node_id: str, node_type: str, name: str, qualified_name: str, meta: dict) -> None:
        self.by_qname[qualified_name] = node_id
        if node_type == NodeType.METHOD.value:
            self.by_method_name[name].append(node_id)
            owner = (meta or {}).get("owner") or ""
            owner_simple = owner.split("::")[-1].split(".")[-1].split("#")[0]
            if owner_simple:
                self.by_owner_method[(owner_simple, name)].append(node_id)
        elif node_type == NodeType.CLASS.value:
            self.by_class_name[name].append(node_id)

    def resolve(self, hint: str, prefer: str = "method") -> list[str]:
        """힌트 하나를 0~N 개의 노드 id 로 해석한다."""
        if not hint:
            return []

        # 1) 정확 일치 (논리 경로 / mapper: 참조)
        exact = self.by_qname.get(hint)
        if exact:
            return [exact]

        # 2) 타입 한정 호출 "Type#method"
        if "#" in hint:
            owner, method = hint.split("#", 1)
            owner_simple = owner.split(".")[-1].strip()
            method = method.split("(")[0].strip()
            candidates = self.by_owner_method.get((owner_simple, method), [])
            if candidates:
                return candidates[:MAX_AMBIGUOUS_CANDIDATES]
            # 타입 추론이 빗나갔을 수 있으므로 메서드 단순명으로 재시도
            hint = method

        # 3) 단순명
        simple = hint.split("(")[0].strip()
        if prefer == "class":
            candidates = self.by_class_name.get(simple, [])
            if candidates:
                return candidates[:MAX_AMBIGUOUS_CANDIDATES]
            candidates = self.by_method_name.get(simple, [])
        else:
            candidates = self.by_method_name.get(simple, [])
            if not candidates:
                candidates = self.by_class_name.get(simple, [])

        if not candidates or len(candidates) > MAX_AMBIGUOUS_CANDIDATES:
            return []
        return candidates


class GraphStore:
    """프로젝트 1개의 그래프를 읽고 쓰는 저장소."""

    def __init__(self, db: Session, project_id: str) -> None:
        self.db = db
        self.project_id = project_id

    # ------------------------------------------------------------------
    #  쓰기
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """프로젝트의 그래프를 전부 삭제한다 (전체 재수집 시)."""
        self.db.execute(delete(GraphEdge).where(GraphEdge.project_id == self.project_id))
        self.db.execute(delete(GraphNode).where(GraphNode.project_id == self.project_id))
        self.db.flush()

    def delete_by_files(self, file_paths: list[str]) -> int:
        """
        해당 파일들에 속한 모든 노드를 삭제한다 (PR 에서 파일이 삭제/수정된 경우).

        간선은 FK ondelete=CASCADE 로 함께 정리된다.
        """
        if not file_paths:
            return 0
        node_ids = list(
            self.db.scalars(
                select(GraphNode.id).where(
                    GraphNode.project_id == self.project_id,
                    GraphNode.file_path.in_(file_paths),
                )
            )
        )
        if not node_ids:
            return 0
        # SQLite 는 FK CASCADE 가 기본 비활성이므로 간선을 명시적으로 지운다.
        self.db.execute(
            delete(GraphEdge).where(
                GraphEdge.project_id == self.project_id,
                GraphEdge.source_id.in_(node_ids),
            )
        )
        self.db.execute(
            delete(GraphEdge).where(
                GraphEdge.project_id == self.project_id,
                GraphEdge.target_id.in_(node_ids),
            )
        )
        self.db.execute(delete(GraphNode).where(GraphNode.id.in_(node_ids)))
        self.db.flush()
        return len(node_ids)

    def upsert_nodes(self, parsed_nodes: list[ParsedNode]) -> dict[str, str]:
        """
        노드를 삽입/갱신하고 {qualified_name: node_id} 를 반환한다.

        같은 qualified_name 이 이미 있으면 위치/시그니처/해시만 갱신한다.
        """
        existing: dict[str, GraphNode] = {
            node.qualified_name: node
            for node in self.db.scalars(
                select(GraphNode).where(GraphNode.project_id == self.project_id)
            )
        }

        mapping: dict[str, str] = {}
        seen: set[str] = set()
        for parsed in parsed_nodes:
            qname = parsed.qualified_name
            if qname in seen:
                continue  # 같은 배치 안의 중복 방지
            seen.add(qname)

            row = existing.get(qname)
            if row is None:
                row = GraphNode(
                    project_id=self.project_id,
                    node_type=parsed.node_type.value,
                    name=parsed.name,
                    qualified_name=qname,
                    file_path=parsed.file_path,
                    language=parsed.language,
                    start_line=parsed.start_line,
                    end_line=parsed.end_line,
                    signature=parsed.signature,
                    fingerprint=parsed.compute_fingerprint(),
                    meta=parsed.meta,
                )
                self.db.add(row)
                self.db.flush()  # id 확보
                existing[qname] = row
            else:
                row.node_type = parsed.node_type.value
                row.name = parsed.name
                row.file_path = parsed.file_path
                row.language = parsed.language
                row.start_line = parsed.start_line
                row.end_line = parsed.end_line
                row.signature = parsed.signature
                row.fingerprint = parsed.compute_fingerprint()
                row.meta = parsed.meta
            mapping[qname] = row.id

        self.db.flush()
        return mapping

    def build_index(self) -> SymbolIndex:
        """DB 에 적재된 전체 노드로 심볼 인덱스를 만든다."""
        index = SymbolIndex()
        rows = self.db.execute(
            select(
                GraphNode.id,
                GraphNode.node_type,
                GraphNode.name,
                GraphNode.qualified_name,
                GraphNode.meta,
            ).where(GraphNode.project_id == self.project_id)
        ).all()
        for node_id, node_type, name, qname, meta in rows:
            index.add(node_id, node_type, name, qname, meta or {})
        return index

    def persist_edges(self, parsed_edges: list[ParsedEdge], index: SymbolIndex) -> int:
        """
        간선을 해석해 저장한다.

        - target_qname 이 있으면 그대로 사용
        - 없으면 target_hint 를 SymbolIndex 로 해석 (해석 실패 시 조용히 버림 —
          외부 라이브러리 호출 등 프로젝트 밖 심볼은 그래프에 담지 않는다)
        """
        existing_keys: set[tuple[str, str, str]] = {
            (edge.source_id, edge.target_id, edge.edge_type)
            for edge in self.db.scalars(
                select(GraphEdge).where(GraphEdge.project_id == self.project_id)
            )
        }

        created = 0
        for parsed in parsed_edges:
            source_id = index.by_qname.get(parsed.source_qname)
            if source_id is None:
                continue

            target_ids: list[str] = []
            if parsed.target_qname:
                resolved = index.by_qname.get(parsed.target_qname)
                if resolved:
                    target_ids = [resolved]
                elif parsed.target_hint:
                    target_ids = index.resolve(parsed.target_hint)
            elif parsed.target_hint:
                prefer = "class" if parsed.edge_type == EdgeType.USES else "method"
                target_ids = index.resolve(parsed.target_hint, prefer=prefer)

            for target_id in target_ids:
                if target_id == source_id:
                    continue  # 자기 자신 호출(재귀)은 영향도 계산에 무의미
                key = (source_id, target_id, parsed.edge_type.value)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                self.db.add(
                    GraphEdge(
                        project_id=self.project_id,
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=parsed.edge_type.value,
                        meta=parsed.meta,
                    )
                )
                created += 1

        self.db.flush()
        return created

    # ------------------------------------------------------------------
    #  읽기
    # ------------------------------------------------------------------
    def all_nodes(self) -> list[GraphNode]:
        return list(
            self.db.scalars(
                select(GraphNode).where(GraphNode.project_id == self.project_id)
            )
        )

    def all_edges(self) -> list[GraphEdge]:
        return list(
            self.db.scalars(
                select(GraphEdge).where(GraphEdge.project_id == self.project_id)
            )
        )

    def nodes_in_files(self, file_paths: list[str]) -> list[GraphNode]:
        if not file_paths:
            return []
        return list(
            self.db.scalars(
                select(GraphNode).where(
                    GraphNode.project_id == self.project_id,
                    GraphNode.file_path.in_(file_paths),
                )
            )
        )

    def entrypoints(self) -> list[GraphNode]:
        """워크플로우 시작점이 되는 메서드 노드 목록."""
        return [
            node
            for node in self.all_nodes()
            if node.node_type == NodeType.METHOD.value and (node.meta or {}).get("entrypoint")
        ]

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for node in self.all_nodes():
            counts[node.node_type] += 1
        return dict(counts)

    def edge_counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for edge in self.all_edges():
            counts[edge.edge_type] += 1
        return dict(counts)
