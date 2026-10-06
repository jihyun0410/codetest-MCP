"""tree-sitter 없이 도는 정규식 폴백이 병적인 입력에서 폭주하지 않는지 검증한다.

예전 폴백은 `\\s` 가 겹치는 반복 때문에, 상수만 나열한 enum(11KB)이 수십 초,
빈 줄 1600개가 20초 넘게 걸렸다. 이 서버는 파싱 중 로그가 없어서 그대로 "멈춤"으로 보인다.
제한 시간은 일부러 넉넉하게 잡았다 — 고치기 전에는 이 입력들이 40초를 넘겼다.
"""

from __future__ import annotations

import time

import pytest

from codetest_mcp.parsing import java_parser, js_ts_parser

LIMIT_SECONDS = 2.0


def _timed(fn, *args):
    started = time.perf_counter()
    result = fn(*args)
    return time.perf_counter() - started, result


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_enum_of_bare_constants_does_not_blow_up(newline):
    constants = "".join(f"    ERR_{i:05d},\n" for i in range(5000))
    source = ("package a;\n\npublic enum Code {\n" + constants + "    LAST\n}\n").replace("\n", newline)

    elapsed, result = _timed(java_parser._regex_fallback, "src/Code.java", source)

    assert elapsed < LIMIT_SECONDS
    assert [n.name for n in result.nodes if n.node_type.name == "CLASS"] == ["Code"]


#: 값 자체를 parametrize 하면 2만 자가 테스트 ID 가 되어 Windows 환경변수 한도를 넘는다 — 이름만 받는다.
FILLERS = {"lf": "\n" * 20000, "spaces": " " * 20000, "crlf": "\r\n" * 10000}


@pytest.mark.parametrize("kind", FILLERS)
def test_long_blank_runs_do_not_blow_up(kind):
    source = "package a;\n" + FILLERS[kind] + "int x;\n"

    elapsed, _ = _timed(java_parser._regex_fallback, "src/A.java", source)

    assert elapsed < LIMIT_SECONDS


def test_many_methods_scale_linearly():
    methods = "".join(f"    public int m{i}(int a) {{\n        return a;\n    }}\n\n" for i in range(8000))
    source = "package a;\npublic class A {\n" + methods + "}\n"

    elapsed, result = _timed(java_parser._regex_fallback, "src/A.java", source)

    assert elapsed < LIMIT_SECONDS
    assert sum(n.node_type.name == "METHOD" for n in result.nodes) == 8000


def test_js_blank_lines_do_not_blow_up():
    source = "var a = 1;\n" + "\n" * 50000 + "function f() {}\n"

    elapsed, result = _timed(js_ts_parser._regex_fallback, "static/lib.js", source, "javascript")

    assert elapsed < LIMIT_SECONDS
    assert [n.name for n in result.nodes if n.node_type.name == "METHOD"] == ["f"]


def test_fallback_still_extracts_types_methods_and_constructors():
    source = (
        "package com.example;\n"                                   # 1
        "\n"                                                       # 2
        "public class OrderController {\n"                         # 3
        "    private final Svc svc;\n"                             # 4
        "\n"                                                       # 5
        "    @GetMapping(\"/{id}\")\n"                             # 6
        "    public Map<String, List<Integer>> get(Long id) throws IOException {\n"  # 7
        "        validate(id);\n"                                  # 8  호출문 — 메서드가 아니다
        "        return null;\n"                                   # 9
        "    }\n"                                                  # 10
        "\n"                                                       # 11
        "    private void validate(Long id) {}\n"                  # 12
        "    OrderController(Svc s) { this.svc = s; }\n"           # 13 수식어 없는 생성자
        "}\n"
    )

    result = java_parser._regex_fallback("src/OrderController.java", source)

    by_name = {n.name: n for n in result.nodes if n.node_type.name != "FILE"}
    assert set(by_name) == {"OrderController", "get", "validate"}
    assert by_name["OrderController"].start_line in {3, 13}   # 클래스 선언 또는 생성자
    assert by_name["get"].start_line == 6                     # 애너테이션 줄부터
    assert by_name["validate"].start_line == 12               # 앞 빈 줄이 아니라 선언 줄
    assert {n.signature for n in result.nodes if n.name == "OrderController"} >= {"class com.example.OrderController"}
