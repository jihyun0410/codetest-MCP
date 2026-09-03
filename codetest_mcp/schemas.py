"""MCP 도구 응답 스키마.

CLI(codereview_gitver)가 MCP 로 호출하는 계약이다. 요청 파라미터는 도구 시그니처가
그대로 JSON Schema 가 되므로 여기엔 없다.

흐름은 **CLI → MCP → Agent** 다. MCP 응답은 두 가지가 섞인다.
  · 코드로 확정한 사실 — 변경 단위·영향도·기능 중요도·실행 집계·커버리지
  · Agent(LLM)가 돌려준 판단 — 변경 의도·사고의 사슬·Test Code·적절성 판정
CLI 는 이 둘을 하나의 결과 화면으로 보여 준다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  register_project — 프로젝트 개요 (정의서 상세 1)
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
#  변경 단위 식별 — Git Diff + AST (정의서 (2))
#  도구로 노출하지 않는다. test_generate / test_run 이 내부에서 만들어
#  Agent 프롬프트 입력으로 넘기는 중간 산출물이다.
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


class ChangeAnalysis(BaseModel):
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
#  기능 중요도 — 코드 기반 판정 (정의서 [UI] 4)
# ---------------------------------------------------------------------------
class ImportanceMixin(BaseModel):
    """High / Mid / Low 와 **그렇게 판단한 근거**.

    정의서 [UI] 4 + 흐름 3 "반드시 어떠한 근거로 표시하는지 명확히 제시해야 함".
    CLI 는 등급과 근거를 결과 화면에 함께 출력한다.
    """

    importance: str = "LOW"
    importance_score: int = 0
    #: 등급이 나온 이유 (한 줄에 하나) — 결과 화면에 그대로 실린다
    importance_rationale: str = ""
    importance_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  test_generate — 변경 분석 + 중요도(MCP) + Test Code 생성(Agent)
# ---------------------------------------------------------------------------
class GeneratedTest(ImportanceMixin):
    """CLI `codetest generate` 응답.

    MCP 가 확정한 사실(중요도·기준 패키지)과 Agent 가 만든 판단(의도·사고의 사슬·
    Test Code)을 합쳐 돌려준다.
    """

    project_id: str = ""
    #: --- Agent(LLM) 판단 ---
    thinking: str = ""
    intent: str = ""
    intent_rationale: str = ""
    test_cases: str = ""
    test_code: str = ""
    rationale: str = ""
    target_code: str = ""
    #: --- MCP 가 코드로 확정한 사실 ---
    base_package: str | None = None
    risk: str = "LOW"
    risk_score: int = 0
    changed_units: list[ChangedUnit] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    #: 그래프 미수집 등 CLI 가 사용자에게 알려야 하는 주의 사항
    analysis_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  execute_tests — @SpringBootTest + JaCoCo 실행 (정의서 상세 4) + 적절성 판단
# ---------------------------------------------------------------------------
class ExecuteResponse(ImportanceMixin):
    """CLI `codetest test` 응답 (= `test_run` 의 report).

    실행 집계·커버리지·기능 중요도는 MCP 가 코드로 확정한 사실이고,
    적절성 판정(verdict)과 변경 의도는 Agent(LLM)가 돌려준 판단이다.
    """

    project_id: str
    #: PASS / FAIL — JUnit 집계와 gradle exit code 로 확정한다
    result: str = "PASS"
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

    #: --- Agent(LLM) 판단 ---
    verdict: str = ""
    verdict_rationale: str = ""
    details: str = ""
    intent: str = ""
    intent_rationale: str = ""


# ---------------------------------------------------------------------------
#  test_run — 생성 + 실행을 한 번에 (CLI `codetest run`)
# ---------------------------------------------------------------------------
class RunResponse(BaseModel):
    """생성 결과와 실행 리포트를 함께 돌려준다."""

    generated: GeneratedTest
    report: ExecuteResponse
