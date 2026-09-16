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


# --- 응답 도중 연결이 끊긴 경우 ------------------------------------------------
#
# `RemoteProtocolError: peer closed connection without sending complete message
# body (incomplete chunked read)` — 상대가 본문을 끝맺지 않고 닫았다는 뜻이다.
# httpx 의 ConnectError 도 TimeoutException 도 아니라 따로 잡지 않으면 그대로
# 새어 나가 CLI 에 파이썬 트레이스백이 찍힌다.
CUT = httpx.RemoteProtocolError(
    "peer closed connection without sending complete message body (incomplete chunked read)"
)


def _truncated(*messages: dict) -> httpx.Response:
    """줄을 보낸 뒤 종료 청크 없이 끊기는 NDJSON 스트림."""
    def _stream():
        for message in messages:
            yield (json.dumps(message, ensure_ascii=False) + "\n").encode()
        raise CUT

    return httpx.Response(200, headers={"content-type": NDJSON_MEDIA_TYPE}, content=_stream())


def test_result_survives_a_stream_that_is_cut_after_the_result_line(monkeypatch):
    """Agent 는 결과를 보낸 뒤 스트림을 닫는다 — 그 닫힘만 잘려도 결과는 살린다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _truncated(
            {"type": "ping"},
            {"type": "result", "data": {"test_code": "class T {}"}},
        )

    assert _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], []) == {
        "test_code": "class T {}"
    }


def test_cut_before_any_result_becomes_a_readable_agent_error(monkeypatch):
    """결과를 못 받았으면 트레이스백 대신 무엇을 확인할지 알려 준다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _truncated({"type": "ping"})

    with pytest.raises(AgentError) as exc:
        _client(monkeypatch, handler).generate("p1", PAYLOAD["analysis"], [])

    message = str(exc.value)
    assert "연결을 끊었습니다" in message
    assert "RemoteProtocolError" in message      # 원인을 지우지는 않는다
    assert "proxy_read_timeout" in message       # 어디를 볼지 알려 준다


def test_health_also_reports_a_cut_connection(monkeypatch):
    """스트림이 아닌 호출도 같은 오류를 만난다 — 여기서도 새어 나가면 안 된다."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise CUT

    with pytest.raises(AgentError, match="연결을 끊었습니다"):
        _client(monkeypatch, handler).health()
