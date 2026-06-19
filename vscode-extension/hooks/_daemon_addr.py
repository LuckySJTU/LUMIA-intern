"""Shared helper to discover the running feishu daemon's HTTP/WS endpoints.

The daemon now binds to ephemeral ports (not the legacy 18080/18081) and writes
its actual ports to /tmp/feishu_daemon.json on startup. All hooks/CLI/extension
that talk to the daemon MUST resolve the address through here.

If the daemon is not running, these functions raise FileNotFoundError. Callers
should let it propagate (rule 6: no defensive fallback) so the failure is loud.
"""
import json
import os

PID_FILE = "/tmp/feishu_daemon.json"


def _read_pid_file():
    with open(PID_FILE) as f:
        return json.load(f)


def get_daemon_http_port():
    return int(_read_pid_file()["http_port"])


def get_daemon_ws_port():
    return int(_read_pid_file()["ws_port"])


def get_daemon_http_url():
    return f"http://localhost:{get_daemon_http_port()}"


def get_daemon_ws_url():
    return f"ws://localhost:{get_daemon_ws_port()}"


def is_daemon_running():
    """True if PID file exists AND the recorded pid is alive."""
    if not os.path.exists(PID_FILE):
        return False
    try:
        info = _read_pid_file()
        os.kill(int(info["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
