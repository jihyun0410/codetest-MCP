"""MCP 서버 진입점 (FastMCP).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분"

이 서버는 LLM 을 호출하지 않는다. Agent 가 MCP 클라이언트로 붙어 도구를 호출한다.

    python -m codetest_mcp     # 패키지라 -m 으로 띄운다 (main.py 직접 실행은 import 실패)
    → streamable-http, 0.0.0.0:80 (CODETEST_MCP_TRANSPORT / _HOST / _PORT 로 변경)

도구 (hello 외 전부 정의서 근거)
  hello                 연결 확인용 에코
  register_project      프로젝트 등록 + Git clone + AST → 개요 DB 저장   (상세 1)
  delete_project        등록 정보/그래프/작업 사본 삭제
  get_project_overview  저장된 프로젝트 개요 조회                        (상세 1)
  analyze_changes       Git Diff + AST 로 변경 코드 단위·영향도 식별      (2)
  execute_tests         @SpringBootTest 주입 + JaCoCo 실행               (1), (상세 4)
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

from codetest_mcp import springboot
from codetest_mcp.config import get_logger, settings, setup_logging, verify_api_key
from codetest_mcp.db import (
    GraphNode,
    IngestStatus,
    NodeType,
    Project,
    init_db,
    session_scope,
)
from codetest_mcp.executor import ExecutionError, run_tests
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.graph.impact import ImpactAnalyzer, parse_diff_ranges
from codetest_mcp.graph.store import GraphStore
from codetest_mcp.repo import RepoService
from codetest_mcp.schemas import (
    ChangeAnalysisResponse,
    ChangedUnit,
    ExecuteResponse,
    ImpactedUnit,
    OverviewResponse,
    ProjectRead,
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


# --- 인증 --------------------------------------------------------------------
class ApiKeyMiddleware(Middleware):
    """HTTP 전송일 때만 X-API-Key 를 검사한다.

    stdio 는 Agent 가 이 서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다
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
    logger.info("%s 기동 (transport=%s)", settings.app_name, settings.transport)
    yield
    logger.info("%s 종료", settings.app_name)


mcp = FastMCP(
    name=settings.app_name,
    version="0.1.0",
    instructions=(
        "코드 기반 처리 전담 MCP. Git Diff/AST 변경 단위 식별, 프로젝트 개요 저장, "
        "@SpringBootTest 주입, JaCoCo 실행을 담당하며 LLM 을 호출하지 않는다. "
        "모든 응답은 코드로 확정한 사실만 담으므로 변경 의도 해석·중요도 판정·"
        "테스트 코드 작성·결과 적절성 판단은 호출하는 쪽(Agent)이 수행한다."
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

    즉시 ingest_status=PENDING 으로 반환한다. 수집 완료 여부는
    get_project_overview 로 확인한다 (PENDING → RUNNING → READY/FAILED).
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


@mcp.tool()
def get_project_overview(project_id: str) -> OverviewResponse:
    """DB 에 저장된 프로젝트 개요를 돌려준다 (Agent 프롬프트 컨텍스트).

    프레임워크, 언어 비중, 그래프 노드/간선 수, @SpringBootApplication 기준 패키지.
    """
    with session_scope() as db:
        project = _project(db, project_id)
        store = GraphStore(db, project.id)
        return OverviewResponse(
            project_id=project.id,
            name=project.name,
            ingest_status=project.ingest_status,
            ingest_error=project.ingest_error,
            frameworks=project.frameworks or [],
            language_stats=project.language_stats or {},
            node_counts=store.counts_by_type(),
            edge_counts=store.edge_counts_by_type(),
            base_package=_base_package(db, project.id),
            last_indexed_at=project.last_indexed_at,
        )


# --- 변경 코드 식별 (정의서 (2)) ----------------------------------------------
@mcp.tool()
def analyze_changes(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
) -> ChangeAnalysisResponse:
    """Git Diff 와 AST 분석 기반으로 실제 변경된 코드 단위·영향도를 식별한다.

    확정 가능한 사실만 반환한다. 의도(기능 추가/조건 변경/성능 개선) 해석은
    LLM 의 일이므로 이 결과를 받아 호출하는 쪽에서 수행한다.
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

        return ChangeAnalysisResponse(
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


# --- 테스트 실행 (정의서 (1), 상세 4) ------------------------------------------
@mcp.tool()
def execute_tests(
    project_id: str,
    test_code: Annotated[str, Field(description="생성된 Java 테스트 소스")],
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="실행 전에 작업 사본에 덮어쓸 변경 파일 (미커밋 변경분)"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
    base_package: Annotated[
        str | None,
        Field(description="package 선언이 없을 때 쓸 기준 패키지. 생략하면 개요에서 찾는다"),
    ] = None,
) -> ExecuteResponse:
    """생성된 Test Code 를 @SpringBootTest 에 넣고 JaCoCo 와 함께 실행한다.

    @SpringBootTest 가 없으면 주입하고 import/package 선언도 보강한 뒤
    src/test/java/<package>/<Class>.java 로 저장해 gradle test 를 돌린다.
    실행 사실만 반환한다 — 결과 적절성 판정은 호출하는 쪽의 몫이다.
    """
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
        result = run_tests(
            repo.path,
            prepared,
            overlay_sources=[(item.path, item.content) for item in sources],
        )
    except ExecutionError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        raise ToolError(f"테스트 실행 실패: {exc}") from None

    return ExecuteResponse(
        project_id=project_id,
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
    )


# --- 실행 --------------------------------------------------------------------
def main() -> None:
    """`python -m codetest_mcp` / `python codetest_mcp/main.py` 공통 진입점."""
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
