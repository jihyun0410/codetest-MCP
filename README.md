# codetest-mcp — 코드 기반 처리 전담 MCP 서버

정의서:

> LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을 진행하는
> 부분은 MCP** 로 구분

이 서버는 **LLM 을 전혀 호출하지 않는다.** 파서와 빌드 도구가 확정한 사실만 만들어
Agent 에게 돌려준다. Agent 는 MCP 클라이언트로 붙어 도구를 호출한다.

## 담당 기능 (전부 정의서 근거)

| 기능 | 정의서 근거 |
|---|---|
| Git clone + AST 파싱 → 프로젝트 개요 DB 저장 | [상세] 1 |
| Git Diff + AST 로 변경된 코드 단위 식별 → Agent 전달 | (2) |
| 변경 영향도 / 메소드 추적 → Agent 전달 | 흐름 3 |
| 생성된 Test Code 에 `@SpringBootTest` 주입 | (1) |
| Gradle + JaCoCo 로 Test Code 실행 | [상세] 4 |

**하지 않는 것**: 변경 의도 해석, 중요도 판정, 테스트 코드 작성, 결과 적절성 판단
— 전부 Agent(LLM)의 몫이다. `test_generate` / `test_run` 은 그 판단에 필요한 사실을
정리해 `CODETEST_MCP_AGENT_BASE_URL` 로 넘기고 결과를 받아온다.

## 구조

```
CLI Server  ──command──▶  MCP  ──LLM 이 필요한 것만──▶  AI Agent
                           ◀── LLM 처리 결과 ──────────
```

MCP 는 LLM 을 호출하지 않는다. 코드로 확정 가능한 일(clone/AST/gradle)은 직접
하고, 판단이 필요한 일만 Agent 로 넘긴다. **모든 command 가 Agent 로 나가지는
않는다.**

| command | Agent 송신 | event | 비고 |
|---|---|---|---|
| `hello` | ✗ | — | 로컬 에코 |
| `register_project` | △ | `ingest_completed` | 수집 완료 후. 백그라운드라 응답에 못 실음 |
| `delete_project` | ✗ | — | 삭제 = 코드 작업 |
| `test_generate` | ✓ | `test_generate_requested` | 코드 생성 = LLM |
| `test_run` | ✓ | `test_generate_requested` | 생성만. 실행은 MCP 가 직접 |
| `execute_tests` | ✗ | — | gradle 실행 = 코드 작업 |

송신은 전부 `POST {CODETEST_MCP_AGENT_BASE_URL}` 한 곳으로 나가고, 구분은 경로가
아니라 본문의 `event` 필드로 한다. 코드에서는 `>>> Agent API 송신 <<<` 주석으로
표시했다.

Agent 응답 중 `test_code` 를 뺀 나머지는 **버리지 않고** `analysis` 로 그대로
올라온다. 영향도 해석·요약처럼 LLM 이 만든 내용을 터미널에 보여주기 위한 것이라
MCP 가 규격을 정하지 않는다.

## 도구 (`@mcp.tool`)

| 도구 | 설명 |
|---|---|
| `hello` | 연결 확인용 에코 |
| `register_project` | 프로젝트 등록 + 개요 수집(백그라운드) |
| `project_status` | 등록·수집 성공 여부 확인 |
| `delete_project` | 프로젝트·그래프·작업 사본 삭제 |
| `test_generate` | 컨텍스트 정리 → Agent 가 테스트 코드 생성 |
| `test_run` | 생성부터 실행까지 전 과정 |
| `execute_tests` | 이미 있는 테스트 코드를 실행만 |

### `register_project(...)` / `project_status(project_id)`

`register_project` 는 개요 수집을 **백그라운드로** 돌리고 즉시 `PENDING` 을 반환한다.
실제 성공 여부는 `project_status` 로 확인한다.

| `ingest_status` | 뜻 |
|---|---|
| `PENDING` | 등록됨, 수집 대기 |
| `RUNNING` | 수집 중 |
| `READY` | 수집 완료 — `frameworks`, `last_indexed_at` 이 채워진다 |
| `FAILED` | 수집 실패 — `ingest_error` 에 사유 |

수집 스레드에서 어떤 예외가 나든 `FAILED` 로 기록된다. 예외가 스레드 밖으로
새어나가면 파이썬은 스택트레이스만 찍고 끝내므로, 그대로 두면 DB 가 `PENDING`
인 채 남아 호출자가 실패를 영원히 알 수 없다.

### `test_generate(project_id, diff, sources)`

테스트를 위한 코드를 생성해 돌려준다. **생성 자체는 Agent(LLM)가 한다.** MCP 는
생성에 필요한 사실을 정리해 Agent 로 넘기고, 돌려받은 코드를 반환한다. 실행은
하지 않는다.

```jsonc
// 응답
{
  "test_code": "…Agent 가 생성한 Java 소스…",
  "analysis": {                         // Agent 응답 중 test_code 외 전부
    "impact": "calculateTotal 에 할인 분기 추가", "risk": "MEDIUM",
    "suggested_cases": ["수량 10 이하", "수량 11 이상"]
  },
  "context": {                          // 무엇을 보고 생성했는지
    "base_package": "com.example.demo", "frameworks": ["Spring Boot"],
    "changed_units":  [{"qualified_name": "…OrderService#calculateTotal(Order)",
                        "node_type": "Method", "start_line": 6, "end_line": 12}],
    "impacted_units": [{"qualified_name": "…", "depth": 1, "via": "Calls"}],
    "sources": [{"path": "…", "content": "…"}],
    "graph_ready": true, "ingest_status": "READY", "warnings": []
  }
}
```

