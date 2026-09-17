"""@SpringBootTest 주입 검증 (정의서 (1), [상세] 요구사항).

이 변환은 LLM 없이 코드로만 이뤄져야 하므로 결정적 결과를 확인한다.
"""

from __future__ import annotations

import pytest

from codetest_mcp import springboot

PLAIN_TEST = """\
package com.example.demo;

import org.junit.jupiter.api.Test;

class OrderServiceTest {
    @Test
    void total() {}
}
"""

ALREADY_ANNOTATED = """\
package com.example.demo;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class OrderServiceTest {
    @Test
    void total() {}
}
"""


def test_injects_annotation_when_missing():
    prepared = springboot.prepare(PLAIN_TEST)

    assert "@SpringBootTest" in prepared.source
    assert prepared.springboot_applied is True
    # 애너테이션은 class 선언 바로 위에 온다
    lines = prepared.source.splitlines()
    class_index = next(i for i, line in enumerate(lines) if line.startswith("class "))
    assert lines[class_index - 1].strip() == "@SpringBootTest"


def test_injects_required_import():
    prepared = springboot.prepare(PLAIN_TEST)
    assert "import org.springframework.boot.test.context.SpringBootTest;" in prepared.source


def test_is_idempotent_when_already_annotated():
    prepared = springboot.prepare(ALREADY_ANNOTATED)
    assert prepared.source.count("@SpringBootTest") == 1
    assert prepared.source.count("import org.springframework.boot.test.context.SpringBootTest;") == 1
    assert prepared.applied == []


def test_computes_test_file_path_from_package():
    prepared = springboot.prepare(PLAIN_TEST)
    assert prepared.package == "com.example.demo"
    assert prepared.class_name == "OrderServiceTest"
    assert prepared.file_path == "src/test/java/com/example/demo/OrderServiceTest.java"


def test_adds_package_when_missing():
    source = "class FooTest {\n    @Test\n    void t() {}\n}\n"
    prepared = springboot.prepare(source, base_package="com.example.demo")

    assert prepared.source.startswith("package com.example.demo;")
    assert prepared.file_path == "src/test/java/com/example/demo/FooTest.java"
    assert any("package 선언 추가" in item for item in prepared.applied)


def test_adds_junit_import_when_test_annotation_present():
    source = "package com.example.demo;\n\nclass FooTest {\n    @Test\n    void t() {}\n}\n"
    prepared = springboot.prepare(source)
    assert "import org.junit.jupiter.api.Test;" in prepared.source


def test_wildcard_import_is_respected():
    source = (
        "package com.example.demo;\n\n"
        "import org.springframework.boot.test.context.*;\n"
        "import org.junit.jupiter.api.*;\n\n"
        "class FooTest {\n    @Test\n    void t() {}\n}\n"
    )
    prepared = springboot.prepare(source)
    # 와일드카드가 덮으므로 중복 import 를 넣지 않는다
    assert "import org.springframework.boot.test.context.SpringBootTest;" not in prepared.source
    assert "@SpringBootTest" in prepared.source


def test_empty_source_is_rejected():
    with pytest.raises(ValueError):
        springboot.prepare("   ")


def test_source_without_class_is_rejected():
    with pytest.raises(ValueError):
        springboot.prepare("package com.example.demo;\n\n// 클래스 없음\n")


# --- 기준 패키지 추론 ---------------------------------------------------------
def test_detect_base_package_picks_topmost():
    paths = [
        "src/main/java/com/example/demo/DemoApplication.java",
        "src/main/java/com/example/demo/service/OrderService.java",
        "src/main/java/com/example/demo/controller/OrderController.java",
    ]
    assert springboot.detect_base_package(paths) == "com.example.demo"


def test_detect_base_package_ignores_non_java_sources():
    assert springboot.detect_base_package(["build.gradle", "README.md"]) is None


# --- 폴더 구조를 못 박지 않는다 --------------------------------------------------
#
# `src/test/java` 를 상수로 쓰면 단일 모듈이 저장소 루트에 있을 때만 맞는다.
# 멀티 모듈에서는 대상 코드와 같은 모듈에 테스트가 들어가야 한다.
def test_single_module_layout_is_unchanged():
    layout = springboot.detect_layout(["src/main/java/com/example/demo/DemoApplication.java"])

    assert layout.base_package == "com.example.demo"
    assert layout.test_root == "src/test/java"
    assert layout.module == ""


