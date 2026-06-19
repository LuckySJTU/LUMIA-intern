"""Shared JSON contract helpers for user-facing CLI commands."""

from __future__ import annotations


def ensure_cli_report_contract(
    report: dict,
    *,
    ok: bool,
    command: str,
    default_next_action: str,
) -> dict:
    """Add blocking summary fields without hiding daemon/relay response fields."""
    report["ok"] = bool(ok)
    blocking_checks = report.get("blocking_checks")
    if not isinstance(blocking_checks, list):
        blocking_checks = _derive_blocking_checks(report, ok=ok, command=command)
        report["blocking_checks"] = blocking_checks
    report["blocking_count"] = len(blocking_checks)
    next_actions = report.get("next_actions")
    if not isinstance(next_actions, list):
        report["next_actions"] = _derive_next_actions(blocking_checks, default_next_action)
    return report


def _derive_blocking_checks(report: dict, *, ok: bool, command: str) -> list[dict]:
    checks = report.get("checks")
    if isinstance(checks, list):
        return [check for check in checks if isinstance(check, dict) and check.get("blocking")]
    if ok:
        return []
    code = str(report.get("code") or report.get("error") or "CLI_COMMAND_FAILED")
    status = str(report.get("status") or "failed")
    message = str(report.get("message") or report.get("error") or f"{command} failed")
    return [{
        "id": f"{command.replace(' ', '.')}.result",
        "status": status,
        "passed": False,
        "blocking": True,
        "code": code,
        "message": message,
        "hint": str(report.get("hint") or ""),
        "admin_managed": bool(report.get("admin_managed") or status == "admin_action_required"),
    }]


def _derive_next_actions(blocking_checks: list[dict], default_next_action: str) -> list[str]:
    if not blocking_checks:
        return []
    if any(check.get("admin_managed") for check in blocking_checks if isinstance(check, dict)):
        return ["Ask an administrator to review the policy/RBAC denial."]
    return [default_next_action]
