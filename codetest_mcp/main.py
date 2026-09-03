"""MCP 서버 진입점 (FastMCP).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

CLI 가 MCP 클라이언트로 붙어 도구를 호출한다. MCP 는 코드 기반 사실을 확정하고,
LLM 판단이 필요한 부분만 Agent(codetest)에 FastAPI 로 넘긴 뒤 결과를 합쳐 돌려준다.

    IntelliJ Terminal → CLI → MCP(이 서버) → Agent(LLM) → MCP → CLI

    python -m codetest_mcp     # 패키지라 -m 으로 띄운다 (main.py 직접 실행은 import 실패)
    → streamable-http, 0.0.0.0:80 (CODETEST_MCP_TRANSPORT / _HOST / _PORT 로 변경)

도구 (hello 외 전부 정의서 근거)
  hello                 연결 확인용 에코
  register_project      프로젝트 등록 + Git clone + AST → 개요 DB 저장   (상세 1)
  delete_project        등록 정보/그래프/작업 사본 삭제
  test_generate         codetest generate — 분석 → Agent 생성
  test_run              codetest run      — 분석 → 생성 → 실행 → 판정
  execute_tests         codetest test     — 실행 → 판정                 (1), (상세 4)

프로젝트 개요 조회와 변경 단위 식별은 **도구로 노출하지 않는다.** CLI 가 직접 쓸 일이
없고, test_generate / test_run 이 내부에서(`orchestrator.analyze`) 만들어 쓰는 중간
산출물이다. 개요 수집이 끝나지 않았으면 test_generate 응답의 analysis_warnings 로 알린다.

기능 중요도(High/Mid/Low)는 Agent 에 묻지 않는다. 코드 그래프로 확정하는 값이라
MCP 가 정한다 (`importance.py`).
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

from codetest_mcp import orchestrator
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger, settings, setup_logging, verify_api_key
from codetest_mcp.db import IngestStatus, Project, init_db, session_scope
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.orchestrator import FlowError, project_or_fail
from codetest_mcp.repo import RepoService
from codetest_mcp.schemas import (
    GeneratedResult,
    ProjectRead,
    ReportResult,
    RunResult,
    SourceFilePayload,
)

setup_logging()
logger = get_logger(__name__)


# --- 헬퍼 --------------------------------------------------------------------
def _to_read(project: Project) -> ProjectRead:
    payload = ProjectRead.model_validate(project)
    payload.has_github_token = bool(project.github_token)
    return payload


def _flow(call, *args, **kwargs):
    """FlowError 를 ToolError 로 옮긴다 — CLI 화면에 이유가 그대로 뜬다."""
    try:
        return call(*args, **kwargs)
    except FlowError as exc:
        raise ToolError(str(exc)) from None


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
        "%s 기동 (transport=%s, agent=%s)",
        settings.app_name, settings.transport, settings.agent_base_url,
    )
    yield
    logger.info("%s 종료", settings.app_name)


mcp = FastMCP(
    name=settings.app_name,
    version="0.1.0",
    instructions=(
        "CLI 명령의 진입점. Git Diff/AST 변경 단위 식별, 프로젝트 개요 저장, "
        "기능 중요도 판단, @SpringBootTest 주입, JaCoCo 실행을 코드 기반으로 수행하고, "
        "변경 의도 해석·테스트 코드 작성·결과 적절성 판단처럼 LLM 이 필요한 부분만 "
        "Agent 에 FastAPI 로 위임한다."
    ),
    lifespan=lifespan,
    middleware=[ApiKeyMiddleware()],
)


# --- 연결 확인 ----------------------------------------------------------------
@mcp.tool()
def hello(name: str) -> str:
    """연결 확인용 에코. Agent 연결 상태도 함께 알린다."""
    try:
        agent_client.health()
        agent = "ok"
    except AgentError as exc:
        agent = f"unreachable: {exc}"
    return f"Hello! Test Code MCP ! {name} (agent: {agent})"


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
    test_generate 응답의 analysis_warnings 로 알린다 (PENDING → RUNNING → READY/FAILED).

    **같은 이름·같은 git_url 로 다시 부르면 기존 프로젝트를 그대로 돌려준다.**
    CLI 는 project_id 를 로컬 `.codetest/config.json` 에만 두는데, 이 파일은
    .gitignore 대상이라 clone·PC 교체·정리 명령으로 쉽게 사라진다. 그때 재등록을
    거부하면 CLI 가 project_id 를 되찾을 길이 없어
    "등록된 프로젝트가 없습니다" → register → "이미 있습니다" 가 무한히 반복된다.
    이 도구가 재조회 경로를 겸해야 그 고리가 끊긴다.
    """
    if not git_url.startswith(("http://", "https://", "git@")):
        raise ToolError("git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")

    normalized_url = git_url.rstrip("/")

    with session_scope() as db:
        existing = db.scalar(select(Project).where(Project.name == name))
        if existing is not None:
            # 이름이 같아도 저장소가 다르면 진짜 충돌이다. 조용히 넘기면 엉뚱한
            # 저장소를 대상으로 테스트를 돌리게 되므로 그대로 막는다.
            if existing.git_url != normalized_url:
                raise ToolError(
                    f"이미 같은 이름의 프로젝트가 있고 git_url 이 다릅니다: {name}\n"
                    f"  등록된 주소: {existing.git_url}\n"
                    f"  요청한 주소: {normalized_url}\n"
                    "  다른 이름으로 등록하거나 기존 프로젝트를 삭제하세요."
                )

            # 같은 저장소면 재등록이 아니라 조회다 — CLI 가 project_id 를 되찾는다.
            # 지난 수집이 실패한 채로 남아 있으면 여기서 다시 시작해 준다.
            # (실패 상태 그대로 돌려주면 삭제 말고는 복구할 방법이 없다)
            if existing.ingest_status == IngestStatus.FAILED.value:
                logger.info("[%s] 지난 개요 수집이 실패해 다시 시작합니다", existing.name)
                threading.Thread(target=run_ingest, args=(existing.id,), daemon=True).start()
            else:
                logger.info("[%s] 이미 등록된 프로젝트를 그대로 돌려줍니다", existing.name)
            return _to_read(existing)

        project = Project(
            name=name,
            git_url=normalized_url,
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
        project = _flow(project_or_fail, db, project_id)
        repo = RepoService(project.id, project.git_url, project.github_token)
        db.delete(project)
        db.commit()

    repo.remove()
    return {"deleted": project_id}


# --- CLI 명령 흐름 -------------------------------------------------------------
@mcp.tool()
def test_generate(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload], Field(description="변경 파일 본문 (테스트 대상 코드)")
    ] = [],  # noqa: B006
) -> GeneratedResult:
    """`codetest generate` — 변경 의도를 파악하고 @SpringBootTest 를 생성한다.

    MCP 가 변경 단위·영향도·중요도를 확정한 뒤 Agent 에 생성을 맡긴다. 실행은 하지 않는다.
    """
    return _flow(orchestrator.test_generate, project_id, diff, sources)


