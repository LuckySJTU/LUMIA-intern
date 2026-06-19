"""internctl workspace — enterprise workspace registry commands."""

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
from lib.codeup import codeup_branch_protection
from lib.user_env import load_enterprise_user_env

PID_FILE = os.environ.get("FEISHU_DAEMON_ADDR_FILE") or "/tmp/feishu_daemon.json"


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("workspace", help="Manage enterprise workspaces")
    ws_sub = p.add_subparsers(dest="workspace_command")

    list_cmd = ws_sub.add_parser("list", help="List relay workspaces with local enable state")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=run_list)

    create = ws_sub.add_parser("create", help="Create a relay workspace registry entry")
    create.add_argument("--repo-url", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--provider", required=True, choices=["github", "codeup", "gitlab", "local"])
    create.add_argument("--mode", required=True, choices=["repo_dotdir", "metadata_branch", "local_only"])
    create.add_argument("--metadata-branch", default="")
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=run_create)

    enable = ws_sub.add_parser("enable", help="Enable a workspace on this machine")
    enable.add_argument("workspace_id")
    enable.add_argument("--local-path", default="")
    enable.add_argument("--json", action="store_true")
    enable.set_defaults(func=run_enable)

    disable = ws_sub.add_parser("disable", help="Disable a workspace on this machine")
    disable.add_argument("workspace_id")
    disable.add_argument("--json", action="store_true")
    disable.set_defaults(func=run_disable)

    doctor = ws_sub.add_parser("doctor", help="Inspect local workspace health")
    doctor.add_argument("workspace_id")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=run_doctor)

    delete = ws_sub.add_parser("delete", help="Delete a relay workspace registry entry")
    delete.add_argument("workspace_id")
    delete.add_argument("--confirm", action="store_true")
    delete.add_argument("--json", action="store_true")
    delete.set_defaults(func=run_delete)

    mode = ws_sub.add_parser("mode", help="Validate or set workspace metadata mode")
    mode_sub = mode.add_subparsers(dest="mode_command")
    validate = mode_sub.add_parser("validate", help="Validate metadata mode")
    validate.add_argument("workspace_id")
    validate.add_argument("--mode", required=True, choices=["repo_dotdir", "metadata_branch", "local_only"])
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=run_mode_validate)

    set_mode = mode_sub.add_parser("set", help="Set metadata mode")
    set_mode.add_argument("workspace_id")
    set_mode.add_argument("--mode", required=True, choices=["repo_dotdir", "metadata_branch", "local_only"])
    set_mode.add_argument("--json", action="store_true")
    set_mode.set_defaults(func=run_mode_set)

    migrate = ws_sub.add_parser("migrate", help="Migrate legacy workspace config")
    migrate_sub = migrate.add_subparsers(dest="migrate_command")
    legacy = migrate_sub.add_parser("legacy", help="Migrate .intern-config.json and .local-workspace.json")
    group = legacy.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    legacy.add_argument("--mode", choices=["repo_dotdir", "metadata_branch", "local_only"], default="")
    legacy.add_argument("--metadata-branch", default="")
    legacy.add_argument("--json", action="store_true")
    legacy.set_defaults(func=run_migrate_legacy)


def _daemon_base() -> str:
    try:
        data = json.loads(Path(PID_FILE).read_text(encoding="utf-8"))
        port = int(data["http_port"])
    except Exception as exc:
        raise RuntimeError(f"daemon address unavailable: {PID_FILE}: {exc}") from exc
    return f"http://127.0.0.1:{port}"


def _load_workspace_user_env() -> None:
    root = os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()
    load_enterprise_user_env(root)


def _request(method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_daemon_base() + path, data=data, method=method, headers=headers)
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
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _workspace_guard_failure(args: argparse.Namespace, message: str, *, command: str | None = None, available: bool | None = None) -> int:
    body: dict = {"error": message, "message": message}
    if available is not None:
        body["available"] = available
        body["reasons"] = [message]
        body["warnings"] = []
        body["required_actions"] = []
    body = ensure_cli_report_contract(
        body,
        ok=False,
        command=command or f"workspace {getattr(args, 'workspace_command', '')}",
        default_next_action="Use metadata_branch for protected Codeup default branches, or fix local Codeup credentials and retry.",
    )
    _print(body, getattr(args, "json", False))
    return 1


def _validate_codeup_repo_dotdir(repo_url: str) -> str:
    protected, branch, err = codeup_branch_protection(repo_url)
    if protected is True:
        return f"default branch {branch or '<unknown>'} is protected; repo_dotdir would require direct metadata writes"
    if err:
        return f"could not verify Codeup default branch protection: {err}"
    return ""


