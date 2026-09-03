"""기능 중요도 판단 (코드 기반).

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP** 로 구분"
  "[UI] 4. 기능 중요도 High / Mid / Low"

중요도는 **LLM 없이** 정한다. AST 로 만든 코드 그래프에서 파급 범위를 세어 나온
사실만 쓰므로 같은 입력이면 항상 같은 답이 나오고, 근거가 숫자로 남는다.

판단 기준
  1. 그래프 영향도 점수(0~100) → 기본 등급          HIGH / MID / LOW
  2. 사용자 노출 진입점(Controller 매핑)에 닿으면    최소 MID 로 올린다
  3. SQL 실행 지점에 닿으면                          최소 MID 로 올린다
  4. 둘 다 해당하면                                  HIGH

2~4 는 점수만으로는 낮게 나올 수 있는 변경을 잡기 위한 것이다. 한 줄짜리
조건 변경이라도 그것이 공개 API 이면서 DB 를 건드리면 중요도가 낮을 수 없다.

**하지 못하는 것**: "주문 금액 계산은 매출에 직결된다" 같은 업무적 의미 판단.
그것은 LLM(Agent)의 몫이고, 여기서는 구조적 파급도만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codetest_mcp.db import NodeType, RiskLevel
from codetest_mcp.graph.impact import ImpactNodeInfo, ImpactReport

#: 영향도 등급 → 기능 중요도 표기 (정의서 UI 4 는 High/Mid/Low 3단계)
_RISK_TO_IMPORTANCE = {
    RiskLevel.HIGH.value: "HIGH",
    RiskLevel.MEDIUM.value: "MID",
    RiskLevel.LOW.value: "LOW",
}

_ORDER = {"LOW": 0, "MID": 1, "HIGH": 2}


@dataclass
class ImportanceVerdict:
    """MCP 가 코드로 확정한 기능 중요도."""

    #: HIGH / MID / LOW
    importance: str = "LOW"
    #: 판단 근거 (한 줄에 하나) — 그대로 화면에 뿌릴 수 있는 형태
    rationale: str = ""
    #: 근거가 된 그래프 영향도 점수 (0~100)
    score: int = 0
    #: 등급을 올린 신호 (진입점 도달 / SQL 도달)
    signals: list[str] = field(default_factory=list)


def _at_least(current: str, floor: str) -> str:
    return current if _ORDER[current] >= _ORDER[floor] else floor


def _entrypoints(nodes: list[ImpactNodeInfo]) -> list[ImpactNodeInfo]:
    """Controller 매핑처럼 사용자에게 노출된 진입점."""
    return [
        node
        for node in nodes
        if node.node_type == NodeType.METHOD.value and (node.meta or {}).get("entrypoint")
    ]


def _sql_nodes(nodes: list[ImpactNodeInfo]) -> list[ImpactNodeInfo]:
    return [node for node in nodes if node.node_type == NodeType.SQL.value]


def _route_of(node: ImpactNodeInfo) -> str:
    meta = node.meta or {}
    return f"{meta.get('http_method') or 'ENTRY'} {meta.get('route') or node.name}"


def judge(report: ImpactReport, graph_ready: bool = True) -> ImportanceVerdict:
    """영향도 계산 결과에서 기능 중요도를 정한다.

    :param report:      ImpactAnalyzer 가 만든 변경/파급 노드와 점수
    :param graph_ready: 개요 수집이 끝났는지. 아직이면 판단 근거가 비어 있으므로
                        등급을 올리지 않고 그 사실을 근거에 적는다.
    """
    nodes = report.all_nodes
    importance = _RISK_TO_IMPORTANCE.get(report.risk.value, "LOW")
    signals: list[str] = []
    lines = [f"영향도 점수 {report.score}/100 → 기본 등급 {importance}"]
    lines += [f"{reason}" for reason in report.reasons]

    if not graph_ready:
        lines.append(
            "프로젝트 개요 수집이 끝나지 않아 그래프 근거가 불완전합니다 — "
            "등급을 올리지 않았습니다."
        )
        return ImportanceVerdict(importance, "\n".join(f"- {line}" for line in lines),
                                 report.score, signals)

    entrypoints = _entrypoints(nodes)
    sql = _sql_nodes(nodes)

    if entrypoints:
        importance = _at_least(importance, "MID")
        sample = ", ".join(_route_of(node) for node in entrypoints[:3])
        signals.append("entrypoint")
        lines.append(f"사용자 노출 진입점 {len(entrypoints)}개에 영향 ({sample}) → 최소 MID")

    if sql:
        importance = _at_least(importance, "MID")
        tables: set[str] = set()
        for node in sql:
            tables.update((node.meta or {}).get("tables") or [])
        suffix = f" / 대상 테이블: {', '.join(sorted(tables)[:5])}" if tables else ""
        signals.append("sql")
        lines.append(f"SQL 실행 지점 {len(sql)}개 연관{suffix} → 최소 MID")

    if entrypoints and sql:
        importance = "HIGH"
        lines.append("공개 진입점과 DB 접근이 동시에 걸려 있음 → HIGH")

    return ImportanceVerdict(
        importance=importance,
        rationale="\n".join(f"- {line}" for line in lines),
        score=report.score,
        signals=signals,
    )
