"""
Linter 검증 (Headless).

1순위: 호스트에 설치된 **실제 린터**를 무헤드로 실행
        - Python : `ruff check --output-format json`
        - JS/TS  : `npx --no-install eslint --format json`
2순위: 린터가 없으면 **내장 결정적 규칙**으로 폴백
        - 추론이 아니라 AST/토큰 사실로 판정 가능한 항목만 포함한다
          (오탈자성 오류, 잘못된 변수 사용, 명백한 버그 패턴)

린터 미설치 환경에서도 검증이 비지 않도록 하는 것이 목적이다.
"""

from __future__ import annotations

import ast as pyast
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from codetest_mcp.models import FileInput, Finding

#: 외부 린터 1회 실행 타임아웃(초)
LINTER_TIMEOUT = 45


def available_linters() -> dict[str, bool]:
    """health 도구용 — 어떤 외부 린터를 쓸 수 있는지."""
    return {
        "ruff": shutil.which("ruff") is not None,
        "eslint": shutil.which("npx") is not None or shutil.which("eslint") is not None,
    }


def run(files: list[FileInput]) -> list[Finding]:
    findings: list[Finding] = []

    python_files = [f for f in files if f.language == "python"]
    web_files = [f for f in files if f.language in {"javascript", "typescript", "tsx"}]
    java_files = [f for f in files if f.language == "java"]
    sql_files = [f for f in files if f.language == "sql"]

    if python_files:
        findings.extend(_lint_python(python_files))
    if web_files:
        findings.extend(_lint_web(web_files))
    if java_files:
        findings.extend(_lint_java(java_files))
    if sql_files:
        findings.extend(_lint_sql(sql_files))

    return findings


# ---------------------------------------------------------------------------
#  Python
# ---------------------------------------------------------------------------
def _lint_python(files: list[FileInput]) -> list[Finding]:
    if shutil.which("ruff"):
        external = _run_ruff(files)
        if external is not None:
            return external
    return [finding for file_input in files for finding in _builtin_python_rules(file_input)]