def _workspace_from_list(workspace_id: str) -> dict:
    status, body = _request("GET", "/api/workspaces")
    if status >= 400:
        raise RuntimeError(body.get("error") or f"workspace list failed: HTTP {status}")
    for item in body.get("workspaces") or []:
        if isinstance(item, dict) and str(item.get("workspace_id") or "") == workspace_id:
            return item
    raise RuntimeError(f"workspace not found: {workspace_id}")


def _validate_codeup_repo_dotdir_for_workspace(workspace_id: str) -> str:
    workspace = _workspace_from_list(workspace_id)
    if workspace.get("provider") != "codeup":
        return ""
    return _validate_codeup_repo_dotdir(str(workspace.get("repo_url") or ""))


def _run_request(args: argparse.Namespace, method: str, path: str, payload: dict | None = None, success=(200, 201)) -> int:
    try:
        status, body = _request(method, path, payload)
    except Exception as exc:
        if getattr(args, "json", False):
            body = ensure_cli_report_contract(
                {"error": "WORKSPACE_DAEMON_UNAVAILABLE", "message": str(exc)},
                ok=False,
                command=f"workspace {getattr(args, 'workspace_command', '')}",
                default_next_action="Start the local daemon with `internctl daemon start`, then rerun the workspace command.",
            )
            _print(body, True)
            return 1
        print(f"workspace command failed: {exc}", file=sys.stderr)
        return 1
    ok = status in success and body.get("ok", True) is not False
    body = ensure_cli_report_contract(
        body,
        ok=ok,
        command=f"workspace {getattr(args, 'workspace_command', '')}",
        default_next_action="Review the workspace daemon response, fix the blocking check, then rerun the workspace command.",
    )
    _print(body, getattr(args, "json", False))
    return 0 if ok else 1


def run_list(args: argparse.Namespace) -> int:
    return _run_request(args, "GET", "/api/workspaces")


def run_create(args: argparse.Namespace) -> int:
    _load_workspace_user_env()
    if args.provider == "codeup" and args.mode == "repo_dotdir":
        reason = _validate_codeup_repo_dotdir(args.repo_url)
        if reason:
            return _workspace_guard_failure(args, reason)
    payload = {
        "repo_url": args.repo_url,
        "display_name": args.display_name,
        "provider": args.provider,
        "metadata_mode": args.mode,
    }
    if args.metadata_branch:
        payload["metadata_branch"] = args.metadata_branch
    return _run_request(args, "POST", "/api/workspaces", payload, success=(200, 201))


def run_enable(args: argparse.Namespace) -> int:
    payload = {"local_path": args.local_path} if args.local_path else {}
    return _run_request(args, "POST", f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}/enable", payload)


def run_disable(args: argparse.Namespace) -> int:
    return _run_request(args, "POST", f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}/disable", {})


def run_doctor(args: argparse.Namespace) -> int:
    return _run_request(args, "POST", f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}/doctor", {})


def run_delete(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing to delete workspace without --confirm", file=sys.stderr)
        return 1
    return _run_request(args, "DELETE", f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}")


def run_mode_validate(args: argparse.Namespace) -> int:
    _load_workspace_user_env()
    if args.mode == "repo_dotdir":
        try:
            reason = _validate_codeup_repo_dotdir_for_workspace(args.workspace_id)
        except Exception as exc:
            reason = str(exc)
        if reason:
            return _workspace_guard_failure(args, reason, command="workspace mode validate", available=False)
    try:
        status, body = _request(
            "POST",
            f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}/mode/validate",
            {"mode": args.mode},
        )
    except Exception as exc:
        if getattr(args, "json", False):
            body = ensure_cli_report_contract(
                {"error": "WORKSPACE_DAEMON_UNAVAILABLE", "message": str(exc)},
                ok=False,
                command="workspace mode validate",
                default_next_action="Start the local daemon with `internctl daemon start`, then rerun workspace mode validate.",
            )
            _print(body, True)
            return 1
        print(f"workspace command failed: {exc}", file=sys.stderr)
        return 1
    body = ensure_cli_report_contract(
        body,
        ok=status < 400 and bool(body.get("available")),
        command="workspace mode validate",
        default_next_action="Choose an allowed workspace metadata mode, then rerun workspace mode validate.",
    )
    _print(body, getattr(args, "json", False))
    if status >= 400:
        return 1
    return 0 if body.get("available") else 2


def run_mode_set(args: argparse.Namespace) -> int:
    _load_workspace_user_env()
    if args.mode == "repo_dotdir":
        try:
            reason = _validate_codeup_repo_dotdir_for_workspace(args.workspace_id)
        except Exception as exc:
            reason = str(exc)
        if reason:
            return _workspace_guard_failure(args, reason, command="workspace mode set", available=False)
    return _run_request(
        args,
        "POST",
        f"/api/workspaces/{urllib.parse.quote(args.workspace_id)}/mode/set",
        {"mode": args.mode},
    )


