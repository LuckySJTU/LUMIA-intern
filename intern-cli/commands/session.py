"""internctl session - headless intern runtime lifecycle commands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from commands.metadata import bind_repo_dotdir_metadata_to_code_repo, resolve_metadata_for_workspace_id
from lib.git_ops import add_commit_push


STOP_GRACE_SECONDS = 20.0
POST_EXIT_SETTLE_SECONDS = 6.0
SHELL_EXIT_GRACE_SECONDS = 6.0


def setup_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("session", help="Manage headless intern sessions")
    sub = p.add_subparsers(dest="session_command")

    start = sub.add_parser("start", help="Start a Codex intern session")
    start.add_argument("name")
    start.add_argument("--project", required=True)
    start.add_argument("--no-attach", action="store_true", help="Leave tmux detached")
    start.set_defaults(func=run)

    status = sub.add_parser("status", help="Show tmux session status")
    status.add_argument("name")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=run)

    stop = sub.add_parser("stop", help="Stop a tmux session")
    stop.add_argument("name")
    stop.set_defaults(func=run)

    p.set_defaults(func=run)


def _root() -> str:
    return os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()


def _cli_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _session_entry(name: str, project: str) -> dict:
    sessions_path = Path(_root()) / ".intern_sessions.json"
    try:
        data = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"session registry unavailable: {sessions_path}: {exc}") from exc
    matches = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if value.get("intern_name", key) == name and value.get("project") == project:
            matches.append(value)
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one session registry entry for {project}:{name}, found {len(matches)}")
    return dict(matches[0])


def _metadata_resolver(intern_dir: str) -> dict:
    state_path = Path(intern_dir) / ".hook_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"hook state unavailable: {state_path}: {exc}") from exc
    resolver = state.get("metadata_resolver")
    if not isinstance(resolver, dict):
        raise RuntimeError(f"hook state missing metadata_resolver: {state_path}")
    return resolver


def _status_task_id(status_path: str) -> str:
    try:
        text = Path(status_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    match = re.search(r"<!--\s*METADATA:[^>]*\bTASK=([^,>\s]*)", text)
    return match.group(1).strip() if match else ""


def _copy_if_missing(src: str, dst: str) -> bool:
    if not src or not dst or os.path.abspath(src) == os.path.abspath(dst):
        return False
    if not os.path.isfile(src) or os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _commit_metadata_refresh(resolver: dict, copied_paths: list[str], intern_name: str) -> None:
    if not copied_paths or resolver.get("metadata_mode") == "local_only":
        return
    checkout = str(resolver.get("metadata_checkout_path") or "")
    if not checkout or not os.path.isdir(os.path.join(checkout, ".git")):
        return
    rels = []
    checkout_abs = os.path.abspath(checkout)
    for path in copied_paths:
        path_abs = os.path.abspath(path)
        try:
            if os.path.commonpath([checkout_abs, path_abs]) != checkout_abs:
                continue
        except ValueError:
            continue
        rels.append(os.path.relpath(path_abs, checkout_abs))
    if not rels:
        return
    add_commit_push(
        repo_path=checkout,
        paths=sorted(set(rels)),
        message=f"[{intern_name}] metadata: refresh after workspace mode switch",
        branch=resolver.get("metadata_branch") or None,
    )


def _write_hook_state_resolver(intern_dir: str, resolver: dict, project: str, workspace_id: str) -> None:
    state_path = Path(intern_dir) / ".hook_state.json"
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state["project"] = project
    if workspace_id:
        state["workspace_id"] = workspace_id
    state["metadata_resolver"] = resolver
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def _refresh_enterprise_resolver(entry: dict, intern_dir: str, project: str, old_resolver: dict) -> dict:
    workspace_id = str(entry.get("workspace_id") or "")
    if not workspace_id:
        return old_resolver
    task_id = _status_task_id(str(old_resolver.get("status_path") or ""))
    try:
        resolver = resolve_metadata_for_workspace_id(workspace_id, entry.get("intern_name") or "", task_id)
    except Exception:
        return old_resolver

    code_repo = os.path.join(str(intern_dir), project)
    if os.path.isdir(os.path.join(code_repo, ".git")):
        resolver["code_repo_path"] = code_repo
        resolver["code_worktree_path"] = code_repo
    elif old_resolver.get("code_worktree_path") or old_resolver.get("code_repo_path"):
        resolver["code_repo_path"] = old_resolver.get("code_repo_path") or old_resolver.get("code_worktree_path")
        resolver["code_worktree_path"] = old_resolver.get("code_worktree_path") or old_resolver.get("code_repo_path")
    resolver = bind_repo_dotdir_metadata_to_code_repo(
        resolver,
        str(resolver.get("code_worktree_path") or resolver.get("code_repo_path") or ""),
        str(entry.get("intern_name") or ""),
        task_id,
    )

    copied = []
    for key in ("status_path", "knowledge_path"):
        old_path = str(old_resolver.get(key) or "")
        new_path = str(resolver.get(key) or "")
        if _copy_if_missing(old_path, new_path):
            copied.append(new_path)
    _commit_metadata_refresh(resolver, copied, str(entry.get("intern_name") or ""))
    _write_hook_state_resolver(intern_dir, resolver, project, workspace_id)
    return resolver


def _tmux_running(name: str) -> bool:
    result = subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True)
    return result.returncode == 0


def _pane_current_command(name: str) -> str:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={name}", "-F", "#{pane_current_command}"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


def _is_idle_shell_command(command: str) -> bool:
    base = os.path.basename((command or "").strip()).lower()
    return base in {"", "bash", "sh", "zsh", "fish", "tmux"}


def _is_codex_command_name(command: str) -> bool:
    return "codex" in os.path.basename((command or "").strip()).lower()


def _child_cmdline_contains(parent_pid: str, needle: str) -> bool:
    if not parent_pid or not needle:
        return False
    needle = needle.lower()
    commands = (
        ["ps", "--ppid", str(parent_pid), "-o", "args="],
        ["pgrep", "-P", str(parent_pid), "-fl", "."],
    )
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if result.returncode in (0, 1) and needle in (result.stdout or "").lower():
            return True
    return False


def _codex_process_running(name: str) -> bool:
    if not _tmux_running(name):
        return False
    try:
        command = _pane_current_command(name)
        if _is_codex_command_name(command):
            return True

        pane_pid = subprocess.run(
            ["tmux", "list-panes", "-t", f"={name}", "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
        pid = pane_pid.stdout.strip().splitlines()[0] if pane_pid.stdout.strip() else ""
        return _child_cmdline_contains(pid, "codex")
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return False


def _send_codex_exit(name: str) -> None:
    target = f"={name}:"
    subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", "/exit"], check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, capture_output=True)


def _wait_for_codex_exit(name: str, timeout_seconds: float = STOP_GRACE_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _tmux_running(name) or not _codex_process_running(name):
            return True
        time.sleep(0.5)
    return not _tmux_running(name) or not _codex_process_running(name)


def _wait_for_post_exit_shell(name: str, timeout_seconds: float = POST_EXIT_SETTLE_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _tmux_running(name):
            return True
        try:
            if _is_idle_shell_command(_pane_current_command(name)):
                return True
        except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
            return False
        time.sleep(0.2)
    return not _tmux_running(name)


def _send_shell_exit(name: str) -> None:
    target = f"={name}:"
    subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", "exit"], check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, capture_output=True)


def _wait_for_tmux_gone(name: str, timeout_seconds: float = SHELL_EXIT_GRACE_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _tmux_running(name):
            return True
        time.sleep(0.2)
    return not _tmux_running(name)


def _kill_tmux_session(name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "kill-session", "-t", f"={name}"],
        capture_output=True,
        text=True,
    )


def run_start(args: argparse.Namespace) -> int:
    try:
        entry = _session_entry(args.name, args.project)
        intern_dir = entry.get("intern_dir") or os.path.join(_root(), args.name)
        resolver = _refresh_enterprise_resolver(
            entry,
            str(intern_dir),
            args.project,
            _metadata_resolver(str(intern_dir)),
        )
        code_repo = os.path.join(str(intern_dir), args.project)
        if not os.path.isdir(os.path.join(code_repo, ".git")):
            code_repo = str(resolver.get("code_worktree_path") or resolver.get("code_repo_path") or "")
        metadata_intern_dir = os.path.dirname(str(resolver.get("status_path") or ""))
        if not metadata_intern_dir:
            raise RuntimeError("metadata_resolver missing status_path")
        script = _cli_root() / "scripts" / "intern_start_codex.sh"
        env = os.environ.copy()
        env.update({
            "WORK_AGENTS_ROOT": _root(),
            "INTERN_DIR": str(intern_dir),
            "INTERN_CODE_REPO_PATH": code_repo,
            "INTERN_METADATA_INTERN_DIR": metadata_intern_dir,
            "INTERN_SESSION_REGISTRY_KEY": f"{entry.get('workspace_id') or args.project}:{args.name}",
            "INTERN_WORKSPACE_ID": str(entry.get("workspace_id") or ""),
            "INTERN_START_NO_ATTACH": "1" if args.no_attach else "0",
        })
        result = subprocess.run(
            ["bash", str(script), args.name, args.project],
            env=env,
            text=True,
        )
        return int(result.returncode)
    except Exception as exc:
        print(f"session start failed: {exc}", file=sys.stderr)
        return 1


def run_status(args: argparse.Namespace) -> int:
    running = _tmux_running(args.name)
    if args.json:
        print(json.dumps({"schema": "intern-agents.session-status.v1", "name": args.name, "running": running}, indent=2))
    else:
        print(f"{args.name}: {'running' if running else 'not running'}")
    return 0 if running else 1


def run_stop(args: argparse.Namespace) -> int:
    if not _tmux_running(args.name):
        print(f"{args.name}: not running")
        return 0

    graceful = False
    post_exit_shell = False
    if _codex_process_running(args.name):
        try:
            _send_codex_exit(args.name)
            graceful = _wait_for_codex_exit(args.name)
            if graceful:
                post_exit_shell = _wait_for_post_exit_shell(args.name)
        except subprocess.CalledProcessError as exc:
            print(f"{args.name}: /exit send failed: {exc}", file=sys.stderr)
        if not graceful:
            print(f"{args.name}: /exit did not finish within {STOP_GRACE_SECONDS:.0f}s; killing tmux session", file=sys.stderr)
    else:
        print(f"{args.name}: Codex process not running; cleaning tmux session")

    if not _tmux_running(args.name):
        print(f"{args.name}: stopped via /exit")
        return 0

    if graceful and post_exit_shell:
        try:
            _send_shell_exit(args.name)
            if _wait_for_tmux_gone(args.name):
                print(f"{args.name}: stopped via /exit")
                return 0
        except subprocess.CalledProcessError as exc:
            print(f"{args.name}: shell exit send failed: {exc}", file=sys.stderr)

    result = _kill_tmux_session(args.name)
    if result.returncode != 0 and not _tmux_running(args.name):
        print(f"{args.name}: stopped via /exit")
        return 0
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "tmux kill-session failed", file=sys.stderr)
        return result.returncode
    if graceful:
        print(f"{args.name}: stopped after graceful /exit")
    else:
        print(f"{args.name}: stopped")
    return 0


def run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "session_command", None)
    if cmd == "start":
        return run_start(args)
    if cmd == "status":
        return run_status(args)
    if cmd == "stop":
        return run_stop(args)
    print("Usage: internctl session {start|status|stop}", file=sys.stderr)
    return 1