def _run_ruff(files: list[FileInput]) -> list[Finding] | None:
    """ruff 를 임시 디렉터리에서 실행하고 JSON 결과를 파싱한다."""
    with tempfile.TemporaryDirectory(prefix="codetest-ruff-") as tmp:
        root = Path(tmp)
        mapping: dict[str, str] = {}
        for index, file_input in enumerate(files):
            target = root / f"{index}_{Path(file_input.path).name}"
            target.write_text(file_input.content, encoding="utf-8")
            mapping[str(target.resolve())] = file_input.path

        try:
            completed = subprocess.run(  # noqa: S603 — 고정 실행 파일, 사용자 입력 아님
                ["ruff", "check", "--output-format", "json", "--no-cache", str(root)],
                capture_output=True,
                text=True,
                timeout=LINTER_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        try:
            items = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return None

    findings: list[Finding] = []
    for item in items:
        resolved = str(Path(item.get("filename", "")).resolve())
        findings.append(
            Finding(
                file_path=mapping.get(resolved, item.get("filename", "")),
                line=(item.get("location") or {}).get("row"),
                severity="error" if str(item.get("code", "")).startswith(("E9", "F")) else "warning",
                rule=item.get("code"),
                message=item.get("message", ""),
                source="linter",
            )
        )
    return findings


def _builtin_python_rules(file_input: FileInput) -> list[Finding]:
    """
    ruff 가 없을 때의 내장 규칙.

    AST 로 확정 가능한 항목만 본다 — 추측성 지적은 하지 않는다.
    """
    findings: list[Finding] = []
    try:
        tree = pyast.parse(file_input.content, filename=file_input.path)
    except SyntaxError:
        return findings  # 문법 오류는 ast_check 가 이미 보고한다

    imported: dict[str, int] = {}
    used: set[str] = set()

    for node in pyast.walk(tree):
        # 1) 잘못된 변수 사용: 정의되지 않은 이름 사용은 오탐 위험이 커 제외하고,
        #    확정 가능한 "미사용 import" 만 본다.
        if isinstance(node, pyast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, pyast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
        elif isinstance(node, pyast.Name):
            used.add(node.id)
        elif isinstance(node, pyast.Attribute):
            base = node
            while isinstance(base, pyast.Attribute):
                base = base.value
            if isinstance(base, pyast.Name):
                used.add(base.id)

        # 2) 가변 기본 인자 — 확정적 버그 패턴
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            for default in node.args.defaults:
                if isinstance(default, (pyast.List, pyast.Dict, pyast.Set)):
                    findings.append(
                        Finding(
                            file_path=file_input.path,
                            line=node.lineno,
                            severity="warning",
                            rule="mutable-default-arg",
                            message=f"함수 `{node.name}` 의 기본 인자가 가변 객체입니다. "
                            "호출 간 상태가 공유되어 버그를 유발합니다.",
                            source="linter",
                        )
                    )

        # 3) 빈 except / 광범위 except
        if isinstance(node, pyast.ExceptHandler):
            if node.type is None:
                findings.append(
                    Finding(
                        file_path=file_input.path,
                        line=node.lineno,
                        severity="warning",
                        rule="bare-except",
                        message="bare except 는 KeyboardInterrupt/SystemExit 까지 삼킵니다.",
                        source="linter",
                    )
                )
            if len(node.body) == 1 and isinstance(node.body[0], pyast.Pass):
                findings.append(
                    Finding(
                        file_path=file_input.path,
                        line=node.lineno,
                        severity="warning",
                        rule="silent-except",
                        message="예외를 조용히 삼키고 있습니다 (except: pass).",
                        source="linter",
                    )
                )

        # 4) `== None` / `!= None` — 오탈자성 비교
        if isinstance(node, pyast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (pyast.Eq, pyast.NotEq)) and isinstance(
                    comparator, pyast.Constant
                ) and comparator.value is None:
                    findings.append(
                        Finding(
                            file_path=file_input.path,
                            line=node.lineno,
                            severity="info",
                            rule="none-comparison",
                            message="None 비교는 `is` / `is not` 를 사용해야 합니다.",
                            source="linter",
                        )
                    )

    for name, line in imported.items():
        if name not in used:
            findings.append(
                Finding(
                    file_path=file_input.path,
                    line=line,
                    severity="info",
                    rule="unused-import",
                    message=f"사용되지 않는 import: `{name}`",
                    source="linter",
                )
            )

    return findings


# ---------------------------------------------------------------------------
#  JavaScript / TypeScript
# ---------------------------------------------------------------------------
def _lint_web(files: list[FileInput]) -> list[Finding]:
    external = _run_eslint(files)
    if external is not None:
        return external
    return [finding for file_input in files for finding in _builtin_web_rules(file_input)]


def _run_eslint(files: list[FileInput]) -> list[Finding] | None:
    """
    프로젝트에 eslint 가 설치되어 있을 때만 사용한다.

    `npx --no-install` 로 실행해 네트워크 설치를 시도하지 않는다.
    """
    if shutil.which("eslint"):
        base_command = ["eslint"]
    elif shutil.which("npx"):
        base_command = ["npx", "--no-install", "eslint"]
    else:
        return None

    with tempfile.TemporaryDirectory(prefix="codetest-eslint-") as tmp:
        root = Path(tmp)
        mapping: dict[str, str] = {}
        paths: list[str] = []
        for index, file_input in enumerate(files):
            target = root / f"{index}_{Path(file_input.path).name}"
            target.write_text(file_input.content, encoding="utf-8")
            mapping[str(target.resolve())] = file_input.path
            paths.append(str(target))

        try:
            completed = subprocess.run(  # noqa: S603
                [*base_command, "--format", "json", "--no-error-on-unmatched-pattern", *paths],
                capture_output=True,
                text=True,
                timeout=LINTER_TIMEOUT,
                cwd=os.getcwd(),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        try:
            reports = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return None

    findings: list[Finding] = []
    for report in reports:
        resolved = str(Path(report.get("filePath", "")).resolve())
        for message in report.get("messages", []):
            findings.append(
                Finding(
                    file_path=mapping.get(resolved, report.get("filePath", "")),
                    line=message.get("line"),
                    severity="error" if message.get("severity") == 2 else "warning",
                    rule=message.get("ruleId"),
                    message=message.get("message", ""),
                    source="linter",
                )
            )
    return findings


#: 내장 JS/TS 규칙 — 정규식으로 확정 가능한 항목만
_WEB_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "loose-equality",
        re.compile(r"[^=!<>]==[^=]"),
        "info",
        "느슨한 동등 비교(`==`)가 사용되었습니다. `===` 사용을 권장합니다.",
    ),
    (
        "debugger-statement",
        re.compile(r"^\s*debugger\s*;?\s*$"),
        "error",
        "`debugger` 문이 남아 있습니다.",
    ),
    (
        "empty-catch",
        re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"),
        "warning",
        "예외를 조용히 삼키는 빈 catch 블록입니다.",
    ),
    (
        "var-declaration",
        re.compile(r"^\s*var\s+\w+"),
        "info",
        "`var` 대신 `let`/`const` 사용을 권장합니다.",
    ),
    (
        "await-in-loop-forEach",
        re.compile(r"\.forEach\s*\(\s*async"),
        "warning",
        "`forEach(async …)` 는 await 가 무시됩니다. `for…of` 를 사용하세요.",
    ),
]


def _builtin_web_rules(file_input: FileInput) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(file_input.content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for rule, pattern, severity, message in _WEB_RULES:
            if pattern.search(line):
                findings.append(
                    Finding(
                        file_path=file_input.path,
                        line=line_number,
                        severity=severity,
                        rule=rule,
                        message=message,
                        source="linter",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
#  Java
# ---------------------------------------------------------------------------
_JAVA_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "string-equality",
        re.compile(r'\w+\s*==\s*"'),
        "error",
        "문자열을 `==` 로 비교하고 있습니다. `equals()` 를 사용해야 합니다.",
    ),
    (
        "empty-catch",
        re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"),
        "warning",
        "예외를 조용히 삼키는 빈 catch 블록입니다.",
    ),
    (
        "printstacktrace",
        re.compile(r"\.printStackTrace\s*\("),
        "warning",
        "`printStackTrace()` 대신 로거를 사용해야 합니다.",
    ),
    (
        "system-out",
        re.compile(r"System\.(out|err)\.print"),
        "info",
        "`System.out/err` 출력이 남아 있습니다. 로거로 대체하세요.",
    ),
    (
        "sql-string-concat",
        re.compile(r'"\s*(SELECT|INSERT|UPDATE|DELETE)[^"]*"\s*\+', re.IGNORECASE),
        "error",
        "SQL 문자열 연결은 SQL Injection 위험이 있습니다. 바인딩 파라미터를 사용하세요.",
    ),
]


def _lint_java(files: list[FileInput]) -> list[Finding]:
    findings: list[Finding] = []
    for file_input in files:
        in_block_comment = False
        for line_number, line in enumerate(file_input.content.splitlines(), start=1):
            stripped = line.strip()
            if in_block_comment:
                if "*/" in stripped:
                    in_block_comment = False
                continue
            if stripped.startswith("/*"):
                in_block_comment = "*/" not in stripped
                continue
            if stripped.startswith("//"):
                continue
            for rule, pattern, severity, message in _JAVA_RULES:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            file_path=file_input.path,
                            line=line_number,
                            severity=severity,
                            rule=rule,
                            message=message,
                            source="linter",
                        )
                    )
    return findings


