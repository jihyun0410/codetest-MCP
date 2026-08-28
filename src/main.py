"""MCP 서버 진입점 (FastMCP).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분"

이 서버는 LLM 을 호출하지 않는다. Agent 가 MCP 클라이언트로 붙어 도구를 호출한다.

    python -m src     # 패키지라 -m 으로 띄운다 (main.py 직접 실행은 import 실패)
    → http(streamable), 0.0.0.0:8100 (CODETEST_MCP_TRANSPORT / _HOST / _PORT 로 변경)

구조:  CLI Server  ──command──▶  MCP  ──LLM 이 필요한 것만──▶  AI Agent
                                  ◀── LLM 처리 결과 ──────────

MCP 는 LLM 을 호출하지 않는다. 코드로 확정 가능한 일(clone/AST/gradle)은 직접
하고, 판단이 필요한 일(테스트 코드 생성, 변경 의도·영향도 해석)만 Agent 로
넘긴다. 그래서 **모든 command 가 Agent 로 나가지는 않는다.**

  command           Agent 송신 (POST {base_url} + 경로)      비고
  ────────────────  ──────────────────────────────────────  ────────────────────
  hello             ✗                                       로컬 에코
  register_project  △ /api/v1/projects                      수집 완료 후. 통보
  delete_project    ✗                                       삭제 = 코드 작업
  test_generate     ✓ /api/v1/tests/generate                코드 생성 = LLM
  test_run          ✓ /api/v1/tests/run                     생성만. 실행은 MCP
  execute_tests     ✗                                       gradle 실행 = 코드 작업

송신 지점은 코드에서 `>>> Agent API 송신 <<<` 주석으로 표시했다.

개요 수집이 끝나면 CODETEST_MCP_AGENT_BASE_URL 로 결과를 POST 한다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
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

from src import springboot
from src.config import get_logger, settings, setup_logging, verify_api_key
from src.db import (
    GraphNode,
    IngestStatus,
    NodeType,
    Project,
    init_db,
    session_scope,
)
from src.executor import ExecutionError, run_tests
from src.graph.builder import GraphBuilder
from src.graph.impact import ImpactAnalyzer, parse_diff_ranges
from src.graph.store import GraphStore
from src.repo import RepoService
from src.schemas import (
    ChangedUnit,
    ExecuteResponse,
    GenerationContext,
    ImpactedUnit,
    ProjectRead,
    SourceFilePayload,
    TestGenerateResponse,
    TestRunResponse,
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


# --- Agent 연동 --------------------------------------------------------------
#
#  Agent 와 주고받는 계약은 전부 이 블록에 있다. 규격이 바뀌면 여기만 고친다.
#
#  요청은 base_url 뒤에 command 별 경로를 붙여 POST 한다.
#
#    command           경로                        본문 event
#    ────────────────  ──────────────────────────  ─────────────────────────
#    register_project  /api/v1/projects            ingest_completed
#    test_generate     /api/v1/tests/generate      test_generate_requested
#    test_run          /api/v1/tests/run           test_run_requested
#
#  예: base_url=http://host/agent/1121365 이면
#      POST http://host/agent/1121365/api/v1/tests/generate
#
AGENT_PATH_INGEST = "/api/v1/projects"
AGENT_PATH_GENERATE = "/api/v1/tests/generate"
AGENT_PATH_RUN = "/api/v1/tests/run"


def _agent_url(path: str) -> str:
    """base_url 과 command 경로를 잇는다. 양쪽 슬래시 중복을 막는다."""
    return f"{settings.agent_base_url.rstrip('/')}/{path.lstrip('/')}"


def _post_agent(path: str, payload: dict, timeout: int) -> dict:
    """Agent 로 JSON 을 POST 하고 응답 본문을 dict 로 돌려준다."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _agent_url(path),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # 어디로 쏘는지 남긴다. 본문은 sources 때문에 커질 수 있어 크기만 찍는다.
    logger.info(
        "Agent 요청 -> %s %s (event=%s, %d bytes, timeout=%ds)",
        request.get_method(), request.full_url,
        payload.get("event", "?"), len(data), timeout,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            logger.info(
                "Agent 응답 <- HTTP %s %s (%d bytes)",
                response.status, request.full_url, len(body),
            )
    except urllib.error.HTTPError as exc:
        # 서버가 거절한 이유는 응답에 들어 있다. 그냥 삼키면 405/415 같은 에러가
        # 상태 코드만 남아 원인을 알 수 없다. 405 는 규격상 Allow 헤더가 붙는다.
        detail = exc.read().decode("utf-8", errors="replace").strip()
        hints = [f"HTTP {exc.code} {exc.reason}"]
        if exc.headers.get("Allow"):
            hints.append(f"허용 메서드: {exc.headers['Allow']}")
        if exc.headers.get("Content-Type"):
            hints.append(f"응답 타입: {exc.headers['Content-Type']}")
        if detail:
            hints.append(f"본문: {detail[:300]}")
        logger.error("Agent 요청 실패 <- %s : %s", request.full_url, " | ".join(hints))
        raise RuntimeError(" | ".join(hints)) from None
    except urllib.error.URLError as exc:
        # 연결 자체가 안 된 경우 (DNS/방화벽/포트). 주소를 함께 남긴다.
        logger.error("Agent 연결 실패 <- %s : %s", request.full_url, exc.reason)
        raise RuntimeError(f"연결 실패({exc.reason})") from None
    return json.loads(body) if body.strip() else {}


def notify_agent(path: str, payload: dict) -> None:
    """수집 결과를 Agent 로 통보한다 (fire-and-forget).

    Agent 가 죽어 있거나 느려도 결과는 이미 DB 에 있으므로 어떤 실패든 삼키고
    로그만 남긴다. 상태는 test_generate/test_run 응답의 context 로도 드러난다.
    """
    if not settings.agent_base_url:
        return
    try:
        _post_agent(path, payload, timeout=10)
        logger.info("Agent 통보 완료: %s", payload.get("status"))
    except Exception as exc:
        logger.warning(
            "Agent 통보 실패 — 수집 결과는 DB 에 남아 있다 (%s): %s",
            _agent_url(path), exc,
        )


def request_generation(
    path: str, event: str, context: GenerationContext, diff: str = ""
) -> dict:
    """정리한 컨텍스트를 Agent 로 넘겨 LLM 처리 결과를 받아온다.

    코드 생성과 영향도 해석은 LLM 의 일이라 MCP 가 하지 않는다. 여기서는 Agent 가
    판단에 필요한 사실을 넘기고 결과를 받아올 뿐이다. 통보와 달리 이 응답이 없으면
    할 일이 없으므로 실패를 삼키지 않고 그대로 올린다.

    응답 **전체**를 돌려준다. test_code 만 꺼내고 나머지를 버리면 Agent 가 만든
    영향도 해석·요약이 사라져 터미널에 보여줄 수 없다.
    """
    if not settings.agent_base_url:
        raise ToolError(
            "Agent 주소가 설정되지 않아 테스트 코드를 생성할 수 없습니다. "
            "CODETEST_MCP_AGENT_BASE_URL 을 확인하세요."
        )

    # Agent 는 최상위에 diff 를 요구한다(422: loc=["body","diff"]).
    # context 안에는 파싱 결과(changed_ranges)만 있고 원문이 없어 따로 싣는다.
    payload = {
        "event": event,
        "project_id": context.project_id,
        "diff": diff,
        "context": context.model_dump(mode="json"),
    }
    try:
        answer = _post_agent(path, payload, timeout=settings.agent_timeout_seconds)
    except Exception as exc:
        raise ToolError(
            f"Agent 로 코드 생성을 요청하지 못했습니다 ({_agent_url(path)}): {exc}"
        ) from None

    if not (answer.get("test_code") or "").strip():
        raise ToolError(
            "Agent 응답에 test_code 가 없습니다. 응답 형식은 "
            '{"test_code": "<Java 소스>", ...} 여야 합니다.'
        )
    logger.info(
        "Agent 생성 결과 수신 — test_code %d자, 추가 필드: %s",
        len(answer["test_code"]),
        ", ".join(k for k in answer if k != "test_code") or "(없음)",
    )
    return answer


# --- 백그라운드 수집 ---------------------------------------------------------
def run_ingest(project_id: str) -> None:
    """등록 직후 개요를 수집한다 (정의서 상세 1). 백그라운드 스레드의 진입점.

    스레드에서 예외가 새어나가면 파이썬은 스택트레이스만 찍고 조용히 끝낸다.
    그러면 DB 는 PENDING 인 채로 영원히 남아 호출자가 실패를 알 방법이 없다.
    그래서 어떤 실패든 여기서 잡아 상태에 기록하고 Agent 로 통보한다.
    """
    logger.info("[%s] 개요 수집 시작", project_id)
    try:
        payload = _collect_overview(project_id)
    except Exception as exc:
        # 스레드 밖으로 새어나가면 기본 excepthook 이 stderr 에 찍고 끝나므로
        # 로거를 거치지 않는다. 여기서 잡아야 배포 환경 로그에 남는다.
        logger.exception("[%s] 개요 수집이 예기치 않게 중단됐습니다", project_id)
        payload = _mark_ingest_failed(project_id, exc)

    if payload:
        logger.info("[%s] 개요 수집 결과: %s", project_id, payload["status"])
        # >>> Agent API 송신 <<<  POST {AGENT_BASE_URL}/api/v1/projects
        # 세션을 닫은 뒤에 통보한다 — 네트워크 대기 동안 DB 커넥션을 잡지 않도록
        notify_agent(AGENT_PATH_INGEST, payload)
    else:
        logger.error("[%s] 개요 수집 결과를 기록하지 못했습니다", project_id)


def _mark_ingest_failed(project_id: str, exc: Exception) -> dict | None:
    """수집 실패를 DB 에 남긴다. 앞선 세션이 깨졌을 수 있어 새 세션을 연다."""
    try:
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project is None:
                return None
            project.ingest_status = IngestStatus.FAILED.value
            project.ingest_error = f"{type(exc).__name__}: {exc}"
            db.commit()
            logger.error("[%s] 수집 실패로 기록: %s", project.name, project.ingest_error)
            return {
                "event": "ingest_completed",
                "project_id": project.id,
                "name": project.name,
                "status": IngestStatus.FAILED.value,
                "error": project.ingest_error,
            }
    except Exception:
        logger.exception("수집 실패 상태를 기록하지 못했습니다: %s", project_id)
        return None


def _collect_overview(project_id: str) -> dict | None:
    """clone → AST 파싱 → Graph 적재 → 개요 DB 저장."""
    payload: dict | None = None

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
            payload = {
                "event": "ingest_completed",
                "project_id": project.id,
                "name": project.name,
                "status": IngestStatus.READY.value,
                "frameworks": stats.frameworks,
                "language_stats": stats.language_stats,
                "node_count": stats.node_count,
                "edge_count": stats.edge_count,
            }
        except Exception as exc:  # 어떤 실패든 상태에 남긴다
            db.rollback()
            project.ingest_status = IngestStatus.FAILED.value
            project.ingest_error = str(exc)
            db.commit()
            logger.exception("개요 수집 실패: %s", project_id)
            payload = {
                "event": "ingest_completed",
                "project_id": project.id,
                "name": project.name,
                "status": IngestStatus.FAILED.value,
                "error": str(exc),
            }

    return payload


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
    logger.info(
        "Agent 주소: %s (timeout=%ds)",
        settings.agent_base_url or "(설정 안 됨 — test_generate/test_run 불가)",
        settings.agent_timeout_seconds,
    )
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
# Agent API 송신: 없음 (로컬 에코)
@mcp.tool()
def hello(name: str) -> str:
    """연결 확인용 에코. 이름을 그대로 돌려준다."""
    return f"Hello! Test Code MCP ! {name}"


# --- 프로젝트 개요 (정의서 상세 1) --------------------------------------------
# Agent API 송신: 이 도구 안에서는 없음.
#   수집이 끝난 뒤 백그라운드 스레드가 POST {AGENT_BASE_URL}/api/v1/projects
#   (run_ingest → notify_agent). 응답을 이미 PENDING 으로 보낸 뒤라 통보가 필요하다.
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
    test_generate/test_run 응답의 context.ingest_status 로 확인한다
    (PENDING → RUNNING → READY/FAILED). 완료 시 Agent 로 통보도 간다.
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

        logger.info(
            "[%s] 프로젝트 등록 완료 — id=%s, git=%s, branch=%s",
            project.name, project.id, project.git_url, project.default_branch,
        )
        # args 는 반드시 튜플이어야 한다. 문자열을 넘기면 글자 단위로 풀려
        # run_ingest() takes 1 positional argument but 32 were given 이 난다.
        threading.Thread(target=run_ingest, args=(project.id,), daemon=True).start()
        return _to_read(project)


# Agent API 송신: 없음.
#   삭제는 코드로 끝나는 작업이고 결과는 이 호출의 반환값으로 이미 전달된다.
#   이력 관리를 위해 통보가 필요해지면 여기에 notify_agent() 를 추가하면 된다.
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


# --- 컨텍스트 정리 (MCP 의 몫) ------------------------------------------------
def _build_context(
    db: Session, project: Project, diff: str, sources: list[SourceFilePayload]
) -> GenerationContext:
    """Agent 가 코드 생성·영향도 분석을 하는 데 필요한 사실을 한데 모은다.

    개요(프레임워크/기준 패키지/그래프 규모) + Diff 로 확정한 변경 지점 + 변경
    파일 본문. 여기까지가 코드로 확정 가능한 범위이고, 이후 해석은 Agent 몫이다.
    """
    store = GraphStore(db, project.id)
    ranges = parse_diff_ranges(diff)
    report = ImpactAnalyzer(store).analyze(ranges)

    graph_ready = project.ingest_status == IngestStatus.READY.value
    warnings: list[str] = []
    if not graph_ready:
        warnings.append(
            f"프로젝트 개요 수집이 완료되지 않았습니다 (상태: {project.ingest_status}). "
            "AST 기반 변경 단위 식별 결과가 비어 있을 수 있습니다."
        )
    if ranges and not report.changed:
        warnings.append("Diff 라인과 겹치는 그래프 노드를 찾지 못했습니다.")

    return GenerationContext(
        project_id=project.id,
        name=project.name,
        ingest_status=project.ingest_status,
        graph_ready=graph_ready,
        frameworks=project.frameworks or [],
        language_stats=project.language_stats or {},
        base_package=_base_package(db, project.id),
        node_counts=store.counts_by_type(),
        edge_counts=store.edge_counts_by_type(),
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
        sources=sources,
        warnings=warnings,
    )


# --- 테스트 코드 생성 (정의서 (1)) ---------------------------------------------
# Agent API 송신: ✓ POST {AGENT_BASE_URL}
#   POST {AGENT_BASE_URL}/api/v1/tests/generate
#   보냄  {"event": "test_generate_requested", "project_id": …, "diff": "<원문>",
#          "context": {…15개 필드}}
#   받음  {"test_code": "<Java 소스>", ...그 외는 analysis 로 전달}
@mcp.tool()
def test_generate(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="테스트 대상 파일 본문 (미커밋 변경분 포함)"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
) -> TestGenerateResponse:
    """테스트를 위한 코드를 생성해 반환한다.

    생성 자체는 LLM 의 일이라 Agent 가 수행한다. MCP 는 생성에 필요한 정보를
    정리해 Agent 로 넘기고(프로젝트 개요·기준 패키지·Diff 로 확정한 변경 단위·
    변경 파일 본문), 돌려받은 코드를 반환한다. 실행은 하지 않는다.
    """
    with session_scope() as db:
        project = _project(db, project_id)
        context = _build_context(db, project, diff, sources)

    # >>> Agent API 송신 <<<  POST {AGENT_BASE_URL}/api/v1/tests/generate
    # 세션을 닫은 뒤 호출한다 — LLM 생성은 오래 걸리므로 DB 커넥션을 물지 않는다
    answer = request_generation(
        AGENT_PATH_GENERATE, "test_generate_requested", context, diff
    )
    return TestGenerateResponse(
        project_id=context.project_id,
        test_code=answer["test_code"],
        analysis={k: v for k, v in answer.items() if k != "test_code"},
        context=context,
    )


# --- 테스트 실행 (정의서 (1), 상세 4) ------------------------------------------
def _execute(
    project_id: str,
    test_code: str,
    sources: list[SourceFilePayload],
    base_package: str | None,
) -> ExecuteResponse:
    """@SpringBootTest 주입 + gradle/JaCoCo 실행. execute_tests 와 test_run 이 공유한다."""
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


# Agent API 송신: 없음.
#   @SpringBootTest 주입과 gradle/JaCoCo 실행은 전부 코드 기반 처리라 LLM 이
#   필요 없다. 결과는 반환값으로 전달된다. (실행 결과 통보가 필요하면 _execute 에)
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
    return _execute(project_id, test_code, sources, base_package)


# --- 생성 + 실행 전 과정 (정의서 (1), (2), 상세 4) ------------------------------
# Agent API 송신: ✓ POST {AGENT_BASE_URL}  (생성 단계 1회. 실행은 MCP 가 직접)
#   POST {AGENT_BASE_URL}/api/v1/tests/run
#   보냄  {"event": "test_run_requested", "project_id": …, "diff": "<원문>",
#          "context": {…}}
#   받음  {"test_code": "<Java 소스>", ...그 외는 analysis 로 전달}
@mcp.tool()
def test_run(
    project_id: str,
    diff: Annotated[str, Field(description="변경분 unified diff")] = "",
    sources: Annotated[
        list[SourceFilePayload],
        Field(description="테스트 대상 파일 본문 (실행 전 작업 사본에 덮어쓴다)"),
    ] = [],  # noqa: B006 — 읽기 전용. pydantic 이 호출마다 복사한다
    base_package: Annotated[
        str | None,
        Field(description="package 선언이 없을 때 쓸 기준 패키지. 생략하면 개요에서 찾는다"),
    ] = None,
) -> TestRunResponse:
    """테스트 코드 생성부터 실제 실행까지 전 과정을 수행한다.

    코드 생성과 Diff/AST 기반 영향도 해석은 LLM 의 일이라 Agent 가 수행한다.
    MCP 는 (1) 두 작업에 필요한 정보를 정리해 Agent 로 넘기고, (2) 돌려받은
    코드에 @SpringBootTest 를 주입해 JaCoCo 와 함께 실행한 뒤, (3) 생성 근거와
    실행 사실을 함께 반환한다. 결과 적절성 판정은 호출하는 쪽의 몫이다.

    LLM 생성 + gradle 빌드를 연달아 하므로 응답까지 수 분이 걸릴 수 있다.
    """
    with session_scope() as db:
        project = _project(db, project_id)
        context = _build_context(db, project, diff, sources)

    # >>> Agent API 송신 <<<  POST {AGENT_BASE_URL}/api/v1/tests/run
    answer = request_generation(AGENT_PATH_RUN, "test_run_requested", context, diff)
    test_code = answer["test_code"]

    # 실행은 MCP 몫 — Agent 로 쏘지 않는다 (gradle/JaCoCo 는 코드 기반 처리)
    execution = _execute(project_id, test_code, sources, base_package or context.base_package)

    return TestRunResponse(
        project_id=context.project_id,
        test_code=test_code,
        analysis={k: v for k, v in answer.items() if k != "test_code"},
        context=context,
        execution=execution,
    )


# --- 실행 --------------------------------------------------------------------
def main() -> None:
    """`python -m src` / `python src/main.py` 공통 진입점."""
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
