"""MCP 도구 계약 검증 (Agent 가 호출하는 형태 그대로, in-memory 클라이언트).

MCP 는 LLM 을 쓰지 않으므로 스텁이 필요 없다. Git clone / Gradle 만 대체한다.
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


# --- 도구 목록 ----------------------------------------------------------------
async def test_exposes_the_code_based_tools(client):
    names = {t.name for t in await client.list_tools()}
    assert names == {
        "hello", "register_project", "delete_project", "get_project_overview",
        "analyze_changes", "execute_tests",
    }


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


async def test_overview_reports_stored_summary(client):
    project_id = await _register(client)
    body = await _call(client, "get_project_overview", project_id=project_id)

    assert body["project_id"] == project_id
    assert body["ingest_status"] == "PENDING"
    assert body["node_counts"] == {}


async def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = await _register(client)

    assert (await _call(client, "delete_project", project_id=project_id))["deleted"]
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "delete_project", project_id=project_id)


# --- 변경 단위 식별 (정의서 (2)) ----------------------------------------------
async def test_analyze_changes_parses_diff_ranges(client):
    project_id = await _register(client)
    body = await _call(client, "analyze_changes", project_id=project_id, diff=DIFF)

    path = "src/main/java/com/example/demo/service/OrderService.java"
    assert path in body["changed_ranges"]
    assert body["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert body["risk_reasons"]


async def test_analyze_warns_when_overview_not_ready(client):
    project_id = await _register(client)     # ingest 는 스텁이라 PENDING 상태
    body = await _call(client, "analyze_changes", project_id=project_id, diff=DIFF)

    assert body["graph_ready"] is False
    assert any("개요 수집" in w for w in body["warnings"])


async def test_analyze_unknown_project_is_rejected(client):
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "analyze_changes", project_id="nope", diff="")


# --- 테스트 실행 (정의서 (1), [상세] 4) ----------------------------------------
async def test_execute_injects_springboot_and_reports_facts(client, monkeypatch):
    project_id = await _register(client)
    captured: dict = {}

    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _fake_run(repo_path, prepared: PreparedTest, overlay_sources=None):
        captured["source"] = prepared.source
        captured["file_path"] = prepared.file_path
        captured["overlay"] = overlay_sources
        return ExecutionResult(
            exit_code=0, output="BUILD SUCCESSFUL",
            passed=3, failed=0, skipped=0, total=3,
            coverage={"line_rate": 92.0, "line_covered": 23, "line_missed": 2,
                      "branch_rate": 75.0, "branch_covered": 3, "branch_missed": 1},
            jacoco_enabled=True,
            test_file_path=prepared.file_path,
            springboot_applied=prepared.springboot_applied,
            applied=prepared.applied,
            command=["sh", "./gradlew", "test"],
        )

    monkeypatch.setattr(main, "run_tests", _fake_run)

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
