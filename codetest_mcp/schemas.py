"""MCP REST 요청/응답 스키마.

Agent(LLM 서비스)가 FastAPI 로 호출하는 계약이다.
모든 응답은 **코드로 확정한 사실**만 담는다 — 추론/판정 필드는 없다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  POST /api/v1/projects  — 프로젝트 개요 수집 (정의서 상세 1)
# ---------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="프로젝트 명")
    git_url: str = Field(..., description="대상 저장소 Git URL")
    owner: str = Field(..., min_length=1, max_length=100, description="담당자")
    github_token: str | None = Field(default=None, description="Github API Token")
    default_branch: str = Field(default="main", description="기준 브랜치")

    @field_validator("git_url")
    @classmethod
    def _validate_git_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://", "git@")):
            raise ValueError("git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")
        return v.rstrip("/")


class ProjectRead(BaseModel):
    """github_token 은 보유 여부만 노출한다."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    owner: str
    default_branch: str
    ingest_status: str
    ingest_error: str | None
    last_indexed_at: datetime | None
    frameworks: list[str]
    language_stats: dict
    created_at: datetime
    updated_at: datetime
    has_github_token: bool = False


class OverviewResponse(BaseModel):
    """DB 에 저장된 프로젝트 개요 (정의서: "프로젝트 개요를 파악하고 DB에 저장함")."""

    project_id: str
    name: str
    ingest_status: str
    ingest_error: str | None = None
    frameworks: list[str] = Field(default_factory=list)
    language_stats: dict = Field(default_factory=dict)
    node_counts: dict = Field(default_factory=dict)
    edge_counts: dict = Field(default_factory=dict)
    #: @SpringBootApplication 이 있는 기준 패키지 (테스트 배치에 사용)
    base_package: str | None = None
    last_indexed_at: datetime | None = None


# ---------------------------------------------------------------------------
#  POST /api/v1/analysis/changes — Git Diff + AST 변경 단위 식별 (정의서 (2))
# ---------------------------------------------------------------------------
class ChangeAnalysisRequest(BaseModel):
    project_id: str
    #: 변경분 unified diff
    diff: str = ""
    #: 변경 파일 본문 (그래프에 없는 신규 파일도 파싱하기 위함)
    sources: list[SourceFilePayload] = Field(default_factory=list)


class ChangedUnit(BaseModel):
    """Diff 라인과 겹치는 것으로 확정된 코드 단위."""

    qualified_name: str
    name: str
    node_type: str
    file_path: str
    language: str | None = None
    signature: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    #: 진입점(Controller 매핑 등) 여부
    entrypoint: bool = False
    http_method: str | None = None
    route: str | None = None


class ImpactedUnit(BaseModel):
    """변경 단위로부터 그래프를 타고 도달한 영향 대상."""

    qualified_name: str
    node_type: str
    file_path: str
    depth: int
    via: str | None = None


class ChangeAnalysisResponse(BaseModel):
    """AST/그래프가 확정한 사실. 의도 해석은 Agent 가 한다."""

    project_id: str
    #: 변경된 파일 → 변경 라인 구간
    changed_ranges: dict[str, list[tuple[int, int]]] = Field(default_factory=dict)
    changed_units: list[ChangedUnit] = Field(default_factory=list)
    impacted_units: list[ImpactedUnit] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    #: 그래프 영향도 등급 LOW / MEDIUM / HIGH 와 산정 근거
    risk: str = "LOW"
    risk_score: int = 0
    risk_reasons: list[str] = Field(default_factory=list)
    #: 개요 (Agent 프롬프트 컨텍스트용)
    frameworks: list[str] = Field(default_factory=list)
    base_package: str | None = None
    #: 그래프가 비어 있는 경우(수집 미완료/실패) Agent 가 알 수 있도록
    graph_ready: bool = True
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  POST /api/v1/tests/execute — @SpringBootTest + JaCoCo 실행 (정의서 상세 4)
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    project_id: str
    #: Agent(LLM)가 생성한 Java 테스트 소스
    test_code: str
    #: 실행 전에 작업 사본에 덮어쓸 변경 파일 (개발자의 미커밋 변경)
    sources: list[SourceFilePayload] = Field(default_factory=list)
    #: package 선언이 없을 때 사용할 기준 패키지. 생략하면 개요에서 찾는다.
    base_package: str | None = None


class ExecuteResponse(BaseModel):
    """실행 사실만 담는다. 적절성 판정은 Agent 가 한다."""

    project_id: str
    exit_code: int = 0
    output: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    failures: list[str] = Field(default_factory=list)
    #: JaCoCo LINE/BRANCH 커버리지
    coverage: dict | None = None
    jacoco_enabled: bool = False
    #: @SpringBootTest 가 최종 소스에 존재하는지
    springboot_applied: bool = False
    #: 주입 과정에서 실제로 수행한 변환 목록
    applied: list[str] = Field(default_factory=list)
    test_file_path: str = ""
    #: 실제 실행한 gradle 명령
    command: list[str] = Field(default_factory=list)
