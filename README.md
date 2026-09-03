# codetest-mcp — 코드 기반 처리 전담 MCP 서버

정의서:

> LLM을 사용하여 판단하는 부분은 Agent, **코드 기반으로 단순 처리 및 판단을 진행하는
> 부분은 MCP** 로 구분

이 서버는 **LLM 을 직접 호출하지 않는다.** 파서와 빌드 도구가 확정한 사실을 만들고,
LLM 판단이 필요한 부분만 Agent(FastAPI)로 넘긴다.

```
CLI(codereview_gitver)  →  MCP(codetest-MCP)  →  Agent(codetest)
      MCP 도구 호출            REST /api/v1/tests/*
```

## 담당 기능 (전부 정의서 근거)

| 기능 | 정의서 근거 |
|---|---|
| Git clone + AST 파싱 → 프로젝트 개요 DB 저장 | [상세] 1 |
| Git Diff + AST 로 변경된 코드 단위 식별 | (2) |
| 변경 영향도 / 메소드 추적 | 흐름 3 |
| **기능 중요도 High/Mid/Low 판정 + 판단 근거** | [UI] 4 |
| 생성된 Test Code 에 `@SpringBootTest` 주입 | (1) |
| Gradle + JaCoCo 로 Test Code 실행 | [상세] 4 |

**하지 않는 것**: 변경 의도 해석, 테스트 코드 작성, 결과 적절성 판단 — 전부 Agent(LLM)의
몫이라 `agent_client.py` 로 넘긴다.

## 도구 (`@mcp.tool`)

| 도구 | 설명 |
|---|---|
| `hello` | 연결 확인용 에코 |
| `register_project` | 프로젝트 등록 + 개요 수집(백그라운드) |
| `delete_project` | 프로젝트·그래프·작업 사본 삭제 |
| `test_generate` | 변경 분석 + 중요도 판정 + Test Code 생성 (CLI `codetest generate`) |
| `test_run` | 생성 + `@SpringBootTest` 실행 + 적절성 판정 (CLI `codetest run`) |
| `execute_tests` | `@SpringBootTest` 주입 + JaCoCo 실행 + 판정 (CLI `codetest test`) |

> 프로젝트 개요 조회(`get_project_overview`)와 변경 단위 식별(`analyze_changes`)은
> **도구로 노출하지 않는다.** CLI 가 직접 쓸 일이 없고, `test_generate` / `test_run` 이
> 내부에서(`orchestrator.analyze`) 만들어 Agent 프롬프트 입력으로 넘기는 중간
> 산출물이다. 개요 수집이 끝나지 않았다면 `test_generate` 응답의
> `analysis_warnings` 로 알려 준다.

### `test_generate(project_id, diff, sources)`

```jsonc
{
  // --- MCP 가 코드로 확정한 사실 ---
  "importance": "MID",
  "importance_rationale": "- 영향도 점수 30점 → MID\n- 사용자 노출 진입점 1개에 영향 (GET /orders) → 최소 MID",
  "base_package": "com.example.demo",
  "graph_ready": true, "analysis_warnings": [],

  // --- Agent(LLM)가 돌려준 판단 ---
  "intent": "조건 변경", "intent_rationale": "- quantity > 10 …",
  "thinking": "…", "test_cases": "- [정상] …\n- [실패] …",
  "test_code": "class GeneratedOrderTest { … }", "rationale": "- …", "target_code": "…"
}
```

기능 중요도는 `importance.py` 가 그래프 사실만으로 정한다 — 영향도 등급에 더해
사용자 노출 진입점·SQL 실행 지점이 걸리면 등급을 승격하고, **그 이유를 전부
`importance_rationale` 에 남긴다.** CLI 는 이 값을 결과 화면에 그대로 출력한다.

### `execute_tests(project_id, test_code, sources, base_package, diff, intent, intent_rationale)`

`test_code` 에 `@SpringBootTest` 가 없으면 **주입한 뒤** 실행한다. 필요한 import 와
`package` 선언도 보강하고, `src/test/java/<package>/<Class>.java` 로 저장해 돌린다.
`sources`(미커밋 변경분)를 작업 사본에 덮어쓴 뒤 실행하므로 개발자의 로컬 변경이
실제로 테스트된다. 실행이 끝나면 작업 사본을 원상 복구한다.