@mcp.tool()
def test_run(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload], Field(description="변경 파일 본문 (테스트 대상 코드)")
    ] = [],  # noqa: B006
) -> RunResult:
    """`codetest run` — 분석 → 생성 → 실행 → 판정을 한 번에 (정의서 흐름 3~5).

    Gradle 빌드와 Spring 컨텍스트 기동이 포함되어 수 분이 걸릴 수 있다.
    """
    return _flow(orchestrator.test_run, project_id, diff, sources)


# --- 테스트 실행 + 판정 (정의서 (1), 상세 4) ------------------------------------
@mcp.tool()
def execute_tests(
    project_id: str,
    test_code: Annotated[str, Field(description="실행할 Java 테스트 소스")],
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="실행 전에 작업 사본에 덮어쓸 변경 파일 (미커밋 변경분)"),
    ] = [],  # noqa: B006
    base_package: Annotated[
        str | None,
        Field(description="package 선언이 없을 때 쓸 기준 패키지. 생략하면 개요에서 찾는다"),
    ] = None,
    diff: Annotated[
        str, Field(description="변경분 unified diff — 기능 중요도를 다시 판단하는 데 쓴다")
    ] = "",
    intent: Annotated[str, Field(description="이전에 파악한 변경 의도")] = "",
    intent_rationale: Annotated[str, Field(description="그 의도의 근거")] = "",
) -> ReportResult:
    """`codetest test` — Test Code 를 @SpringBootTest 로 실행하고 적절성을 판정한다.

    @SpringBootTest 가 없으면 주입하고 import/package 선언도 보강한 뒤
    src/test/java/<package>/<Class>.java 로 저장해 gradle test 를 돌린다.
    실행 사실은 MCP 가, 적절성 판단은 Agent(LLM)가 만든다.
    """
    return _flow(
        orchestrator.execute_tests,
        project_id, test_code, sources, base_package, diff, intent, intent_rationale,
    )


# --- 실행 --------------------------------------------------------------------
def main() -> None:
    """`python -m codetest_mcp` 진입점."""
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
