"""CLI 명령 흐름 (MCP 가 진입점).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

CLI 가 도구를 부르면 MCP 가 **코드 기반 사실을 먼저 확정**하고, LLM 판단이 필요한
부분만 Agent 에 REST 로 넘긴 뒤 결과를 합쳐 돌려준다.

  codetest generate   analyze → Agent 생성 → 합치기
  codetest run        analyze → Agent 생성 → Gradle 실행 → Agent 판정 → 합치기
  codetest test       analyze → Gradle 실행 → Agent 판정 → 합치기

기능 중요도는 Agent 에 묻지 않는다. 코드 그래프로 확정하는 값이므로 MCP 가 정한다
(`importance.py`). 그래서 run 과 test 양쪽 모두 LLM 추가 호출 없이 중요도를 싣는다.

DB 세션은 그래프 조회 구간에만 연다. Agent 호출(LLM)과 Gradle 실행은 수 분이 걸려
그동안 세션을 붙들고 있으면 안 된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from codetest_mcp import importance as importance_mod
from codetest_mcp import springboot
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger
from codetest_mcp.db import GraphNode, IngestStatus, NodeType, Project, session_scope
from codetest_mcp.executor import ExecutionError, run_tests
from codetest_mcp.graph.impact import ImpactAnalyzer, parse_diff_ranges
from codetest_mcp.graph.store import GraphStore
from codetest_mcp.repo import RepoService
from codetest_mcp.schemas import (
    ChangeAnalysisResponse,
    ChangedUnit,
    ExecuteResponse,
    GeneratedResult,
    ImpactedUnit,
    ReportResult,
    RunResult,
    SourceFilePayload,
)

logger = get_logger(__name__)


class FlowError(RuntimeError):
    """CLI 명령을 수행할 수 없는 상태. 도구 계층이 ToolError 로 옮긴다."""


@dataclass
class ProjectSnapshot:
    """DB 세션 밖에서도 쓸 수 있게 떠 둔 프로젝트 정보."""

    id: str
    name: str
    git_url: str
    github_token: str | None
    default_branch: str
    ingest_status: str


# --- 헬퍼 --------------------------------------------------------------------
def project_or_fail(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise FlowError(f"프로젝트를 찾을 수 없습니다: {project_id}")
    return project


def _snapshot(project_id: str) -> ProjectSnapshot:
    with session_scope() as db:
        project = project_or_fail(db, project_id)
        return ProjectSnapshot(
            id=project.id,
            name=project.name,
            git_url=project.git_url,
            github_token=project.github_token,
            default_branch=project.default_branch,
            ingest_status=project.ingest_status,
        )


def base_package(db: Session, project_id: str) -> str | None:
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


def as_pairs(sources) -> list[tuple[str, str]]:
    """SourceFilePayload / dict / (경로, 본문) 을 (경로, 본문) 으로 정규화한다.

    흐름 안에서 여러 번 거쳐도 결과가 같아야 한다 — 이미 정규화된 값을 다시 넣어도
    그대로 나온다.
    """
    pairs: list[tuple[str, str]] = []
    for item in sources or []:
        if isinstance(item, SourceFilePayload):
            pairs.append((item.path, item.content))
        elif isinstance(item, dict):
            pairs.append((item.get("path", ""), item.get("content", "")))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
        else:
            pairs.append((getattr(item, "path", ""), getattr(item, "content", "")))
    return [(path, content) for path, content in pairs if path]


def _agent_payload(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"path": path, "content": content} for path, content in pairs]


def _target_code(pairs: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"### {path}\n```java\n{body}\n```" for path, body in pairs)


# ===========================================================================
#  1. 변경 단위 식별 + 기능 중요도 (정의서 (2), [UI] 4) — LLM 미개입
# ===========================================================================
def analyze(project_id: str, diff: str = "", sources=None) -> ChangeAnalysisResponse:
    """Git Diff 와 AST 로 변경 단위·영향도·기능 중요도를 확정한다.

    `sources` 는 CLI 가 함께 보내는 미커밋 변경 파일 본문이다. Diff 에 hunk 가 없어
    라인 구간을 못 구한 파일은 파일 전체를 변경 구간으로 잡는 근거로 쓴다(신규 파일 등).
    """
    pairs = as_pairs(sources)

    with session_scope() as db:
        project = project_or_fail(db, project_id)

        ranges = parse_diff_ranges(diff)
        for path, content in pairs:
            if path not in ranges:
                ranges[path] = [(1, content.count("\n") + 1)]

        report = ImpactAnalyzer(GraphStore(db, project.id)).analyze(ranges)
        graph_ready = project.ingest_status == IngestStatus.READY.value
        verdict = importance_mod.judge(report, graph_ready)

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
            diff=diff,
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
            importance=verdict.importance,
            importance_rationale=verdict.rationale,
            frameworks=project.frameworks or [],
            base_package=base_package(db, project.id),
            graph_ready=graph_ready,
            warnings=warnings,
        )


# ===========================================================================
#  2. @SpringBootTest 주입 + JaCoCo 실행 (정의서 (1), [상세] 4) — LLM 미개입
# ===========================================================================
def execute(
    project_id: str, test_code: str, sources=None, base_package_hint: str | None = None
) -> ExecuteResponse:
    """생성된 Test Code 를 @SpringBootTest 로 실행한다. 판정은 하지 않는다."""
    snapshot = _snapshot(project_id)
    if base_package_hint is None:
        with session_scope() as db:
            base_package_hint = base_package(db, snapshot.id)

    try:
        prepared = springboot.prepare(test_code, base_package_hint)
    except ValueError as exc:
        raise FlowError(str(exc)) from None

    repo = RepoService(snapshot.id, snapshot.git_url, snapshot.github_token)
    try:
        repo.ensure_clone(snapshot.default_branch)
        result = run_tests(repo.path, prepared, overlay_sources=as_pairs(sources))
    except ExecutionError as exc:
        raise FlowError(str(exc)) from None
    except Exception as exc:
        raise FlowError(f"테스트 실행 실패: {exc}") from None

    return ExecuteResponse(
        project_id=snapshot.id,
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


# ===========================================================================
#  3. Agent(LLM) 위임
# ===========================================================================
def _ask_agent_to_generate(
    snapshot: ProjectSnapshot, analysis: ChangeAnalysisResponse, pairs: list[tuple[str, str]]
) -> dict:
    try:
        return agent_client.generate(
            snapshot.id, analysis.model_dump(mode="json"),
            _agent_payload(pairs), snapshot.name,
        ) or {}
    except AgentError as exc:
        raise FlowError(f"Agent 생성 호출 실패 — {exc}") from None


def _ask_agent_to_judge(
    project_id: str, execution: ExecuteResponse, test_code: str,
    intent: str, intent_rationale: str,
) -> dict:
    try:
        return agent_client.report(
            project_id, execution.model_dump(mode="json"),
            test_code, intent, intent_rationale,
        ) or {}
    except AgentError as exc:
        raise FlowError(f"Agent 판정 호출 실패 — {exc}") from None


def _to_generated(
    analysis: ChangeAnalysisResponse, judged: dict, pairs: list[tuple[str, str]]
) -> GeneratedResult:
    """Agent 의 LLM 판단 + MCP 의 중요도를 합친다."""
    return GeneratedResult(
        thinking=judged.get("thinking", ""),
        intent=judged.get("intent", ""),
        intent_rationale=judged.get("intent_rationale", ""),
        # 중요도는 Agent 응답을 쓰지 않는다 — MCP 가 코드로 확정한 값이다.
        importance=analysis.importance,
        importance_rationale=analysis.importance_rationale,
        test_cases=judged.get("test_cases", ""),
        test_code=judged.get("test_code", ""),
        rationale=judged.get("rationale", ""),
        target_code=judged.get("target_code") or _target_code(pairs),
        base_package=analysis.base_package,
        graph_ready=analysis.graph_ready,
        analysis_warnings=list(analysis.warnings),
    )


def _to_report(
    execution: ExecuteResponse, judged: dict, analysis: ChangeAnalysisResponse,
    intent: str, intent_rationale: str,
) -> ReportResult:
    """실행 사실(MCP) + 적절성 판단(Agent) + 중요도(MCP)를 합친다."""
    return ReportResult(
        # exit code 와 JUnit 집계는 사실이므로 LLM 판정보다 우선한다.
        result="PASS" if execution.exit_code == 0 else "FAIL",
        verdict=judged.get("verdict", ""),
        verdict_rationale=judged.get("verdict_rationale", ""),
        details=judged.get("details", ""),
        intent=judged.get("intent") or intent,
        intent_rationale=judged.get("intent_rationale") or intent_rationale,
        importance=analysis.importance,
        importance_rationale=analysis.importance_rationale,
        passed=execution.passed,
        failed=execution.failed,
        skipped=execution.skipped,
        total=execution.total,
        failures=list(execution.failures),
        coverage=execution.coverage,
        jacoco_enabled=execution.jacoco_enabled,
        springboot_applied=execution.springboot_applied,
        applied=list(execution.applied),
        test_file_path=execution.test_file_path,
        exit_code=execution.exit_code,
        output=execution.output,
    )


# ===========================================================================
#  CLI 명령 흐름
# ===========================================================================
def test_generate(project_id: str, diff: str = "", sources=None) -> GeneratedResult:
    """`codetest generate` — 분석 → Agent 생성. 실행은 하지 않는다."""
    pairs = as_pairs(sources)
    analysis = analyze(project_id, diff, pairs)
    snapshot = _snapshot(project_id)
    judged = _ask_agent_to_generate(snapshot, analysis, pairs)
    return _to_generated(analysis, judged, pairs)


def test_run(project_id: str, diff: str = "", sources=None) -> RunResult:
    """`codetest run` — 분석 → 생성 → 실행 → 판정을 한 번에 (정의서 흐름 3~5)."""
    pairs = as_pairs(sources)
    analysis = analyze(project_id, diff, pairs)
    snapshot = _snapshot(project_id)

    judged = _ask_agent_to_generate(snapshot, analysis, pairs)
    generated = _to_generated(analysis, judged, pairs)
    if not generated.test_code.strip():
        raise FlowError("Agent 가 Test Code 를 생성하지 못했습니다.")

    execution = execute(project_id, generated.test_code, pairs, generated.base_package)
    verdict = _ask_agent_to_judge(
        project_id, execution, generated.test_code,
        generated.intent, generated.intent_rationale,
    )
    report = _to_report(
        execution, verdict, analysis, generated.intent, generated.intent_rationale
    )
    return RunResult(generated=generated, report=report)


def execute_tests(
    project_id: str,
    test_code: str,
    sources=None,
    base_package: str | None = None,
    diff: str = "",
    intent: str = "",
    intent_rationale: str = "",
) -> ReportResult:
    """`codetest test` — src/test/test.txt 의 Test Code 를 실행하고 판정한다.

    `diff` 는 기능 중요도를 다시 판단하기 위해 받는다. CLI 가 보내지 않으면
    `sources` 만으로 판단하므로 변경 구간이 파일 전체로 잡혀 등급이 높게 나올 수 있다.
    """
    if not test_code.strip():
        raise FlowError("실행할 Test Code 가 비어 있습니다.")

    pairs = as_pairs(sources)
    analysis = analyze(project_id, diff, pairs)
    execution = execute(project_id, test_code, pairs, base_package or analysis.base_package)
    verdict = _ask_agent_to_judge(
        project_id, execution, test_code, intent, intent_rationale
    )
    return _to_report(execution, verdict, analysis, intent, intent_rationale)
