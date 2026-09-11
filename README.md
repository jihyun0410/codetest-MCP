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
| 커밋 소스 스냅샷 + AST 파싱 → 프로젝트 개요 DB 저장 | [상세] 1 |
| Git Diff + AST 로 변경된 코드 단위 식별 | (2) |
| 변경 영향도 / 메소드 추적 | 흐름 3 |
| **기능 중요도 High/Mid/Low 판정 + 판단 근거** | [UI] 4 |
| 생성된 Test Code 에 `@SpringBootTest` 주입 | (1) |
| Gradle + JaCoCo 실행 결과(CLI 가 보내온 사실)로 리포트 구성 | [상세] 4 |

**하지 않는 것**: 변경 의도 해석, 테스트 코드 작성, 결과 적절성 판단 — 전부 Agent(LLM)의
몫이라 `agent_client.py` 로 넘긴다.

## 도구 (`@mcp.tool`)

| 도구 | 설명 |
|---|---|
| `hello` | 연결 확인용 에코 |
| `register_project` | 프로젝트 등록 + **커밋 소스 스냅샷 저장** + 개요 수집(백그라운드). 같은 이름·같은 git_url 로 다시 부르면 기존 프로젝트를 그대로 돌려주고 스냅샷을 갱신한다 |
| `delete_project` | 프로젝트·그래프·작업 사본 삭제 |
| `test_generate` | 변경 분석 + 중요도 판정 + Test Code 생성 (CLI `codetest generate`) |
| `prepare_test` | `@SpringBootTest` 주입 + 저장 경로 계산 (실행 전 1단계) |
| `report_execution` | 로컬 실행 결과 → 중요도 재판정 + 적절성 판정 (실행 후 2단계) |

> **테스트 실행은 이 서버가 하지 않는다.** CLI 가 개발자 PC 의 프로젝트에서 Gradle 로
> 돌리고 그 결과만 `report_execution` 으로 보내온다. 개발자가 방금 고친 코드가 그대로
> 있는 작업 트리라 사본을 만들 필요가 없고, MCP 서버에 JDK·Gradle 이 필요 없다.
>
> ```
> codetest run / test
>   1. prepare_test      MCP  @SpringBootTest 주입, 경로 계산   (git·JDK·Gradle 불필요)
>   2. gradle test       CLI  개발자 PC 의 프로젝트에서 실행
>   3. report_execution  MCP  중요도 재판정 → Agent 적절성 판정
> ```

> 프로젝트 개요 조회(`get_project_overview`)와 변경 단위 식별(`analyze_changes`)은
> **도구로 노출하지 않는다.** CLI 가 직접 쓸 일이 없고, `test_generate` / `report_execution` 이
> 내부에서(`orchestrator.analyze`) 만들어 Agent 프롬프트 입력으로 넘기는 중간
> 산출물이다. 개요 수집이 끝나지 않았다면 `test_generate` 응답의
> `analysis_warnings` 로 알려 준다.

> `register_project` 는 **멱등**하다. CLI 는 `project_id` 를 로컬
> `.codetest/config.json` 에만 두는데 이 파일은 `.gitignore` 대상이라 clone·PC 교체로
> 쉽게 사라진다. 그때 재등록을 거부하면 CLI 가 id 를 되찾을 길이 없어
> "등록된 프로젝트가 없습니다" → register → "이미 있습니다" 가 무한 반복된다.
> 그래서 같은 이름·같은 저장소면 기존 프로젝트를 돌려주고, 이름만 같고 저장소가
> 다르면 그대로 거부한다. 지난 개요 수집이 FAILED 였다면 이때 다시 시작한다.

## 커밋 소스 스냅샷 — Agent 가 "현재 코드" 를 보게 하는 장치

CLI 의 `generate`/`run`/`test` 는 `git diff HEAD` 기준이라 **미커밋 변경분만** 보낸다.
그것만 Agent 에 넘기면 LLM 이 변경 지점이 호출하는 커밋된 구현을 못 봐서 구조만 보고
테스트를 짜게 된다.

```
register  : CLI → 커밋된 소스 전체 → MCP 가 project_files 에 저장
generate  : CLI → 미커밋 변경분만  → MCP 가 스냅샷 위에 덮어 "현재 코드" 를 구성 → Agent
run/test  : 같은 방식. 실행은 CLI 가 개발자 PC 의 작업 트리에서 하므로 MCP 는
            리포트를 만들 "현재 코드" 만 조립한다
```

Agent 에 실어 보낼 파일은 이 순서로 고르고 `MAX_CONTEXT_FILES`(40개)와
`MAX_CONTEXT_TOTAL_CHARS`(45,000자) 중 먼저 걸리는 쪽에서 끊는다. 프롬프트 길이는
그대로 생성 시간이 되므로 "바뀐 곳과 그에 닿는 곳" 으로 좁힌다.

