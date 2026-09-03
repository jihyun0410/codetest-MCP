"""MCP 서버 진입점 (FastMCP).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분"

흐름은 **CLI(codereview_gitver) → MCP(codetest-MCP) → Agent(codetest)** 다.
이 서버는 LLM 을 직접 호출하지 않는다. 코드 기반 처리(Git clone·AST·변경 단위 식별·
기능 중요도 판정·@SpringBootTest 주입·JaCoCo 실행)를 끝낸 뒤, LLM 판단이 필요한
부분만 Agent(FastAPI)로 넘긴다.

    python -m codetest_mcp     # 패키지라 -m 으로 띄운다 (main.py 직접 실행은 import 실패)
    → streamable-http, 0.0.0.0:80 (CODETEST_MCP_TRANSPORT / _HOST / _PORT 로 변경)

도구 (hello 외 전부 정의서 근거)
  hello                 연결 확인용 에코
  register_project      프로젝트 등록 + Git clone + AST → 개요 DB 저장   (상세 1)
  delete_project        등록 정보/그래프/작업 사본 삭제
  test_generate         변경 분석 + 중요도 판정 + Test Code 생성          (1), (2), (3)
  test_run              생성 + @SpringBootTest 실행 + 적절성 판정         (1)
  execute_tests         @SpringBootTest 주입 + JaCoCo 실행 + 판정         (1), (상세 4)

변경 단위 식별과 프로젝트 개요 조회는 **도구로 노출하지 않는다.** CLI 가 직접 쓸 일이
없고, test_generate / test_run 이 내부에서 만들어 Agent 프롬프트 입력으로 넘긴다.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from codetest_mcp import importance as importance_module
from codetest_mcp import springboot
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger, settings, setup_logging, verify_api_key
from codetest_mcp.db import (
    GraphNode,
    IngestStatus,
    NodeType,
    Project,
    init_db,
    session_scope,
)
from codetest_mcp.executor import ExecutionError, ExecutionResult, run_tests
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.graph.impact import ImpactAnalyzer, parse_diff_ranges
from codetest_mcp.graph.store import GraphStore
from codetest_mcp.repo import RepoService
from codetest_mcp.schemas import (
    ChangeAnalysis,
    ChangedUnit,
    ExecuteResponse,
    GeneratedTest,
    ImpactedUnit,
    ProjectRead,
    RunResponse,
    SourceFilePayload,
)

setup_logging()
logger = get_logger(__name__)


# --- 헬퍼 --------------------------------------------------------------------
def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ToolError(f"프로젝트를 찾을 수 없습니다: {project_id}")
    return project


def _to_read(project: Project) -> ProjectRead:
    payload = ProjectRead.model_validate(project)
    payload.has_github_token = bool(project.github_token)
    return payload


def _base_package(db: Session, project_id: str) -> str | None:
    """저장된 그래프의 파일 경로에서 기준 패키지를 추론한다."""
    paths = list(
        db.scalars(
            select(GraphNode.file_path).where(
                GraphNode.project_id == project_id,
                GraphNode.node_type == NodeType.FILE.value,
            )
        )
    )
    return springboot.detect_base_package(paths)


# --- 백그라운드 수집 ---------------------------------------------------------
def run_ingest(project_id: str) -> None:
    """등록 직후: clone → AST 파싱 → Graph 적재 → 개요 DB 저장 (정의서 상세 1)."""
    with session_scope() as db:
        project = db.get(Project, project_id)
        if project is None:
            return

        project.ingest_status = IngestStatus.RUNNING.value
        project.ingest_error = None
        db.commit()

        try:
            stats = GraphBuilder(db, project).build_full(reset=True)

            project.ingest_status = IngestStatus.READY.value
            project.frameworks = stats.frameworks
            project.language_stats = stats.language_stats
            project.last_indexed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(
                "[%s] 개요 수집 완료 — 노드 %d, 간선 %d (%.2fs)",
                project.name, stats.node_count, stats.edge_count, stats.elapsed_seconds,
            )
        except Exception as exc:  # 어떤 실패든 상태에 남긴다
            db.rollback()
            project.ingest_status = IngestStatus.FAILED.value
            project.ingest_error = str(exc)
            db.commit()
            logger.exception("개요 수집 실패: %s", project_id)


# --- 변경 분석 (내부 전용, 정의서 (2)) ------------------------------------------
def _analyze(project_id: str, diff: str) -> tuple[ChangeAnalysis, importance_module.ImportanceVerdict]:
    """Git Diff + AST 로 변경 단위·영향도를 확정하고 기능 중요도까지 판정한다.

    도구로 노출하지 않는다 — test_generate / test_run 의 첫 단계다.
    확정 가능한 사실만 만든다. 의도(기능 추가/조건 변경/성능 개선) 해석은 Agent 의 몫이다.
    """
    with session_scope() as db:
        project = _project(db, project_id)

        analyzer = ImpactAnalyzer(GraphStore(db, project.id))
        ranges = parse_diff_ranges(diff)
        report = analyzer.analyze(ranges)

        graph_ready = project.ingest_status == IngestStatus.READY.value
        warnings: list[str] = []
        if not graph_ready:
            warnings.append(
                f"프로젝트 개요 수집이 완료되지 않았습니다 (상태: {project.ingest_status}). "
                "AST 기반 변경 단위 식별 결과가 비어 있을 수 있습니다."
            )
        if ranges and not report.changed:
            warnings.append("Diff 라인과 겹치는 그래프 노드를 찾지 못했습니다.")

        analysis = ChangeAnalysis(
            project_id=project.id,
            changed_ranges=ranges,
            changed_units=[
                ChangedUnit(
                    qualified_name=info.qualified_name,
                    name=info.name,
                    node_type=info.node_type,
                    file_path=info.file_path,
                    language=(info.meta or {}).get("language"),
                    signature=info.signature,
                    start_line=info.start_line,
                    end_line=info.end_line,
                    entrypoint=bool((info.meta or {}).get("entrypoint")),
                    http_method=(info.meta or {}).get("http_method"),
                    route=(info.meta or {}).get("route"),
                )
                for info in report.changed
            ],
            impacted_units=[
                ImpactedUnit(
                    qualified_name=info.qualified_name,
                    node_type=info.node_type,
                    file_path=info.file_path,
                    depth=info.depth,
                    via=info.via,
                )
                for info in report.impacted
            ],
            affected_files=report.affected_files,
            risk=report.risk.value,
            risk_score=report.score,
            risk_reasons=report.reasons,
            frameworks=project.frameworks or [],
            base_package=_base_package(db, project.id),
            graph_ready=graph_ready,
            warnings=warnings,
        )

    # [UI] 4 기능 중요도 — 그래프 사실만으로 확정한다 (LLM 미사용)
    return analysis, importance_module.judge(report, ranges)


def _run_gradle(project_id: str, test_code: str, base_package: str | None,
                sources: list[SourceFilePayload]) -> ExecutionResult:
    """@SpringBootTest 를 주입해 Gradle + JaCoCo 로 실행한다 (정의서 상세 4)."""
    with session_scope() as db:
        project = _project(db, project_id)
        pkg = base_package or _base_package(db, project.id)
        repo = RepoService(project.id, project.git_url, project.github_token)
        branch = project.default_branch

    try:
        prepared = springboot.prepare(test_code, pkg)
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    try:
        repo.ensure_clone(branch)
        return run_tests(
            repo.path,
            prepared,
            overlay_sources=[(item.path, item.content) for item in sources],
        )
    except ExecutionError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        raise ToolError(f"테스트 실행 실패: {exc}") from None


def _execution_facts(result: ExecutionResult) -> dict:
    """Agent 에 넘길 실행 사실 (판정 근거)."""
    return {
        "exit_code": result.exit_code,
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "total": result.total,
        "failures": result.failures,
        "coverage": result.coverage,
        "jacoco_enabled": result.jacoco_enabled,
        "springboot_applied": result.springboot_applied,
        "applied": result.applied,
        "test_file_path": result.test_file_path,
        "output": result.output,
    }


def _result_of(execution: ExecutionResult) -> str:
    """PASS / FAIL 은 집계로 확정한다 (판정이 아니라 사실이다)."""
    return "PASS" if execution.exit_code == 0 and execution.failed == 0 else "FAIL"


def _agent(call, *args, **kwargs):
    """Agent 호출 실패를 MCP 도구 오류로 옮긴다."""
    try:
        return call(*args, **kwargs)
    except AgentError as exc:
        raise ToolError(str(exc)) from None


# --- 인증 --------------------------------------------------------------------
class ApiKeyMiddleware(Middleware):
    """HTTP 전송일 때만 X-API-Key 를 검사한다.

    stdio 는 CLI 가 이 서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다
    (헤더 자체가 없다). CODETEST_MCP_API_KEYS 가 비어 있으면 인증 비활성화.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        if headers and not verify_api_key(headers.get("x-api-key")):
            raise ToolError("유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.")
        return await call_next(context)


