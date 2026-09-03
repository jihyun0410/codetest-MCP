"""MCP 도구 계약 검증 (CLI 가 호출하는 형태 그대로, in-memory 클라이언트).

MCP 는 LLM 을 직접 쓰지 않는다. LLM 판단은 Agent(FastAPI)로 넘기므로
Git clone / Gradle 과 함께 **Agent 호출**을 스텁으로 대체한다.
"""

from __future__ import annotations

import pathlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codetest_mcp import db as db_module
from codetest_mcp import main
from codetest_mcp.config import settings
from codetest_mcp.db import Base
from codetest_mcp.executor import ExecutionResult
from codetest_mcp.main import mcp
from codetest_mcp.springboot import PreparedTest

ORDER_SERVICE = """\
package com.example.demo.service;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    public double calculateTotal(Order order) {
        double subtotal = order.getQuantity() * order.getUnitPrice();
        if (order.getQuantity() > 10) {
            return subtotal * 0.9;
        }
        return subtotal;
    }
}
"""

DIFF = """\
diff --git a/src/main/java/com/example/demo/service/OrderService.java b/src/main/java/com/example/demo/service/OrderService.java
--- a/src/main/java/com/example/demo/service/OrderService.java
+++ b/src/main/java/com/example/demo/service/OrderService.java
@@ -6,4 +6,7 @@
     public double calculateTotal(Order order) {
         double subtotal = order.getQuantity() * order.getUnitPrice();
+        if (order.getQuantity() > 10) {
+            return subtotal * 0.9;
+        }
         return subtotal;
     }
"""


GENERATED_TEST = """\
package com.example.demo;

class GeneratedOrderTest {
    @Test
    void t() {}
}
"""


def _stub_generate(**kwargs) -> dict:
    """Agent `/api/v1/tests/generate` 대역 — 받은 분석을 그대로 되비춘다."""
    _stub_generate.calls.append(kwargs)
    return {
        "thinking": "수량 10 초과 분기가 새로 생겼다",
        "intent": "조건 변경",
        "intent_rationale": "- quantity > 10 조건이 추가됨",
        "test_cases": "- [정상] 10개 이하\n- [실패] 11개",
        "test_code": GENERATED_TEST,
        "rationale": "- 경계값을 검증한다",
        "target_code": "### OrderService.java",
        "base_package": kwargs.get("analysis", {}).get("base_package"),
    }


_stub_generate.calls = []


def _stub_report(**kwargs) -> dict:
    """Agent `/api/v1/tests/execute` 대역 — 적절성 판정만 돌려준다."""
    _stub_report.calls.append(kwargs)
    return {
        "verdict": "적절",
        "verdict_rationale": "- 경계값이 모두 검증됨",
        "details": "- 3건 성공",
        "intent": kwargs.get("intent", ""),
        "intent_rationale": kwargs.get("intent_rationale", ""),
    }


_stub_report.calls = []


@pytest.fixture
async def client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'mcp.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    # session_scope 가 호출 시점에 조회하므로 여기만 바꾸면 전 도구에 적용된다.
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(main, "init_db", lambda: None)
    # clone 은 네트워크가 필요하므로 등록 테스트에서는 수집을 건너뛴다.
    monkeypatch.setattr(main, "run_ingest", lambda project_id: None)
    monkeypatch.setattr(settings, "api_keys", [])
    # Agent 는 별도 프로세스다. LLM 판단만 대신 돌려주는 스텁으로 바꾼다.
    monkeypatch.setattr(main.agent_client, "generate", _stub_generate)
    monkeypatch.setattr(main.agent_client, "report", _stub_report)

    async with Client(mcp) as c:
        yield c


async def _call(client, tool: str, **args) -> dict:
    return (await client.call_tool(tool, args)).structured_content


async def _register(client, name="demo") -> str:
    body = await _call(
        client, "register_project",
        name=name, git_url="https://github.com/acme/demo",
        owner="kim", github_token="ghp_secret", default_branch="main",
    )
    return body["id"]


# --- 도구 목록 ----------------------------------------------------------------
async def test_exposes_the_code_based_tools(client):
    names = {t.name for t in await client.list_tools()}
    assert names == {
        "hello", "register_project", "delete_project",
        "test_generate", "test_run", "execute_tests",
    }


async def test_removed_tools_are_not_exposed(client):
    """get_project_overview / analyze_changes 는 도구로 노출하지 않는다."""
    names = {t.name for t in await client.list_tools()}
    assert "get_project_overview" not in names
    assert "analyze_changes" not in names


async def test_hello_echoes(client):
    assert (await client.call_tool("hello", {"name": "kim"})).data.endswith("kim")