1. 변경 파일 자체
2. 그래프가 짚은 변경 단위·영향 단위·영향 파일
3. 변경 코드가 **이름으로 참조**하는 커밋 파일 — 그래프가 비었을 때(수집 미완료·수집
   실패)의 대비다. 이게 없으면 그래프 장애가 곧 품질 저하로 이어진다
4. 같은 패키지의 커밋 파일 — 남는 자리를 채운다

**변경 파일은 예산과 무관하게 항상 싣는다** — 그게 테스트의 대상이다. 맥락 파일은
남는 자리에만 넣고, 예산을 넘으면 잘라 넣지 않고 통째로 뺀다. 메서드 중간에서
끊긴 클래스를 보내면 모델이 그것을 파일 전체로 믿고 없는 시그니처를 지어낸다.

### 스냅샷은 개요 수집(AST)에도 쓴다 — **MCP 서버에 git 이 필요 없다**

등록 직후 도는 개요 수집(`run_ingest` → `GraphBuilder.build_full`)은 예전에 저장소를
clone 해서 파싱했다. 지금은 방금 저장한 스냅샷을 그대로 파싱한다. 그래서 이 서버에
git 실행 파일도, 저장소 접근 권한도 필요 없다. `build.gradle` / `pom.xml` 같은 빌드
파일도 스냅샷에 함께 오므로 프레임워크 판정 역시 clone 없이 된다.

clone 은 **스냅샷이 없을 때만** 하는 하위 호환 경로다 — 스냅샷을 보내지 않던 예전
CLI 로 등록한 프로젝트가 그렇다. 이때는 서버에 git 이 있어야 하고, 없으면 개요 수집이
`FAILED` 로 남는다(`Bad git executable`). 최신 CLI 로 `codetest project register` 를
다시 실행하면 스냅샷이 채워져 clone 없이 수집된다.

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

### `prepare_test(project_id, test_code, base_package)`

`codetest run` / `codetest test` **1단계**. `test_code` 에 `@SpringBootTest` 가 없으면
주입하고, 필요한 import 와 `package` 선언도 보강한 뒤 `src/test/java/<package>/<Class>.java`
저장 경로를 계산해 돌려준다. 문자열 변환뿐이라 git·JDK·Gradle 이 필요 없다.

```jsonc
{
  "source": "package com.example.demo;\n\n@SpringBootTest\nclass GeneratedOrderTest { … }",
  "file_path": "src/test/java/com/example/demo/GeneratedOrderTest.java",
  "class_name": "GeneratedOrderTest", "package": "com.example.demo",
  "springboot_applied": true,
  "applied": ["@SpringBootTest 주입 (class GeneratedOrderTest)", "import 보강: …"]
}
```

주석·문자열 안의 `@SpringBootTest` 는 주입 여부 판정에서 제외한다. 주석으로 적어 둔
한 줄 때문에 실제 주입이 건너뛰어지면 테스트가 Spring 컨텍스트 없이 돌아 깨진다.

### `report_execution(project_id, execution, test_code, diff, sources, intent, intent_rationale)`

**2단계**. CLI 가 개발자 PC 에서 Gradle 로 돌린 결과(`execution`)를 사실로 그대로 받고,
기능 중요도만 MCP 가 코드로 다시 판정한 뒤 결과 적절성을 Agent(LLM)에 묻는다.
`diff` 를 함께 보내야 이번 실행 기준으로 등급 근거가 채워진다.

`result` 는 **gradle 종료 코드**가 정한다 (`exit_code == 0` 이면 PASS). 컴파일이
깨지면 테스트가 한 건도 안 돌아 `total/passed/failed` 가 전부 0 인 채로 FAIL 이
되는데, 그 이유는 CLI 가 보내온 `build_errors` 에만 있다. 그래서 이 값은 리포트와
Agent 프롬프트 양쪽에 그대로 싣는다 — 빼면 LLM 이 "실패 0건이니 통과" 로 읽는다.

```jsonc
{
  // --- CLI 가 보내온 실행 사실 ---
  "result": "PASS", "exit_code": 0, "passed": 3, "failed": 0, "total": 3, "failures": [],
  "coverage": {"line_rate": 80.0, "branch_rate": 100.0, "line_covered": 16, "line_missed": 4},
  "jacoco_enabled": true, "springboot_applied": true,
  "applied": ["@SpringBootTest 주입 (class GeneratedOrderTest)", "import 보강: …"],
  "test_file_path": "src/test/java/com/example/demo/GeneratedOrderTest.java",
  "build_errors": [],   // 비어 있지 않으면 테스트가 시작조차 못한 것이다

  // --- MCP 가 코드로 확정한 사실 ---
  "importance": "MID", "importance_rationale": "- 영향도 점수 30점 → MID …",

  // --- Agent(LLM) 판단 ---
  "verdict": "적절", "verdict_rationale": "- 경계값이 모두 검증됨", "details": "…",
  "intent": "조건 변경", "intent_rationale": "- …"
}
```

