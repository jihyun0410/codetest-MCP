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
  prepare_test          @SpringBootTest 주입 + 저장 경로 계산            (1)
  report_execution      로컬 실행 결과 → 중요도 재판정 + Agent 적절성 판정 (상세 4)

**테스트 실행은 이 서버가 하지 않는다.** CLI 가 개발자 PC 의 프로젝트에서 Gradle 로
돌리고 그 결과만 report_execution 으로 보내온다. 그래서 실행 단계에는 git·JDK·Gradle
이 필요 없다 (등록 시 AST 수집에는 여전히 clone 이 필요하다).

프로젝트 개요 조회와 변경 단위 식별은 **도구로 노출하지 않는다.** CLI 가 직접 쓸 일이
없고, test_generate / test_run 이 내부에서(`orchestrator.analyze`) 만들어 쓰는 중간
산출물이다. 개요 수집이 끝나지 않았으면 test_generate 응답의 analysis_warnings 로 알린다.

기능 중요도(High/Mid/Low)는 Agent 에 묻지 않는다. 코드 그래프로 확정하는 값이라
MCP 가 정한다 (`importance.py`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from typing import Annotated, TypeVar

from anyio import to_thread
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import Field
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from codetest_mcp import orchestrator
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger, settings, setup_logging, trace, verify_api_key
from codetest_mcp.db import IngestStatus, Project, init_db, session_scope
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.orchestrator import FlowError, project_or_fail
from codetest_mcp.repo import RepoService, SourceFile
from codetest_mcp.schemas import (
    GeneratedResult,
    PreparedTestResponse,
    ProjectRead,
    ReportResult,
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


T = TypeVar("T")


def _progress_token(meta: object) -> object | None:
    """요청 `_meta` 에서 progressToken 을 꺼낸다.

    라이브러리 버전에 따라 meta 가 객체(`.progressToken`)이거나 dict
    (`progress_token` / `progressToken`)다. 속성으로만 읽으면 dict 일 때
    AttributeError 로 도구 호출이 시작하자마자 죽는다.
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get("progress_token", meta.get("progressToken"))
    return getattr(meta, "progressToken", None)


async def _keepalive(ctx: Context, work: Callable[[], T]) -> T:
    """work 를 스레드에서 돌리며, 끝날 때까지 CLI 쪽으로 진행 알림을 보낸다.

    MCP 가 올라가는 서버는 60초 동안 오가는 메시지가 없으면 Fallback 처리한다.
    SSE 의 `: ping` 주석은 바이트일 뿐 MCP 메시지가 아니므로, 도구 호출 스트림에
    진짜 JSON-RPC 알림을 흘린다. 호출자가 progressToken 을 주면 진행 알림,
    안 주면 로그 알림 — 어느 쪽이든 이 요청의 응답 스트림으로 나간다.
    Agent 가 살아 있는지는 agent_client 가 Agent 의 ping 으로 따로 지킨다.
    """
    task = asyncio.ensure_future(to_thread.run_sync(work))
    started = time.monotonic()
    meta = ctx.request_context.meta if ctx.request_context else None
    has_token = _progress_token(meta) is not None
    trace(
        logger, "작업 스레드 시작 — %.0f초마다 CLI 로 %s 알림",
        settings.heartbeat_seconds, "진행(progress)" if has_token else "로그(log)",
    )

    while True:
        done, _ = await asyncio.wait({task}, timeout=settings.heartbeat_seconds)
        if done:
            trace(logger, "작업 스레드 종료 — %.1f초", time.monotonic() - started)
            return task.result()
        elapsed = int(time.monotonic() - started)
        message = f"처리 중 — {elapsed}초 경과"
        trace(logger, "keepalive 전송 (%s) — %d초 경과", "progress" if has_token else "log", elapsed)
        if has_token:
            await ctx.report_progress(elapsed, message=message)
        else:
            await ctx.info(message)


# --- 개요 수집 (register_project 가 끝날 때까지 기다린다) ---------------------
def run_ingest(project_id: str) -> None:
    """등록 직후: AST 파싱 → Graph 적재 → 개요 DB 저장 (정의서 상세 1).

    CLI 가 등록 때 올려 준 **커밋 소스 스냅샷**으로 파싱한다. 그래서 이 서버에
    git 이 없어도 개요가 수집된다. 스냅샷이 없을 때만 저장소를 clone 한다
    (예전 CLI 로 등록했거나 sources 를 보내지 않은 경우).
    """
    trace(logger, "[ingest 1/4] 개요 수집 시작: %s", project_id)
    with session_scope() as db:
        project = db.get(Project, project_id)
        if project is None:
            trace(logger, "[ingest] 프로젝트가 없어 중단: %s", project_id)
            return

        project.ingest_status = IngestStatus.RUNNING.value
        project.ingest_error = None
        db.commit()
        trace(logger, "[ingest 2/4] [%s] 상태 RUNNING 으로 변경", project.name)

        stored = orchestrator.committed_sources(db, project_id)
        sources = (
            [SourceFile(path=path, content=content) for path, content in stored.items()]
            if stored
            else None
        )
        if sources is None:
            trace(
                logger,
                "[%s] 커밋 스냅샷이 없어 저장소를 clone 합니다 (git 필요). "
                "최신 CLI 로 `codetest project register` 를 다시 실행하면 clone 없이 수집됩니다.",
                project.name,
            )
        else:
            trace(logger, "[ingest 3/4] 커밋 스냅샷 %d개로 AST 파싱·Graph 적재", len(sources))

        try:
            stats = GraphBuilder(db, project).build_full(reset=True, sources=sources)

            project.ingest_status = IngestStatus.READY.value
            project.frameworks = stats.frameworks
            project.language_stats = stats.language_stats
            project.last_indexed_at = datetime.now(timezone.utc)
            db.commit()
            trace(
                logger,
                "[ingest 4/4] [%s] 개요 수집 완료 — 노드 %d, 간선 %d (%.2fs)",
                project.name, stats.node_count, stats.edge_count, stats.elapsed_seconds,
            )
        except Exception as exc:  # 어떤 실패든 상태에 남긴다
            db.rollback()
            project.ingest_status = IngestStatus.FAILED.value
            project.ingest_error = str(exc)
            db.commit()
            logger.exception("개요 수집 실패: %s", project_id)
            trace(logger, "[ingest] 개요 수집 실패 — 상태 FAILED: %s: %s", type(exc).__name__, exc)


#: 값을 로그에 남기지 않는 인자 (비밀값)
_SECRET_ARGS = {"github_token"}


def _summarize(arguments: dict | None) -> str:
    """도구 인자를 로그용 한 줄로 줄인다 — 소스 본문·diff 는 길이만, 비밀값은 가린다."""
    parts = []
    for key, value in (arguments or {}).items():
        if key == "git_url" and isinstance(value, str):
            value = value.rsplit("@", 1)[-1]  # user:token@ 제거
        if key in _SECRET_ARGS:
            shown = "***" if value else "없음"
        elif isinstance(value, str):
            shown = repr(value) if len(value) <= 60 else f"<{len(value)}자>"
        elif isinstance(value, (list, tuple)):
            shown = f"<{len(value)}개>"
        elif isinstance(value, dict):
            shown = f"<키 {sorted(value)}>"
        else:
            shown = repr(value)
        parts.append(f"{key}={shown}")
    return ", ".join(parts) or "인자 없음"


# --- 인증 · 호출 추적 -----------------------------------------------------------
class TraceMiddleware(Middleware):
    """모든 도구 호출의 수신·완료·실패를 남긴다 (인증 전에 걸려 거부된 호출도 보인다)."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        trace(logger, "[command 수신]: %s (%s)", name, _summarize(context.message.arguments))
        started = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as exc:
            trace(
                logger, "[command 실패]: %s — %.1fs — %s: %s",
                name, time.monotonic() - started, type(exc).__name__, exc,
            )
            raise
        trace(logger, "[command 완료]: %s — %.1fs", name, time.monotonic() - started)
        return result


class ApiKeyMiddleware(Middleware):
    """HTTP 전송일 때만 X-API-Key 를 검사한다.

    stdio 는 CLI 가 이 서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다
    (헤더 자체가 없다). CODETEST_MCP_API_KEYS 가 비어 있으면 인증 비활성화.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        if not headers:
            trace(logger, "API Key 검사 생략 — HTTP 헤더 없음(stdio)")
        elif not verify_api_key(headers.get("x-api-key")):
            trace(logger, "API Key 검사 실패 — 요청 거부")
            raise ToolError("유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.")
        else:
            trace(logger, "API Key 검사 통과%s", "" if settings.api_keys else " (인증 비활성화)")
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
    middleware=[TraceMiddleware(), ApiKeyMiddleware()],
)


# --- 연결 확인 ----------------------------------------------------------------
@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """liveness probe 용. DB·Agent·스레드풀을 건드리지 않아 Agent 대기 중에도 즉시 답한다.

    probe 경로는 업로드 서버가 정하므로 루트(`/`)도 같은 응답을 준다.
    """
    return JSONResponse({"status": "ok"})


@mcp.tool()
def hello(name: str) -> str:
    """연결 확인용 에코. Agent 연결 상태도 함께 알린다."""
    trace(logger, "[hello] Agent 연결 확인")
    try:
        agent_client.health()
        agent = "ok"
    except AgentError as exc:
        agent = f"unreachable: {exc}"
    trace(logger, "[hello] Agent 상태: %s", agent.splitlines()[0])
    return f"Hello! Test Code MCP ! {name} (agent: {agent})"


# --- 프로젝트 개요 (정의서 상세 1) --------------------------------------------
@mcp.tool()
async def register_project(
    ctx: Context,
    name: Annotated[str, Field(min_length=1, max_length=200, description="프로젝트 명")],
    git_url: Annotated[str, Field(description="대상 저장소 Git URL (http(s):// 또는 git@)")],
    owner: Annotated[str, Field(min_length=1, max_length=100, description="담당자")],
    github_token: Annotated[str | None, Field(description="Github API Token")] = None,
    default_branch: Annotated[str, Field(description="기준 브랜치")] = "main",
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="이미 커밋된 소스 본문 — 이후 실행에서 변경분을 덮을 바탕이 된다"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
) -> ProjectRead:
    """프로젝트를 등록하고 Git clone + AST 개요 수집을 **끝낸 뒤** 응답한다.

    `sources` 로 **이미 커밋된 소스 본문**을 함께 받아 저장한다. 이후 generate/run/test
    는 미커밋 변경분만 보내오는데, MCP 가 이 스냅샷 위에 그 변경분을 덮어 "현재 코드"
    를 만들어 Agent 에 넘긴다. 그래야 LLM 이 변경 지점뿐 아니라 그 코드가 호출하는
    커밋된 구현까지 보고 테스트를 만들 수 있다.

    개요 수집(PENDING → RUNNING → READY/FAILED)이 끝난 뒤에 돌려주므로 응답의
    ingest_status 는 READY 또는 FAILED 다 (실패 사유는 ingest_error). 수집이 길어져도
    `_keepalive` 의 진행 알림이 CLI↔MCP 구간을 지킨다.

    **같은 이름·같은 git_url 로 다시 부르면 기존 프로젝트를 그대로 돌려준다.**
    CLI 는 project_id 를 로컬 `.codetest/config.json` 에만 두는데, 이 파일은
    .gitignore 대상이라 clone·PC 교체·정리 명령으로 쉽게 사라진다. 그때 재등록을
    거부하면 CLI 가 project_id 를 되찾을 길이 없어
    "등록된 프로젝트가 없습니다" → register → "이미 있습니다" 가 무한히 반복된다.
    이 도구가 재조회 경로를 겸해야 그 고리가 끊긴다.
    """
    return await _keepalive(ctx, partial(
        _register_and_ingest, name, git_url, owner, github_token, default_branch, sources,
    ))


def _register_and_ingest(
    name: str, git_url: str, owner: str, github_token: str | None,
    default_branch: str, sources: list[SourceFilePayload],
) -> ProjectRead:
    """등록 → (필요하면) 개요 수집 → 수집 결과가 반영된 프로젝트를 돌려준다."""
    payload, ingest_id = _register(name, git_url, owner, github_token, default_branch, sources)
    if ingest_id is None:
        return payload

    trace(logger, "[register 4/5] 개요 수집 시작 — 끝날 때까지 CLI 응답을 보류")
    run_ingest(ingest_id)
    with session_scope() as db:
        payload = _to_read(_flow(project_or_fail, db, ingest_id))
    trace(logger, "[register 5/5] 개요 수집 종료 (상태 %s) — CLI 로 응답", payload.ingest_status)
    return payload


def _register(
    name: str, git_url: str, owner: str, github_token: str | None,
    default_branch: str, sources: list[SourceFilePayload],
) -> tuple[ProjectRead, str | None]:
    """프로젝트 행과 커밋 스냅샷을 저장한다. 개요 수집이 필요하면 그 project_id 를 함께 돌려준다."""
    # URL 에 user:token@ 이 섞여 올 수 있어 '@' 앞은 로그에 남기지 않는다.
    trace(logger, "[register 1/5] git_url 검증: %s", git_url.rsplit("@", 1)[-1])
    if not git_url.startswith(("http://", "https://", "git@")):
        raise ToolError("git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")

    normalized_url = git_url.rstrip("/")

    with session_scope() as db:
        existing = db.scalar(select(Project).where(Project.name == name))
        trace(
            logger, "[register 2/5] 이름 '%s' 조회 — %s", 
            name, "기존 프로젝트 있음" if existing else "신규")
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
            # 재등록은 스냅샷 갱신 기회다 — 그 사이 커밋이 쌓였을 수 있다.
            refreshed = orchestrator.store_committed_sources(
                db, existing.id, orchestrator.as_pairs(sources)
            )
            if refreshed:
                trace(logger, "[%s] 커밋 소스 %d개 갱신", existing.name, refreshed)

            if existing.ingest_status == IngestStatus.FAILED.value:
                trace(logger, "[%s] 지난 개요 수집이 실패해 다시 시작합니다", existing.name)
                return _to_read(existing), existing.id
            trace(
                logger, "[%s] 이미 등록된 프로젝트를 그대로 돌려줍니다 (상태 %s)",
                existing.name, existing.ingest_status,
            )
            return _to_read(existing), None

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
        trace(logger, "[register 3/5] 프로젝트 DB 저장: id=%s", project.id)

        stored = orchestrator.store_committed_sources(db, project.id, orchestrator.as_pairs(sources))
        if stored:
            trace(logger, "[%s] 커밋 소스 %d개 저장", project.name, stored)

        return _to_read(project), project.id


@mcp.tool()
def delete_project(project_id: str) -> dict:
    """프로젝트와 그래프를 함께 삭제하고 작업 사본(clone)도 제거한다."""
    with session_scope() as db:
        project = _flow(project_or_fail, db, project_id)
        trace(logger, "[delete 1/3] 프로젝트 확인: %s", project.name)
        repo = RepoService(project.id, project.git_url, project.github_token)
        db.delete(project)
        db.commit()
        trace(logger, "[delete 2/3] DB 에서 프로젝트·그래프 삭제")

    repo.remove()
    trace(logger, "[delete 3/3] 작업 사본(clone) 제거")
    return {"deleted": project_id}


# --- CLI 명령 흐름 -------------------------------------------------------------
@mcp.tool()
async def test_generate(
    ctx: Context,
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload], Field(description="변경 파일 본문 (테스트 대상 코드)")
    ] = [],  # noqa: B006
) -> GeneratedResult:
    """`codetest generate` — 변경 의도를 파악하고 @SpringBootTest 를 생성한다.

    MCP 가 변경 단위·영향도·중요도를 확정한 뒤 Agent 에 생성을 맡긴다. 실행은 하지 않는다.
    MCP↔Agent 구간은 Agent 의 keep-alive 스트림이, CLI↔MCP 구간은 `_keepalive` 의
    진행 알림이 끊기지 않게 지킨다.
    """
    return await _keepalive(
        ctx, partial(_flow, orchestrator.test_generate, project_id, diff, sources)
    )


@mcp.tool()
def prepare_test(
    project_id: str,
    test_code: Annotated[str, Field(description="실행할 Java 테스트 소스")],
    base_package: Annotated[
        str | None,
        Field(description="package 선언이 없을 때 쓸 기준 패키지. 생략하면 개요에서 찾는다"),
    ] = None,
) -> PreparedTestResponse:
    """`codetest run` / `codetest test` 1단계 — @SpringBootTest 를 주입한다.

    @SpringBootTest 가 없으면 붙이고 import/package 선언도 보강한 뒤
    src/test/java/<package>/<Class>.java 저장 경로를 계산해 돌려준다.
    **실행은 CLI 가 개발자 PC 의 프로젝트에서 한다** — 이 단계는 문자열 변환뿐이라
    git·JDK·Gradle 이 필요 없다.
    """
    return _flow(orchestrator.prepare_test, project_id, test_code, base_package)


@mcp.tool()
async def report_execution(
    ctx: Context,
    project_id: str,
    execution: Annotated[dict, Field(description="CLI 가 로컬에서 돌린 실행 결과")],
    test_code: Annotated[str, Field(description="실행한 Java 테스트 소스")] = "",
    diff: Annotated[
        str, Field(description="변경분 unified diff — 기능 중요도를 다시 판단하는 데 쓴다")
    ] = "",
    sources: Annotated[
        list[SourceFilePayload], Field(description="미커밋 변경 파일 본문")
    ] = [],  # noqa: B006
    intent: Annotated[str, Field(description="이전에 파악한 변경 의도")] = "",
    intent_rationale: Annotated[str, Field(description="그 의도의 근거")] = "",
) -> ReportResult:
    """`codetest run` / `codetest test` 2단계 — 로컬 실행 결과로 리포트를 만든다.

    실행 집계는 CLI 가 준 사실을 그대로 쓰고, 기능 중요도는 MCP 가 코드로 다시
    판정하며, 결과 적절성만 Agent(LLM)가 판단한다.
    """
    return await _keepalive(ctx, partial(
        _flow, orchestrator.report_execution,
        project_id, execution, test_code, diff, sources, intent, intent_rationale,
    ))


# --- 실행 --------------------------------------------------------------------
def main() -> None:
    """`python -m codetest_mcp` 진입점."""
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
