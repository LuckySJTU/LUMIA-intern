"""internctl daemon - manage the local Feishu daemon on headless machines."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

from lib.user_env import load_enterprise_user_env


PID_FILE = Path("/tmp/feishu_daemon.json")


def setup_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("daemon", help="Manage local daemon")
    sub = p.add_subparsers(dest="daemon_command", help="Daemon sub-commands")

    start = sub.add_parser("start", help="Start local daemon in background")
    start.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    start.set_defaults(func=run)

    stop = sub.add_parser("stop", help="Stop local daemon")
    stop.set_defaults(func=run)

    status = sub.add_parser("status", help="Show local daemon status")
    status.add_argument("--json", action="store_true", help="Output JSON")
    status.set_defaults(func=run)

    restart = sub.add_parser("restart", help="Restart local daemon")
    restart.set_defaults(func=run)

    p.set_defaults(func=run)


def _root() -> str:
    return os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()


def _load_user_env(root: str) -> dict[str, str]:
    env = os.environ.copy()
    env["WORK_AGENTS_ROOT"] = root
    load_enterprise_user_env(root, env=env)
    return env


def _daemon_script() -> str:
    cli_root = Path(__file__).resolve().parents[1]
    return str(cli_root / "scripts" / "daemon" / "feishu_daemon.py")


def _python_executable(env: dict[str, str]) -> str:
    return env.get("PYTHON") or sys.executable or "python3"


def _pid_payload() -> dict:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _http_status(payload: dict, timeout: float = 3.0) -> tuple[bool, dict, str | None]:
    try:
        port = int(payload.get("http_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not port:
        return False, {}, None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:
        return False, {}, str(exc)


def _status_payload() -> dict:
    payload = _pid_payload()
    pid = int(payload.get("pid") or 0)
    running = bool(pid and _pid_is_running(pid))
    http_ok, http_payload, http_error = _http_status(payload)
    if http_ok:
        running = True
    result = {
        "schema": "intern-agents.daemon-status.v1",
        "running": running,
        "pid": pid or None,
        "pid_file": str(PID_FILE),
        "work_agents_root": payload.get("work_agents_root") or "",
        "http_port": payload.get("http_port"),
        "ws_port": payload.get("ws_port"),
        "status": http_payload,
    }
    if http_error:
        result["status_error"] = http_error
    return result


def _print_status(json_output: bool) -> int:
    status = _status_payload()
    if json_output:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        state = "running" if status["running"] else "not running"
        print(f"Daemon: {state}")
        if status["pid"]:
            print(f"  pid:  {status['pid']}")
        if status["http_port"]:
            print(f"  http: localhost:{status['http_port']}")
        if status["ws_port"]:
            print(f"  ws:   localhost:{status['ws_port']}")
        if status.get("status_error"):
            print(f"  status_error: {status['status_error']}")
    return 0 if status["running"] else 1


def _cmd_start(args) -> int:
    status = _status_payload()
    if status["running"]:
        print(f"Daemon already running (PID {status['pid']}).")
        return 0
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    script = _daemon_script()
    if not os.path.isfile(script):
        print(f"Error: daemon script not found: {script}", file=sys.stderr)
        return 1

    root = _root()
    log_dir = os.path.join(root, "llm_intern_logs", "_daemon")
    os.makedirs(log_dir, exist_ok=True)
    env = _load_user_env(root)
    python = _python_executable(env)
    if getattr(args, "foreground", False):
        os.execvpe(python, [python, script], env)

    log_file = os.path.join(log_dir, "feishu_daemon.wrapper.log")
    with open(log_file, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [python, script],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.time() + 12
    last = {}
    while time.time() < deadline:
        last = _status_payload()
        if last["running"]:
            print(f"Daemon started (PID {last['pid']}).")
            print(f"  Log: {log_file}")
            return 0
        if proc.poll() is not None:
            print(f"Error: daemon exited immediately. Check log: {log_file}", file=sys.stderr)
            return 1
        time.sleep(0.5)
    print(f"Error: daemon did not become ready before timeout. Check log: {log_file}", file=sys.stderr)
    return 1


def _cmd_stop(_args) -> int:
    payload = _pid_payload()
    pid = int(payload.get("pid") or 0)
    http_ok, _, _ = _http_status(payload)
    if not pid and not http_ok:
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except OSError:
                pass
        print("Daemon not running.")
        return 0
    if http_ok:
        try:
            port = int(payload.get("http_port") or 0)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
    elif pid and _pid_is_running(pid):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 8
    while time.time() < deadline:
        http_ok, _, _ = _http_status(payload, timeout=0.5)
        if not http_ok and (not pid or not _pid_is_running(pid)):
            break
        time.sleep(0.3)
    http_ok, _, _ = _http_status(payload, timeout=0.5)
    if http_ok or (pid and _pid_is_running(pid)):
        print(f"Error: daemon PID {pid} did not stop.", file=sys.stderr)
        return 1
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    print(f"Daemon stopped (PID {pid}).")
    return 0


def run(args) -> int:
    cmd = getattr(args, "daemon_command", None)
    if not cmd:
        print("Usage: internctl daemon {start|stop|restart|status}")
        return 1
    if cmd == "start":
        return _cmd_start(args)
    if cmd == "stop":
        return _cmd_stop(args)
    if cmd == "status":
        return _print_status(bool(getattr(args, "json", False)))
    if cmd == "restart":
        rc = _cmd_stop(args)
        if rc != 0:
            return rc
        return _cmd_start(argparse.Namespace(foreground=False))
    print(f"Unknown daemon command: {cmd}", file=sys.stderr)
    return 1
