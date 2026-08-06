"""MCP 엔드포인트 계약 검증 (Agent 가 호출하는 형태 그대로).

MCP 는 LLM 을 쓰지 않으므로 스텁이 필요 없다. Git clone / Gradle 만 대체한다.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codetest_mcp import main
from codetest_mcp.config import settings
from codetest_mcp.db import Base, get_db
from codetest_mcp.executor import ExecutionResult
from codetest_mcp.main import app
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
def client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'mcp.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    # clone 은 네트워크가 필요하므로 등록 테스트에서는 수집을 건너뛴다.
    monkeypatch.setattr(main, "run_ingest", lambda project_id: None)
    monkeypatch.setattr(settings, "api_keys", [])

    with TestClient(app) as c:
        c.session_factory = Session
        yield c
    app.dependency_overrides.clear()


def _register(client, name="demo") -> str:
    res = client.post("/api/v1/projects", json={
        "name": name, "git_url": "https://github.com/acme/demo",
        "owner": "kim", "github_token": "ghp_secret", "default_branch": "main",
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


# --- health ------------------------------------------------------------------
def test_health_declares_code_based_role(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["role"] == "code-based"


# --- 프로젝트 개요 (정의서 [상세] 1) -------------------------------------------
def test_create_project_hides_the_token(client):
    body = client.post("/api/v1/projects", json={
        "name": "demo", "git_url": "https://github.com/acme/demo/",
        "owner": "kim", "github_token": "ghp_secret",
    }).json()

    assert body["ingest_status"] == "PENDING"
    assert body["has_github_token"] is True
    assert "ghp_secret" not in str(body)
    assert body["git_url"] == "https://github.com/acme/demo"   # 끝 슬래시 정규화


def test_duplicate_name_is_409(client):
    _register(client)
    assert client.post("/api/v1/projects", json={
        "name": "demo", "git_url": "https://github.com/acme/other", "owner": "kim",
    }).status_code == 409


def test_bad_git_url_is_422(client):
    assert client.post("/api/v1/projects", json={
        "name": "x", "git_url": "ftp://nope", "owner": "kim",
    }).status_code == 422


def test_overview_reports_stored_summary(client):
    project_id = _register(client)
    body = client.get(f"/api/v1/projects/{project_id}/overview").json()

    assert body["project_id"] == project_id
    assert body["ingest_status"] == "PENDING"
    assert body["node_counts"] == {}


def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = _register(client)

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 404


# --- 변경 단위 식별 (정의서 (2)) ----------------------------------------------
def test_analyze_changes_parses_diff_ranges(client):
    project_id = _register(client)
    body = client.post("/api/v1/analysis/changes", json={
        "project_id": project_id, "diff": DIFF, "sources": [],
    }).json()

    path = "src/main/java/com/example/demo/service/OrderService.java"
    assert path in body["changed_ranges"]
    assert body["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert body["risk_reasons"]


def test_analyze_warns_when_overview_not_ready(client):
    project_id = _register(client)          # ingest 는 스텁이라 PENDING 상태
    body = client.post("/api/v1/analysis/changes", json={
        "project_id": project_id, "diff": DIFF, "sources": [],
    }).json()

    assert body["graph_ready"] is False
    assert any("개요 수집" in w for w in body["warnings"])


def test_analyze_unknown_project_is_404(client):
    assert client.post("/api/v1/analysis/changes", json={
        "project_id": "nope", "diff": "", "sources": [],
    }).status_code == 404


# --- 테스트 실행 (정의서 (1), [상세] 4) ----------------------------------------
def test_execute_injects_springboot_and_reports_facts(client, monkeypatch):
    project_id = _register(client)
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

    body = client.post("/api/v1/tests/execute", json={
        "project_id": project_id,
        "test_code": "package com.example.demo;\n\nclass FooTest {\n  @Test\n  void t() {}\n}\n",
        "sources": [{"path": "src/main/java/com/example/demo/service/OrderService.java",
                     "content": ORDER_SERVICE}],
    }).json()

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


def test_execute_rejects_unparseable_test_code(client, monkeypatch):
    project_id = _register(client)
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )
    res = client.post("/api/v1/tests/execute", json={
        "project_id": project_id, "test_code": "// class 선언이 없다", "sources": [],
    })
    assert res.status_code == 422


def test_execute_surfaces_missing_gradle_as_503(client, monkeypatch):
    from codetest_mcp.executor import ExecutionError

    project_id = _register(client)
    monkeypatch.setattr(
        main.RepoService, "ensure_clone", lambda self, branch=None: pathlib.Path("/tmp/x")
    )

    def _boom(*args, **kwargs):
        raise ExecutionError("Gradle 을 찾을 수 없습니다.")

    monkeypatch.setattr(main, "run_tests", _boom)
    res = client.post("/api/v1/tests/execute", json={
        "project_id": project_id, "test_code": "class T {}", "sources": [],
    })
    assert res.status_code == 503
    assert "Gradle" in res.json()["detail"]


# --- 인증 ---------------------------------------------------------------------
def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    payload = {"name": "x", "git_url": "https://github.com/a/b", "owner": "kim"}

    assert client.post("/api/v1/projects", json=payload).status_code == 401
    assert client.post("/api/v1/projects", json=payload,
                       headers={"X-API-Key": "s3cret"}).status_code == 201
    assert client.get("/api/v1/health").status_code == 200