`diff` 를 함께 보내면 이번 실행 기준으로 기능 중요도를 다시 판정한다. 보내지 않으면
변경 구간을 알 수 없어 등급 근거가 비게 된다.

```jsonc
{
  // --- MCP 가 확정한 사실 ---
  "result": "PASS", "exit_code": 0, "passed": 3, "failed": 0, "total": 3, "failures": [],
  "coverage": {"line_rate": 80.0, "branch_rate": 100.0, "line_covered": 16, "line_missed": 4},
  "jacoco_enabled": true, "springboot_applied": true,
  "applied": ["@SpringBootTest 주입 (class GeneratedOrderTest)", "import 보강: …"],
  "test_file_path": "src/test/java/com/example/demo/GeneratedOrderTest.java",
  "importance": "MID", "importance_rationale": "- 영향도 점수 30점 → MID …",

  // --- Agent(LLM) 판단 ---
  "verdict": "적절", "verdict_rationale": "- 경계값이 모두 검증됨", "details": "…",
  "intent": "조건 변경", "intent_rationale": "- …"
}
```

### `test_run(project_id, diff, sources)`

분석 → 생성 → 실행 → 판정을 한 번에 수행하고 `{"generated": …, "report": …}` 로
돌려준다. CLI `codetest run` 이 부르는 도구다. 흐름 조립은 `orchestrator.py` 가 맡는다.

## 실행

```bash
pip install -e .
python -m codetest_mcp     # 기본: streamable-http, 0.0.0.0:80
```

MCP 를 띄우기 전에 **Agent 가 먼저 떠 있어야 한다** (`CODETEST_MCP_AGENT_URL`).

### CLI 쪽 등록

CLI 는 `CODETEST_SERVER_URL` 로 이 서버의 MCP 엔드포인트만 알면 된다.

```bash
export CODETEST_SERVER_URL="http://<host>:80/mcp"
export CODETEST_API_KEY="…"        # CODETEST_MCP_API_KEYS 중 하나
```

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODETEST_MCP_TRANSPORT` | `streamable-http` | `streamable-http` 또는 `stdio` |
| `CODETEST_MCP_AGENT_URL` | `http://localhost:8000` | Agent(LLM 판단) FastAPI 주소 |
| `CODETEST_MCP_AGENT_API_KEY` | (없음) | Agent 가 요구하는 `X-API-Key` |
| `CODETEST_MCP_AGENT_TIMEOUT` | `600` | Agent 응답 대기 시간(초) |
| `CODETEST_MCP_PORT` | `80` | 수신 포트. root 아니면 `8100` 등으로 바꿀 것 |
| `CODETEST_MCP_API_KEYS` | (없음) | Agent 인증 키(CSV). 비우면 인증 비활성화 |
| `CODETEST_MCP_DATABASE_URL` | `sqlite:///./data/codetest_mcp.db` | 개요/그래프 저장소 |
| `CODETEST_MCP_WORKSPACE_DIR` | `./workspace` | 대상 저장소 clone 위치 |
| `CODETEST_MCP_GRADLE_COMMAND` | `gradle` | `gradlew` 가 없을 때 쓸 실행 파일 |
| `CODETEST_MCP_TEST_TIMEOUT` | `900` | gradle test 최대 실행 시간(초) |

API Key 는 **http 전송일 때만** 검사한다 (`X-API-Key` 헤더). stdio 는 CLI 가 이
서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다.

대상 프로젝트에 `gradlew` 가 있으면 우선 사용한다. JaCoCo 커버리지는 프로젝트
`build.gradle` 에 `jacoco` 플러그인이 적용되어 있을 때만 수집된다.

## 테스트

```bash
python -m pytest tests/ -q
```

## 배포 위치

Agent 와 동일한 **별도 관리 서버**. 대상 저장소를 clone 하고 Gradle 을 실행하므로
JDK 와 Gradle 이 필요하다. Agent 에 REST 로 붙으므로 두 서비스가 서로 통신할 수
있어야 한다.
