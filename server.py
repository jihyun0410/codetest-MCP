"""
AST & Linter MCP 서버 (stdio, Headless).

agent-server 가 자식 프로세스로 기동해 `ast_check` / `lint_check` 를 호출한다.
결과는 100% 정적 분석 사실만 담으며, LLM 추론은 포함하지 않는다.
"""

from __future__ import annotations

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

# 도구 함수 이름과 모듈 이름이 겹치므로 모듈은 별칭으로 import 한다.
from codetest_mcp.checks import ast_check as ast_module
from codetest_mcp.checks import lint_check as lint_module
from codetest_mcp.models import parse_files

# stdio 를 프로토콜 채널로 쓰므로 로그는 반드시 stderr 로만 보낸다.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | codetest-mcp | %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("codetest-ast-linter")

#: 한 번의 호출에서 처리할 최대 파일 수 (과도한 페이로드 방어)
MAX_FILES = 100


def _dump(findings) -> str:
    """도구 응답 직렬화 — agent-server 가 그대로 파싱한다."""
    return json.dumps(
        {"findings": [finding.to_dict() for finding in findings]}, ensure_ascii=False
    )


@mcp.tool()
def ast_check(files: list[dict]) -> str:
    """
    AST 문법 검증.

    Python 은 표준 compile(), 그 외 언어는 Tree-sitter 파스 트리의
    ERROR/MISSING 노드를 검출한다. 파서가 확정한 사실만 보고한다.

    Args:
        files: [{"path": "src/A.java", "content": "...", "language": "java"}, ...]
               language 를 생략하면 확장자로 추론한다.

    Returns:
        {"findings": [{file_path, line, severity, rule, message, source}, ...]} JSON 문자열
    """
    parsed = parse_files({"files": files})[:MAX_FILES]
    logger.info("ast_check: %d개 파일 검사", len(parsed))
    return _dump(ast_module.run(parsed))


@mcp.tool()
def lint_check(files: list[dict]) -> str:
    """
    Linter 검증 (Headless).

    설치된 실제 린터(ruff/eslint)를 우선 실행하고, 없으면 내장 결정적 규칙으로
    폴백한다. 문법·타입·오탈자·잘못된 변수 사용 등 정통적 오류를 검출한다.

    Args:
        files: [{"path": "src/a.py", "content": "...", "language": "python"}, ...]

    Returns:
        {"findings": [...]} JSON 문자열
    """
    parsed = parse_files({"files": files})[:MAX_FILES]
    logger.info("lint_check: %d개 파일 검사", len(parsed))
    return _dump(lint_module.run(parsed))


@mcp.tool()
def health() -> str:
    """서버 상태와 사용 가능한 Tree-sitter 문법 / 외부 린터 목록을 반환한다."""
    payload = {
        "status": "ok",
        "grammars": ast_module.available_grammars(),
        "linters": lint_module.available_linters(),
    }
    return json.dumps(payload, ensure_ascii=False)


def main() -> None:
    """stdio 트랜스포트로 MCP 서버를 실행한다."""
    logger.info("codetest-mcp 기동 (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
