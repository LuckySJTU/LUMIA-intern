"""internctl metadata — enterprise metadata resolver commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from commands import workspace as workspace_cmd
from lib.metadata_checkout import ensure_metadata_branch_checkout


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("metadata", help="Resolve enterprise metadata paths")
    meta_sub = p.add_subparsers(dest="metadata_command")

    resolve = meta_sub.add_parser("resolve", help="Resolve metadata paths")
    resolve.add_argument("--workspace", required=True, help="workspace_id")
    resolve.add_argument("--intern", required=True, help="intern name")
    resolve.add_argument("--task", default="", help="optional task id for task metadata paths")
    resolve.add_argument("--json", action="store_true")
    resolve.set_defaults(func=run_resolve)


def _print(data: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def resolve_metadata_from_workspace(workspace: dict, *, workspace_id: str, intern_name: str, task_id: str = "") -> dict:
    mode = workspace.get("metadata_mode") or ""
    local_path = workspace.get("local_path") or ""
    metadata_cache = workspace.get("metadata_cache_path") or ""
    if mode == "repo_dotdir":
        if not local_path or not os.path.isabs(local_path):
            raise RuntimeError(f"workspace {workspace_id} missing absolute local_path for repo_dotdir mode")
        metadata_root = str(Path(local_path) / ".intern_workspace")
        checkout = local_path
    elif mode == "metadata_branch":
        if not metadata_cache or not os.path.isabs(metadata_cache):
            raise RuntimeError(f"workspace {workspace_id} missing absolute metadata_cache_path for metadata_branch mode")
        if not workspace.get("metadata_branch"):
            raise RuntimeError(f"workspace {workspace_id} missing metadata_branch for metadata_branch mode")
        ensure_metadata_branch_checkout(workspace, workspace_id=workspace_id, checkout_path=metadata_cache)
        metadata_root = str(Path(metadata_cache) / ".intern_workspace")
        checkout = metadata_cache
    elif mode == "local_only":
        if not metadata_cache or not os.path.isabs(metadata_cache):
            raise RuntimeError(f"workspace {workspace_id} missing absolute metadata_cache_path for local_only mode")
        metadata_root = str(Path(metadata_cache) / "local" / ".intern_workspace")
        checkout = ""
    else:
        raise RuntimeError(f"invalid metadata mode for workspace {workspace_id}: {mode!r}")
    tasks_dir = Path(metadata_root) / "tasks"
    task_dir = tasks_dir / task_id if task_id else None
    provider_config = workspace.get("provider_config") if isinstance(workspace.get("provider_config"), dict) else {}
    repo_provider = workspace.get("repo_provider") or workspace.get("provider") or ""
    runtime_provider = workspace.get("runtime_provider") or workspace.get("provider") or ""
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "intern_name": intern_name,
        "task_id": task_id or "",
        "metadata_mode": mode,
        "metadata_branch": workspace.get("metadata_branch") if mode == "metadata_branch" else None,
        "repo_provider": repo_provider,
        "runtime_provider": runtime_provider,
        "default_branch": workspace.get("default_branch") or provider_config.get("default_branch") or "",
        "code_repo_path": local_path,
        "metadata_checkout_path": checkout,
        "metadata_root": metadata_root,
        "workspace_source_path": local_path,
        "project_rule_path": str(Path(metadata_root) / "project_rule.txt"),
        "error_book_path": str(Path(metadata_root) / "ERROR_BOOK.md"),
        "tasks_dir": str(tasks_dir),
        "task_readme_path": str(task_dir / "README.md") if task_dir else None,
        "history_log_path": str(task_dir / "history_log.md") if task_dir else None,
        "task_knowledge_path": str(task_dir / "task_knowledge.md") if task_dir else None,
        "status_path": str(Path(metadata_root) / "interns" / intern_name / "status.md"),
        "knowledge_path": str(Path(metadata_root) / "interns" / intern_name / "knowledge.md"),
    }


def bind_repo_dotdir_metadata_to_code_repo(resolver: dict, code_repo: str, intern_name: str, task_id: str = "") -> dict:
    """Return an intern-runtime resolver whose repo_dotdir metadata lives in code_repo.

    The daemon workspace cache has a provider checkout at ``workspace.local_path``.
    A running intern works in its own cloned code repo. In repo_dotdir mode the
    authoritative metadata must travel with the intern's code repo/MR, not the
    provider cache, otherwise hooks and smoke tests read different task/status
    trees from the one the model edits.
    """
    if resolver.get("metadata_mode") != "repo_dotdir" or not code_repo:
        return resolver
    code_root = Path(code_repo)
    metadata_root = code_root / ".intern_workspace"
    tasks_dir = metadata_root / "tasks"
    task_dir = tasks_dir / task_id if task_id else None
    updated = dict(resolver)
    updated.update({
        "code_repo_path": str(code_root),
        "code_worktree_path": str(code_root),
        "metadata_checkout_path": str(code_root),
        "metadata_root": str(metadata_root),
        "workspace_source_path": str(code_root),
        "project_rule_path": str(metadata_root / "project_rule.txt"),
        "error_book_path": str(metadata_root / "ERROR_BOOK.md"),
        "tasks_dir": str(tasks_dir),
        "task_readme_path": str(task_dir / "README.md") if task_dir else None,
        "history_log_path": str(task_dir / "history_log.md") if task_dir else None,
        "task_knowledge_path": str(task_dir / "task_knowledge.md") if task_dir else None,
        "status_path": str(metadata_root / "interns" / intern_name / "status.md"),
        "knowledge_path": str(metadata_root / "interns" / intern_name / "knowledge.md"),
    })
    return updated


def resolve_metadata_for_workspace_id(workspace_id: str, intern_name: str, task_id: str = "") -> dict:
    status, body = workspace_cmd._request("GET", "/api/workspaces")
    if status >= 400:
        raise RuntimeError(body.get("error") or f"daemon returned HTTP {status}")
    workspace = next(
        (item for item in body.get("workspaces", []) if item.get("workspace_id") == workspace_id),
        None,
    )
    if not workspace:
        raise RuntimeError(f"workspace not found: {workspace_id}")
    return resolve_metadata_from_workspace(
        workspace,
        workspace_id=workspace_id,
        intern_name=intern_name,
        task_id=task_id,
    )


def run_resolve(args: argparse.Namespace) -> int:
    try:
        report = resolve_metadata_for_workspace_id(args.workspace, args.intern, args.task)
    except Exception as exc:
        print(f"metadata resolve failed: {exc}", file=sys.stderr)
        return 1
    _print(report, args.json)
    return 0