### `test_run(project_id, diff, sources, base_package)`

**코드 생성부터 실제 실행까지 한 호출로 처리한다.** `test_generate` 의 결과에
`@SpringBootTest` 를 주입하고 JaCoCo 와 함께 돌린 뒤, 생성 근거와 실행 사실을
함께 반환한다.

```jsonc
{ "test_code": "…", "analysis": { … }, "context": { … },
  "execution": { "passed": 3, "coverage": {…}, … } }
```

LLM 생성 + gradle 빌드가 연달아 일어나므로 응답까지 **수 분**이 걸릴 수 있다.
MCP 클라이언트 타임아웃을 넉넉히 잡아라.

### `execute_tests(project_id, test_code, sources, base_package)`

`test_code` 에 `@SpringBootTest` 가 없으면 **주입한 뒤** 실행한다. 필요한 import 와
`package` 선언도 보강하고, `src/test/java/<package>/<Class>.java` 로 저장해 돌린다.
`sources`(미커밋 변경분)를 작업 사본에 덮어쓴 뒤 실행하므로 개발자의 로컬 변경이
실제로 테스트된다. 실행이 끝나면 작업 사본을 원상 복구한다.

```jsonc
// 응답 — 판정 없이 사실만
{
  "exit_code": 0, "passed": 3, "failed": 0, "total": 3, "failures": [],
  "coverage": {"line_rate": 80.0, "branch_rate": 100.0, "line_covered": 16, "line_missed": 4},
  "jacoco_enabled": true, "springboot_applied": true,
  "applied": ["@SpringBootTest 주입 (class GeneratedOrderTest)", "import 보강: …"],
  "test_file_path": "src/test/java/com/example/demo/GeneratedOrderTest.java",
  "command": ["sh", "./gradlew", "test", "--tests", "…", "jacocoTestReport"]
}
```

## 실행

```bash
pip install -e .
python -m src     # 기본: http(streamable), 0.0.0.0:8100
```

### Agent 쪽 등록

```jsonc
// http — 별도 서버로 띄운 경우
{"mcpServers": {"codetest": {
  "url": "http://<서비스명>/mcp",     // Service:80 -> container:8100
  "headers": {"X-API-Key": "…"}
}}}

// stdio — Agent 가 자식 프로세스로 띄우는 경우
{"mcpServers": {"codetest": {
  "command": "python", "args": ["-m", "src"],
  "env": {"CODETEST_MCP_TRANSPORT": "stdio"}
}}}
```

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODETEST_MCP_TRANSPORT` | `http` | `http`(=streamable-http) 또는 `stdio` |
| `CODETEST_MCP_PORT` | `8100` | 수신 포트 (Service 80 -> targetPort http) |
| `CODETEST_MCP_AGENT_BASE_URL` | `http://maxis-proxy.mks01.test.com:80/agent/1121365` | 개요 수집 완료 시 결과를 POST 할 Agent 주소. 비우면 통보 안 함 |
| `CODETEST_MCP_AGENT_TIMEOUT` | `120` | Agent 의 코드 생성 응답 대기(초) |
| `CODETEST_MCP_API_KEYS` | (없음) | Agent 인증 키(CSV). 비우면 인증 비활성화 |
| `CODETEST_MCP_DATABASE_URL` | `sqlite:///./data/codetest_mcp.db` | 개요/그래프 저장소 |
| `CODETEST_MCP_WORKSPACE_DIR` | `./workspace` | 대상 저장소 clone 위치 |
| `CODETEST_MCP_GRADLE_COMMAND` | `gradle` | `gradlew` 가 없을 때 쓸 실행 파일 |
| `CODETEST_MCP_TEST_TIMEOUT` | `900` | gradle test 최대 실행 시간(초) |

API Key 는 **http 전송일 때만** 검사한다 (`X-API-Key` 헤더). stdio 는 Agent 가 이
서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다.

대상 프로젝트에 `gradlew` 가 있으면 우선 사용한다. JaCoCo 커버리지는 프로젝트
`build.gradle` 에 `jacoco` 플러그인이 적용되어 있을 때만 수집된다.

## Agent 로 보내는 통보

평소에는 Agent 가 도구를 호출하고 MCP 가 응답하는 단방향이다. 예외가 하나 있다:
`register_project` 는 개요 수집을 **백그라운드로** 돌리므로 Agent 가 완료 시점을
알 방법이 폴링밖에 없다. 그래서 수집이 끝나면 `CODETEST_MCP_AGENT_BASE_URL` 로
결과를 POST 한다.

```jsonc
// 성공
{ "event": "ingest_completed", "project_id": "…", "name": "demo", "status": "READY",
  "frameworks": ["Spring Boot"], "language_stats": {…}, "node_count": 812, "edge_count": 1934 }

// 실패
{ "event": "ingest_completed", "project_id": "…", "name": "demo", "status": "FAILED",
  "error": "…" }
```

통보 실패는 무시한다(로그만 남김). 수집 결과는 이미 DB 에 있고 Agent 는
`test_generate`/`test_run` 응답의 `context.ingest_status` 로도 같은 상태를 읽을 수
있으므로, Agent 가 죽어 있다고 해서
수집을 실패로 만들 이유가 없다. 타임아웃은 10초 고정이다.

## 테스트

```bash
python -m pytest tests/ -q
```

## 배포 위치

Agent 와 동일한 **별도 관리 서버**. 대상 저장소를 clone 하고 Gradle 을 실행하므로
JDK 와 Gradle 이 필요하다.
