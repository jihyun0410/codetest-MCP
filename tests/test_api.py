"""MCP 도구 계약 검증 — CLI 가 호출하는 형태 그대로 (in-memory 클라이언트).

MCP 가 진입점이다. 코드 기반 사실은 직접 만들고, LLM 판단만 Agent 에 넘긴다.
Agent(HTTP)·Git clone·Gradle 만 대역을 쓴다 — MCP 자신은 스텁하지 않는다.
"""

from __future__ import annotations

import pathlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codetest_mcp import db as db_module
from codetest_mcp import main, orchestrator
from codetest_mcp.config import settings
from codetest_mcp.db import Base
from codetest_mcp.executor import ExecutionResult
from codetest_mcp.main import mcp
from codetest_mcp.springboot import PreparedTest

ORDER_PATH = "src/main/java/com/example/demo/service/OrderService.java"

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

DIFF = f"""\
diff --git a/{ORDER_PATH} b/{ORDER_PATH}
--- a/{ORDER_PATH}
+++ b/{ORDER_PATH}
@@ -6,4 +6,7 @@
     public double calculateTotal(Order order) {{
+        if (order.getQuantity() > 10) {{
+            return subtotal * 0.9;
+        }}
         return subtotal;
     }}
"""

SOURCES = [{"path": ORDER_PATH, "content": ORDER_SERVICE}]

GENERATED = {
    "thinking": "- 조건 분기가 추가됐다",
    "intent": "조건 변경",
    "intent_rationale": "- `if (order.getQuantity() > 10)` 추가",
    "test_cases": "- [정상] 11개면 할인\n- [실패] 10개는 할인 없음",
    "test_code": "package com.example.demo;\n\nclass OrderServiceTest {\n  void t() {}\n}\n",
    "rationale": "- 임계값 경계 검증",
    "target_code": "",
    "base_package": "com.example.demo",
}

JUDGED = {
    "verdict": "적절",
    "verdict_rationale": "- 변경된 분기를 모두 통과함",
    "details": "- 2 passed",
    "intent": "조건 변경",
    "intent_rationale": "- `if (order.getQuantity() > 10)` 추가",
}


