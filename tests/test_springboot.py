"""@SpringBootTest 주입 검증 (정의서 (1), [상세] 요구사항).

이 변환은 LLM 없이 코드로만 이뤄져야 하므로 결정적 결과를 확인한다.
"""

from __future__ import annotations

import pytest

from src import springboot

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
