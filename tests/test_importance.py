"""기능 중요도 판정 검증 (정의서 [UI] 4 + 근거 제시 의무).

LLM 없이 그래프 사실만으로 등급과 근거가 정해지는지 확인한다.
"""

from __future__ import annotations

from codetest_mcp import importance
from codetest_mcp.db import NodeType, RiskLevel
from codetest_mcp.graph.impact import ImpactNodeInfo, ImpactReport


def _node(node_type: str, **meta) -> ImpactNodeInfo:
    return ImpactNodeInfo(
        node_id=meta.pop("node_id", "n1"),
        qualified_name=meta.pop("qualified_name", "com.example.demo.Foo#bar"),
        node_type=node_type,
        file_path=meta.pop("file_path", "src/main/java/com/example/demo/Foo.java"),
        depth=meta.pop("depth", 0),
        name=meta.pop("name", "bar"),
        meta=meta,
    )


def _report(score: int, changed=(), impacted=(), reasons=()) -> ImpactReport:
    return ImpactReport(
        changed=list(changed),
        impacted=list(impacted),
        score=score,
        risk=RiskLevel.LOW,
        reasons=list(reasons),
    )


def test_score_maps_to_three_levels():
    assert importance.judge(_report(70), {}).level == "HIGH"
    assert importance.judge(_report(30), {}).level == "MID"
    assert importance.judge(_report(5), {}).level == "LOW"


def test_rationale_always_explains_the_level():
    verdict = importance.judge(_report(30, reasons=["변경 파일 2개 / 변경 라인 약 12줄"]), {})

    assert verdict.reasons[0].startswith("영향도 점수 30점 → MID")
    # 분석이 만든 근거도 그대로 이어진다
    assert "변경 파일 2개" in verdict.rationale
    # UI 는 rationale 을 그대로 출력한다 — 한 줄에 하나씩
    assert verdict.rationale.startswith("- ")


def test_entrypoint_raises_low_to_mid():
    entry = _node(NodeType.METHOD.value, entrypoint=True, http_method="POST", route="/orders")
    verdict = importance.judge(_report(5, changed=[entry]), {})

    assert verdict.level == "MID"
    assert any("승격" in reason and "POST /orders" in reason for reason in verdict.reasons)


def test_entrypoint_with_sql_raises_to_high():
    entry = _node(NodeType.METHOD.value, entrypoint=True, http_method="GET", route="/orders")
    sql = _node(NodeType.SQL.value, node_id="n2")
    verdict = importance.judge(_report(10, changed=[entry], impacted=[sql]), {})

    assert verdict.level == "HIGH"
    assert any("HIGH 로 올림" in reason for reason in verdict.reasons)


def test_sql_alone_raises_low_to_mid():
    verdict = importance.judge(_report(5, changed=[_node(NodeType.SQL.value)]), {})

    assert verdict.level == "MID"
    assert any("데이터 정합성" in reason for reason in verdict.reasons)


def test_promotion_never_lowers_a_level():
    entry = _node(NodeType.METHOD.value, entrypoint=True)
    verdict = importance.judge(_report(80, changed=[entry]), {})

    assert verdict.level == "HIGH"          # MID 승격 규칙이 HIGH 를 끌어내리지 않는다


def test_empty_graph_says_so_in_the_rationale():
    verdict = importance.judge(_report(2), {"a.java": [(1, 5)]})

    assert verdict.level == "LOW"
    assert any("AST 그래프에서 변경 지점을 찾지 못해" in reason for reason in verdict.reasons)


def test_to_dict_matches_the_response_fields():
    payload = importance.judge(_report(30), {}).to_dict()

    assert set(payload) == {
        "importance", "importance_score", "importance_reasons", "importance_rationale",
    }
    assert payload["importance"] == "MID"
    assert payload["importance_score"] == 30