# --- 서버 --------------------------------------------------------------------
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    """기동 시 런타임 디렉터리 생성 + 테이블 초기화."""
    settings.ensure_directories()
    init_db()
    logger.info(
        "%s 기동 (transport=%s, agent=%s)", settings.app_name, settings.transport, settings.agent_url
    )
    yield
    logger.info("%s 종료", settings.app_name)


mcp = FastMCP(
    name=settings.app_name,
    version="0.1.0",
    instructions=(
        "코드 기반 처리 전담 MCP. Git Diff/AST 변경 단위 식별, 프로젝트 개요 저장, "
        "기능 중요도 판정, @SpringBootTest 주입, JaCoCo 실행을 담당하며 LLM 을 직접 "
        "호출하지 않는다. 변경 의도 해석·테스트 코드 작성·결과 적절성 판단이 필요하면 "
        "Agent(FastAPI)로 넘긴다."
    ),
    lifespan=lifespan,
    middleware=[ApiKeyMiddleware()],
)


# --- 연결 확인 ----------------------------------------------------------------
@mcp.tool()
def hello(name: str) -> str:
    """연결 확인용 에코. 이름을 그대로 돌려준다."""
    return f"Hello! Test Code MCP ! {name}"


# --- 프로젝트 개요 (정의서 상세 1) --------------------------------------------
@mcp.tool()
def register_project(
    name: Annotated[str, Field(min_length=1, max_length=200, description="프로젝트 명")],
    git_url: Annotated[str, Field(description="대상 저장소 Git URL (http(s):// 또는 git@)")],
    owner: Annotated[str, Field(min_length=1, max_length=100, description="담당자")],
    github_token: Annotated[str | None, Field(description="Github API Token")] = None,
    default_branch: Annotated[str, Field(description="기준 브랜치")] = "main",
) -> ProjectRead:
    """프로젝트를 등록하고 Git clone + AST 개요 수집을 백그라운드로 시작한다.

    즉시 ingest_status=PENDING 으로 반환한다 (PENDING → RUNNING → READY/FAILED).
    수집이 끝나기 전에 test_generate 를 부르면 응답의 analysis_warnings 로 알려 준다.
    """
    if not git_url.startswith(("http://", "https://", "git@")):
        raise ToolError("git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")

    with session_scope() as db:
        if db.scalar(select(Project).where(Project.name == name)):
            raise ToolError(f"이미 같은 이름의 프로젝트가 있습니다: {name}")

        project = Project(
            name=name,
            git_url=git_url.rstrip("/"),
            owner=owner,
            github_token=github_token,
            default_branch=default_branch,
            ingest_status=IngestStatus.PENDING.value,
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        threading.Thread(target=run_ingest, args=(project.id,), daemon=True).start()
        return _to_read(project)


@mcp.tool()
def delete_project(project_id: str) -> dict:
    """프로젝트와 그래프를 함께 삭제하고 작업 사본(clone)도 제거한다."""
    with session_scope() as db:
        project = _project(db, project_id)
        repo = RepoService(project.id, project.git_url, project.github_token)
        db.delete(project)
        db.commit()

    repo.remove()
    return {"deleted": project_id}


# --- Test Code 생성 (정의서 (1) generate, (2), (3)) ------------------------------
@mcp.tool()
def test_generate(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="변경된 소스 파일 (테스트 대상 코드)"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
) -> GeneratedTest:
    """변경 분석 + 기능 중요도 판정(MCP) 후 Test Code 생성(Agent)까지 마친다.

    1. Git Diff + AST 로 변경된 코드 단위와 영향 범위를 확정한다 (코드 기반)
    2. [UI] 4 기능 중요도 High/Mid/Low 와 **판단 근거**를 확정한다 (코드 기반)
    3. 확정한 사실을 Agent 로 넘겨 변경 의도·사고의 사슬·@SpringBootTest 코드를 받는다
    """
    return _generate(project_id, diff, sources)


def _generate(project_id: str, diff: str, sources: list[SourceFilePayload]) -> GeneratedTest:
    """test_generate 본문. test_run 이 도구 껍데기를 거치지 않고 바로 쓴다."""
    analysis, verdict = _analyze(project_id, diff)

    payload = dict(analysis.model_dump())
    payload["diff"] = diff  # Agent 가 원본 Diff 를 인용해 근거를 쓴다

    generated = _agent(
        agent_client.generate,
        project_id=project_id,
        analysis=payload,
        sources=[(item.path, item.content) for item in sources],
        project_name=_project_name(project_id),
    )

    return GeneratedTest(
        project_id=project_id,
        thinking=generated.get("thinking", ""),
        intent=generated.get("intent", ""),
        intent_rationale=generated.get("intent_rationale", ""),
        test_cases=generated.get("test_cases", ""),
        test_code=generated.get("test_code", ""),
        rationale=generated.get("rationale", ""),
        target_code=generated.get("target_code", ""),
        base_package=generated.get("base_package") or analysis.base_package,
        risk=analysis.risk,
        risk_score=analysis.risk_score,
        changed_units=analysis.changed_units,
        affected_files=analysis.affected_files,
        analysis_warnings=analysis.warnings,
        **verdict.to_dict(),
    )


# --- 생성 + 실행 (정의서 (1) run) ------------------------------------------------
@mcp.tool()
def test_run(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="변경된 소스 파일 (테스트 대상 코드 + 작업 사본 덮어쓰기)"),
    ] = [],  # noqa: B006
) -> RunResponse:
    """Test Code 생성부터 @SpringBootTest 실행·적절성 판정까지 한 번에 수행한다.

    CLI `codetest run` 이 부르는 도구다. 생성 결과와 실행 리포트를 함께 돌려준다.
    """
    generated = _generate(project_id, diff, sources)
    if not generated.test_code:
        raise ToolError("Agent 가 Test Code 를 생성하지 못했습니다.")

    report = _execute(
        project_id,
        test_code=generated.test_code,
        sources=sources,
        base_package=generated.base_package,
        diff=diff,
        intent=generated.intent,
        intent_rationale=generated.intent_rationale,
    )
    return RunResponse(generated=generated, report=report)


