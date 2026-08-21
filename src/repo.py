"""
대상 저장소 관리 (clone / fetch / checkout / diff / 파일 읽기).

정의서: "Project가 최초로 등록되는 시점에 Project 정보에 있는 Git URL에서
전체 소스 코드를 가져옵니다."
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from src.config import settings
from src.config import get_logger
from src.parsing.registry import EXCLUDED_DIRS, is_supported

logger = get_logger(__name__)


@dataclass
class SourceFile:
    """저장소에서 읽어온 소스 파일 1개."""

    path: str      # 저장소 루트 기준 상대 경로 (POSIX)
    content: str


class RepoError(RuntimeError):
    """저장소 작업 실패."""


class RepoService:
    """
    프로젝트 1개에 대응하는 로컬 작업 사본을 관리한다.

    작업 경로: <workspace_dir>/<project_id>
    """

    def __init__(self, project_id: str, git_url: str, github_token: str | None = None) -> None:
        self.project_id = project_id
        self.git_url = git_url
        self.github_token = github_token
        self.path: Path = settings.workspace_dir / project_id

    # ------------------------------------------------------------------
    #  Git 조작
    # ------------------------------------------------------------------
    @property
    def authenticated_url(self) -> str:
        """
        Private 저장소 접근을 위해 https URL 에 토큰을 주입한다.

        토큰은 URL 에만 임시로 실리고 원격(remote)에는 저장하지 않는다.
        """
        if not self.github_token or not self.git_url.startswith("http"):
            return self.git_url
        parsed = urlparse(self.git_url)
        netloc = f"x-access-token:{quote(self.github_token, safe='')}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    def ensure_clone(self, branch: str | None = None) -> Path:
        """
        작업 사본이 없으면 clone, 있으면 fetch + reset 으로 최신화한다.

        blob:none 부분 클론을 쓰지 않고 전체 트리를 받는다 — AST 파싱에 본문이 필요하다.
        """
        from git import GitCommandError, Repo  # 지역 import: 기동 속도 확보

        try:
            if (self.path / ".git").exists():
                repo = Repo(self.path)
                with repo.git.custom_environment(GIT_TERMINAL_PROMPT="0"):
                    repo.git.remote("set-url", "origin", self.authenticated_url)
                    repo.git.fetch("--all", "--prune")
                    target = branch or repo.active_branch.name
                    repo.git.checkout(target)
                    repo.git.reset("--hard", f"origin/{target}")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                kwargs = {"branch": branch} if branch else {}
                Repo.clone_from(self.authenticated_url, self.path, **kwargs)
        except GitCommandError as exc:
            # 토큰이 로그에 남지 않도록 메시지에서 제거
            raise RepoError(_sanitize(str(exc), self.github_token)) from None
        except Exception as exc:
            raise RepoError(_sanitize(str(exc), self.github_token)) from None

        return self.path

    def checkout(self, ref: str) -> None:
        """특정 커밋/브랜치로 체크아웃한다 (PR head 분석 시 사용)."""
        from git import GitCommandError, Repo

        try:
            repo = Repo(self.path)
            repo.git.fetch("origin", ref)
            repo.git.checkout(ref)
        except GitCommandError as exc:
            raise RepoError(_sanitize(str(exc), self.github_token)) from None

    def diff(self, base: str, head: str) -> str:
        """두 리비전 사이의 unified diff 를 반환한다."""
        from git import GitCommandError, Repo

        try:
            repo = Repo(self.path)
            repo.git.fetch("origin", base, head)
            return repo.git.diff(f"{base}...{head}", "--unified=3")
        except GitCommandError as exc:
            logger.warning("diff 실패 (%s...%s): %s", base, head, exc)
            return ""

    def remove(self) -> None:
        """작업 사본을 삭제한다 (프로젝트 삭제 시)."""
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

    # ------------------------------------------------------------------
    #  파일 접근
    # ------------------------------------------------------------------
    def iter_source_files(self) -> list[SourceFile]:
        """
        지원 확장자 소스 파일을 모두 읽어 반환한다.

        node_modules / target / dist 등 빌드 산출물 디렉터리는 건너뛴다.
        """
        files: list[SourceFile] = []
        if not self.path.exists():
            return files

        for candidate in self.path.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(self.path).as_posix()
            except ValueError:
                continue
            if set(Path(relative).parts) & EXCLUDED_DIRS:
                continue
            if not is_supported(relative):
                continue
            content = self.read_file(relative)
            if content is not None:
                files.append(SourceFile(path=relative, content=content))
        return files

    def read_file(self, relative_path: str) -> str | None:
        """
        저장소 내부 파일을 읽는다.

        보안: 심볼릭 링크/`..` 를 이용한 작업 디렉터리 밖 접근을 차단한다.
        """
        try:
            target = (self.path / relative_path).resolve()
            root = self.path.resolve()
            if not target.is_relative_to(root):
                logger.warning("작업 디렉터리 밖 경로 접근 차단: %s", relative_path)
                return None
            if not target.is_file():
                return None
            return target.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.debug("파일 읽기 실패 %s: %s", relative_path, exc)
            return None

    def read_lines(self, relative_path: str, start: int, end: int) -> str | None:
        """지정한 라인 구간만 읽는다 (Tier 2 컨텍스트 구성용)."""
        content = self.read_file(relative_path)
        if content is None:
            return None
        lines = content.splitlines()
        start_index = max(0, (start or 1) - 1)
        end_index = min(len(lines), end or len(lines))
        return "\n".join(lines[start_index:end_index])


def _sanitize(message: str, token: str | None) -> str:
    """에러 메시지에서 토큰 문자열을 마스킹한다."""
    if token:
        return message.replace(token, "***")
    return message
