# Code Test AI Agent (`codetest`)

로컬 터미널에서 실행하는 **CLI 기반 AI 테스트 에이전트**입니다. Spring Boot 프로젝트의
변경된 코드를 감지하여, 변경 의도를 분석하고, 추론(chain-of-thought)을 거쳐
`@SpringBootTest` 테스트 코드를 자동 생성한 뒤, JaCoCo와 함께 실행하고 결과를
Terminal UI로 리포트합니다.

> 요구사항 정의서(`code-test.txt`)의 워크플로우를 구현한 MVP입니다.

## 워크플로우

```
Terminal 명령 입력
   └─▶ 1. 변경 소스 조회        (Git & File MCP: working-tree / staged, 공백·줄바꿈 노이즈 제외)
       2. 변경 메서드 분석       (AST & Flow MCP: 시그니처·의존 Bean·호출 순서만 필터링해 전달)
       3. 단 1회 LLM 호출        (의도/중요도 분석 근거 + @SpringBootTest 테스트 코드 동시 수신)
       4. 실행 및 정합성 판단     (Test Execution MCP: gradlew test + JaCoCo → 리포트)

   ※ 1~4단계는 모두 메모리상의 변수로 주고받으며, DB를 거치지 않습니다.
```

## 아키텍처

기능별 4계층으로 나누고, **의존 방향은 항상 아래로만** 흐릅니다.

```
cli  →  agent  →  mcp  →  storage
             ↘  models  ↙        (전 계층 공유 계약, 아무것도 import 하지 않음)
```

```
codetest/
├── config.py                  # 환경 변수, LLM 백엔드, 경로 설정
├── models.py                  # 전 계층 공유 도메인 모델 (+ MCP 전송 포맷)
│
├── cli/                       # [계층 1] 터미널 인터페이스 & UI
│   ├── cli_parser.py          #   명령어 파싱 (run / run --stage / generate / test / features)
│   ├── ui_renderer.py         #   [결과 예시] 렌더링 (<Test Code 보기>, High/Mid/Low)
│   └── test_txt_handler.py    #   src/test/test.txt 명령 처리 (워크플로는 agent에 위임)
│
├── agent/                     # [계층 2] Agent Core — 의사결정, 파이프라인, LLM 제어
│   ├── pipeline.py            #   4단계 오케스트레이션 + 정합성(validity) 판정
│   ├── change_analyzer.py     #   MCP Tool 호출 조립 → ChangeUnit / ChangeAnalysis
│   ├── prompt_engine.py       #   One-Shot CoT 프롬프트 빌드 + 단일 응답 파싱
│   ├── intent_rules.py        #   의도·중요도 규칙 (baseline · 폴백 · 병합)
│   └── llm/
│       ├── base_client.py     #   LLM 통신 인터페이스 (analyze_and_generate 단 하나)
│       └── mock_client.py     #   로컬 개발/테스트용 결정론적 Mock
│
├── mcp/                       # [계층 3] MCP Servers — 로컬 환경 제어 Tool 제공자
│   ├── base_server.py         #   JSON-RPC 프로토콜 + Tool 레지스트리
│   ├── client.py              #   Agent측 클라이언트 (inprocess / stdio)
│   ├── git_file/              # ① Git & File MCP Server
│   │   ├── server.py
│   │   ├── git_tool.py        #   Staged/Unstaged Diff 조회 (노이즈 필터링)
│   │   └── file_tool.py       #   소스 읽기, @SpringBootTest .java 생성/저장
│   ├── ast_flow/              # ② AST & Flow MCP Server
│   │   ├── server.py
│   │   ├── ast_tool.py        #   AST 파싱 (javalang → regex 폴백) + 캐시
│   │   ├── pruner.py          #   가지치기: 시그니처 / 의존 Bean / 호출 순서만 추출
│   │   └── flow_tool.py       #   다중 파일 호출 순서(Call Graph) 및 의존성 분석
│   └── test_exec/             # ③ Test Execution MCP Server
│       ├── server.py
│       ├── build_tool.py      #   ./gradlew test 1회 실행 트리거
│       └── jacoco_tool.py     #   JUnit/JaCoCo XML 파싱 (분기 커버리지·실패 로그)
│
└── storage/                   # [계층 4] 저장소 & 캐시 (기본 메모리, SQLite는 옵트인)
    ├── base_store.py          #   FeatureStore 프로토콜
    ├── memory_store.py        #   세션 기본값 — DB 미경유
    ├── db_manager.py          #   SQLite 연결 관리 (--persist)
    ├── schema.py              #   테이블 DDL
    └── cache_service.py       #   변경되지 않은 파일의 AST 캐싱 + 테스트 이력
```

**계층 규칙**

- `models.py`는 최상단에 둡니다. storage 하위에 두면 계층 1~3이 전부 계층 4에 의존하게 되어
  방향이 뒤집힙니다. 런타임 계약(`models.py`)과 테이블 DDL(`storage/schema.py`)도 분리했습니다.
