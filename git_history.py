"""Read commits and small change excerpts from a local Git working tree."""

import os
import re
import subprocess
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path


class GitError(RuntimeError):
    pass


def _command(repo: Path, *args: str) -> list[str]:
    return ["git", "--no-pager", "--no-optional-locks", "-C", str(repo), *args]


def _environment() -> dict[str, str]:
    # A Git hook can export its own repository path into child processes.
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    return env


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            _command(repo, *args), capture_output=True, env=_environment(), check=False
        )
    except OSError as exc:
        raise GitError(f"Git을 실행할 수 없습니다: {exc}") from exc
    if check and result.returncode:
        raise GitError(result.stderr.decode("utf-8", "replace").strip() or "Git 조회 실패")
    return result


def resolve_repo(path: Path) -> Path:
    """Resolve a working tree or one of its subdirectories to the tree root."""
    root = _run(path, "rev-parse", "--show-toplevel").stdout.rstrip(b"\r\n")
    return Path(os.fsdecode(root))


def collect_commits(repo: Path, start: date, end: date) -> list[dict[str, str]]:
    """Read commits reachable from HEAD by committer time, using local calendar days."""
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    head = _run(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    if head.returncode == 1:  # An unborn branch has no commits yet.
        return []
    if head.returncode:
        raise GitError(head.stderr.decode("utf-8", "replace").strip() or "HEAD 조회 실패")

    since = int(datetime.combine(start, time.min).timestamp())
    until = int(datetime.combine(end + timedelta(days=1), time.min).timestamp()) - 1
    data = _run(
        repo, "log", "--no-show-signature", "--reverse", "-z", "--encoding=UTF-8",
        "--format=%H%x00%ct%x00%s", f"--since-as-filter=@{since}", f"--until=@{until}",
        head.stdout.decode("ascii").strip(), "--",
    ).stdout
    if not data:
        return []
    # Git's -z separator also terminates the last record. Splitting fields on
    # NUL preserves tabs, punctuation, and empty subjects in commit messages.
    fields = data.removesuffix(b"\0").split(b"\0")
    commits = []
    for i in range(0, len(fields), 3):
        sha, timestamp, subject = fields[i:i + 3]
        commits.append({
            "sha": sha.decode("ascii"),
            "date": datetime.fromtimestamp(int(timestamp)).date().isoformat(),
            "subject": subject.decode("utf-8", "replace"),
        })
    return commits


def read_diff(repo: Path, sha: str, max_chars: int = 6000) -> str:
    """Get a bounded patch, including the first-parent diff for a merge commit."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise GitError("전체 커밋 SHA가 필요합니다.")
    if max_chars < 1:
        raise ValueError("변경 내역 길이는 1 이상이어야 합니다.")
    command = _command(
        repo, "-c", "core.quotepath=false", "show", "--format=", "--no-color",
        "--no-show-signature", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--diff-merges=first-parent", "--unified=2", sha, "--",
    )
    try:
        # Spool stderr separately so a warning cannot block the bounded stdout read.
        with tempfile.TemporaryFile() as errors, subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, env=_environment(),
            text=True, encoding="utf-8", errors="replace",
        ) as process:
            excerpt = process.stdout.read(max_chars + 1)
            if len(excerpt) > max_chars:
                process.kill()
                marker = "\n… (생략)"
                return (excerpt[:max(0, max_chars - len(marker))] + marker)[:max_chars]
            if process.wait():
                errors.seek(0)
                raise GitError(errors.read(4096).decode("utf-8", "replace").strip() or "Git 변경 내역 조회 실패")
            return excerpt
    except OSError as exc:
        raise GitError(f"Git을 실행할 수 없습니다: {exc}") from exc
