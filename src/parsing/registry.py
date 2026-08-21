"""
파서 레지스트리.

파일 경로 → 언어 판별 → 담당 파서 선택 → ParseResult 반환.
지원 언어(정의서): Java / JavaScript / TypeScript / Python / MyBatis XML / SQL(DML)
"""

from __future__ import annotations

from pathlib import PurePosixPath

from src.config import get_logger
from src.parsing.base import ParseResult
from src.parsing.java_parser import JavaParser
from src.parsing.js_ts_parser import EXT_LANGUAGE, JsTsParser
from src.parsing.mybatis_parser import MyBatisXmlParser
from src.parsing.python_parser import PythonParser
from src.parsing.sql_parser import SqlParser

logger = get_logger(__name__)

#: 확장자 → 내부 언어 코드
EXTENSION_LANGUAGE: dict[str, str] = {
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".py": "python",
    ".xml": "xml",
    ".sql": "sql",
}

#: 파싱 대상에서 제외할 디렉터리 (빌드 산출물/의존성)
EXCLUDED_DIRS: set[str] = {
    ".git", ".svn", ".hg", "node_modules", "dist", "build", "out", "target",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", ".next", ".nuxt",
    "coverage", ".pytest_cache", ".mypy_cache", "vendor", "bin", "obj",
}

#: 파일 1개 최대 파싱 크기 (1MB) — 생성 코드/미니파이 번들 방어
MAX_FILE_BYTES = 1_000_000

_java = JavaParser()
_python = PythonParser()
_mybatis = MyBatisXmlParser()
_sql = SqlParser()
_jsts_cache: dict[str, JsTsParser] = {}


def detect_language(file_path: str) -> str | None:
    """확장자로 언어를 판별한다. 지원 대상이 아니면 None."""
    suffix = PurePosixPath(file_path).suffix.lower()
    return EXTENSION_LANGUAGE.get(suffix)


def is_supported(file_path: str) -> bool:
    """파싱 대상 파일인지 (확장자 + 제외 디렉터리) 판정한다."""
    normalized = file_path.replace("\\", "/")
    parts = set(PurePosixPath(normalized).parts)
    if parts & EXCLUDED_DIRS:
        return False
    if ".min." in PurePosixPath(normalized).name:
        return False
    return detect_language(normalized) is not None


def parse_source(file_path: str, source: str) -> ParseResult:
    """
    파일 1개를 파싱한다. 실패해도 예외를 밖으로 던지지 않고 warning 으로 남긴다.

    :param file_path: 저장소 루트 기준 상대 경로 (POSIX 구분자)
    :param source: 파일 전체 텍스트
    """
    normalized = file_path.replace("\\", "/")
    language = detect_language(normalized)
    if language is None:
        return ParseResult()

    if len(source.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        result = ParseResult()
        result.warnings.append(f"{normalized}: 파일이 너무 커서 파싱을 건너뜁니다.")
        return result

    try:
        if language == "java":
            return _java.parse(normalized, source)
        if language == "python":
            return _python.parse(normalized, source)
        if language == "xml":
            # pom.xml, logback.xml 등은 MyBatis 파서가 스스로 걸러낸다.
            return _mybatis.parse(normalized, source)
        if language == "sql":
            return _sql.parse(normalized, source)
        if language in {"javascript", "typescript", "tsx"}:
            ts_language = EXT_LANGUAGE.get(PurePosixPath(normalized).suffix.lower(), language)
            parser = _jsts_cache.get(ts_language)
            if parser is None:
                parser = JsTsParser(ts_language)
                _jsts_cache[ts_language] = parser
            return parser.parse(normalized, source)
    except Exception as exc:  # 개별 파일 실패가 전체 수집을 막지 않도록
        logger.exception("파싱 실패: %s", normalized)
        result = ParseResult()
        result.warnings.append(f"{normalized}: 파싱 예외 ({exc})")
        return result

    return ParseResult()