- 변경 분석(`change_analyzer.py`)은 MCP Tool이 아니라 **에이전트**입니다. 어떤 Tool을 어떤
  순서로 부를지 정하는 의사결정이라, ast_tool 안에 넣으면 AST 서버가 git·ChangeUnit을
  알아야 해서 Tool 경계가 무너집니다.
- 정합성 판단(`judge_validity`)도 실행기가 아닌 에이전트에 둡니다. test_exec은 실행 사실만
  반환하고, 그 결과가 타당한지는 계층 2가 판단합니다.
- MCP 서버는 agent/cli를 절대 import 하지 않습니다 (직렬화 가능한 경계 유지).

### 1. 단일 API 호출 (분석 + 생성 통합)

`reason()` → `generate_test()` 2회 호출을 `analyze_and_generate()` **1회**로 통합했습니다.
한 번의 응답(`CombinedAnalysis`)이 두 결과물을 동시에 담습니다.

```python
combined = llm.analyze_and_generate(req)   # 유일한 API 호출
combined.analyses      # [의도/중요도 분석 근거]  → 리포트의 의도·중요도·근거
combined.test_source   # [테스트 코드]           → @SpringBootTest 클래스
combined.llm_calls     # 항상 1
```

프롬프트 조립과 응답 파싱은 `agent/prompt_engine.py` 한 곳에서만 일어나며, mock 백엔드도
동일한 `build_prompt` → `parse_response` 경로를 밟습니다. 응답 계약이 매 실행마다 검증되는 셈입니다.
모델이 일부 유닛을 빠뜨리거나 JSON이 깨져도 `intent_rules.apply_analyses()`가 규칙 기반
baseline으로 채워 넣기 때문에 라벨이 비는 유닛은 없습니다.

### 2. MCP Servers

세 서버 모두 독립 실행 가능하며, MCP 호스트에 그대로 등록할 수 있습니다.

| 서버 | 모듈 | Tool |
|------|------|------|
| ① Git & File | `codetest.mcp.git_file.server` | `git_scan_changes`, `file_read_source`, `file_write_test` |
| ② AST & Flow | `codetest.mcp.ast_flow.server` | `ast_parse_file`, `ast_method_context`, `ast_change_context`, `flow_summary` |
| ③ Test Exec | `codetest.mcp.test_exec.server` | `test_run`, `coverage_report` |

**가지치기(Pruning)** — AST/전체 소스를 그대로 넘기지 않고 3가지만 전달합니다.

| 전달 항목 | 예시 |
|-----------|------|
| 수정된 대상 메서드의 시그니처 | `public double calculateTotal(Order order)` |
| 의존 Bean 클래스 이름 목록 | `DiscountPolicy, OrderRepository` |
| 호출 순서 요약 | `DiscountPolicy.apply() → OrderRepository.save()` |

의존 Bean은 생성자 주입 / `@Autowired`·`@Resource`·`@Inject` / Spring 네이밍 규칙으로
판별하고, 호출 순서에서는 DTO·파라미터의 단순 getter/setter를 제외해 협력 흐름만 남깁니다.

**호출 순서 분석(flow_tool)** — 여러 파일이 함께 바뀌면 의존 그래프를 위상 정렬해 하나의
비즈니스 흐름으로 정리합니다. 순환이 있어도 계층 순위로 폴백하므로 멈추지 않습니다.

```
비즈니스 흐름: OrderController#total() → OrderService#calculateTotal()
```

전송 방식은 두 가지이며 결과는 동일합니다(같은 레지스트리를 거치므로 차이가 날 수 없습니다).

```bash
codetest run --mcp inprocess           # 기본: 같은 프로세스에서 Tool 직접 호출
codetest run --mcp stdio               # JSON-RPC 서브프로세스
codetest mcp-serve ast_flow            # 외부 MCP 호스트용 stdio 기동
# MCP 호스트 설정: {"command": "python", "args": ["-m", "codetest.mcp.ast_flow.server"]}
```

### 3. 메모리 기반 세션 파이프라인

CLI 한 번의 실행 안에서는 DB를 거치지 않고 파이썬 변수로만 데이터를 주고받습니다.
`pipeline.build_report()`가 세션 스토어 하나를 만들어 각 단계에 넘기며, 기본값은
`MemoryFeatureStore`라 `.codetest/features.db` 파일 자체가 생성되지 않습니다.
AST 캐시(`cache_service.py`)도 같은 원칙으로 기본은 프로세스 메모리이며, 파일 지문
(크기+mtime)이 바뀌지 않은 파일만 재사용합니다.

```bash
codetest run              # 메모리 전용 (기본)
codetest run --persist    # 동일 인터페이스로 SQLite에도 기록 → codetest features 로 조회
```

