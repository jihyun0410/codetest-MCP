"""MCP 도구 응답 스키마.

MCP 가 진입점이다. CLI 가 도구를 호출하면 MCP 가 코드 기반 사실을 확정하고,
LLM 판단이 필요한 부분만 Agent(codetest)에 REST 로 넘긴 뒤 결과를 합쳐 돌려준다.
요청 파라미터는 도구 시그니처가 그대로 JSON Schema 가 되므로 여기엔 없다.

  ChangeAnalysis / Execute  — 코드로 확정한 사실 (LLM 미개입)
  Generated / Report / Run  — 위 사실에 Agent 의 LLM 판단을 합친 최종 결과
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  register_project / get_project_overview — 프로젝트 개요 (정의서 상세 1)
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
#  analyze_changes — Git Diff + AST 변경 단위 식별 (정의서 (2))
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


class ChangeAnalysisResponse(BaseModel):
    """AST/그래프가 확정한 사실. 의도 해석은 Agent 가 한다."""

    project_id: str
    #: 분석한 원본 unified diff — Agent 프롬프트에 그대로 들어간다
    diff: str = ""
    #: 변경된 파일 → 변경 라인 구간
    changed_ranges: dict[str, list[tuple[int, int]]] = Field(default_factory=dict)
    changed_units: list[ChangedUnit] = Field(default_factory=list)
    impacted_units: list[ImpactedUnit] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    #: 그래프 영향도 등급 LOW / MEDIUM / HIGH 와 산정 근거
    risk: str = "LOW"
    risk_score: int = 0
    risk_reasons: list[str] = Field(default_factory=list)
    #: [UI 4] 기능 중요도 HIGH / MID / LOW — 코드 그래프로 확정한다 (importance.py)
    importance: str = "LOW"
    importance_rationale: str = ""
    #: 개요 (Agent 프롬프트 컨텍스트용)
    frameworks: list[str] = Field(default_factory=list)
    base_package: str | None = None
    #: 그래프가 비어 있는 경우(수집 미완료/실패) Agent 가 알 수 있도록
    graph_ready: bool = True
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  execute_tests — @SpringBootTest + JaCoCo 실행 (정의서 상세 4)
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
#  CLI 가 받는 최종 결과 — MCP 의 사실 + Agent 의 LLM 판단을 합친 것
# ---------------------------------------------------------------------------
class GeneratedResult(BaseModel):
    """`test_generate` / `test_run` 의 생성 결과 (CLI `codetest generate`)."""

    #: [상세 2] 사고의 사슬 — Agent
    thinking: str = ""
    #: (2) 변경 의도와 근거 — Agent
    intent: str = ""
    intent_rationale: str = ""
    #: [UI 4] 기능 중요도 — **MCP** 가 코드 그래프로 판단
    importance: str = "LOW"
    importance_rationale: str = ""
    #: (3) 정상/실패 케이스 — Agent
    test_cases: str = ""
    #: @SpringBootTest 테스트 코드 — Agent
    test_code: str = ""
    rationale: str = ""
    #: 테스트 대상 코드 (CLI "Test Code 보기")
    target_code: str = ""
    #: MCP 가 추론한 기준 패키지 — 실행 시 그대로 쓴다
    base_package: str | None = None
    #: MCP 개요 수집 완료 여부 / 경고
    graph_ready: bool = True
    analysis_warnings: list[str] = Field(default_factory=list)


class ReportResult(BaseModel):
    """`test_run` / `execute_tests` 의 실행+판정 결과 (CLI `codetest test`)."""

    #: PASS / FAIL — gradle exit code 가 사실이다
    result: str = "FAIL"
    #: [UI 3] 결과 적절성 판단과 근거 — Agent
    verdict: str = ""
    verdict_rationale: str = ""
    details: str = ""

    #: (2) "파악한 의도와 근거를 <Test Result 보기>의 결과값에 넣는다"
    intent: str = ""
    intent_rationale: str = ""
    #: [UI 4] 기능 중요도 — **MCP** 가 코드 그래프로 판단
    importance: str = "LOW"
    importance_rationale: str = ""

    # --- MCP 가 확정한 실행 사실 ---
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    failures: list[str] = Field(default_factory=list)
    #: [상세 4] JaCoCo 커버리지
    coverage: dict | None = None
    jacoco_enabled: bool = False
    #: (1) "생성된 Test Code를 @SpringBootTest 에 넣고 실행"
    springboot_applied: bool = False
    applied: list[str] = Field(default_factory=list)
    test_file_path: str = ""
    exit_code: int = 0
    output: str = ""


class RunResult(BaseModel):
    """`test_run` — 생성과 판정을 함께 돌려준다 (CLI `codetest run`)."""

    generated: GeneratedResult
    report: ReportResult
