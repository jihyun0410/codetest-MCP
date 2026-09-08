"""커밋 소스 스냅샷 + 미커밋 변경분 덮어쓰기 검증.

핵심: Agent 는 변경 파일만이 아니라 **커밋된 코드까지** 봐야 한다.
그래야 변경 지점이 호출하는 구현을 읽고 테스트를 만들 수 있다.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codetest_mcp import db as db_module
from codetest_mcp import main, orchestrator
from codetest_mcp.config import settings
from codetest_mcp.db import Base, ProjectFile, session_scope
from codetest_mcp.main import mcp

ORDER_SERVICE = "src/main/java/com/example/demo/service/OrderService.java"
ORDER_CONTROLLER = "src/main/java/com/example/demo/controller/OrderController.java"

COMMITTED = [
    {"path": ORDER_SERVICE, "content": "class OrderService { double calc() { return 1; } }"},
    {"path": ORDER_CONTROLLER, "content": "class OrderController { }"},
    {"path": "README.md", "content": "# demo"},
]


@pytest.fixture
def agent_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def _generate(project_id, analysis, sources, project_name="", **kwargs):
        calls.append({"project_id": project_id, "analysis": analysis,
                      "sources": sources, "project_name": project_name})
        return {"test_code": "class T {}", "intent": "조건 변경"}

    monkeypatch.setattr(main.agent_client, "generate", _generate)
    monkeypatch.setattr(orchestrator.agent_client, "generate", _generate)
    return calls


@pytest.fixture
async def client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'mcp.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "run_ingest", lambda project_id: None)
    monkeypatch.setattr(settings, "api_keys", [])
    async with Client(mcp) as c:
        yield c


async def _register(client, sources=COMMITTED) -> str:
    result = await client.call_tool("register_project", {
        "name": "demo", "git_url": "https://github.com/acme/demo",
        "owner": "kim", "sources": sources,
    })
    return result.structured_content["id"]


# --- 1) 등록: 커밋 소스를 저장한다 ---------------------------------------------
async def test_register_stores_committed_sources(client):
    project_id = await _register(client)

    with session_scope() as db:
        stored = orchestrator.committed_sources(db, project_id)

    assert set(stored) == {ORDER_SERVICE, ORDER_CONTROLLER, "README.md"}
    assert "double calc()" in stored[ORDER_SERVICE]


async def test_register_without_sources_stores_nothing(client):
    project_id = await _register(client, sources=[])
    with session_scope() as db:
        assert orchestrator.committed_sources(db, project_id) == {}


async def test_re_register_refreshes_the_snapshot(client):
    """재등록은 그 사이 쌓인 커밋을 반영할 기회다."""
    project_id = await _register(client)
    await _register(client, sources=[
        {"path": ORDER_SERVICE, "content": "class OrderService { double calc() { return 2; } }"},
        {"path": "src/main/java/com/example/demo/model/Order.java", "content": "class Order {}"},
    ])

    with session_scope() as db:
        stored = orchestrator.committed_sources(db, project_id)

    assert "return 2" in stored[ORDER_SERVICE]                 # 교체됨
    assert "src/main/java/com/example/demo/model/Order.java" in stored   # 추가됨
    assert ORDER_CONTROLLER in stored                          # 기존 것도 남아 있음


async def test_delete_project_removes_its_files(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = await _register(client)

    await client.call_tool("delete_project", {"project_id": project_id})

    with session_scope() as db:
        assert db.query(ProjectFile).filter_by(project_id=project_id).count() == 0


# --- 2) 실행: 커밋 소스 위에 미커밋 변경분을 덮어 Agent 로 ----------------------
async def test_generate_sends_committed_code_not_just_the_diff(client, agent_calls):
    """변경 파일만 보내면 LLM 이 호출 대상 구현을 못 본다."""
    project_id = await _register(client)

    await client.call_tool("test_generate", {
        "project_id": project_id,
        "diff": "",
        "sources": [{"path": ORDER_CONTROLLER, "content": "class OrderController { /* 수정됨 */ }"}],
    })

    sent = {item["path"]: item["content"] for item in agent_calls[-1]["sources"]}
    # 미커밋 변경분은 그대로
    assert "수정됨" in sent[ORDER_CONTROLLER]


async def test_uncommitted_change_overrides_the_committed_copy(client, agent_calls):
    project_id = await _register(client)

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{"path": ORDER_SERVICE, "content": "class OrderService { double calc() { return 99; } }"}],
    })

    sent = {item["path"]: item["content"] for item in agent_calls[-1]["sources"]}
    assert "return 99" in sent[ORDER_SERVICE]      # 커밋본(return 1) 이 아니라 변경본
    assert "return 1" not in sent[ORDER_SERVICE]


async def test_context_is_capped(client, agent_calls):
    """프로젝트 전체를 실어 보내면 프롬프트가 감당이 안 된다."""
    many = [{"path": f"src/main/java/F{i}.java", "content": f"class F{i} {{}}"} for i in range(200)]
    project_id = await _register(client, sources=many)

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{"path": "src/main/java/F0.java", "content": "class F0 { /* 수정 */ }"}],
    })

    assert len(agent_calls[-1]["sources"]) <= orchestrator.MAX_CONTEXT_FILES


async def test_long_file_is_clipped(client, agent_calls):
    huge = "x" * (orchestrator.MAX_CONTEXT_CHARS + 5000)
    project_id = await _register(client, sources=[{"path": ORDER_SERVICE, "content": huge}])

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{"path": ORDER_SERVICE, "content": huge}],
    })

    sent = agent_calls[-1]["sources"][0]["content"]
    assert len(sent) < len(huge)
    assert sent.endswith("(이하 생략)")


# --- 3) 그래프가 비어도 맥락은 실어야 한다 -------------------------------------
async def test_referenced_committed_file_is_sent_without_a_graph(client, agent_calls):
    """개요 수집 미완료·clone 실패로 그래프가 비어도 호출 대상 구현은 보내야 한다."""
    project_id = await _register(client)      # run_ingest 는 스텁 → 그래프 비어 있음

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{
            "path": ORDER_CONTROLLER,
            # Java 는 호출하려면 타입을 선언하거나 import 해야 한다 —
            # 그래서 파일 안에 반드시 타입명이 남는다.
            "content": (
                "import com.example.demo.service.OrderService;\n"
                "class OrderController {\n"
                "    private final OrderService orderService;\n"
                "    double t() { return orderService.calc(); }\n"
                "}"
            ),
        }],
    })

    sent = {item["path"] for item in agent_calls[-1]["sources"]}
    assert ORDER_CONTROLLER in sent
    # 변경 코드가 이름으로 참조하므로 커밋된 OrderService 도 함께 간다
    assert ORDER_SERVICE in sent


async def test_unrelated_committed_file_is_not_sent(client, agent_calls):
    """맥락이라고 프로젝트 전체를 실으면 프롬프트가 감당이 안 된다."""
    project_id = await _register(client, sources=[
        {"path": ORDER_SERVICE, "content": "class OrderService {}"},
        {"path": "src/main/java/com/other/pkg/Unrelated.java", "content": "class Unrelated {}"},
    ])

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{"path": ORDER_CONTROLLER,
                     "content": ("import com.example.demo.service.OrderService;\n"
                                 "class OrderController { OrderService orderService; }")}],
    })

    sent = {item["path"] for item in agent_calls[-1]["sources"]}
    assert "src/main/java/com/other/pkg/Unrelated.java" not in sent


async def test_same_package_committed_files_fill_the_context(client, agent_calls):
    project_id = await _register(client, sources=[
        {"path": "src/main/java/com/example/demo/service/Helper.java", "content": "class Helper {}"},
    ])

    await client.call_tool("test_generate", {
        "project_id": project_id, "diff": "",
        "sources": [{"path": "src/main/java/com/example/demo/service/Main.java",
                     "content": "class Main {}"}],
    })

    sent = {item["path"] for item in agent_calls[-1]["sources"]}
    assert "src/main/java/com/example/demo/service/Helper.java" in sent
