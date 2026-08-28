"""MCP 도구 응답 스키마.

Agent(LLM 서비스)가 MCP 로 호출하는 계약이다. 요청 파라미터는 도구 시그니처가
그대로 JSON Schema 가 되므로 여기엔 없다.
모든 응답은 **코드로 확정한 사실**만 담는다 — 추론/판정 필드는 없다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  register_project — 프로젝트 등록 (정의서 상세 1)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  GenerationContext 구성 요소 — Git Diff + AST 변경 단위 (정의서 (2))
# ---------------------------------------------------------------------------
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



class GenerationContext(BaseModel):
    """Agent 가 테스트 코드 생성·영향도 분석을 하는 데 필요한, MCP 가 정리한 사실.

    정의서의 역할 분담을 그대로 따른다 — 여기 담긴 것은 전부 파서와 그래프가
    확정한 사실이고, 해석(무엇을 테스트할지, 영향이 얼마나 큰지)은 Agent 몫이다.
    """

    # 프로젝트 개요
    project_id: str
    name: str
    ingest_status: str
    graph_ready: bool = True
    frameworks: list[str] = Field(default_factory=list)
    language_stats: dict = Field(default_factory=dict)
    #: @SpringBootApplication 이 있는 기준 패키지 (테스트 배치에 사용)
    base_package: str | None = None
    node_counts: dict = Field(default_factory=dict)
    edge_counts: dict = Field(default_factory=dict)

    # Diff 로 확정한 변경 지점 (영향도 "판정" 은 하지 않는다)
    changed_ranges: dict[str, list[tuple[int, int]]] = Field(default_factory=dict)
    changed_units: list[ChangedUnit] = Field(default_factory=list)
    impacted_units: list[ImpactedUnit] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    #: 호출자가 넘긴 변경 파일 본문 (Agent 가 테스트를 쓰려면 실제 코드가 필요하다)
    sources: list[SourceFilePayload] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)



# ---------------------------------------------------------------------------
#  execute_tests / test_run — @SpringBootTest + JaCoCo 실행 (정의서 상세 4)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  test_generate / test_run — Agent 가 생성한 테스트 코드 (정의서 (1))
# ---------------------------------------------------------------------------
class TestGenerateResponse(BaseModel):
    """Agent 가 생성한 테스트 코드 + 생성에 쓰인 컨텍스트."""

    project_id: str
    #: Agent(LLM)가 생성해 돌려준 Java 테스트 소스
    test_code: str
    #: Agent 응답 중 test_code 외의 전부 — 영향도 해석·요약 등 LLM 이 만든 내용.
    #: 규격을 MCP 가 정하지 않는다(LLM 산출물이라). 받은 그대로 터미널로 넘긴다.
    analysis: dict = Field(default_factory=dict)
    #: MCP 가 정리해 Agent 로 넘긴 사실 (Agent 가 무엇을 보고 썼는지 추적용)
    context: GenerationContext


class TestRunResponse(BaseModel):
    """생성부터 실행까지 전 과정의 결과."""

    project_id: str
    test_code: str
    #: Agent 응답 중 test_code 외의 전부 (영향도 해석 등)
    analysis: dict = Field(default_factory=dict)
    context: GenerationContext
    #: @SpringBootTest 주입 + JaCoCo 실행 결과 (판정 없이 사실만)
    execution: ExecuteResponse
