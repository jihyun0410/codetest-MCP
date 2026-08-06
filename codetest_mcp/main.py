"""MCP 서비스 진입점 + REST API.

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

이 서비스는 LLM 을 호출하지 않는다. Agent 가 FastAPI 로 호출한다.

    uvicorn codetest_mcp.main:app --host 0.0.0.0 --port 8100

담당 기능 (전부 정의서 근거)
  POST   /api/v1/projects              프로젝트 등록 + Git clone + AST → 개요 DB 저장  (상세 1)
  DELETE /api/v1/projects/{id}         등록 정보/그래프/작업 사본 삭제
  GET    /api/v1/projects/{id}/overview 저장된 프로젝트 개요 조회                      (상세 1)
  POST   /api/v1/analysis/changes      Git Diff + AST 로 변경 코드 단위·영향도 식별     (2)
  POST   /api/v1/tests/execute         @SpringBootTest 주입 + JaCoCo 실행               (1), (상세 4)
  GET    /api/v1/health                헬스체크
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from codetest_mcp import springboot
from codetest_mcp.config import get_logger, settings, setup_logging, verify_api_key
from codetest_mcp.db import (
    GraphNode,
    IngestStatus,
    NodeType,
    Project,
    SessionLocal,
    get_db,
    init_db,
)
from codetest_mcp.executor import ExecutionError, run_tests
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.graph.impact import ImpactAnalyzer, parse_diff_ranges
from codetest_mcp.graph.store import GraphStore
from codetest_mcp.repo import RepoService
from codetest_mcp.schemas import (
    ChangeAnalysisRequest,
    ChangeAnalysisResponse,
    ChangedUnit,
    ExecuteRequest,
    ExecuteResponse,
    ImpactedUnit,
    OverviewResponse,
    ProjectCreate,
    ProjectRead,
)

setup_logging()
logger = get_logger(__name__)


# --- 의존성 / 헬퍼 -----------------------------------------------------------
def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """CODETEST_MCP_API_KEYS 가 비어 있으면 인증 비활성화(로컬 개발용)."""
    if not verify_api_key(x_api_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.",
        )


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"프로젝트를 찾을 수 없습니다: {project_id}"
        )
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
    db = SessionLocal()
    project = db.get(Project, project_id)
    if project is None:
        db.close()
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
    finally:
        db.close()


# --- 라우터 ------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    """연결 확인용 (인증 불필요)."""
    return {"status": "ok", "app": settings.app_name, "role": "code-based"}


# --- 프로젝트 개요 (정의서 상세 1) --------------------------------------------
projects = APIRouter(
    prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_key)]
)


@projects.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED,
               summary="프로젝트 등록 + 개요 수집")
def create_project(
    payload: ProjectCreate, background: BackgroundTasks, db: Session = Depends(get_db)
) -> ProjectRead:
    """등록 후 Git Diff/AST 기반 개요 수집을 백그라운드로 시작한다."""
    if db.scalar(select(Project).where(Project.name == payload.name)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"이미 같은 이름의 프로젝트가 있습니다: {payload.name}"
        )

    project = Project(**payload.model_dump(), ingest_status=IngestStatus.PENDING.value)
    db.add(project)
    db.commit()
    db.refresh(project)

    background.add_task(run_ingest, project.id)
    return _to_read(project)


@projects.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT,
                 summary="프로젝트 삭제")
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    """프로젝트와 그래프를 함께 삭제하고 작업 사본도 제거한다."""
    project = _project(db, project_id)
    repo = RepoService(project.id, project.git_url, project.github_token)
    db.delete(project)
    db.commit()
    repo.remove()


@projects.get("/{project_id}/overview", response_model=OverviewResponse,
              summary="프로젝트 개요 조회")
def get_overview(project_id: str, db: Session = Depends(get_db)) -> OverviewResponse:
    """DB 에 저장된 개요를 돌려준다 (Agent 프롬프트 컨텍스트)."""
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
analysis = APIRouter(
    prefix="/analysis", tags=["analysis"], dependencies=[Depends(require_api_key)]
)


@analysis.post("/changes", response_model=ChangeAnalysisResponse,
               summary="Git Diff + AST 변경 단위 식별")
def analyze_changes(
    payload: ChangeAnalysisRequest, db: Session = Depends(get_db)
) -> ChangeAnalysisResponse:
    """
    "Git Diff 와 AST 분석 기반으로 실제 변경된 코드 단위를 식별한다."

    확정 가능한 사실만 반환한다. 의도(기능 추가/조건 변경/성능 개선) 해석은
    LLM 의 일이므로 Agent 가 이 결과를 받아 수행한다.
    """
    project = _project(db, payload.project_id)

    store = GraphStore(db, project.id)
    analyzer = ImpactAnalyzer(store)
    ranges = parse_diff_ranges(payload.diff)
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
tests = APIRouter(
    prefix="/tests", tags=["tests"], dependencies=[Depends(require_api_key)]
)


@tests.post("/execute", response_model=ExecuteResponse,
            summary="@SpringBootTest 주입 + JaCoCo 실행")
def execute_tests(payload: ExecuteRequest, db: Session = Depends(get_db)) -> ExecuteResponse:
    """
    "생성된 Test Code를 @SpringBootTest 에 넣고 실행시킨다."
    "JaCoCo와 @SpringBootTest 를 사용하여 Test Code 실행."
    """
    project = _project(db, payload.project_id)

    base_package = payload.base_package or _base_package(db, project.id)
    try:
        prepared = springboot.prepare(payload.test_code, base_package)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    repo = RepoService(project.id, project.git_url, project.github_token)
    try:
        repo.ensure_clone(project.default_branch)
        result = run_tests(
            repo.path,
            prepared,
            overlay_sources=[(item.path, item.content) for item in payload.sources],
        )
    except ExecutionError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"테스트 실행 실패: {exc}"
        ) from None

    return ExecuteResponse(
        project_id=project.id,
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


router.include_router(projects)
router.include_router(analysis)
router.include_router(tests)


# --- 앱 ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """기동 시 런타임 디렉터리 생성 + 테이블 초기화."""
    settings.ensure_directories()
    init_db()
    logger.info("%s 기동 (port=%d)", settings.app_name, settings.port)
    yield
    logger.info("%s 종료", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description=(
        "코드 기반 처리 전담 서비스. Git Diff/AST 변경 단위 식별, 프로젝트 개요 저장, "
        "@SpringBootTest 주입, JaCoCo 실행을 담당하며 LLM 을 호출하지 않는다."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """처리되지 않은 예외를 500 JSON 으로 정규화. 스택트레이스는 서버 로그에만 남긴다."""
    logger.exception("처리되지 않은 예외: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "MCP 내부 오류가 발생했습니다.", "error": str(exc)},
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs", "api": "/api/v1"}
