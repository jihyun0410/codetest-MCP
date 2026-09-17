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
  4. 저장 경로(<테스트 소스 루트>/<package>/<Class>.java)를 계산한다

**저장 경로를 못 박지 않는다.** 예전에는 `src/test/java/…` 를 상수로 썼는데, 그건
단일 모듈이 저장소 루트에 있을 때만 맞는다. 멀티 모듈(`api/`, `batch/`)에서는
`api/src/test/java/…` 여야 한다. `detect_layout` 이 **실제 소스 경로에서** 모듈
접두사를 읽어 테스트 루트를 만든다. 이 서버에는 사용자의 작업 트리가 없으므로
여기서 나온 값은 **추정**이고, 최종 확인은 파일 시스템을 가진 CLI 가 한다
(codereview_gitver `project_layout.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Spring Boot 테스트 컨텍스트 애너테이션
SPRING_BOOT_TEST = "@SpringBootTest"

#: 모듈을 못 찾았을 때 쓰는 표준 레이아웃
DEFAULT_TEST_ROOT = "src/test/java"
#: 생성물은 Java 다 — Gradle 의 java 플러그인도 Maven 의 testSourceDirectory 도
#: 이 디렉터리를 컴파일하므로 Kotlin 프로젝트에서도 여기에 둔다.
_TEST_LANGUAGE = "java"
#: `api/src/main/java/com/example/demo/Foo.java` 를 모듈·종류·언어·나머지로 가른다
_SOURCE_ROOT_RE = re.compile(
    r"^(?P<module>(?:.*/)?)src/(?P<kind>main|test)/(?P<lang>java|kotlin|groovy)/(?P<tail>.+)$"
)

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

#: 줄 주석 / 블록 주석 / 문자열 리터럴
_NON_CODE = re.compile(
    r'//[^\n]*'          # 줄 주석
    r'|/\*.*?\*/'        # 블록 주석
    r'|"(?:\\.|[^"\\])*"',  # 문자열 리터럴
    re.DOTALL,
)


def _code_only(source: str) -> str:
    """주석과 문자열 리터럴을 지운 사본.

    `@SpringBootTest` 가 **실제 애너테이션으로** 있는지 판단하는 데 쓴다.
    설명 주석에 그 낱말이 적혀 있다는 이유로 주입을 건너뛰면, 애너테이션이 없는
    채로 실행돼 @Autowired 가 null 이 되고 NullPointerException 이 난다.
    """
    return _NON_CODE.sub(" ", source)


@dataclass
class PreparedTest:
    """@SpringBootTest 주입을 마친 테스트 소스."""

    source: str
    class_name: str
    package: str
    #: 저장소 루트 기준 상대 경로 (예: api/src/test/java/com/example/demo/FooTest.java)
    file_path: str
    #: 그 경로를 만든 테스트 소스 루트 (예: api/src/test/java)
    test_root: str = DEFAULT_TEST_ROOT
    #: 이번 변환에서 실제로 무엇을 했는지 (리포트에 근거로 남긴다)
    applied: list[str] = field(default_factory=list)

    @property
    def springboot_applied(self) -> bool:
        return SPRING_BOOT_TEST in _code_only(self.source)


def prepare(
    test_code: str,
    base_package: str | None = None,
    test_root: str | None = None,
) -> PreparedTest:
    """
    테스트 소스에 @SpringBootTest 를 보장하고 저장 경로를 계산한다.

    :param test_code:     Agent(LLM)가 생성한 Java 테스트 소스
    :param base_package:  package 선언이 없을 때 사용할 기준 패키지
                          (프로젝트 개요에서 얻은 @SpringBootApplication 패키지)
    :param test_root:     테스트 소스 루트 (예: `api/src/test/java`).
                          생략하면 표준 단일 모듈 레이아웃으로 본다.
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

    if SPRING_BOOT_TEST not in _code_only(source):
        source = _inject_annotation(source, class_name)
        applied.append(f"{SPRING_BOOT_TEST} 주입 (class {class_name})")

    source, added_imports = _ensure_imports(source)
    if added_imports:
        applied.append("import 보강: " + ", ".join(added_imports))

    root = (test_root or DEFAULT_TEST_ROOT).replace("\\", "/").strip("/") or DEFAULT_TEST_ROOT
    package_path = package.replace(".", "/")
    file_path = f"{root}/{package_path}/{class_name}.java" if package_path else f"{root}/{class_name}.java"

    return PreparedTest(
        source=source,
        class_name=class_name,
        package=package,
        file_path=file_path,
        test_root=root,
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

    if SPRING_BOOT_TEST in _code_only(source) and not _covered(existing, _IMPORT_SPRING_BOOT_TEST):
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


# ---------------------------------------------------------------------------
#  레이아웃 추론 (폴더 구조를 못 박지 않기 위한 부분)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceLayout:
    """소스 경로에서 읽어 낸 프로젝트 레이아웃."""

    #: @SpringBootApplication 이 있을 최상위 패키지 (없으면 None)
    base_package: str | None = None
    #: 그 패키지가 속한 모듈의 테스트 소스 루트 (예: `api/src/test/java`)
    test_root: str = DEFAULT_TEST_ROOT
    #: 모듈 접두사 (`api/`, 단일 모듈이면 `""`)
    module: str = ""


def detect_layout(source_paths: list[str]) -> SourceLayout:
    """
    소스 경로에서 기준 패키지와 테스트 소스 루트를 함께 추론한다.

        src/main/java/com/example/demo/DemoApplication.java
          → com.example.demo / src/test/java

        api/src/main/kotlin/com/example/api/ApiApplication.kt
          → com.example.api / api/src/test/java

    모듈이 여럿이면 **가장 짧은(=최상위) 패키지**를 가진 모듈을 기준으로 삼는다.
    그 패키지가 애플리케이션 루트일 가능성이 가장 높고, 예전 동작과도 같다.
    테스트 언어 디렉터리는 main 이 Kotlin 이어도 `java` 다 — 생성물이 Java 이고
    두 빌드 도구 모두 그 경로를 컴파일한다.
    """
    candidates: list[tuple[str, str]] = []      # (패키지, 모듈 접두사)
    fallback: list[tuple[str, str]] = []        # main 이 없을 때 쓸 test 쪽 후보

    for path in source_paths or []:
        match = _SOURCE_ROOT_RE.match(path.replace("\\", "/").lstrip("./"))
        if match is None:
            continue
        parts = match.group("tail").split("/")[:-1]     # 파일명 제외
        if not parts:
            continue
        entry = (".".join(parts), match.group("module"))
        (candidates if match.group("kind") == "main" else fallback).append(entry)

    chosen = candidates or fallback
    if not chosen:
        return SourceLayout()

    package, module = min(chosen, key=lambda item: (item[0].count("."), len(item[0])))
    return SourceLayout(
        base_package=package,
        test_root=f"{module}src/test/{_TEST_LANGUAGE}",
        module=module,
    )


def detect_base_package(source_paths: list[str]) -> str | None:
    """기준 패키지만 필요할 때 쓰는 단축 경로."""
    return detect_layout(source_paths).base_package