# --- 프로젝트 개요 (정의서 [상세] 1) -------------------------------------------
async def test_register_project_hides_the_token(client):
    body = await _call(
        client, "register_project",
        name="demo", git_url="https://github.com/acme/demo/",
        owner="kim", github_token="ghp_secret",
    )

    assert body["ingest_status"] == "PENDING"
    assert body["has_github_token"] is True
    assert "ghp_secret" not in str(body)
    assert body["git_url"] == "https://github.com/acme/demo"   # 끝 슬래시 정규화


async def test_duplicate_name_is_rejected(client):
    await _register(client)
    with pytest.raises(ToolError, match="같은 이름"):
        await _call(client, "register_project",
                    name="demo", git_url="https://github.com/acme/other", owner="kim")


async def test_bad_git_url_is_rejected(client):
    with pytest.raises(ToolError, match="git_url"):
        await _call(client, "register_project",
                    name="x", git_url="ftp://nope", owner="kim")


async def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = await _register(client)

    assert (await _call(client, "delete_project", project_id=project_id))["deleted"]
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "delete_project", project_id=project_id)


# --- Test Code 생성 (정의서 (1) generate, (2), (3)) ---------------------------
async def test_generate_analyzes_then_asks_the_agent(client):
    _stub_generate.calls.clear()
    project_id = await _register(client)
    body = await _call(client, "test_generate", project_id=project_id, diff=DIFF,
                       sources=[{"path": "src/main/java/com/example/demo/service/OrderService.java",
                                 "content": ORDER_SERVICE}])

    # MCP 가 Diff 를 코드 기반으로 분석해 Agent 에게 넘겼다
    analysis = _stub_generate.calls[-1]["analysis"]
    path = "src/main/java/com/example/demo/service/OrderService.java"
    assert path in analysis["changed_ranges"]
    assert analysis["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert analysis["diff"] == DIFF          # 원본 Diff 도 함께 넘어간다

    # Agent 의 LLM 판단이 응답에 실렸다
    assert body["intent"] == "조건 변경"
    assert "@SpringBootTest" not in body["test_code"]   # 주입은 실행 단계의 일
    assert body["test_code"].strip()


async def test_generate_reports_importance_with_its_rationale(client):
    """[UI] 4 기능 중요도 + 그렇게 판단한 근거를 함께 돌려준다."""
    project_id = await _register(client)
    body = await _call(client, "test_generate", project_id=project_id, diff=DIFF)

    assert body["importance"] in {"HIGH", "MID", "LOW"}
    assert body["importance_reasons"]
    # 근거 첫 줄은 등급이 나온 계산 기준을 밝힌다
    assert str(body["importance_score"]) in body["importance_rationale"]
    assert body["importance"] in body["importance_rationale"]


async def test_generate_warns_when_overview_not_ready(client):
    project_id = await _register(client)     # ingest 는 스텁이라 PENDING 상태
    body = await _call(client, "test_generate", project_id=project_id, diff=DIFF)

    assert any("개요 수집" in w for w in body["analysis_warnings"])


async def test_generate_unknown_project_is_rejected(client):
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "test_generate", project_id="nope", diff="")


async def test_generate_surfaces_agent_failure(client, monkeypatch):
    from codetest_mcp.agent_client import AgentError

    project_id = await _register(client)

    def _boom(**kwargs):
        raise AgentError("Agent 에 연결할 수 없습니다: http://localhost:8000")

    monkeypatch.setattr(main.agent_client, "generate", _boom)
    with pytest.raises(ToolError, match="Agent"):
        await _call(client, "test_generate", project_id=project_id, diff=DIFF)


# --- 테스트 실행 (정의서 (1), [상세] 4) ----------------------------------------
def _stub_gradle(monkeypatch, captured: dict, *, failed: int = 0, exit_code: int = 0):
    """clone 과 gradle 을 대체하고, 실제로 실행된 소스를 captured 에 남긴다."""
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _fake_run(repo_path, prepared: PreparedTest, overlay_sources=None):
        captured["source"] = prepared.source
        captured["file_path"] = prepared.file_path
        captured["overlay"] = overlay_sources
        return ExecutionResult(
            exit_code=exit_code, output="BUILD SUCCESSFUL",
            passed=3, failed=failed, skipped=0, total=3 + failed,
            coverage={"line_rate": 92.0, "line_covered": 23, "line_missed": 2,
                      "branch_rate": 75.0, "branch_covered": 3, "branch_missed": 1},
            jacoco_enabled=True,
            test_file_path=prepared.file_path,
            springboot_applied=prepared.springboot_applied,
            applied=prepared.applied,
            command=["sh", "./gradlew", "test"],
        )

    monkeypatch.setattr(main, "run_tests", _fake_run)