def _legacy_config_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("WORK_AGENTS_ROOT") or os.getcwd())
    return (
        root / "axis_intern_agents" / "workspace" / ".intern-config.json",
        root / ".local-workspace.json",
    )


def _existing_workspace_id_from_create_failure(status: int, body: dict) -> str:
    if status != 409:
        return ""
    workspace_id = body.get("workspace_id") if isinstance(body, dict) else ""
    if workspace_id:
        return str(workspace_id)
    workspace = body.get("workspace") if isinstance(body, dict) else None
    if isinstance(workspace, dict) and workspace.get("workspace_id"):
        return str(workspace["workspace_id"])
    return ""


def run_migrate_legacy(args: argparse.Namespace) -> int:
    config_path, local_path = _legacy_config_paths()
    report: dict[str, object] = {
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "legacy_config_path": str(config_path),
        "local_config_path": str(local_path),
        "projects": [],
        "enabled_projects": [],
        "created": [],
        "applied": False,
    }
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        projects = data.get("projects") if isinstance(data, dict) else []
        report["projects"] = [
            {
                "display_name": item.get("name") or item.get("projectId"),
                "project_id": item.get("projectId") or item.get("name"),
                "repo_url": item.get("repoUrl", ""),
                "provider": item.get("provider", "github"),
            }
            for item in projects if isinstance(item, dict)
        ]
    if local_path.exists():
        data = json.loads(local_path.read_text(encoding="utf-8"))
        enabled = data.get("enabledProjects") if isinstance(data, dict) else []
        if isinstance(enabled, list):
            report["enabled_projects"] = [str(v) for v in enabled]
    if args.apply:
        if not args.mode:
            report["ok"] = False
            report["error"] = "--mode is required with --apply"
            _print(report, args.json)
            return 1
        if args.mode == "metadata_branch" and not args.metadata_branch:
            report["ok"] = False
            report["error"] = "--metadata-branch is required when applying metadata_branch mode"
            _print(report, args.json)
            return 1
        report["applied"] = True
        errors: list[dict[str, object]] = []
        created: list[dict[str, object]] = []
        enabled = set(report["enabled_projects"]) if isinstance(report.get("enabled_projects"), list) else set()
        for project in report["projects"] if isinstance(report.get("projects"), list) else []:
            if not isinstance(project, dict):
                continue
            payload = {
                "repo_url": project.get("repo_url", ""),
                "display_name": project.get("display_name") or project.get("project_id"),
                "provider": project.get("provider", "github"),
                "metadata_mode": args.mode,
            }
            if args.metadata_branch:
                payload["metadata_branch"] = args.metadata_branch
            status, body = _request("POST", "/api/workspaces", payload)
            existing_workspace_id = _existing_workspace_id_from_create_failure(status, body)
            using_existing = False
            if existing_workspace_id:
                get_status, get_body = _request("GET", f"/api/workspaces/{urllib.parse.quote(existing_workspace_id)}")
                if get_status < 400:
                    status = 200
                    body = get_body
                    using_existing = True
            item: dict[str, object] = {
                "project_id": project.get("project_id"),
                "display_name": project.get("display_name"),
                "create_status": status,
                "create_result": "existing" if using_existing else "created",
                "response": body,
            }
            workspace = body.get("workspace") if isinstance(body, dict) else None
            workspace_id = body.get("workspace_id") if isinstance(body, dict) else None
            if not workspace_id and isinstance(workspace, dict):
                workspace_id = workspace.get("workspace_id")
            if workspace_id:
                item["workspace_id"] = workspace_id
            if status not in (200, 201):
                errors.append({"project": project.get("project_id"), "stage": "create", "status": status, "response": body})
                created.append(item)
                continue
            if workspace_id and (
                str(project.get("project_id")) in enabled or str(project.get("display_name")) in enabled
            ):
                enable_status, enable_body = _request(
                    "POST",
                    f"/api/workspaces/{urllib.parse.quote(str(workspace_id))}/enable",
                    {},
                )
                item["enable_status"] = enable_status
                item["enable_response"] = enable_body
                if enable_status not in (200, 201):
                    errors.append({
                        "project": project.get("project_id"),
                        "stage": "enable",
                        "status": enable_status,
                        "response": enable_body,
                    })
            created.append(item)
        report["created"] = created
        if errors:
            report["ok"] = False
            report["errors"] = errors
            _print(report, args.json)
            return 1
    _print(report, args.json)
    return 0