`codetest run` 은 `test_generate` → `prepare_test` → (로컬 Gradle) → `report_execution`
순서로 이 도구들을 이어 붙인다. 흐름 조립은 `orchestrator.py` 가 맡는다.

## Agent 호출 — 504 Gateway Time-out 을 만드는 것

앞단 nginx 의 `proxy_read_timeout` 은 **총 소요 시간이 아니라 무응답 시간**이다.
예전에는 Agent 가 LLM 을 다 기다린 뒤 JSON 을 한 덩어리로 보냈으므로, 생성이 3분
걸리면 그 3분 내내 이 구간이 조용했다 — 60초에 프록시가 끊고 504 를 만든다.
`CODETEST_MCP_AGENT_GENERATE_TIMEOUT` 을 아무리 늘려도 소용이 없었던 이유다
(프록시가 먼저 끊으므로 그 값은 애초에 도달하지 않는다).

이제 `agent_client` 는 `Accept: application/x-ndjson` 으로 요청해 생성 중에도
`{"type":"ping"}` 줄을 계속 받는다. ping 은 버리고 마지막 `result`/`error` 줄만 쓴다.
Accept 를 이해하지 못하는 예전 Agent 는 예전처럼 JSON 으로 답하고, 그것도 그대로 받는다.

측정값 (LLM 이 25초 걸리는 스텁):

| 구간 | 바이트 사이 최대 침묵 |
|---|---|
| MCP → Agent, 수정 전 | **25.1s** (= 생성 시간 그대로) |
| MCP → Agent, 수정 후 | **10.1s** (= ping 간격) |
| CLI → MCP | 15.0s (sse-starlette 가 SSE 에 자체 ping 을 넣는다) |

그래서 타임아웃 설정도 총 시간이 아니라 무응답 시간으로 바꿨다
(`CODETEST_MCP_AGENT_STREAM_IDLE`). 오래 걸리는 생성은 기다리고, 조용히 죽은
Agent 는 2분 만에 포기한다.

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
| `CODETEST_MCP_AGENT_TIMEOUT` | `60` | Agent 일반 요청(헬스 등) 대기 시간(초) |
| `CODETEST_MCP_AGENT_STREAM_IDLE` | `120` | LLM 호출의 **무응답** 상한(초). 총 소요 시간이 아니다 |
| `CODETEST_MCP_PORT` | `80` | 수신 포트. root 아니면 `8100` 등으로 바꿀 것 |
| `CODETEST_MCP_API_KEYS` | (없음) | Agent 인증 키(CSV). 비우면 인증 비활성화 |
| `CODETEST_MCP_DATABASE_URL` | `sqlite:///./data/codetest_mcp.db` | 개요/그래프 저장소 |
| `CODETEST_MCP_WORKSPACE_DIR` | `./workspace` | clone 대비용 작업 디렉터리. 스냅샷 경로에서는 비어 있다 |

API Key 는 **http 전송일 때만** 검사한다 (`X-API-Key` 헤더). stdio 는 CLI 가 이
서버를 자식 프로세스로 띄운 것이라 신뢰 경계가 아니다.

`gradlew` 선택과 JaCoCo 커버리지 수집은 실행을 맡은 **CLI 쪽 관심사**다
(`codereview_gitver/local-client/codetest/executor.py`). 커버리지는 개발자 프로젝트
`build.gradle` 에 `jacoco` 플러그인이 적용되어 있을 때만 붙는다.

## 테스트

```bash
python -m pytest tests/ -q
```

## 배포 위치

Agent 와 동일한 **별도 관리 서버**. 필요한 것은 Python 런타임과 Agent 로 나가는
네트워크뿐이다.

| | 필요한가 | 이유 |
|---|---|---|
| Python 3.10+ | O | 이 서버 자체 |
| Agent 로의 REST 접근 | O | LLM 판단을 전부 위임한다 |
| JDK / Gradle | X | 테스트는 개발자 PC 의 CLI 가 돌린다 |
| git | X | 개요 수집은 CLI 가 올려 준 커밋 스냅샷으로 한다 |
| 대상 저장소 접근 권한 | X | 소스는 CLI 가 실어 보낸다 |

git 이 필요한 경우는 하나뿐이다 — 스냅샷 없이 등록된 예전 프로젝트를 clone 할 때.
최신 CLI 로 다시 등록하면 그마저 사라진다.