async def test_execute_injects_springboot_and_reports_facts(client, monkeypatch):
    project_id = await _register(client)
    captured: dict = {}
    _stub_gradle(monkeypatch, captured)

    body = await _call(
        client, "execute_tests",
        project_id=project_id,
        test_code="package com.example.demo;\n\nclass FooTest {\n  @Test\n  void t() {}\n}\n",
        sources=[{"path": "src/main/java/com/example/demo/service/OrderService.java",
                  "content": ORDER_SERVICE}],
    )

    # (1) @SpringBootTest 가 실제로 주입되어 실행됐다
    assert "@SpringBootTest" in captured["source"]
    assert body["springboot_applied"] is True
    assert captured["file_path"] == "src/test/java/com/example/demo/FooTest.java"
    # 변경 파일이 실행 전에 작업 사본으로 전달된다
    assert captured["overlay"][0][0].endswith("OrderService.java")
    # [상세 4] JaCoCo 커버리지가 사실로 돌아온다
    assert body["coverage"]["line_rate"] == 92.0
    assert body["jacoco_enabled"] is True
    assert body["passed"] == 3
    # PASS/FAIL 은 집계로 확정하고, 적절성 판정만 Agent 가 채운다
    assert body["result"] == "PASS"
    assert body["verdict"] == "적절"
    assert body["verdict_rationale"]
    # [UI] 4 중요도와 근거는 실행 리포트에도 실린다
    assert body["importance"] in {"HIGH", "MID", "LOW"}
    assert body["importance_rationale"]


async def test_execute_marks_failure_and_keeps_the_intent(client, monkeypatch):
    project_id = await _register(client)
    _stub_gradle(monkeypatch, {}, failed=2, exit_code=1)

    body = await _call(
        client, "execute_tests",
        project_id=project_id,
        test_code="package com.example.demo;\n\nclass FooTest {}\n",
        diff=DIFF,
        intent="조건 변경",
        intent_rationale="- quantity > 10 조건이 추가됨",
    )

    assert body["result"] == "FAIL"
    # 정의서 (2): 파악한 의도와 근거를 결과값에 넣는다
    assert body["intent"] == "조건 변경"
    assert body["intent_rationale"]


# --- 생성 + 실행 (정의서 (1) run) ------------------------------------------------
async def test_run_generates_then_executes(client, monkeypatch):
    project_id = await _register(client)
    captured: dict = {}
    _stub_gradle(monkeypatch, captured)

    body = await _call(
        client, "test_run",
        project_id=project_id, diff=DIFF,
        sources=[{"path": "src/main/java/com/example/demo/service/OrderService.java",
                  "content": ORDER_SERVICE}],
    )

    generated, report = body["generated"], body["report"]
    # Agent 가 만든 코드가 그대로 실행 단계로 넘어가 @SpringBootTest 가 주입됐다
    assert "GeneratedOrderTest" in generated["test_code"]
    assert "@SpringBootTest" in captured["source"]
    assert report["result"] == "PASS"
    # 생성 결과와 리포트 양쪽에서 중요도와 근거를 볼 수 있다
    assert generated["importance"] == report["importance"]
    assert generated["importance_rationale"]
    assert report["importance_rationale"]
    # 생성 때 파악한 의도가 판정 단계로 이어진다
    assert report["intent"] == generated["intent"]


async def test_run_rejects_empty_test_code(client, monkeypatch):
    project_id = await _register(client)
    monkeypatch.setattr(main.agent_client, "generate", lambda **kw: {"test_code": ""})

    with pytest.raises(ToolError, match="생성하지 못했습니다"):
        await _call(client, "test_run", project_id=project_id, diff=DIFF)


async def test_execute_rejects_unparseable_test_code(client, monkeypatch):
    project_id = await _register(client)
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )
    with pytest.raises(ToolError):
        await _call(client, "execute_tests",
                    project_id=project_id, test_code="// class 선언이 없다")


async def test_execute_surfaces_missing_gradle(client, monkeypatch):
    from codetest_mcp.executor import ExecutionError

    project_id = await _register(client)
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _boom(*args, **kwargs):
        raise ExecutionError("Gradle 을 찾을 수 없습니다.")

    monkeypatch.setattr(main, "run_tests", _boom)
    with pytest.raises(ToolError, match="Gradle"):
        await _call(client, "execute_tests", project_id=project_id, test_code="class T {}")


# --- 인증 (http 전송에서만 검사) -----------------------------------------------
async def test_api_key_is_enforced_over_http(monkeypatch):
    """in-memory 전송은 HTTP 헤더가 없으므로 미들웨어가 통과시킨다."""
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    from fastmcp.server.dependencies import get_http_headers

    assert get_http_headers() == {}          # stdio/in-memory: 신뢰 경계 아님
    assert main.verify_api_key("s3cret") is True
    assert main.verify_api_key("wrong") is False
