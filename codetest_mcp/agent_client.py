"""Agent(codetest) REST 클라이언트.

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 **Fast API를 통해 송/수신**하는 방식으로 구현"

MCP 가 진입점이다. CLI 명령을 받아 코드 기반 사실을 확정한 뒤, **LLM 판단이
필요한 부분만** 이 클래스를 통해 Agent 에 넘긴다.

호출하는 주소는 Agent(codetest)의 것을 그대로 쓴다.
  POST /api/v1/tests/generate   변경 의도 파악 + 사고의 사슬 + Test Code 생성
  POST /api/v1/tests/execute    실행 결과 적절성 판단
  GET  /api/v1/health           연결 확인

**기능 중요도는 보내지 않는다** — 코드 그래프로 확정하는 값이라 MCP 가 정한다
(`importance.py`).
"""

from __future__ import annotations

from typing import Any

import httpx

from codetest_mcp.config import get_logger, settings

logger = get_logger(__name__)


class AgentError(RuntimeError):
    """Agent 가 4xx/5xx 를 반환했거나 연결에 실패한 경우."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentClient:
    """Agent FastAPI 서비스 호출기."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        raw = (base_url or settings.agent_base_url).rstrip("/")
        self.base_url = f"{raw}/api/v1"
        self.api_key = api_key if api_key is not None else settings.agent_api_key
        self.timeout = timeout if timeout is not None else settings.agent_timeout_seconds

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, timeout: float | None = None, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise AgentError(
                f"Agent 에 연결할 수 없습니다: {self.base_url}\n"
                f"  · Agent 가 실행 중인지 확인하세요 (uvicorn app.main:app).\n"
                f"  · CODETEST_MCP_AGENT_BASE_URL 환경변수로 주소를 바꿀 수 있습니다.\n"
                f"  ({exc})"
            ) from None
        except httpx.TimeoutException:
            raise AgentError(
                f"Agent 요청이 시간 초과되었습니다 ({timeout or self.timeout:.0f}s). "
                "LLM 생성이 오래 걸리면 CODETEST_MCP_AGENT_GENERATE_TIMEOUT 를 늘리세요."
            ) from None

        if response.status_code >= 400:
            raise AgentError(_extract_detail(response), response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- 헬스 ----------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", timeout=10.0)

    # --- 생성 (정의서 (2)(3), [상세] 2·3) --------------------------------
    def generate(
        self,
        project_id: str,
        analysis: dict,
        sources: list[dict],
        project_name: str = "",
    ) -> dict:
        """MCP 가 확정한 변경 사실을 넘겨 Test Code 와 의도 판단을 받는다."""
        return self._request(
            "POST",
            "/tests/generate",
            timeout=settings.agent_generate_timeout_seconds,
            json={
                "project_id": project_id,
                "project_name": project_name,
                "analysis": analysis,
                "sources": sources,
            },
        )

    # --- 판정 (정의서 [UI] 3) --------------------------------------------
    def report(
        self,
        project_id: str,
        execution: dict,
        test_code: str,
        intent: str = "",
        intent_rationale: str = "",
    ) -> dict:
        """MCP 가 실행한 결과를 넘겨 적절성 판단을 받는다."""
        return self._request(
            "POST",
            "/tests/execute",
            timeout=settings.agent_generate_timeout_seconds,
            json={
                "project_id": project_id,
                "execution": execution,
                "test_code": test_code,
                "intent": intent,
                "intent_rationale": intent_rationale,
            },
        )


def _extract_detail(response: httpx.Response) -> str:
    """FastAPI 오류 응답에서 사람이 읽을 메시지를 뽑는다."""
    try:
        payload = response.json()
    except ValueError:
        return f"Agent HTTP {response.status_code}: {response.text[:300]}"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):  # pydantic 검증 오류
        parts = [
            f"{'.'.join(str(x) for x in item.get('loc', []))}: {item.get('msg')}"
            for item in detail
        ]
        return f"Agent HTTP {response.status_code}: " + " / ".join(parts)
    return f"Agent HTTP {response.status_code}: {detail or response.text[:300]}"


#: 애플리케이션 전역 싱글턴
agent_client = AgentClient()