def test_multi_module_keeps_the_module_prefix():
    layout = springboot.detect_layout([
        "api/src/main/java/com/example/api/ApiApplication.java",
        "batch/src/main/java/com/example/batch/job/DailyJob.java",
    ])

    # 가장 짧은(=최상위) 패키지를 가진 모듈이 기준이다
    assert layout.base_package == "com.example.api"
    assert layout.test_root == "api/src/test/java"
    assert layout.module == "api/"


def test_nested_module_paths_are_kept_whole():
    layout = springboot.detect_layout(["services/core/src/main/java/com/acme/core/Core.java"])
    assert layout.test_root == "services/core/src/test/java"


def test_kotlin_sources_still_map_to_a_java_test_root():
    """생성물은 Java 다 — Gradle 의 java 플러그인도 Maven 도 그 경로를 컴파일한다."""
    layout = springboot.detect_layout(["api/src/main/kotlin/com/example/api/App.kt"])

    assert layout.base_package == "com.example.api"
    assert layout.test_root == "api/src/test/java"


def test_test_sources_are_used_when_there_is_no_main():
    layout = springboot.detect_layout(["api/src/test/java/com/example/api/ExistingTest.java"])
    assert layout.test_root == "api/src/test/java"


def test_layout_falls_back_to_the_standard_paths():
    layout = springboot.detect_layout(["build.gradle", "README.md"])

    assert layout.base_package is None
    assert layout.test_root == springboot.DEFAULT_TEST_ROOT


def test_prepare_puts_the_test_in_the_given_module():
    prepared = springboot.prepare(PLAIN_TEST, test_root="api/src/test/java")

    assert prepared.file_path == "api/src/test/java/com/example/demo/OrderServiceTest.java"
    assert prepared.test_root == "api/src/test/java"


def test_prepare_normalizes_a_windows_style_test_root():
    prepared = springboot.prepare(PLAIN_TEST, test_root="api\\src\\test\\java/")
    assert prepared.file_path == "api/src/test/java/com/example/demo/OrderServiceTest.java"


# --- 주석에 속지 않아야 한다 ---------------------------------------------------
def test_comment_mentioning_the_annotation_does_not_block_injection():
    """설명 주석에 @SpringBootTest 가 적혀 있다고 주입을 건너뛰면
    애너테이션 없이 실행돼 @Autowired 가 null 이 되고 NPE 가 난다."""
    source = """package com.example.demo;

// NOTE: This file is consumed by `codetest test`. The agent ensures it is
// annotated with @SpringBootTest before executing it.
class ProvidedOrderTest {
    @Autowired
    private OrderService orderService;
}
"""
    prepared = springboot.prepare(source, "com.example.demo")

    assert any("주입" in note for note in prepared.applied)
    assert prepared.springboot_applied is True
    # 주석이 아니라 클래스 선언 바로 위에 붙었는지
    lines = prepared.source.splitlines()
    marker = lines.index("@SpringBootTest")
    assert lines[marker + 1].startswith("class ProvidedOrderTest")


def test_block_comment_is_ignored_too():
    source = """package com.example.demo;

/* 이 테스트는 @SpringBootTest 로 돌아야 한다 */
class FooTest { }
"""
    prepared = springboot.prepare(source, "com.example.demo")
    assert prepared.springboot_applied is True
    assert "@SpringBootTest\nclass FooTest" in prepared.source


def test_string_literal_is_ignored_too():
    source = """package com.example.demo;

class FooTest {
    String hint = "@SpringBootTest 를 붙이세요";
}
"""
    prepared = springboot.prepare(source, "com.example.demo")
    assert prepared.springboot_applied is True
    assert "@SpringBootTest\nclass FooTest" in prepared.source


def test_real_annotation_is_not_duplicated():
    source = """package com.example.demo;

import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class AlreadyTest { }
"""
    prepared = springboot.prepare(source, "com.example.demo")

    assert prepared.applied == []
    assert prepared.source.count("@SpringBootTest") == 1
