"""internctl helper - local machine helper runtime smoke-test commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lib.cli_contract import ensure_cli_report_contract


PID_FILE = os.environ.get("FEISHU_DAEMON_ADDR_FILE") or "/tmp/feishu_daemon.json"


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("helper", help="Manage the local machine helper runtime")
    sub = p.add_subparsers(dest="helper_command")

    start = sub.add_parser("start", help="Start this machine's helper runtime")
    start.add_argument("--issue", default="", help="Initial issue summary")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=run)

    stop = sub.add_parser("stop", help="Stop this machine's helper runtime")
    stop.add_argument("--json", action="store_true")
    stop.set_defaults(func=run)

    status = sub.add_parser("status", help="Show this machine's helper runtime status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=run)

    migrate = sub.add_parser("migrate", help="Send a migration prompt to this machine's helper runtime")
    migrate.add_argument("endpoint", help="Target endpoint, for example 10.0.0.5:22")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(func=run)

    invite = sub.add_parser("invite-owner", help="Send owner-assist context to this machine's helper runtime")
    invite.add_argument("--issue", default="", help="Issue summary to explain to the owner")
    invite.add_argument("--json", action="store_true")
    invite.set_defaults(func=run)

    p.set_defaults(func=run)


def _daemon_base() -> str:
    try:
        data = json.loads(Path(PID_FILE).read_text(encoding="utf-8"))
        port = int(data["http_port"])
    except Exception as exc:
        raise RuntimeError(f"daemon address unavailable: {PID_FILE}: {exc}") from exc
    return f"http://127.0.0.1:{port}"


def _request(payload: dict, timeout: float = 120.0) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _daemon_base() + "/api/helper/action",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {"error": raw}
        return int(exc.code), body


def _print(data: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        status = data.get("status") or ("ok" if data.get("ok") else "error")
        helper_id = data.get("helper_id") or ""
        print(f"helper: {status}" + (f" ({helper_id})" if helper_id else ""))
        if data.get("error"):
            print(f"error: {data['error']}", file=sys.stderr)


def _payload(args: argparse.Namespace) -> dict:
    command = getattr(args, "helper_command", "")
    action = "invite_owner" if command == "invite-owner" else command
    payload = {"action": action}
    issue = getattr(args, "issue", "")
    if issue:
        payload["issue_summary"] = issue
    endpoint = getattr(args, "endpoint", "")
    if endpoint:
        payload["endpoint"] = endpoint
    return payload


def run(args: argparse.Namespace) -> int:
    if not getattr(args, "helper_command", None):
        print("Usage: internctl helper {start|stop|status|migrate|invite-owner}", file=sys.stderr)
        return 1
    try:
        status, body = _request(_payload(args))
    except Exception as exc:
        if getattr(args, "json", False):
            body = ensure_cli_report_contract(
                {"error": "HELPER_DAEMON_UNAVAILABLE", "message": str(exc)},
                ok=False,
                command=f"helper {getattr(args, 'helper_command', '')}",
                default_next_action="Start the local daemon with `internctl daemon start`, then rerun the helper command.",
            )
            _print(body, True)
            return 1
        print(f"helper command failed: {exc}", file=sys.stderr)
        return 1
    ok = status < 400 and body.get("ok", status == 200)
    body = ensure_cli_report_contract(
        body,
        ok=ok,
        command=f"helper {getattr(args, 'helper_command', '')}",
        default_next_action="Review the helper daemon response, fix the blocking check, then rerun the helper command.",
    )
    _print(body, getattr(args, "json", False))
    return 0 if ok else 1
