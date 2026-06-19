"""Git 操作封装 — clone / commit / push，带 flock 文件锁。"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


# ── flock 文件锁 ──────────────────────────────
_LOCK_DIR: str = "/tmp/internctl_locks"
LOCAL_WRITES_BRANCH = "intern/local-writes"


@contextmanager
def git_lock(repo_path: str) -> Generator[None, None, None]:
    """对 repo 级别加 flock，防多进程并发。"""
    os.makedirs(_LOCK_DIR, exist_ok=True)
    lock_name = repo_path.replace("/", "_")
    lock_file = os.path.join(_LOCK_DIR, f"{lock_name}.lock")
    fd = open(lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def run_git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """执行 git 命令并返回结果。"""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"git {' '.join(args)} failed (exit {result.returncode}){suffix}")
    return result


def _git_error_message(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    return f"git {' '.join(args)} failed (exit {result.returncode}){suffix}"


def _is_transient_git_transport_error(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    return (
        "could not resolve hostname" in text
        or "no address associated with hostname" in text
        or "temporary failure in name resolution" in text
        or "connection timed out" in text
        or "connection reset by peer" in text
        or "network is unreachable" in text
        or "failed to connect" in text
        or "connection refused" in text
        or "could not read from remote repository" in text and "resolve hostname" in text
    )


def _is_non_fast_forward_push(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    return (
        "fetch first" in text
        or "non-fast-forward" in text
        or "updates were rejected" in text
        or "[rejected]" in text
    )


def _push_with_rebase_retry(repo_path: str, branch: str, attempts: int = 4) -> None:
    push_args = ["push", "origin", branch]
    last = None
    for attempt in range(max(1, attempts)):
        result = run_git(push_args, cwd=repo_path, check=False)
        if result.returncode == 0:
            return
        last = result
        if not _is_non_fast_forward_push(result) or attempt == attempts - 1:
            raise RuntimeError(_git_error_message(push_args, result))
        run_git(
            ["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=repo_path,
        )
        run_git(["rebase", "--autostash", f"origin/{branch}"], cwd=repo_path)
    if last is not None:
        raise RuntimeError(_git_error_message(push_args, last))


def _git_text(args: list[str], cwd: str) -> str | None:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _strip_origin(branch: str) -> str:
    return branch.removeprefix("origin/")


def _has_head_commit(repo_path: str) -> bool:
    return _git_text(["rev-parse", "--verify", "HEAD"], cwd=repo_path) is not None


def get_current_branch_or_none(repo_path: str) -> str | None:
    """Return current branch, or None for detached HEAD / unborn unknown state."""
    branch = _git_text(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if not branch or branch == "HEAD":
        return None
    return branch


def ensure_git_identity(repo_path: str) -> None:
    """Ensure local git user.name and user.email exist before committing."""
    user_name = run_git(["config", "--get", "user.name"], cwd=repo_path, check=False).stdout.strip()
    user_email = run_git(["config", "--get", "user.email"], cwd=repo_path, check=False).stdout.strip()
    if user_name and user_email:
        return

    hostname = socket.gethostname()
    if not user_name:
        run_git(["config", "user.name", f"intern-agent@{hostname}"], cwd=repo_path)
    if not user_email:
        run_git(["config", "user.email", f"intern-agent@{hostname}"], cwd=repo_path)


def _get_unborn_head_branch(repo_path: str) -> str | None:
    if _has_head_commit(repo_path):
        return None
    branch = _git_text(["symbolic-ref", "--short", "HEAD"], cwd=repo_path)
    return branch or None


def get_default_branch(repo_path: str) -> str:
    """检测远程默认分支名（main / master / 带 slash 的分支名等）。"""
    branch = _git_text(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_path)
    if branch:
        return _strip_origin(branch)

    remote_head = _git_text(["ls-remote", "--symref", "origin", "HEAD"], cwd=repo_path)
    if remote_head:
        for line in remote_head.splitlines():
            match = re.match(r"^ref:\s+refs/heads/(.+)\s+HEAD$", line)
            if match:
                return match.group(1)

    refs = _git_text(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"], cwd=repo_path)
    if refs:
        branches = [
            _strip_origin(line.strip())
            for line in refs.splitlines()
            if line.strip() and line.strip() != "origin/HEAD"
        ]
        if "main" in branches:
            return "main"
        if "master" in branches:
            return "master"
        if branches:
            return branches[0]

    unborn_branch = _get_unborn_head_branch(repo_path)
    if unborn_branch:
        return unborn_branch

    raise RuntimeError(f"Unable to determine default branch for repo: {repo_path}")


def clone(repo_url: str, dest: str, attempts: int = 4) -> None:
    """git clone repo_url 到 dest。失败时抛 RuntimeError 并带上 git stderr。"""
    last = None
    clone_args = ["clone", repo_url, dest]
    for attempt in range(max(1, attempts)):
        r = subprocess.run(
            ["git", *clone_args],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return
        last = r
        if not _is_transient_git_transport_error(r) or attempt == attempts - 1:
            raise RuntimeError(_git_error_message(clone_args, r))
        shutil.rmtree(dest, ignore_errors=True)
        time.sleep(min(2 ** attempt, 8))
    if last is not None:
        raise RuntimeError(_git_error_message(clone_args, last))


def ensure_local_write_branch(repo_path: str, branch: str = LOCAL_WRITES_BRANCH) -> str:
    """Ensure local-only commits in detached checkouts land on a named branch."""
    current = get_current_branch_or_none(repo_path)
    if current:
        return current

    if _git_text(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo_path) is not None:
        run_git(["checkout", branch], cwd=repo_path)
    else:
        run_git(["checkout", "-b", branch], cwd=repo_path)
    return branch


def add_commit_push(
    repo_path: str,
    paths: list[str],
    message: str,
    branch: str | None = None,
    push: bool = True,
) -> None:
    """git add + commit + push（带 flock）。branch=None 时自动检测默认分支。"""
    if branch is None and push:
        branch = get_default_branch(repo_path)
    with git_lock(repo_path):
        for p in paths:
            run_git(["add", p], cwd=repo_path)
        staged = run_git(["diff", "--cached", "--name-only", "--", *paths], cwd=repo_path).stdout.strip()
        if not staged:
            return
        ensure_git_identity(repo_path)
        run_git(["commit", "-m", message, "--", *paths], cwd=repo_path)
        if push:
            _push_with_rebase_retry(repo_path, branch)


def remove_and_push(
    repo_path: str,
    paths: list[str],
    message: str,
    branch: str | None = None,
    push: bool = True,
) -> None:
    """git rm -r + commit + push（带 flock）。branch=None 时自动检测默认分支。"""
    if branch is None and push:
        branch = get_default_branch(repo_path)
    with git_lock(repo_path):
        for p in paths:
            run_git(["rm", "-r", p], cwd=repo_path)
        staged = run_git(["diff", "--cached", "--name-only", "--", *paths], cwd=repo_path).stdout.strip()
        if not staged:
            return
        ensure_git_identity(repo_path)
        run_git(["commit", "-m", message, "--", *paths], cwd=repo_path)
        if push:
            _push_with_rebase_retry(repo_path, branch)