# ---------------------------------------------------------------------------
#  SQL (DML)
# ---------------------------------------------------------------------------
_SQL_NO_WHERE = re.compile(r"^\s*(UPDATE|DELETE)\b(?!.*\bWHERE\b)", re.IGNORECASE | re.DOTALL)
_SQL_SELECT_STAR = re.compile(r"\bSELECT\s+\*", re.IGNORECASE)


def _lint_sql(files: list[FileInput]) -> list[Finding]:
    findings: list[Finding] = []
    for file_input in files:
        statements = [s for s in file_input.content.split(";") if s.strip()]
        offset = 1
        for statement in statements:
            line_number = offset
            offset += statement.count("\n")
            if _SQL_NO_WHERE.match(statement.strip()):
                findings.append(
                    Finding(
                        file_path=file_input.path,
                        line=line_number,
                        severity="error",
                        rule="dml-without-where",
                        message="WHERE 절 없는 UPDATE/DELETE 입니다. 전체 행이 영향을 받습니다.",
                        source="linter",
                    )
                )
            if _SQL_SELECT_STAR.search(statement):
                findings.append(
                    Finding(
                        file_path=file_input.path,
                        line=line_number,
                        severity="info",
                        rule="select-star",
                        message="`SELECT *` 는 스키마 변경에 취약합니다. 컬럼을 명시하세요.",
                        source="linter",
                    )
                )
    return findings
