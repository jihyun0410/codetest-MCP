"""MCP 서비스 설정 · 로깅 · API Key 인증.

이 서비스는 **LLM 을 호출하지 않는다**. 정의서의
"코드 기반으로 단순 처리 및 판단을 진행하는 부분은 MCP" 에 해당하는 작업만 수행하므로
Anthropic 관련 설정이 존재하지 않는다.

비밀값(API Key, GitHub Token)은 코드에 두지 않고 .env / OS 환경변수로만 주입한다.
"""

from __future__ import annotations

import hmac
import logging
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = Field(default="Code Test MCP", alias="CODETEST_MCP_APP_NAME")
    host: str = Field(default="0.0.0.0", alias="CODETEST_MCP_HOST")
    port: int = Field(default=80, alias="CODETEST_MCP_PORT")
    #: MCP 전송 방식. streamable-http (Agent 가 원격 호출) 또는 stdio (자식 프로세스로 기동)
    transport: str = Field(default="streamable-http", alias="CODETEST_MCP_TRANSPORT")

    #: Agent 인증용 키 목록. http 전송일 때 X-API-Key 헤더와 대조한다.
    #: 비어 있으면 인증 비활성화(로컬 개발 편의).
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CODETEST_MCP_API_KEYS"
    )

    # --- Agent (LLM 판단 위임 대상) ---
    #: 정의서: "Fast API를 통해 송/수신하는 방식으로 구현"
    agent_base_url: str = Field(
        default="http://localhost:8000", alias="CODETEST_MCP_AGENT_BASE_URL"
    )
    agent_api_key: str = Field(default="", alias="CODETEST_MCP_AGENT_API_KEY")
    agent_timeout_seconds: float = Field(
        default=180.0, alias="CODETEST_MCP_AGENT_TIMEOUT"
    )
    #: LLM 호출은 총 소요 시간이 아니라 **무응답 시간**으로 끊는다.
    #: Agent 가 생성 중에 keep-alive 를 흘려보내므로, 살아 있는 한 이 타이머는
    #: 계속 초기화된다. 즉 오래 걸리는 생성은 기다리고 죽은 Agent 는 빨리 포기한다.
    #: (예전 CODETEST_MCP_AGENT_GENERATE_TIMEOUT 을 대신한다 — 총 시간 상한이던
    #:  그 값은 앞단 nginx 가 먼저 504 를 내는 바람에 실효가 없었다.)
    agent_stream_idle_seconds: float = Field(
        default=180.0, alias="CODETEST_MCP_AGENT_STREAM_IDLE"
    )
    #: Agent 를 기다리는 동안 CLI 쪽으로 진행 알림을 보내는 간격(초).
    #: MCP 가 올라가는 서버는 60초 동안 오가는 메시지가 없으면 요청을 Fallback 처리한다.
    heartbeat_seconds: float = Field(default=10.0, alias="CODETEST_MCP_HEARTBEAT")

    database_url: str = Field(
        default="sqlite:///./data/codetest_mcp.db", alias="CODETEST_MCP_DATABASE_URL"
    )
    #: 저장소를 clone 해 두는 작업 디렉터리.
    #: 개요 수집은 CLI 가 올려 준 커밋 스냅샷으로 하므로 보통은 비어 있다 —
    #: 스냅샷 없이 등록된 예전 프로젝트를 clone 할 때만 쓴다.
    workspace_dir: Path = Field(
        default=Path("./workspace"), alias="CODETEST_MCP_WORKSPACE_DIR"
    )

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """list 타입 환경변수는 "a,b,c" CSV 를 허용한다."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def ensure_directories(self) -> None:
        """기동 시 필요한 런타임 디렉터리를 만든다."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            Path(self.database_url.split("///")[-1]).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()


# ---------------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> None:
    """루트 로거에 StreamHandler 를 한 번만 붙인다.

    stdio 전송에서는 stdout 이 MCP 프로토콜 채널이므로 로그는 반드시 stderr 로 간다.
    """
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def trace(log: logging.Logger, message: str, *args) -> None:
    """명령 처리 단계를 log 와 print 로 함께 남긴다.

    stdio 전송에서는 stdout 이 MCP 프로토콜 채널이라 print 가 프로토콜을 깨므로
    그때만 stderr 로 보낸다. flush 로 컨테이너 로그에 바로 보이게 한다.
    """
    try:
        text = message % args if args else message
    except (TypeError, ValueError):
        # %s 개수와 인자 개수가 어긋난 오타가 명령 처리를 죽이면 안 된다. 원문을 그대로 남겨 오타를 드러낸다.
        text = f"[로그 포맷 오류] {message!r} args={args!r}"
    log.info(text)
    stream = sys.stderr if settings.transport == "stdio" else sys.stdout
    line = f"{time.strftime('%H:%M:%S')} | {log.name.rsplit('.', 1)[-1]} | {text}"
    try:
        print(line, file=stream, flush=True)
    except UnicodeEncodeError:
        # cp949/ascii 콘솔이 못 찍는 문자가 있어도 명령 처리를 죽이지 않는다.
        enc = stream.encoding or "ascii"
        print(line.encode(enc, "replace").decode(enc), file=stream, flush=True)


def verify_api_key(provided: str | None) -> bool:
    """settings.api_keys 가 비어 있으면 인증 비활성화. 비교는 타이밍 공격 방지."""
    allowed = settings.api_keys
    if not allowed:
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, key) for key in allowed)
