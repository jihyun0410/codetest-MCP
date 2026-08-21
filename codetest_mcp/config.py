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

    database_url: str = Field(
        default="sqlite:///./data/codetest_mcp.db", alias="CODETEST_MCP_DATABASE_URL"
    )
    #: 대상 프로젝트를 clone 해 두는 작업 디렉터리
    workspace_dir: Path = Field(
        default=Path("./workspace"), alias="CODETEST_MCP_WORKSPACE_DIR"
    )

    # --- 테스트 실행 (정의서: "JaCoCo와 @SpringBootTest 를 사용하여 Test Code 실행") ---
    #: Gradle wrapper 가 없는 프로젝트에서 사용할 gradle 실행 파일
    gradle_command: str = Field(default="gradle", alias="CODETEST_MCP_GRADLE_COMMAND")
    #: gradle test 최대 실행 시간(초). Spring 컨텍스트 기동 + 의존성 해석을 감안한다.
    test_timeout_seconds: int = Field(default=900, alias="CODETEST_MCP_TEST_TIMEOUT")

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


def verify_api_key(provided: str | None) -> bool:
    """settings.api_keys 가 비어 있으면 인증 비활성화. 비교는 타이밍 공격 방지."""
    allowed = settings.api_keys
    if not allowed:
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, key) for key in allowed)
