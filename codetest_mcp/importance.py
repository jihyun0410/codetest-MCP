"""
기능 중요도 판정 (코드 기반, LLM 미사용).

정의서:
  · [UI] 4  "기능 중요도 : High / Mid / Low"
  · 흐름 3  "영향도 등급 … 반드시 어떠한 근거로 영향도를 표시하는지(Rationale)를
             명확히 제시해야 함"

중요도는 **그래프가 확정한 사실**(변경 단위, 파급 노드, 진입점/SQL 도달, 변경 규모)
만으로 결정된다. 해석이 아니라 규칙 적용이므로 MCP 의 책임이다.

판정 방식
  1. `ImpactAnalyzer` 가 산출한 0~100 점수를 임계값으로 3단계에 매핑한다.
  2. 점수만으로 놓치는 위험은 **승격 규칙**으로 올린다.
       · 사용자 노출 진입점(Controller 매핑)에 영향  → 최소 MID
       · 진입점 + SQL 이 함께 걸림                    → HIGH
       · SQL 실행 지점에 영향                         → 최소 MID
  3. 판단에 실제로 쓰인 근거만 rationale 로 남긴다 (UI 가 그대로 보여 준다).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codetest_mcp.db import NodeType
from codetest_mcp.graph.impact import ImpactNodeInfo, ImpactReport

#: 정의서 [UI] 4 의 3단계
HIGH, MID, LOW = "HIGH", "MID", "LOW"

#: 점수 → 등급 임계값 (ImpactAnalyzer 의 risk 임계값과 같은 기준을 쓴다)
_HIGH_THRESHOLD = 55
_MID_THRESHOLD = 25

#: 등급 비교용 순위 (승격 규칙이 등급을 내리지 않도록)
_RANK = {LOW: 0, MID: 1, HIGH: 2}


@dataclass
class ImportanceVerdict:
    """기능 중요도와 그 판단 근거."""

    level: str = LOW
    score: int = 0
    #: 이 등급이 나온 이유 (한 줄에 하나)
    reasons: list[str] = field(default_factory=list)

    @property
    def rationale(self) -> str:
        """UI 에 그대로 실을 수 있는 여러 줄 근거."""
        return "\n".join(f"- {reason}" for reason in self.reasons)

    def to_dict(self) -> dict:
        return {
            "importance": self.level,
            "importance_score": self.score,
            "importance_reasons": list(self.reasons),
            "importance_rationale": self.rationale,
        }


def judge(report: ImpactReport, diff_ranges: dict[str, list[tuple[int, int]]]) -> ImportanceVerdict:
    """영향도 분석 결과로 기능 중요도(High/Mid/Low)와 근거를 확정한다."""
    score = report.score
    level = _by_score(score)
    reasons: list[str] = [
        (
            f"영향도 점수 {score}점 → {level} "
            f"(기준: {_HIGH_THRESHOLD}점 이상 HIGH, {_MID_THRESHOLD}점 이상 MID)"
        )
    ]
    reasons.extend(report.reasons)

    nodes = report.all_nodes
    endpoints = _entrypoints(nodes)
    sql_nodes = [info for info in nodes if info.node_type == NodeType.SQL.value]

    # --- 승격 규칙 ------------------------------------------------------
    if endpoints and sql_nodes:
        level, changed = _raise_to(level, HIGH)
        if changed:
            reasons.append(
                f"승격: 사용자 노출 진입점 {len(endpoints)}개와 SQL 실행 지점 "
                f"{len(sql_nodes)}개가 함께 걸려 HIGH 로 올림"
            )
    elif endpoints:
        level, changed = _raise_to(level, MID)
        if changed:
            sample = ", ".join(_describe_entrypoint(info) for info in endpoints[:3])
            reasons.append(f"승격: 사용자에게 노출되는 진입점에 영향({sample}) — 최소 MID")
    elif sql_nodes:
        level, changed = _raise_to(level, MID)
        if changed:
            reasons.append(
                f"승격: SQL 실행 지점 {len(sql_nodes)}개에 영향 — 데이터 정합성 위험으로 최소 MID"
            )

    # --- 그래프가 비어 근거가 없을 때 ------------------------------------
    if not nodes:
        changed_files = len(diff_ranges)
        reasons.append(
            f"AST 그래프에서 변경 지점을 찾지 못해 변경 규모(파일 {changed_files}개)만으로 판단"
            if changed_files
            else "변경 내용이 없어 판단 근거가 없음"
        )

    return ImportanceVerdict(level=level, score=score, reasons=reasons)


# ---------------------------------------------------------------------------
def _by_score(score: int) -> str:
    if score >= _HIGH_THRESHOLD:
        return HIGH
    if score >= _MID_THRESHOLD:
        return MID
    return LOW


def _raise_to(current: str, target: str) -> tuple[str, bool]:
    """등급을 target 이상으로 올린다. (새 등급, 실제로 올랐는지)"""
    if _RANK[target] > _RANK[current]:
        return target, True
    return current, False


def _entrypoints(nodes: list[ImpactNodeInfo]) -> list[ImpactNodeInfo]:
    return [
        info
        for info in nodes
        if info.node_type == NodeType.METHOD.value and (info.meta or {}).get("entrypoint")
    ]


def _describe_entrypoint(info: ImpactNodeInfo) -> str:
    meta = info.meta or {}
    return f"{meta.get('http_method') or 'ENTRY'} {meta.get('route') or info.name}".strip()
