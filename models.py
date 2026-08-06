"""MCP 도구 입출력 공통 모델."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Finding:
    """검출 결과 1건 — agent-server 의 LintFinding 스키마와 키가 동일하다."""

    file_path: str
    message: str
    line: int | None = None
    severity: str = "warning"   # error | warning | info
    rule: str | None = None
    source: str = "linter"      # ast | linter

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FileInput:
    path: str
    content: str
    language: str

    @classmethod
    def from_dict(cls, payload: dict) -> "FileInput | None":
        path = payload.get("path")
        content = payload.get("content")
        if not path or content is None:
            return None
        language = payload.get("language") or _guess_language(path)
        if language is None:
            return None
        return cls(path=path, content=content, language=language)


_EXT_LANGUAGE = {
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


def _guess_language(path: str) -> str | None:
    for extension, language in _EXT_LANGUAGE.items():
        if path.lower().endswith(extension):
            return language
    return None


def parse_files(payload: dict) -> list[FileInput]:
    """도구 입력 dict 에서 files 배열을 파싱한다."""
    items = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file_input = FileInput.from_dict(item)
        if file_input is not None:
            parsed.append(file_input)
    return parsed
