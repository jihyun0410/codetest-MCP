# codetest-mcp — AST & Linter MCP Server

정의서의 **이중 검증 분석기** 중 "정통적 오류" 축을 담당하는 MCP(stdio) 서버입니다.

> 정통적 오류(문법, 타입, 오탈자, 잘못된 변수 사용):
> **AST & Linter MCP가 Headless 모드로 실행하여 100% 팩트 기반 검출.**

## 제공 도구 (MCP Tools)

| 도구 | 설명 |
|---|---|
| `ast_check` | Tree-sitter 파스 트리의 `ERROR`/`MISSING` 노드, Python `compile()` SyntaxError 를 검출 |
| `lint_check` | 설치된 실제 린터(`ruff`, `eslint`)를 Headless 로 실행. 없으면 내장 결정적 규칙으로 폴백 |
| `health` | 서버 상태 및 사용 가능한 린터/문법 목록 |

모든 도구는 동일한 입력/출력 스키마를 씁니다.

```jsonc
// 입력
{ "files": [ { "path": "src/A.java", "content": "...", "language": "java" } ] }

// 출력
{ "findings": [
    { "file_path": "src/A.java", "line": 42, "severity": "error",
      "rule": "ast-parse-error", "message": "...", "source": "ast" }
] }
```

## 실행

```bash
pip install -e .
python -m codetest_mcp.server     # stdio 모드
```

agent-server 가 `CODETEST_MCP_COMMAND` / `CODETEST_MCP_ARGS` 설정으로 이 서버를
stdio 자식 프로세스로 기동합니다. 별도로 띄울 필요는 없고, 단독 실행은 디버깅 용도입니다.

## 배포 위치

**별도 관리 서버** — agent-server 와 같은 호스트에 배치합니다
(stdio 로 통신하므로 동일 머신에 있어야 합니다).
