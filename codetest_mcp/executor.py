"""
Gradle + JaCoCo 테스트 실행 (코드 기반, LLM 미사용).

정의서
  · "[상세] 4. JaCoCo와 @SpringBootTest 를 사용하여 Test Code 실행."
  · "(1) … 생성된 Test Code를 @SpringBootTest 에 넣고 실행시킨다."

실행 순서
  1. 작업 사본(clone)을 최신화한다
  2. 클라이언트가 보낸 변경 파일 본문을 작업 사본에 덮어쓴다
     (개발자의 미커밋 변경을 대상으로 테스트해야 하므로)
  3. @SpringBootTest 가 주입된 테스트를 src/test/java 아래에 쓴다
  4. gradle test (+ jacocoTestReport) 를 실행한다
  5. JUnit XML / JaCoCo XML 을 파싱해 사실만 반환한다

판정(적절성 여부)은 하지 않는다 — 그것은 Agent(LLM)의 몫이다.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from codetest_mcp.config import get_logger, settings
from codetest_mcp.springboot import PreparedTest

logger = get_logger(__name__)

#: 로그 반환 상한 — Gradle 출력은 수 MB 가 될 수 있다
MAX_OUTPUT_CHARS = 40_000

#: JaCoCo 리포트 위치 (Gradle 기본 규약)
_JACOCO_XML = Path("build") / "reports" / "jacoco" / "test" / "jacocoTestReport.xml"
#: JUnit 결과 XML 디렉터리
_JUNIT_DIR = Path("build") / "test-results" / "test"

#: build 파일에서 jacoco 적용 여부를 판정할 때 훑는 파일
_BUILD_FILES = ("build.gradle", "build.gradle.kts", "pom.xml")


@dataclass
class Coverage:
    """JaCoCo 커버리지 요약."""

    line_covered: int = 0
    line_missed: int = 0
    branch_covered: int = 0
    branch_missed: int = 0

    @property
    def line_rate(self) -> float:
        total = self.line_covered + self.line_missed
        return round(self.line_covered / total * 100, 2) if total else 0.0

    @property
    def branch_rate(self) -> float:
        total = self.branch_covered + self.branch_missed
        return round(self.branch_covered / total * 100, 2) if total else 0.0

    def to_dict(self) -> dict:
        return {
            "line_covered": self.line_covered,
            "line_missed": self.line_missed,
            "line_rate": self.line_rate,
            "branch_covered": self.branch_covered,
            "branch_missed": self.branch_missed,
            "branch_rate": self.branch_rate,
        }


@dataclass
class ExecutionResult:
    """테스트 실행의 객관적 결과. 적절성 판단은 포함하지 않는다."""

    exit_code: int = 0
    output: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)
    coverage: dict | None = None
    test_file_path: str = ""
    springboot_applied: bool = False
    applied: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    #: JaCoCo 리포트를 실제로 얻었는지 (build 설정에 jacoco 가 없으면 False)
    jacoco_enabled: bool = False


class ExecutionError(RuntimeError):
    """테스트를 실행할 수 없는 상태 (Gradle 부재 등)."""


def run_tests(
    repo_path: Path,
    prepared: PreparedTest,
    overlay_sources: list[tuple[str, str]] | None = None,
) -> ExecutionResult:
    """
    작업 사본에서 @SpringBootTest 를 실행하고 결과를 수집한다.

    :param repo_path:        대상 프로젝트 작업 사본
    :param prepared:         @SpringBootTest 주입을 마친 테스트
    :param overlay_sources:  [(경로, 본문)] — 실행 전에 덮어쓸 변경 파일
    """
    if not repo_path.is_dir():
        raise ExecutionError(f"작업 사본이 없습니다: {repo_path}")

    written: list[Path] = []
    try:
        for path, content in overlay_sources or []:
            target = _safe_write(repo_path, path, content)
            if target is not None:
                written.append(target)

        test_file = _safe_write(repo_path, prepared.file_path, prepared.source)
        if test_file is None:
            raise ExecutionError(f"테스트 파일 경로가 올바르지 않습니다: {prepared.file_path}")
        written.append(test_file)

        command = _build_command(repo_path, prepared)
        completed = _run(command, repo_path)

        result = ExecutionResult(
            exit_code=completed.returncode,
            output=_clip(completed.output),
            test_file_path=prepared.file_path,
            springboot_applied=prepared.springboot_applied,
            applied=list(prepared.applied),
            command=command,
            jacoco_enabled=_has_jacoco(repo_path),
        )
        _collect_junit(repo_path, result)
        _collect_jacoco(repo_path, result)
        return result
    finally:
        _restore(repo_path, written)


# ---------------------------------------------------------------------------
#  실행
# ---------------------------------------------------------------------------
@dataclass
class _Completed:
    returncode: int
    output: str


def _build_command(repo_path: Path, prepared: PreparedTest) -> list[str]:
    """
    gradle 실행 명령을 만든다.

    Gradle wrapper 가 있으면 우선 사용한다(프로젝트가 고정한 버전을 존중).
    생성된 테스트만 돌리도록 --tests 로 좁힌다.
    """
    launcher = _gradle_launcher(repo_path)
    fqcn = f"{prepared.package}.{prepared.class_name}" if prepared.package else prepared.class_name

    command = [*launcher, "test", "--tests", fqcn]
    if _has_jacoco(repo_path):
        # test 가 finalizedBy 로 걸어 두지 않은 프로젝트도 있으므로 명시한다.
        command.append("jacocoTestReport")
    command += ["--no-daemon", "--console=plain"]
    return command


def _gradle_launcher(repo_path: Path) -> list[str]:
    """./gradlew → 없으면 시스템 gradle."""
    wrapper = repo_path / "gradlew"
    if wrapper.is_file():
        return ["sh", str(wrapper)]

    gradle = shutil.which(settings.gradle_command)
    if gradle is None:
        raise ExecutionError(
            "Gradle 을 찾을 수 없습니다. 프로젝트에 gradlew 를 두거나 "
            "CODETEST_MCP_GRADLE_COMMAND 로 실행 파일을 지정하세요."
        )
    return [gradle]


def _run(command: list[str], cwd: Path) -> _Completed:
    try:
        completed = subprocess.run(  # noqa: S603 — gradle wrapper / 설정된 gradle
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=settings.test_timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ExecutionError(f"실행 파일을 찾을 수 없습니다: {command[0]} ({exc})") from None
    except subprocess.TimeoutExpired:
        return _Completed(
            returncode=124,
            output=f"테스트가 {settings.test_timeout_seconds}초 안에 끝나지 않아 중단했습니다.",
        )
    return _Completed(
        returncode=completed.returncode,
        output=(completed.stdout or "") + (completed.stderr or ""),
    )


def _has_jacoco(repo_path: Path) -> bool:
    """build 파일에 jacoco 가 적용되어 있는지 확인한다."""
    for name in _BUILD_FILES:
        candidate = repo_path / name
        if not candidate.is_file():
            continue
        try:
            if "jacoco" in candidate.read_text(encoding="utf-8", errors="ignore").lower():
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
#  결과 수집
# ---------------------------------------------------------------------------
def _collect_junit(repo_path: Path, result: ExecutionResult) -> None:
    """build/test-results/test/*.xml 을 합산한다."""
    results_dir = repo_path / _JUNIT_DIR
    if not results_dir.is_dir():
        return

    for xml_file in sorted(results_dir.glob("TEST-*.xml")):
        try:
            root = ET.parse(xml_file).getroot()
        except (ET.ParseError, OSError):
            continue

        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            total = int(suite.get("tests") or 0)
            failures = int(suite.get("failures") or 0)
            errors = int(suite.get("errors") or 0)
            skipped = int(suite.get("skipped") or 0)

            result.total += total
            result.failed += failures + errors
            result.skipped += skipped

            for case in suite.findall("testcase"):
                for tag in ("failure", "error"):
                    node = case.find(tag)
                    if node is None:
                        continue
                    name = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
                    message = (node.get("message") or node.tag).strip()
                    result.failures.append(f"{name}: {message}"[:500])

    result.passed = max(result.total - result.failed - result.skipped, 0)


def _collect_jacoco(repo_path: Path, result: ExecutionResult) -> None:
    """JaCoCo XML 리포트에서 LINE/BRANCH 카운터를 읽는다."""
    report = repo_path / _JACOCO_XML
    if not report.is_file():
        return

    try:
        # JaCoCo XML 은 DTD 를 참조하므로 파서가 외부를 타지 않도록 그대로 읽는다.
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError) as exc:
        logger.warning("JaCoCo 리포트 파싱 실패: %s", exc)
        return

    coverage = Coverage()
    # 최상위 report 요소의 counter 가 프로젝트 전체 합계다.
    for counter in root.findall("counter"):
        covered = int(counter.get("covered") or 0)
        missed = int(counter.get("missed") or 0)
        if counter.get("type") == "LINE":
            coverage.line_covered, coverage.line_missed = covered, missed
        elif counter.get("type") == "BRANCH":
            coverage.branch_covered, coverage.branch_missed = covered, missed

    result.coverage = coverage.to_dict()


# ---------------------------------------------------------------------------
#  파일 조작
# ---------------------------------------------------------------------------
def _safe_write(repo_path: Path, relative_path: str, content: str) -> Path | None:
    """작업 사본 안쪽에만 쓴다 (`..`/심볼릭 링크로 밖을 건드리지 못하게)."""
    root = repo_path.resolve()
    target = (repo_path / relative_path).resolve()
    if not target.is_relative_to(root):
        logger.warning("작업 디렉터리 밖 쓰기 차단: %s", relative_path)
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _restore(repo_path: Path, written: list[Path]) -> None:
    """
    실행 후 작업 사본을 원래 상태로 되돌린다.

    다음 실행이 이전 실행의 덮어쓴 파일에 오염되지 않도록 한다.
    """
    for path in written:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            continue

    # 덮어써서 사라진 원본을 git 으로 복구한다 (우리가 만든 clone 이므로 안전).
    try:
        subprocess.run(  # noqa: S603 — 고정 실행 파일(git)
            ["git", "checkout", "--", "."],
            cwd=str(repo_path),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("작업 사본 복구 실패: %s", exc)


def _clip(text: str) -> str:
    """앞뒤를 남기고 가운데를 잘라 실패 원인이 사라지지 않게 한다."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text.strip()
    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2 :]
    return f"{head}\n\n… (중략) …\n\n{tail}".strip()