# --- 테스트 실행 (정의서 (1) test, 상세 4) ---------------------------------------
@mcp.tool()
def execute_tests(
    project_id: str,
    test_code: Annotated[str, Field(description="실행할 Java 테스트 소스")],
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="실행 전에 작업 사본에 덮어쓸 변경 파일 (미커밋 변경분)"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
    base_package: Annotated[
        str | None,
        Field(description="package 선언이 없을 때 쓸 기준 패키지. 생략하면 개요에서 찾는다"),
    ] = None,
    diff: Annotated[
        str, Field(description="기능 중요도를 다시 판단할 변경분 unified diff")
    ] = "",
    intent: Annotated[str, Field(description="앞서 파악한 변경 의도")] = "",
    intent_rationale: Annotated[str, Field(description="그 의도로 본 근거")] = "",
) -> ExecuteResponse:
    """Test Code 를 @SpringBootTest 에 넣어 JaCoCo 와 함께 실행하고 리포트를 만든다.

    @SpringBootTest 가 없으면 주입하고 import/package 선언도 보강한 뒤
    src/test/java/<package>/<Class>.java 로 저장해 gradle test 를 돌린다.
    실행 집계·커버리지·기능 중요도는 여기서 확정하고, 결과 적절성 판정만 Agent 가 한다.
    """
    return _execute(project_id, test_code, sources, base_package, diff, intent, intent_rationale)


