"""MCP 도구 계약 검증 (Agent 가 호출하는 형태 그대로, in-memory 클라이언트).

MCP 는 LLM 을 쓰지 않으므로 스텁이 필요 없다. Git clone / Gradle 만 대체한다.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import db as db_module
from src import main
from src.config import settings
from src.db import Base
from src.executor import ExecutionResult
from src.main import mcp
from src.springboot import PreparedTest

#: client 픽스처가 main.run_ingest 를 스텁으로 덮으므로 진짜 함수를 미리 잡아둔다.
REAL_RUN_INGEST = main.run_ingest

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
        "hello", "register_project", "delete_project",
        "test_generate", "test_run", "execute_tests",
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


async def test_generate_returns_agent_code_with_context(client, monkeypatch):
    """생성은 Agent 가 하고, MCP 는 정리한 컨텍스트를 넘긴 뒤 결과를 돌려준다."""
    project_id = await _register(client)
    sent = {}

    def _fake_post(path, payload, timeout):
        sent.update(payload)
        sent["path"] = path
        sent["timeout"] = timeout
        return {"test_code": "class GeneratedTest {}",
                "impact": "OrderService.calculateTotal 조건 분기 추가",
                "risk": "MEDIUM"}

    monkeypatch.setattr(main, "_post_agent", _fake_post)
    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")

    body = await _call(client, "test_generate", project_id=project_id, diff=DIFF,
                       sources=[{"path": "a/OrderService.java", "content": ORDER_SERVICE}])

    # Agent 로 넘어간 것: 코드 생성에 필요한 사실
    assert sent["event"] == "test_generate_requested"
    assert sent["path"] == "/api/v1/tests/generate"
    ctx = sent["context"]
    path = "src/main/java/com/example/demo/service/OrderService.java"
    assert path in ctx["changed_ranges"]
    assert ctx["sources"][0]["content"] == ORDER_SERVICE
    assert sent["timeout"] == settings.agent_timeout_seconds
    # 돌려받은 것: Agent 가 생성한 코드 + 근거
    assert body["test_code"] == "class GeneratedTest {}"
    assert body["context"]["graph_ready"] is False
    # LLM 이 만든 나머지(영향도 해석 등)는 버리지 않고 analysis 로 올라온다
    assert body["analysis"]["risk"] == "MEDIUM"
    assert "calculateTotal" in body["analysis"]["impact"]
    assert "test_code" not in body["analysis"]


async def test_generate_requires_agent_url(client, monkeypatch):
    project_id = await _register(client)
    monkeypatch.setattr(settings, "agent_base_url", "")
    with pytest.raises(ToolError, match="Agent 주소"):
        await _call(client, "test_generate", project_id=project_id)


async def test_generate_rejects_agent_response_without_code(client, monkeypatch):
    project_id = await _register(client)
    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")
    monkeypatch.setattr(main, "_post_agent",
                        lambda path, payload, timeout: {"oops": 1})
    with pytest.raises(ToolError, match="test_code"):
        await _call(client, "test_generate", project_id=project_id)


async def test_generate_surfaces_why_the_agent_refused(client, monkeypatch):
    """405/415 처럼 서버가 거절한 경우, 이유가 응답에 있으니 메시지로 끌어올린다."""
    import io
    import urllib.error

    project_id = await _register(client)
    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")

    def _refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 405, "Method Not Allowed",
            {"Allow": "GET, PUT", "Content-Type": "application/json"},
            io.BytesIO(b'{"detail":"use /invoke"}'),
        )

    monkeypatch.setattr(main.urllib.request, "urlopen", _refuse)

    with pytest.raises(ToolError) as caught:
        await _call(client, "test_generate", project_id=project_id)

    message = str(caught.value)
    assert "405" in message
    assert "GET, PUT" in message          # 어떤 메서드를 써야 하는지
    assert "use /invoke" in message       # 서버가 알려준 힌트


# --- 등록 결과 로그 ------------------------------------------------------------
async def test_run_ingest_logs_and_records_failure(client, monkeypatch, caplog):
    """스레드에서 예외가 새어나가도 로그에 남고 PENDING 으로 방치되지 않아야 한다."""
    from src.db import IngestStatus, Project

    with main.session_scope() as db:
        project = Project(name="boom", git_url="https://x/y", owner="kim",
                          ingest_status=IngestStatus.PENDING.value)
        db.add(project); db.commit(); db.refresh(project)
        project_id = project.id

    # _collect_overview 진입 자체가 터지는 상황 (예: 세션/드라이버 실패)
    monkeypatch.setattr(main, "_collect_overview",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("드라이버 없음")))
    notified = {}
    monkeypatch.setattr(main, "notify_agent", lambda path, p: notified.update(p, path=path))

    with caplog.at_level("INFO", logger="src.main"):
        REAL_RUN_INGEST(project_id)               # 예외가 새어나오면 실패

    # 배포 환경에서 kubectl logs 로 볼 수 있어야 하는 것들
    log = caplog.text
    assert "개요 수집 시작" in log
    assert "예기치 않게 중단" in log
    assert "드라이버 없음" in log                  # 스택트레이스까지
    assert "개요 수집 결과: FAILED" in log

    with main.session_scope() as db:
        project = db.get(Project, project_id)
        assert project.ingest_status == "FAILED"
        assert "드라이버 없음" in project.ingest_error
    assert notified["status"] == "FAILED"
    assert notified["path"] == "/api/v1/projects"


async def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = await _register(client)

    assert (await _call(client, "delete_project", project_id=project_id))["deleted"]
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "delete_project", project_id=project_id)


# --- 생성 + 실행 전 과정 ---------------------------------------------------
async def test_run_generates_then_executes(client, monkeypatch):
    project_id = await _register(client)
    ran = {}

    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")
    posted = {}

    def _fake_post(path, payload, timeout):
        posted["path"] = path
        posted["event"] = payload["event"]
        return {"test_code": "class T {}", "impact": "영향 없음"}

    monkeypatch.setattr(main, "_post_agent", _fake_post)
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _fake_run(repo_path, prepared: PreparedTest, overlay_sources=None):
        ran["source"] = prepared.source
        return ExecutionResult(exit_code=0, output="BUILD SUCCESSFUL",
                               passed=2, failed=0, skipped=0, total=2,
                               jacoco_enabled=True, springboot_applied=True,
                               test_file_path=prepared.file_path,
                               applied=prepared.applied, command=["gradle"])

    monkeypatch.setattr(main, "run_tests", _fake_run)

    body = await _call(client, "test_run", project_id=project_id, diff=DIFF)

    # 생성 -> 주입 -> 실행이 한 호출로 이어진다
    assert body["test_code"] == "class T {}"
    assert "@SpringBootTest" in ran["source"]
    assert body["execution"]["passed"] == 2
    # 생성 근거도 함께 돌아온다
    assert body["context"]["project_id"] == project_id
    assert body["analysis"]["impact"] == "영향 없음"
    # test_run 은 generate 와 다른 경로/event 로 나간다
    assert posted["path"] == "/api/v1/tests/run"
    assert posted["event"] == "test_run_requested"


async def test_run_unknown_project_is_rejected(client):
    with pytest.raises(ToolError, match="찾을 수 없습니다"):
        await _call(client, "test_run", project_id="nope")


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
    from src.executor import ExecutionError

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


# --- Agent 통보 ---------------------------------------------------------------
class _FakeResponse:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_notify_agent_posts_json_to_configured_url(monkeypatch):
    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")
    sent = {}

    def _fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["ctype"] = request.headers.get("Content-type")
        sent["body"] = json.loads(request.data.decode("utf-8"))
        sent["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(main.urllib.request, "urlopen", _fake_urlopen)
    main.notify_agent(main.AGENT_PATH_INGEST,
                      {"event": "ingest_completed", "status": "READY", "project_id": "p1"})

    # base_url 뒤에 command 경로가 붙는다
    assert sent["url"] == "http://agent.test/agent/1/api/v1/projects"
    assert sent["method"] == "POST"
    assert sent["ctype"] == "application/json"
    assert sent["body"]["project_id"] == "p1"
    assert sent["timeout"] == 10          # 통보가 스레드를 물고 있지 않도록


def test_notify_agent_swallows_failures(monkeypatch):
    """Agent 가 죽어 있어도 수집은 성공으로 남아야 한다."""
    monkeypatch.setattr(settings, "agent_base_url", "http://agent.test/agent/1")

    def _boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(main.urllib.request, "urlopen", _boom)
    main.notify_agent(main.AGENT_PATH_INGEST, {"status": "READY"})  # 예외가 새면 실패


def test_notify_agent_skipped_when_url_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "agent_base_url", "")

    def _never(request, timeout=None):
        raise AssertionError("URL 이 비었는데 요청을 보냈다")

    monkeypatch.setattr(main.urllib.request, "urlopen", _never)
    main.notify_agent(main.AGENT_PATH_INGEST, {"status": "READY"})
