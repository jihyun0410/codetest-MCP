"""기능 중요도 판단 검증 (정의서 [UI] 4).

MCP 는 LLM 을 쓰지 않는다. 같은 그래프 사실이면 **항상 같은 등급**이 나와야 하고,
근거가 숫자와 문장으로 남아야 한다.
"""

from __future__ import annotations

from codetest_mcp.db import NodeType, RiskLevel
from codetest_mcp.graph.impact import ImpactNodeInfo, ImpactReport
from codetest_mcp.importance import judge


def _node(node_type: str, name: str = "n", depth: int = 0, **meta) -> ImpactNodeInfo:
    return ImpactNodeInfo(
        node_id=name, qualified_name=f"com.example.{name}", node_type=node_type,
        file_path=f"src/main/java/com/example/{name}.java", depth=depth,
        name=name, meta=meta,
    )


def _report(changed=None, impacted=None, score=0, risk=RiskLevel.LOW, reasons=None) -> ImpactReport:
    return ImpactReport(
        changed=changed or [], impacted=impacted or [],
        score=score, risk=risk, reasons=reasons or ["직접 변경된 그래프 노드 1개"],
    )


# --- 기본: 영향도 등급을 그대로 옮긴다 -----------------------------------------
def test_risk_maps_to_importance():
    for risk, expected in (
        (RiskLevel.HIGH, "HIGH"), (RiskLevel.MEDIUM, "MID"), (RiskLevel.LOW, "LOW")
    ):
        verdict = judge(_report(changed=[_node(NodeType.METHOD.value)], risk=risk, score=40))
        assert verdict.importance == expected


def test_rationale_carries_the_score_and_graph_reasons():
    verdict = judge(_report(
        changed=[_node(NodeType.METHOD.value)], score=42, risk=RiskLevel.MEDIUM,
        reasons=["직접 변경된 그래프 노드 1개 (유형: Method)"],
    ))
    assert "영향도 점수 42/100" in verdict.rationale
    assert "직접 변경된 그래프 노드 1개" in verdict.rationale
    assert verdict.score == 42


def test_same_input_gives_the_same_answer():
    """LLM 이 아니므로 결정적이어야 한다."""
    report = _report(changed=[_node(NodeType.METHOD.value, entrypoint=True)], score=30,
                     risk=RiskLevel.MEDIUM)
    assert judge(report).importance == judge(report).importance
    assert judge(report).rationale == judge(report).rationale


# --- 승격 규칙: 점수가 낮아도 놓치면 안 되는 변경 -------------------------------
def test_entrypoint_lifts_a_low_change_to_mid():
    """공개 API 에 닿으면 한 줄 변경이라도 LOW 로 둘 수 없다."""
    verdict = judge(_report(
        changed=[_node(NodeType.METHOD.value, "getOrders",
                       entrypoint=True, http_method="GET", route="/orders")],
        score=5, risk=RiskLevel.LOW,
    ))
    assert verdict.importance == "MID"
    assert "entrypoint" in verdict.signals
    assert "GET /orders" in verdict.rationale


def test_sql_lifts_a_low_change_to_mid():
    verdict = judge(_report(
        changed=[_node(NodeType.METHOD.value)],
        impacted=[_node(NodeType.SQL.value, "sel", depth=1, tables=["orders", "order_items"])],
        score=5, risk=RiskLevel.LOW,
    ))
    assert verdict.importance == "MID"
    assert "sql" in verdict.signals
    assert "orders" in verdict.rationale        # 어느 테이블인지 근거에 남는다


def test_entrypoint_and_sql_together_is_high():
    """공개 진입점이면서 DB 를 건드리면 중요도가 낮을 수 없다."""
    verdict = judge(_report(
        changed=[_node(NodeType.METHOD.value, "getOrders",
                       entrypoint=True, http_method="GET", route="/orders")],
        impacted=[_node(NodeType.SQL.value, "sel", depth=1, tables=["orders"])],
        score=5, risk=RiskLevel.LOW,
    ))
    assert verdict.importance == "HIGH"
    assert set(verdict.signals) == {"entrypoint", "sql"}


def test_promotion_never_lowers_an_existing_high():
    verdict = judge(_report(
        changed=[_node(NodeType.METHOD.value, entrypoint=True)],
        score=80, risk=RiskLevel.HIGH,
    ))
    assert verdict.importance == "HIGH"


# --- 개요 수집 전: 근거가 없으므로 올리지 않는다 --------------------------------
def test_does_not_promote_before_the_overview_is_collected():
    """그래프가 비어 있을 때 진입점 신호를 믿고 올리면 근거 없는 등급이 된다."""
    verdict = judge(
        _report(changed=[_node(NodeType.METHOD.value, entrypoint=True)],
                score=5, risk=RiskLevel.LOW),
        graph_ready=False,
    )
    assert verdict.importance == "LOW"
    assert verdict.signals == []
    assert "개요 수집이 끝나지 않아" in verdict.rationale


def test_empty_change_is_low():
    verdict = judge(_report(reasons=[]))
    assert verdict.importance == "LOW"
    assert "영향도 점수 0/100" in verdict.rationale