def _execute(
    project_id: str,
    test_code: str,
    sources: list[SourceFilePayload],
    base_package: str | None = None,
    diff: str = "",
    intent: str = "",
    intent_rationale: str = "",
) -> ExecuteResponse:
    """execute_tests 본문. test_run 이 도구 껍데기를 거치지 않고 바로 쓴다."""
    _, verdict = _analyze(project_id, diff)
    result = _run_gradle(project_id, test_code, base_package, sources)
    facts = _execution_facts(result)

    judged = _agent(
        agent_client.report,
        project_id=project_id,
        execution=facts,
        test_code=test_code,
        intent=intent,
        intent_rationale=intent_rationale,
    )

    return ExecuteResponse(
        project_id=project_id,
        result=_result_of(result),
        exit_code=result.exit_code,
        output=result.output,
        passed=result.passed,
        failed=result.failed,
        skipped=result.skipped,
        total=result.total,
        failures=result.failures,
        coverage=result.coverage,
        jacoco_enabled=result.jacoco_enabled,
        springboot_applied=result.springboot_applied,
        applied=result.applied,
        test_file_path=result.test_file_path,
        command=result.command,
        verdict=judged.get("verdict", ""),
        verdict_rationale=judged.get("verdict_rationale", ""),
        details=judged.get("details", ""),
        intent=judged.get("intent") or intent,
        intent_rationale=judged.get("intent_rationale") or intent_rationale,
        **verdict.to_dict(),
    )


def _project_name(project_id: str) -> str:
    with session_scope() as db:
        return _project(db, project_id).name


# --- 실행 --------------------------------------------------------------------
def main() -> None:
    """`python -m codetest_mcp` / `python codetest_mcp/main.py` 공통 진입점."""
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
