"""Enterprise user CLI boundary helpers.

Ordinary ``internctl`` in enterprise mode must keep user-side lifecycle and
workspace commands available. Only local/global administrator operations that
cannot be authorized by the daemon/relay are rejected here.
"""

from __future__ import annotations

import json
import os
import sys

from lib.enterprise_policy import enterprise_policy_exists


BOUNDARY_SCHEMA = "intern-agents.enterprise-cli-boundary.v1"
ADMIN_CLI = "administrator tooling"
ERROR_CODE = "ENTERPRISE_ADMIN_COMMAND"

TOP_LEVEL_ADMIN_COMMANDS = {
    "reset-hook-env",
}


def enterprise_mode_active(work_root: str | os.PathLike[str] | None = None) -> bool:
    return enterprise_policy_exists(work_root)


def rejection_report(command: str, detail: str = "") -> dict:
    message = (
        f"`internctl {command}` is administrator-only when enterprise policy is active. "
        "Ask an administrator to make this change."
    )
    if detail:
        message = f"{message} {detail}"
    return {
        "schema": BOUNDARY_SCHEMA,
        "ok": False,
        "error": ERROR_CODE,
        "code": ERROR_CODE,
        "command": command,
        "admin_managed": True,
        "user_actionable": False,
        "admin_cli": ADMIN_CLI,
        "message": message,
    }


def emit_admin_rejection(command: str, *, json_output: bool = False, detail: str = "") -> int:
    report = rejection_report(command, detail)
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Error [{ERROR_CODE}]: {report['message']}", file=sys.stderr)
    return 2