### 4. 공백·줄바꿈 변경 무시

diff 추출 시 `--ignore-all-space --ignore-space-at-eol --ignore-blank-lines`를 기본 적용하고,
파싱 단계에서도 빈 줄만 추가/삭제된 라인을 제외합니다. 들여쓰기만 바뀐 파일은 아예
분석 대상에서 빠지며, 어떤 파일이 제외됐는지는 리포트에 표시됩니다.

```
• 공백/줄바꿈만 변경되어 제외한 파일: src/main/java/com/example/demo/controller/OrderController.java
```

`--no-ignore-whitespace` 로 끄면 기존처럼 모든 변경을 분석합니다.

## 설치

```bash
cd codetest-agent
python -m pip install -e .
# 또는: python -m pip install -r requirements.txt  (그 후 `python -m codetest`)
```

## 명령어

| 명령 | 설명 |
|------|------|
| `codetest run` | 로컬에서 staging에 올라가지 않은(working-tree) 변경 파일에 대해 테스트 생성+실행+리포트 |
| `codetest run --stage` | staging(git staged) 단계 파일에 대해 테스트 생성+실행+리포트 |
| `codetest generate` | Git Working Tree 변경분에 대해 **테스트 코드만** 생성 |
| `codetest test` | `<project>/src/test/test.txt` 의 테스트를 실행하고 리포트 |
| `codetest features` | `--persist` 로 저장된 feature 목록 확인 |
| `codetest mcp-serve <server>` | MCP 서버를 stdio로 기동 (`git_file`/`ast_flow`/`test_exec`) |

공통 옵션: `--project/-p <경로>` (대상 프로젝트, 기본 cwd), `--llm mock|claude`,
`--show code|result|all` (비대화형 펼치기), `--no-interactive`.

`run`/`generate` 추가 옵션:

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--persist` | off | 세션 데이터를 SQLite에도 기록 (기본은 메모리 전용) |
| `--ignore-whitespace` / `--no-ignore-whitespace` | on | 공백·빈 줄만 바뀐 변경 무시 |
| `--mcp inprocess\|stdio` | `inprocess` | MCP 서버 연결 방식 |

환경변수로도 지정할 수 있습니다: `CODETEST_LLM`, `CODETEST_PERSIST`,
`CODETEST_IGNORE_WHITESPACE`, `CODETEST_IGNORE_BLANK_LINES`, `CODETEST_MCP`, `CODETEST_CACHE`.

> 정의서의 `codetest run - stage` 표기는 `codetest run --stage` (별칭 `-stage`)로 구현했습니다.

## 빠른 시작 (샘플 프로젝트)

`sample-springboot/` 에 대상 Spring Boot 프로젝트가 포함되어 있습니다.

```bash
# 1) 샘플을 git 저장소로 만들고 베이스라인 커밋
cd sample-springboot
git init && git add -A && git commit -m "baseline"

# 2) 소스를 변경 (예: OrderService에 할인 로직 추가) 후
codetest run -p .          # working-tree 변경 감지 → 생성 → (Gradle 있으면)실행 → 리포트
codetest generate -p .     # 생성만
codetest test -p .         # src/test/test.txt 실행
```

Java/Gradle이 없는 환경에서는 실행 단계가 **SIMULATED** 로 표시되며(테스트는 실제로
컴파일/실행되지 않음), 리포트에 실제 실행용 Gradle 명령이 안내됩니다.

## LLM 백엔드

기본값은 결정론적 `mock` 입니다. 실제 Claude 연동은 `codetest/agent/llm/` 에
`claude_client.py` 를 추가하고 `--llm claude` (또는 `CODETEST_LLM=claude`) 로 전환하도록
인터페이스만 열어두었습니다. 구현해야 할 메서드는
`analyze_and_generate(req) -> CombinedAnalysis` 하나뿐이고, 프롬프트 조립과 응답 파싱은
`prompt_engine.build_prompt()` / `parse_response()` 를 그대로 재사용하면 됩니다.
요청에는 MCP가 가지치기한 컨텍스트와 변경 라인만 담기므로, 프롬프트 크기가 파일 크기와
무관하게 유지됩니다.

## 테스트

계층별로 파일을 나눠 두었습니다.

```bash
python -m pytest tests/                     # 전체
python -m pytest tests/test_mcp_git_file.py # ① Git & File MCP
python -m pytest tests/test_mcp_ast_flow.py # ② AST & Flow MCP (가지치기·호출 순서)
python -m pytest tests/test_mcp_test_exec.py# ③ Test Execution MCP (JaCoCo 파싱)
python -m pytest tests/test_agent.py        # 단일 호출·프롬프트 계약·의도 규칙
python -m pytest tests/test_storage.py      # 메모리/SQLite 스토어, AST 캐시
python -m pytest tests/test_cli_e2e.py      # CLI end-to-end (4계층 관통)
```
