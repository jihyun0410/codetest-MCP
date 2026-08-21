"""ORM 모델 + 엔진/세션.

프로젝트 개요(Project) + 코드 구조 그래프.

정의서 "1. Git Diff와 AST로 프로젝트 개요를 파악하고 DB에 저장함" 의 저장 계층이다.

Graph Node 관리 객체 : File / Class / Method / Variable / SQL
Graph Edge 관리 관계 : Contains / Calls / Uses / Executes
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from codetest_mcp.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


# ============================================================================
#  Enum
# ============================================================================
class NodeType(str, enum.Enum):
    FILE = "File"
    CLASS = "Class"
    METHOD = "Method"
    VARIABLE = "Variable"
    SQL = "SQL"


class EdgeType(str, enum.Enum):
    CONTAINS = "Contains"   # File → Class, Class → Method
    CALLS = "Calls"         # Method → Method
    USES = "Uses"           # Method → Variable/Class
    EXECUTES = "Executes"   # Method → SQL (MyBatis/JPA)


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IngestStatus(str, enum.Enum):
    """clone → AST 파싱 → Graph 적재 진행 상태."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


# ============================================================================
#  모델
# ============================================================================
class Project(Base, IdMixin, TimestampMixin):
    """등록 입력 4종: Git URL / 프로젝트명 / 담당자 / Github API 토큰."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    git_url: Mapped[str] = mapped_column(String(500), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    #: clone 용 GitHub PAT. 응답에서는 보유 여부만 노출한다.
    github_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(100), default="main")

    ingest_status: Mapped[str] = mapped_column(
        String(20), default=IngestStatus.PENDING.value, nullable=False
    )
    ingest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 탐지된 프레임워크 (예: ["Spring Boot", "MyBatis"])
    frameworks: Mapped[list] = mapped_column(JSON, default=list)
    #: 언어별 파일 수 (예: {"java": 120})
    language_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    nodes: Mapped[list["GraphNode"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    edges: Mapped[list["GraphEdge"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class GraphNode(Base, IdMixin, TimestampMixin):
    """코드 구조 그래프의 정점. qualified_name 이 프로젝트 내 유일 식별자."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint("project_id", "qualified_name", name="uq_node_qname"),
        Index("ix_node_project_type", "project_id", "node_type"),
        Index("ix_node_project_name", "project_id", "name"),
        Index("ix_node_file_path", "project_id", "file_path"),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: 논리 경로 (예: com.demo.UserService#getUser(Long))
    qualified_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: 물리 경로 (저장소 루트 기준 상대 경로)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    language: Mapped[str | None] = mapped_column(String(30), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 본문 대신 제공하는 AST 시그니처 요약
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 본문 해시 — 재파싱 시 변경 여부 판정
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 프레임워크 부가 정보 (@RestController, HTTP 매핑, MyBatis namespace 등)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="nodes")


class GraphEdge(Base, IdMixin, TimestampMixin):
    """코드 구조 그래프의 간선."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("project_id", "source_id", "target_id", "edge_type", name="uq_edge"),
        Index("ix_edge_source", "project_id", "source_id"),
        Index("ix_edge_target", "project_id", "target_id"),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(String(20), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="edges")


# ============================================================================
#  엔진 / 세션
# ============================================================================
# SQLite 는 스레드 간 커넥션 공유를 막으므로 옵션을 완화한다.
_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """기동 시 테이블 생성 (개발/최초 부트스트랩용. 운영 마이그레이션은 alembic)."""
    settings.ensure_directories()
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """도구 호출 단위 세션."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
