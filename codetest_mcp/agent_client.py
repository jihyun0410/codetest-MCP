"""
Agent(LLM 판단 전담) 호출 클라이언트.

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을 진행하는
   부분은 MCP 로 구분하여 **Fast API를 통해 송/수신**하는 방식으로 구현"

흐름은 CLI → MCP → Agent 다. MCP 가 코드 기반 처리(AST 분석·기능 중요도 판정·
@SpringBootTest 주입·JaCoCo 실행)를 끝낸 뒤, LLM 이 필요한 부분만 이 클라이언트로
Agent(FastAPI)에 넘긴다.

  POST {agent_url}/api/v1/tests/generate   분석 사실 → 의도·사고의 사슬·Test Code
  POST {agent_url}/api/v1/tests/execute    실행 결과 → 적절성 판단
"""

from __future__ import annotations

import httpx

from codetest_mcp.config import get_logger, settings

logger = get_logger(__name__)


class AgentError(RuntimeError):
    """Agent 호출 실패 (연결 불가 / 오류 응답 / 시간 초과)."""


class AgentClient:
    """Agent FastAPI 서버로 LLM 판단만 위임한다."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.agent_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.agent_api_key
        self.timeout = timeout or float(settings.agent_timeout_seconds)

    # --- 전송 -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        effective = timeout or self.timeout
        try:
            with httpx.Client(timeout=effective) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.ConnectError as exc:
            raise AgentError(
                f"Agent 에 연결할 수 없습니다: {url}\n"
                f"  · Agent(FastAPI)가 실행 중인지 확인하세요.\n"
                f"  · CODETEST_MCP_AGENT_URL 환경변수로 주소를 바꿀 수 있습니다.\n"
                f"  ({exc})"
            ) from None
        except httpx.TimeoutException:
            raise AgentError(f"Agent 응답이 시간 초과되었습니다 ({effective:.0f}s): {url}") from None

        if response.status_code >= 400:
            detail = response.text[:300] or response.reason_phrase
            raise AgentError(f"Agent 오류 HTTP {response.status_code}: {detail}")

        try:
            body = response.json()
        except ValueError:
            raise AgentError(f"Agent 응답을 JSON 으로 읽을 수 없습니다: {response.text[:200]}") from None
        return body if isinstance(body, dict) else {"result": body}

    # --- 도구 -----------------------------------------------------------
    def generate(
        self,
        project_id: str,
        analysis: dict,
        sources: list[tuple[str, str]],
        project_name: str = "",
        timeout: float | None = None,
    ) -> dict:
        """변경 의도 파악 + 사고의 사슬 + @SpringBootTest 코드 생성을 맡긴다."""
        return self._post(
            "/api/v1/tests/generate",
            {
                "project_id": project_id,
                "project_name": project_name,
                "analysis": analysis,
                "sources": [{"path": path, "content": content} for path, content in sources],
            },
            timeout=timeout,
        )

    def report(
        self,
        project_id: str,
        execution: dict,
        test_code: str,
        intent: str = "",
        intent_rationale: str = "",
        timeout: float | None = None,
    ) -> dict:
        """MCP 가 돌린 실행 결과의 적절성 판단을 맡긴다."""
        return self._post(
            "/api/v1/tests/execute",
            {
                "project_id": project_id,
                "execution": execution,
                "test_code": test_code,
                "intent": intent,
                "intent_rationale": intent_rationale,
            },
            timeout=timeout,
        )


#: 프로세스 전역 클라이언트 (설정은 기동 시 한 번 읽는다)
agent_client = AgentClient()
