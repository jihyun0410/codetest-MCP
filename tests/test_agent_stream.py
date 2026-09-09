"""Agent 응답을 keep-alive 스트림으로 받는 경로 검증.

앞단 nginx 의 proxy_read_timeout 은 총 소요 시간이 아니라 **무응답 시간**이다.
LLM 이 몇 분 생각하는 동안 한 바이트도 안 오면 504 가 만들어진다. Agent 가 그 사이
ping 줄을 흘려보내고, 이 클라이언트가 그것을 버리고 마지막 줄만 쓴다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from codetest_mcp import agent_client as agent_module
from codetest_mcp.agent_client import NDJSON_MEDIA_TYPE, AgentClient, AgentError

PAYLOAD = {"project_id": "p1", "analysis": {"base_package": "com.example.demo"}, "sources": []}


def _client(monkeypatch, handler) -> AgentClient:
    """httpx.Client 가 MockTransport 를 쓰도록 갈아 끼운다."""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent_module.httpx, "Client", _factory)
    return AgentClient(base_url="http://agent", api_key="")


def _ndjson(*messages: dict) -> httpx.Response:
    body = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    return httpx.Response(200, content=body.encode(), headers={"content-type": NDJSON_MEDIA_TYPE})


def test_ping_lines_are_discarded_and_the_result_is_returned(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        return _ndjson(
            {"type": "ping"},
            {"type": "ping"},
            {"type": "result", "data": {"test_code": "class T {}", "intent": "조건 변경"}},
        )

    result = _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], [])

    assert result == {"test_code": "class T {}", "intent": "조건 변경"}
    assert NDJSON_MEDIA_TYPE in seen["accept"], "스트림을 받겠다고 알려야 Agent 가 ping 을 보낸다"


def test_error_line_becomes_an_agent_error_with_its_status(monkeypatch):
    """스트림이 시작된 뒤에는 상태 코드를 못 바꾸므로 오류가 본문으로 온다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson(
            {"type": "ping"},
            {"type": "error", "status": 503, "detail": "OPENAI_API_KEY 가 없습니다"},
        )

    with pytest.raises(AgentError) as exc:
        _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], [])

    assert exc.value.status_code == 503
    assert "OPENAI_API_KEY" in str(exc.value)


def test_stream_without_a_result_line_is_an_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson({"type": "ping"}, {"type": "ping"})

    with pytest.raises(AgentError, match="결과를 보내지 않고"):
        _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], [])


def test_plain_json_from_an_older_agent_still_works(monkeypatch):
    """Accept 를 무시하고 예전처럼 JSON 한 덩어리로 답해도 받아야 한다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"test_code": "class T {}"})

    assert _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], []) == {
        "test_code": "class T {}"
    }


def test_http_error_keeps_its_status_and_detail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "analysis 가 비어 있습니다"})

    with pytest.raises(AgentError) as exc:
        _client(monkeypatch, handler).generate("p1", {}, [])

    assert exc.value.status_code == 422
    assert "analysis" in str(exc.value)


def test_report_uses_the_same_stream_path(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tests/execute")
        return _ndjson({"type": "ping"}, {"type": "result", "data": {"verdict": "적절"}})

    assert _client(monkeypatch, handler).report("p1", {"exit_code": 0}, "class T {}") == {
        "verdict": "적절"
    }