class StubAgent:
    """Agent 대역 — MCP 가 무엇을 위임하는지 기록한다."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last_generate: dict = {}
        self.last_report: dict = {}

    def health(self) -> dict:
        self.calls.append("health")
        return {"status": "ok"}

    def generate(self, project_id, analysis, sources, project_name=""):
        self.calls.append("generate")
        self.last_generate = {
            "project_id": project_id, "analysis": analysis,
            "sources": sources, "project_name": project_name,
        }
        return dict(GENERATED)

    def report(self, project_id, execution, test_code, intent="", intent_rationale=""):
        self.calls.append("report")
        self.last_report = {
            "project_id": project_id, "execution": execution, "test_code": test_code,
            "intent": intent, "intent_rationale": intent_rationale,
        }
        return dict(JUDGED)


@pytest.fixture
def agent(monkeypatch) -> StubAgent:
    stub = StubAgent()
    monkeypatch.setattr(orchestrator, "agent_client", stub)
    monkeypatch.setattr(main, "agent_client", stub)
    return stub


@pytest.fixture
def gradle(monkeypatch) -> dict:
    """Gradle 실행과 clone 을 대역으로 바꾸고 무엇이 넘어갔는지 기록한다."""
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _run(repo_path, prepared: PreparedTest, overlay_sources=None):
        captured["source"] = prepared.source
        captured["file_path"] = prepared.file_path
        captured["overlay"] = overlay_sources
        return ExecutionResult(
            exit_code=captured.get("exit_code", 0), output="BUILD SUCCESSFUL",
            passed=2, failed=captured.get("failed", 0), skipped=0, total=2,
            coverage={"line_rate": 88.0, "branch_rate": 70.0},
            jacoco_enabled=True, test_file_path=prepared.file_path,
            springboot_applied=prepared.springboot_applied,
            applied=prepared.applied, command=["sh", "./gradlew", "test"],
        )

    monkeypatch.setattr(orchestrator, "run_tests", _run)
    return captured


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


# --- 도구 목록 — CLI 가 부르는 이름이 전부 있어야 한다 --------------------------
async def test_exposes_every_tool_the_cli_calls(client):
    names = {t.name for t in await client.list_tools()}
    assert {
        "hello", "register_project", "delete_project",
        "test_generate", "test_run", "execute_tests",
    } <= names
    # 코드 기반 조회 도구
    assert {"analyze_changes", "get_project_overview"} <= names


async def test_hello_reports_agent_status(client, agent):
    text = (await client.call_tool("hello", {"name": "kim"})).data
    assert "kim" in text
    assert "agent: ok" in text
    assert "health" in agent.calls


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
        await _call(client, "register_project", name="x", git_url="ftp://nope", owner="kim")


async def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(orchestrator.RepoService, "remove", lambda self: None)
    project_id = await _register(client)
    assert (await _call(client, "delete_project", project_id=project_id))["deleted"]
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "delete_project", project_id=project_id)


# --- 변경 단위 + 기능 중요도 (정의서 (2), [UI] 4) — LLM 미개입 ------------------
async def test_analyze_identifies_changes_without_the_agent(client, agent):
    project_id = await _register(client)
    body = await _call(client, "analyze_changes",
                       project_id=project_id, diff=DIFF, sources=SOURCES)

    assert ORDER_PATH in body["changed_ranges"]
    assert body["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert body["importance"] in {"HIGH", "MID", "LOW"}
    assert "영향도 점수" in body["importance_rationale"]
    assert agent.calls == []          # 중요도 판단에 LLM 을 쓰지 않는다


async def test_analyze_uses_sources_when_the_diff_has_no_hunk(client):
    project_id = await _register(client)
    body = await _call(client, "analyze_changes", project_id=project_id, diff="",
                       sources=[{"path": "src/main/java/A.java", "content": "class A {}\nint x;\n"}])
    assert body["changed_ranges"]["src/main/java/A.java"] == [[1, 3]]


async def test_analyze_warns_when_overview_not_ready(client):
    project_id = await _register(client)     # ingest 는 스텁이라 PENDING 상태
    body = await _call(client, "analyze_changes", project_id=project_id, diff=DIFF)
    assert body["graph_ready"] is False
    assert any("개요 수집" in w for w in body["warnings"])


async def test_analyze_unknown_project_is_rejected(client):
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "analyze_changes", project_id="nope", diff="")


# --- CLI `codetest generate` ---------------------------------------------------
async def test_generate_delegates_only_the_llm_part(client, agent):
    project_id = await _register(client)
    body = await _call(client, "test_generate",
                       project_id=project_id, diff=DIFF, sources=SOURCES)

    assert agent.calls == ["generate"]           # 실행은 하지 않는다
    assert body["intent"] == "조건 변경"          # Agent 판단
    assert body["thinking"].startswith("- 조건")  # Agent 판단
    assert "class OrderServiceTest" in body["test_code"]
    # 중요도는 MCP 가 정한 값이다 (Agent 응답에는 없다)
    assert "importance" not in GENERATED
    assert body["importance"] in {"HIGH", "MID", "LOW"}
    assert "영향도 점수" in body["importance_rationale"]


async def test_generate_hands_mcp_facts_to_the_agent(client, agent):
    project_id = await _register(client)
    await _call(client, "test_generate", project_id=project_id, diff=DIFF, sources=SOURCES)

    analysis = agent.last_generate["analysis"]
    assert analysis["diff"] == DIFF                     # 원본 diff 를 그대로 넘긴다
    assert ORDER_PATH in analysis["changed_ranges"]
    assert analysis["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert agent.last_generate["project_name"] == "demo"
    assert agent.last_generate["sources"][0]["path"] == ORDER_PATH


# --- CLI `codetest run` --------------------------------------------------------
async def test_run_generates_executes_and_judges(client, agent, gradle):
    project_id = await _register(client)
    body = await _call(client, "test_run", project_id=project_id, diff=DIFF, sources=SOURCES)

    assert agent.calls == ["generate", "report"]
    # (1) @SpringBootTest 가 실제로 주입되어 실행됐다
    assert "@SpringBootTest" in gradle["source"]
    assert gradle["overlay"][0][0] == ORDER_PATH

    generated, report = body["generated"], body["report"]
    assert generated["intent"] == "조건 변경"
    assert report["result"] == "PASS"
    assert report["verdict"] == "적절"                    # [UI 3] Agent 판단
    assert report["coverage"]["line_rate"] == 88.0        # [상세 4] JaCoCo
    assert report["springboot_applied"] is True
    # 같은 분석에서 나온 중요도라 생성/판정 결과가 일치한다
    assert report["importance"] == generated["importance"]


async def test_run_sends_the_execution_facts_to_the_agent(client, agent, gradle):
    project_id = await _register(client)
    await _call(client, "test_run", project_id=project_id, diff=DIFF, sources=SOURCES)

    execution = agent.last_report["execution"]
    assert execution["exit_code"] == 0
    assert execution["passed"] == 2
    assert execution["coverage"]["line_rate"] == 88.0
    assert agent.last_report["intent"] == "조건 변경"      # 생성 때 파악한 의도를 잇는다


async def test_exit_code_beats_the_llm_opinion(client, agent, gradle):
    """Agent 가 '적절' 이라 해도 gradle exit code 가 사실이다."""
    project_id = await _register(client)
    gradle["exit_code"] = 1
    gradle["failed"] = 2
    body = await _call(client, "test_run", project_id=project_id, diff=DIFF, sources=SOURCES)
    assert body["report"]["result"] == "FAIL"


async def test_run_stops_when_the_agent_returns_no_test_code(client, agent, gradle):
    project_id = await _register(client)
    agent.generate = lambda *a, **k: {**GENERATED, "test_code": "   "}
    with pytest.raises(ToolError, match="Test Code"):
        await _call(client, "test_run", project_id=project_id, diff=DIFF, sources=SOURCES)


# --- CLI `codetest test` -------------------------------------------------------
async def test_execute_runs_the_given_code_and_judges(client, agent, gradle):
    project_id = await _register(client)
    body = await _call(
        client, "execute_tests", project_id=project_id,
        test_code="package com.example.demo;\n\nclass FooTest {\n  void t() {}\n}\n",
        sources=SOURCES, diff=DIFF,
        intent="조건 변경", intent_rationale="- 이전 실행에서 파악",
    )

    assert agent.calls == ["report"]                      # 생성은 하지 않는다
    assert "@SpringBootTest" in gradle["source"]
    assert gradle["file_path"] == "src/test/java/com/example/demo/FooTest.java"
    assert gradle["overlay"][0][0] == ORDER_PATH     # 미커밋 변경분이 작업 사본에 반영된다
    assert body["result"] == "PASS"
    assert body["verdict"] == "적절"
    assert body["intent"] == "조건 변경"                   # 이전 의도를 그대로 싣는다
    # diff 를 함께 받았으므로 이번 실행에서도 중요도를 판단한다
    assert body["importance"] in {"HIGH", "MID", "LOW"}
    assert "영향도 점수" in body["importance_rationale"]


async def test_execute_honours_base_package(client, agent, gradle):
    project_id = await _register(client)
    await _call(client, "execute_tests", project_id=project_id,
                test_code="class FooTest {}", base_package="com.acme.billing")
    assert gradle["file_path"] == "src/test/java/com/acme/billing/FooTest.java"


async def test_execute_rejects_empty_test_code(client, agent):
    project_id = await _register(client)
    with pytest.raises(ToolError, match="비어 있습니다"):
        await _call(client, "execute_tests", project_id=project_id, test_code="   ")


async def test_execute_rejects_unparseable_test_code(client, agent, gradle):
    project_id = await _register(client)
    with pytest.raises(ToolError):
        await _call(client, "execute_tests",
                    project_id=project_id, test_code="// class 선언이 없다")


async def test_execute_surfaces_missing_gradle(client, agent, monkeypatch):
    from codetest_mcp.executor import ExecutionError

    project_id = await _register(client)
    monkeypatch.setattr(
        orchestrator.RepoService, "ensure_clone", lambda self, b=None: pathlib.Path("/tmp/x")
    )

    def _boom(*args, **kwargs):
        raise ExecutionError("Gradle 을 찾을 수 없습니다.")

    monkeypatch.setattr(orchestrator, "run_tests", _boom)
    with pytest.raises(ToolError, match="Gradle"):
        await _call(client, "execute_tests", project_id=project_id, test_code="class T {}")


# --- Agent 장애 전파 -----------------------------------------------------------
async def test_agent_failure_is_surfaced_to_the_cli(client, agent, monkeypatch):
    from codetest_mcp.agent_client import AgentError

    project_id = await _register(client)

    def _boom(*args, **kwargs):
        raise AgentError("Agent 에 연결할 수 없습니다: http://localhost:8000/api/v1")

    agent.generate = _boom
    with pytest.raises(ToolError, match="Agent 생성 호출 실패"):
        await _call(client, "test_generate", project_id=project_id, diff=DIFF)


# --- 인증 (http 전송에서만 검사) -----------------------------------------------
async def test_api_key_is_enforced_over_http(monkeypatch):
    """in-memory 전송은 HTTP 헤더가 없으므로 미들웨어가 통과시킨다."""
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    from fastmcp.server.dependencies import get_http_headers

    assert get_http_headers() == {}          # stdio/in-memory: 신뢰 경계 아님
    assert main.verify_api_key("s3cret") is True
    assert main.verify_api_key("wrong") is False


# --- 회귀: 흐름 안에서 sources 가 여러 번 정규화된다 ----------------------------
def test_as_pairs_is_idempotent():
    """test_run 은 sources 를 분석과 실행에 두 번 넘긴다 — 두 번째에 비면 안 된다."""
    from codetest_mcp.orchestrator import as_pairs

    once = as_pairs([{"path": "A.java", "content": "class A {}"}])
    assert once == [("A.java", "class A {}")]
    assert as_pairs(once) == once
    assert as_pairs(as_pairs(once)) == once
