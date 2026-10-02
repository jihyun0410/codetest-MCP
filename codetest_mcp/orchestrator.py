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

import re
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from codetest_mcp import importance as importance_mod
from codetest_mcp import springboot
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger, trace
from codetest_mcp.db import (
    GraphNode,
    IngestStatus,
    NodeType,
    Project,
    ProjectFile,
    session_scope,
)
from codetest_mcp.graph.impact import ImpactAnalyzer, parse_diff_ranges
from codetest_mcp.graph.store import GraphStore
from codetest_mcp.schemas import (
    ChangeAnalysisResponse,
    ChangedUnit,
    ExecuteResponse,
    GeneratedResult,
    ImpactedUnit,
    PreparedTestResponse,
    ReportResult,
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


def project_source_layout(db: Session, project_id: str) -> springboot.SourceLayout:
    """저장된 그래프의 파일 경로에서 기준 패키지와 테스트 소스 루트를 추론한다.

    폴더 구조를 못 박지 않기 위한 부분이다 — 멀티 모듈이면 테스트 루트에 모듈
    접두사가 붙는다 (`api/src/test/java`). 이 서버에는 사용자의 작업 트리가 없어
    여기서 나온 값은 추정이고, CLI 가 자기 파일 시스템으로 최종 확인한다.
    """
    paths = list(
        db.scalars(
            select(GraphNode.file_path).where(
                GraphNode.project_id == project_id,
                GraphNode.node_type == NodeType.FILE.value,
            )
        )
    )
    return springboot.detect_layout(paths)


def project_base_package(db: Session, project_id: str) -> str | None:
    """기준 패키지만 필요할 때 쓰는 단축 경로."""
    return project_source_layout(db, project_id).base_package


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
#  0. 커밋된 소스 스냅샷 — 등록 때 받아 두고, 실행 때 미커밋 변경분을 덮는다
# ===========================================================================
#: Agent 프롬프트에 실을 파일 수 상한. 변경/영향 단위만 고르므로 보통 이보다 훨씬 적다.
MAX_CONTEXT_FILES = 40
#: 파일 하나당 본문 상한 (자). 지나치게 큰 파일이 프롬프트를 잡아먹지 않게 자른다.
#: 자른 자리에는 표시를 남긴다 — 모델이 그 파일의 뒷부분을 다 봤다고 믿으면
#: 없는 시그니처를 지어낸다.
MAX_CONTEXT_CHARS = 20000
#: Agent 로 보낼 맥락 전체의 상한 (자).
#:
#: 프롬프트 길이는 그대로 생성 시간이 된다. 예전에는 상한이 파일당으로만 있어서
#: 최악의 경우 40개 × 2만 자 = 80만 자를 보냈고, Agent 는 그것을 이어 붙인 뒤
#: 앞에서 4만 자만 남기고 잘랐다 — 보내느라 든 시간은 다 쓰고 정작 뒤쪽 맥락은
#: 버려지는 구조였다. 이제 MCP 가 우선순위대로 예산 안에서 끊어 보낸다.
MAX_CONTEXT_TOTAL_CHARS = 45000


def store_committed_sources(db: Session, project_id: str, pairs: list[tuple[str, str]]) -> int:
    """등록 시점의 커밋된 소스를 저장한다. 같은 경로는 새 본문으로 교체한다."""
    if not pairs:
        return 0

    existing = {
        row.path: row
        for row in db.scalars(
            select(ProjectFile).where(ProjectFile.project_id == project_id)
        )
    }
    for path, content in pairs:
        row = existing.get(path)
        if row is None:
            db.add(ProjectFile(project_id=project_id, path=path, content=content))
        else:
            row.content = content
    db.commit()
    return len(pairs)


def committed_sources(db: Session, project_id: str) -> dict[str, str]:
    """등록 때 저장해 둔 커밋 소스 전체."""
    return {
        row.path: row.content
        for row in db.scalars(
            select(ProjectFile).where(ProjectFile.project_id == project_id)
        )
    }


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _referenced_paths(changed_contents: list[str], stored: dict[str, str]) -> list[str]:
    """변경 코드가 이름으로 참조하는 커밋 파일을 찾는다.

    그래프가 비어 있어도(개요 수집 미완료·clone 실패) 최소한의 맥락은 실어야 한다.
    변경 파일이 `OrderService.calculateTotal(...)` 을 부르면 OrderService.java 를
    함께 보내야 LLM 이 그 구현을 보고 테스트를 만들 수 있다.
    """
    words: set[str] = set()
    for content in changed_contents:
        words.update(_IDENTIFIER.findall(content))
    if not words:
        return []

    hits: list[str] = []
    for path in stored:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem and stem in words:
            hits.append(path)
    return sorted(hits)


def _sibling_paths(changed_paths: set[str], stored: dict[str, str]) -> list[str]:
    """변경 파일과 같은 디렉터리(=같은 패키지)의 커밋 파일."""
    dirs = {path.rsplit("/", 1)[0] for path in changed_paths if "/" in path}
    return sorted(
        path for path in stored
        if "/" in path and path.rsplit("/", 1)[0] in dirs
    )


def _context_paths(
    analysis: ChangeAnalysisResponse,
    changed_paths: set[str],
    changed_contents: list[str],
    stored: dict[str, str],
) -> list[str]:
    """Agent 에 실어 보낼 파일 경로를 고른다.

    우선순위대로 채우고 상한에서 끊는다. 프로젝트 전체를 보내면 프롬프트가 감당이
    안 되므로 "바뀐 곳과 그에 닿는 곳" 으로 좁힌다.

      1. 변경 파일 자체
      2. 그래프가 짚은 변경 단위·영향 단위·영향 파일
      3. 변경 코드가 이름으로 참조하는 커밋 파일  (그래프가 비었을 때의 대비)
      4. 같은 패키지의 커밋 파일                  (남는 자리를 채운다)
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)

    for path in sorted(changed_paths):
        add(path)
    for unit in analysis.changed_units:
        add(unit.file_path)
    for unit in analysis.impacted_units:
        add(unit.file_path)
    for path in analysis.affected_files:
        add(path)
    for path in _referenced_paths(changed_contents, stored):
        add(path)
    for path in _sibling_paths(changed_paths, stored):
        add(path)
    return ordered[:MAX_CONTEXT_FILES]


def build_agent_sources(
    project_id: str, analysis: ChangeAnalysisResponse, pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """**커밋된 코드 + 미커밋 변경분**을 합쳐 Agent 가 볼 "현재 코드" 를 만든다.

    미커밋 변경분(pairs)이 같은 경로의 커밋 본문을 덮는다. 변경 파일만 보내면
    LLM 이 호출 대상 메서드의 실제 구현을 못 봐서 구조만 보고 테스트를 짜게 된다.
    """
    overlay = dict(pairs)
    changed_paths = set(overlay)

    with session_scope() as db:
        stored = committed_sources(db, project_id)

    merged: list[tuple[str, str]] = []
    budget = MAX_CONTEXT_TOTAL_CHARS
    candidates = _context_paths(analysis, changed_paths, list(overlay.values()), stored)
    trace(
        logger, "[context] 후보 파일 %d개 (변경 %d, 커밋 스냅샷 %d)",
        len(candidates), len(changed_paths), len(stored),
    )
    skipped = 0

    for path in candidates:
        content = overlay.get(path, stored.get(path))
        if content is None:
            continue
        body = _clip(content)

        # 변경 파일은 테스트의 대상 그 자체라 예산과 무관하게 싣는다.
        # 맥락 파일은 남는 자리에만 넣고, 안 들어가면 통째로 뺀다 —
        # 우선순위가 낮더라도 들어갈 수 있는 파일을 대신 채우는 편이 낫다.
        if path not in changed_paths:
            if len(body) > budget:
                skipped += 1
                continue
            budget -= len(body)
        merged.append((path, body))

    # 그래프가 비어 영향 파일을 못 고른 경우에도 변경분은 반드시 실어 보낸다.
    if not merged:
        trace(logger, "[context] 그래프로 고른 파일이 없어 변경분만 싣는다")
        merged = [(path, _clip(content)) for path, content in pairs]
    trace(
        logger, "[context] Agent 로 보낼 파일 %d개, %d자 (예산 부족으로 제외 %d개)",
        len(merged), sum(len(body) for _, body in merged), skipped,
    )
    return merged


def _clip(content: str) -> str:
    if len(content) <= MAX_CONTEXT_CHARS:
        return content
    return content[:MAX_CONTEXT_CHARS] + "\n… (이하 생략)"


# ===========================================================================
#  1. 변경 단위 식별 + 기능 중요도 (정의서 (2), [UI] 4) — LLM 미개입
# ===========================================================================
def analyze(project_id: str, diff: str = "", sources=None) -> ChangeAnalysisResponse:
    """Git Diff 와 AST 로 변경 단위·영향도·기능 중요도를 확정한다.

    `sources` 는 CLI 가 함께 보내는 미커밋 변경 파일 본문이다. Diff 에 hunk 가 없어
    라인 구간을 못 구한 파일은 파일 전체를 변경 구간으로 잡는 근거로 쓴다(신규 파일 등).
    """
    started = time.monotonic()
    pairs = as_pairs(sources)
    trace(logger, "[analyze 1/4] 시작 — diff %d자, 변경 파일 본문 %d개", len(diff), len(pairs))

    with session_scope() as db:
        project = project_or_fail(db, project_id)

        ranges = parse_diff_ranges(diff)
        from_diff = len(ranges)
        for path, content in pairs:
            if path not in ranges:
                ranges[path] = [(1, content.count("\n") + 1)]
        trace(
            logger, "[analyze 2/4] 변경 구간 — diff 에서 %d개 파일, 파일 전체로 보강 %d개",
            from_diff, len(ranges) - from_diff,
        )

        report = ImpactAnalyzer(GraphStore(db, project.id)).analyze(ranges)
        graph_ready = project.ingest_status == IngestStatus.READY.value
        trace(
            logger, "[analyze 3/4] 영향 분석 — 변경 단위 %d, 영향 단위 %d, 영향 파일 %d, 위험도 %s(%s), 그래프 %s",
            len(report.changed), len(report.impacted), len(report.affected_files),
            report.risk.value, report.score, "준비됨" if graph_ready else project.ingest_status,
        )
        verdict = importance_mod.judge(report, graph_ready)
        trace(logger, "[analyze 4/4] 기능 중요도 %s — 분석 %.2fs", verdict.importance, time.monotonic() - started)

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
            base_package=project_base_package(db, project.id),
            graph_ready=graph_ready,
            warnings=warnings,
        )


# ===========================================================================
#  3. Agent(LLM) 위임
# ===========================================================================
#: Agent 프롬프트가 싣는 개수 상한 (codetest-agent `testgen.py`). 넘는 몫은 보내 봐야 버려진다.
AGENT_MAX_CHANGED_UNITS = 40
AGENT_MAX_IMPACTED_UNITS = 30
AGENT_MAX_LINES = 20


def _agent_analysis(analysis: ChangeAnalysisResponse) -> dict:
    """Agent 프롬프트가 읽는 필드만, Agent 가 자르는 길이까지만 보낸다.

    중요도·경고는 MCP 가 결과에 직접 싣고 Agent 는 읽지 않는다.
    changed_ranges 는 diff 가 비었을 때만 Agent 가 diff 대신 쓴다.
    """
    payload = {
        "diff": _clip(analysis.diff),
        "changed_units": [
            unit.model_dump(exclude={"name", "language"}, exclude_defaults=True)
            for unit in analysis.changed_units[:AGENT_MAX_CHANGED_UNITS]
        ],
        "impacted_units": [
            unit.model_dump(exclude={"node_type"}, exclude_defaults=True)
            for unit in analysis.impacted_units[:AGENT_MAX_IMPACTED_UNITS]
        ],
        # Agent 는 개수만 본다 ("여러 파일이 얽혀 있으면 하나의 테스트로")
        "affected_files": analysis.affected_files,
        "risk": analysis.risk,
        "risk_score": analysis.risk_score,
        "risk_reasons": analysis.risk_reasons,
        "frameworks": analysis.frameworks,
        "base_package": analysis.base_package,
        "graph_ready": analysis.graph_ready,
    }
    if not analysis.diff:
        payload["changed_ranges"] = analysis.changed_ranges
    return payload


def _agent_execution(facts: ExecuteResponse, reported: dict) -> dict:
    """판정에 쓰는 실행 사실만 보낸다. 실행 출력은 Agent 가 자르는 길이까지만.

    build_tool·module 은 CLI 가 보내 온 값을 그대로 넘긴다 — 빠지면 Agent 가
    Maven 프로젝트도 Gradle 로 적는다.
    """
    payload = facts.model_dump(mode="json", include={
        "exit_code", "passed", "failed", "skipped", "total",
        "coverage", "jacoco_enabled", "springboot_applied",
    })
    payload["output"] = _clip(facts.output)
    payload["failures"] = facts.failures[:AGENT_MAX_LINES]
    payload["build_errors"] = facts.build_errors[:AGENT_MAX_LINES]
    payload.update({key: reported[key] for key in ("build_tool", "module") if reported.get(key)})
    return payload


def _ask_agent_to_generate(
    snapshot: ProjectSnapshot, analysis: ChangeAnalysisResponse, pairs: list[tuple[str, str]]
) -> dict:
    started = time.monotonic()
    trace(logger, "[agent] 생성 요청 — 소스 %d개 (프로젝트 %s)", len(pairs), snapshot.name)
    try:
        judged = agent_client.generate(
            snapshot.id, _agent_analysis(analysis),
            _agent_payload(pairs), snapshot.name,
        ) or {}
    except AgentError as exc:
        trace(logger, "[agent] 생성 호출 실패 — %.1fs — %s", time.monotonic() - started, str(exc).splitlines()[0])
        raise FlowError(f"Agent 생성 호출 실패 — {exc}") from None
    trace(
        logger, "[agent] 생성 응답 수신 — %.1fs, 필드 %s, test_code %d자",
        time.monotonic() - started, sorted(judged), len(judged.get("test_code", "") or ""),
    )
    return judged


def _ask_agent_to_judge(
    project_id: str, execution: dict, test_code: str,
    intent: str, intent_rationale: str,
) -> dict:
    started = time.monotonic()
    trace(logger, "[agent] 판정 요청 — test_code %d자, 실행 결과 키 %s", len(test_code), sorted(execution))
    try:
        judged = agent_client.report(
            project_id, execution, _clip(test_code), intent, intent_rationale,
        ) or {}
    except AgentError as exc:
        trace(logger, "[agent] 판정 호출 실패 — %.1fs — %s", time.monotonic() - started, str(exc).splitlines()[0])
        raise FlowError(f"Agent 판정 호출 실패 — {exc}") from None
    trace(
        logger, "[agent] 판정 응답 수신 — %.1fs, verdict=%r",
        time.monotonic() - started, judged.get("verdict", ""),
    )
    return judged


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
        build_errors=list(execution.build_errors),
    )


# ===========================================================================
#  CLI 명령 흐름
# ===========================================================================
def test_generate(project_id: str, diff: str = "", sources=None) -> GeneratedResult:
    """`codetest generate` — 분석 → Agent 생성. 실행은 하지 않는다."""
    trace(logger, "[generate 1/5] 시작: %s", project_id)
    pairs = as_pairs(sources)
    analysis = analyze(project_id, diff, pairs)
    trace(logger, "[generate 2/5] 분석 완료 — 경고 %d건", len(analysis.warnings))
    snapshot = _snapshot(project_id)
    trace(logger, "[generate 3/5] 프로젝트 스냅샷: %s (개요 상태 %s)", snapshot.name, snapshot.ingest_status)
    # 커밋된 코드에 미커밋 변경분을 덮어 "현재 코드" 를 만들어 넘긴다.
    context = build_agent_sources(project_id, analysis, pairs)
    trace(logger, "[generate 4/5] Agent 에 생성 위임")
    judged = _ask_agent_to_generate(snapshot, analysis, context)
    result = _to_generated(analysis, judged, context)
    trace(logger, "[generate 5/5] 결과 합치기 완료 — 중요도 %s", result.importance)
    return result


def prepare_test(
    project_id: str, test_code: str, base_package: str | None = None
) -> PreparedTestResponse:
    """`codetest test` 1단계 — @SpringBootTest 를 주입하고 저장 경로를 계산한다.

    실행은 **CLI 가 개발자 PC 의 프로젝트에서** 한다. 여기서 하는 일은 코드 기반
    문자열 변환뿐이라 git·JDK·Gradle 이 필요 없다 (정의서: 코드 기반 처리 = MCP).
    """
    trace(logger, "[prepare 1/4] 시작: %s — test_code %d자", project_id, len(test_code))
    if not test_code.strip():
        raise FlowError("실행할 Test Code 가 비어 있습니다.")

    # 기준 패키지는 호출자가 줄 수 있지만 **테스트 소스 루트는 언제나 개요에서**
    # 읽는다. 멀티 모듈이면 `api/src/test/java` 처럼 모듈 접두사가 붙어야 한다.
    with session_scope() as db:
        project_or_fail(db, project_id)
        layout = project_source_layout(db, project_id)
    trace(
        logger, "[prepare 2/4] 소스 레이아웃 — base_package=%s, test_root=%s",
        layout.base_package, layout.test_root,
    )

    base_package_hint = layout.base_package if base_package is None else base_package

    try:
        prepared = springboot.prepare(test_code, base_package_hint, layout.test_root)
    except ValueError as exc:
        trace(logger, "[prepare] @SpringBootTest 주입 실패: %s", exc)
        raise FlowError(str(exc)) from None
    trace(
        logger, "[prepare 3/4] @SpringBootTest 주입 — 적용 %s, 저장 경로 %s",
        list(prepared.applied), prepared.file_path,
    )
    trace(logger, "[prepare 4/4] 완료")

    return PreparedTestResponse(
        project_id=project_id,
        source=prepared.source,
        file_path=prepared.file_path,
        class_name=prepared.class_name,
        package=prepared.package,
        test_root=prepared.test_root,
        springboot_applied=prepared.springboot_applied,
        applied=list(prepared.applied),
    )


def report_execution(
    project_id: str,
    execution: dict,
    test_code: str,
    diff: str = "",
    sources=None,
    intent: str = "",
    intent_rationale: str = "",
) -> ReportResult:
    """`codetest test` 2단계 — CLI 가 로컬에서 돌린 결과를 받아 리포트를 만든다.

    실행 집계는 CLI 가 준 사실을 그대로 쓰고, 기능 중요도는 MCP 가 다시 판정하며,
    결과 적절성만 Agent(LLM)에 묻는다.
    """
    trace(
        logger, "[report 1/4] 시작: %s — exit_code=%s, test_code %d자",
        project_id, execution.get("exit_code"), len(test_code),
    )
    pairs = as_pairs(sources)
    analysis = analyze(project_id, diff, pairs)
    trace(logger, "[report 2/4] 중요도 재판정 완료 — %s", analysis.importance)

    facts = ExecuteResponse(
        project_id=project_id,
        exit_code=int(execution.get("exit_code", 0)),
        output=str(execution.get("output", "")),
        passed=int(execution.get("passed", 0)),
        failed=int(execution.get("failed", 0)),
        skipped=int(execution.get("skipped", 0)),
        total=int(execution.get("total", 0)),
        failures=list(execution.get("failures") or []),
        coverage=execution.get("coverage"),
        jacoco_enabled=bool(execution.get("jacoco_enabled", False)),
        springboot_applied=bool(execution.get("springboot_applied", False)),
        applied=list(execution.get("applied") or []),
        test_file_path=str(execution.get("test_file_path", "")),
        command=list(execution.get("command") or []),
        build_errors=list(execution.get("build_errors") or []),
    )
    trace(
        logger, "[report 3/4] 실행 사실 정리 — 통과 %d, 실패 %d, 건너뜀 %d, 전체 %d",
        facts.passed, facts.failed, facts.skipped, facts.total,
    )
    judged = _ask_agent_to_judge(
        project_id, _agent_execution(facts, execution), test_code, intent, intent_rationale
    )
    result = _to_report(facts, judged, analysis, intent, intent_rationale)
    trace(logger, "[report 4/4] 리포트 합치기 완료 — %s", result.result)
    return result
