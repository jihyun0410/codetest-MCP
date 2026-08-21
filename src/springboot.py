"""
@SpringBootTest 주입 (코드 기반, LLM 미사용).

정의서
  · "(1) … 생성된 Test Code를 @SpringBootTest 에 넣고 실행시킨다."
  · "[상세] Spring Boot 환경에서 TDD 기반으로 @SpringBootTest 를 사용하여
     환경에서 동작하도록 한다."

Agent(LLM)가 만든 Java 테스트 소스를 받아 **결정적인 문자열 변환만으로**
@SpringBootTest 클래스로 만든다. 판단이 아니라 규칙 적용이므로 MCP 의 책임이다.

수행하는 일
  1. package 선언 확인 — 없으면 프로젝트의 기준 패키지를 넣는다
  2. 테스트 클래스에 @SpringBootTest 가 없으면 붙인다
  3. @SpringBootTest / @Test 에 필요한 import 를 보강한다
  4. 저장 경로(src/test/java/<package>/<Class>.java)를 계산한다
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Spring Boot 테스트 컨텍스트 애너테이션
SPRING_BOOT_TEST = "@SpringBootTest"

_IMPORT_SPRING_BOOT_TEST = "org.springframework.boot.test.context.SpringBootTest"
_IMPORT_JUNIT_TEST = "org.junit.jupiter.api.Test"

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
#: class / interface 선언 (제네릭·상속 앞까지만 잡는다)
_CLASS_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<modifiers>(?:(?:public|final|abstract|static)\s+)*)"
    r"class\s+(?P<name>\w+)",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.MULTILINE)


@dataclass
class PreparedTest:
    """@SpringBootTest 주입을 마친 테스트 소스."""

    source: str
    class_name: str
    package: str
    #: 저장소 루트 기준 상대 경로 (예: src/test/java/com/example/demo/FooTest.java)
    file_path: str
    #: 이번 변환에서 실제로 무엇을 했는지 (리포트에 근거로 남긴다)
    applied: list[str] = field(default_factory=list)

    @property
    def springboot_applied(self) -> bool:
        return SPRING_BOOT_TEST in self.source


def prepare(test_code: str, base_package: str | None = None) -> PreparedTest:
    """
    테스트 소스에 @SpringBootTest 를 보장하고 저장 경로를 계산한다.

    :param test_code:     Agent(LLM)가 생성한 Java 테스트 소스
    :param base_package:  package 선언이 없을 때 사용할 기준 패키지
                          (프로젝트 개요에서 얻은 @SpringBootApplication 패키지)
    """
    source = (test_code or "").strip()
    applied: list[str] = []

    if not source:
        raise ValueError("Test Code 가 비어 있습니다.")

    package = _find_package(source)
    if package is None:
        package = base_package or ""
        if package:
            source = f"package {package};\n\n{source}"
            applied.append(f"package 선언 추가: {package}")

    class_name = _find_class_name(source)
    if class_name is None:
        raise ValueError("테스트 소스에서 class 선언을 찾지 못했습니다.")

    if SPRING_BOOT_TEST not in source:
        source = _inject_annotation(source, class_name)
        applied.append(f"{SPRING_BOOT_TEST} 주입 (class {class_name})")

    source, added_imports = _ensure_imports(source)
    if added_imports:
        applied.append("import 보강: " + ", ".join(added_imports))

    package_path = package.replace(".", "/")
    file_path = (
        f"src/test/java/{package_path}/{class_name}.java"
        if package_path
        else f"src/test/java/{class_name}.java"
    )

    return PreparedTest(
        source=source,
        class_name=class_name,
        package=package,
        file_path=file_path,
        applied=applied,
    )


# ---------------------------------------------------------------------------
def _find_package(source: str) -> str | None:
    match = _PACKAGE_RE.search(source)
    return match.group(1) if match else None


def _find_class_name(source: str) -> str | None:
    """테스트 클래스명을 찾는다. 여러 개면 첫 번째(최상위) 선언을 쓴다."""
    match = _CLASS_DECL_RE.search(source)
    return match.group("name") if match else None


def _inject_annotation(source: str, class_name: str) -> str:
    """해당 class 선언 바로 위 줄에 @SpringBootTest 를 넣는다."""
    for match in _CLASS_DECL_RE.finditer(source):
        if match.group("name") != class_name:
            continue
        indent = match.group("indent")
        insert_at = match.start()
        return f"{source[:insert_at]}{indent}{SPRING_BOOT_TEST}\n{source[insert_at:]}"
    return source


def _ensure_imports(source: str) -> tuple[str, list[str]]:
    """
    @SpringBootTest / @Test 사용에 필요한 import 를 보강한다.

    이미 있거나 와일드카드(`org.springframework.boot.test.context.*`)로 덮이면
    건드리지 않는다.
    """
    existing = set(_IMPORT_RE.findall(source))
    needed: list[str] = []

    if SPRING_BOOT_TEST in source and not _covered(existing, _IMPORT_SPRING_BOOT_TEST):
        needed.append(_IMPORT_SPRING_BOOT_TEST)
    if re.search(r"@Test\b", source) and not _covered(existing, _IMPORT_JUNIT_TEST):
        needed.append(_IMPORT_JUNIT_TEST)

    if not needed:
        return source, []

    block = "\n".join(f"import {name};" for name in needed)

    # package 선언 다음 줄에 넣는다. 없으면 파일 맨 앞.
    package_match = _PACKAGE_RE.search(source)
    if package_match:
        insert_at = package_match.end()
        return f"{source[:insert_at]}\n\n{block}{source[insert_at:]}", needed
    return f"{block}\n\n{source}", needed


def _covered(existing: set[str], target: str) -> bool:
    """정확히 import 되었거나 같은 패키지 와일드카드로 덮였는지."""
    if target in existing:
        return True
    wildcard = target.rsplit(".", 1)[0] + ".*"
    return wildcard in existing


def detect_base_package(source_paths: list[str]) -> str | None:
    """
    프로젝트의 기준 패키지를 경로에서 추론한다.

    src/main/java/com/example/demo/DemoApplication.java → com.example.demo
    가장 짧은(=최상위) 패키지를 기준으로 삼는다.
    """
    candidates: list[str] = []
    for path in source_paths:
        normalized = path.replace("\\", "/")
        marker = "src/main/java/"
        if marker not in normalized or not normalized.endswith(".java"):
            continue
        tail = normalized.split(marker, 1)[1]
        parts = tail.split("/")[:-1]  # 파일명 제외
        if parts:
            candidates.append(".".join(parts))

    if not candidates:
        return None
    return min(candidates, key=lambda value: (value.count("."), len(value)))
