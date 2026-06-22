"""internctl delete <name> [--confirm] — 删除 intern。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request

from lib.daemon_addr import daemon_addr_file
from lib.intern_registry import (
    WORK_AGENTS_ROOT,
    get_intern,
    parse_status_md,
    validate_name,
)
from lib.git_ops import remove_and_push, run_git
from lib.enterprise_policy import enterprise_policy_exists
from lib.enterprise_state_v1 import intern_runtime_dir
from lib.metadata_checkout import ensure_metadata_branch_checkout
from commands.create import (
    _contract_checkout_root,
    _find_workspace_id_for_project,
    _relative_paths_under,
    _require_contract_path,
    _session_registry_key,
)
from commands.metadata import resolve_metadata_for_workspace_id


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """注册 delete 子命令。"""
    p = subparsers.add_parser("delete", help="删除 intern")
    p.add_argument("name", help="intern 名称")
    p.add_argument("--project", default="axis_intern_agents", help="项目名称（默认 axis_intern_agents）")
    p.add_argument("--branch", default=None, help="metadata repo branch to push")
    p.add_argument("--local-project", action="store_true", help="ignore enterprise policy and delete from WORK_AGENTS_ROOT/<project>")
    p.add_argument("--confirm", action="store_true", help="跳过交互确认")
    p.add_argument("--force", action="store_true", help="强制删除非 Idle intern；仅用于 create 失败回滚")
    p.set_defaults(func=run)


def _delete_local_paths(paths: list[str]) -> None:
    for path_value in sorted(set(paths), key=len, reverse=True):
        if os.path.isdir(path_value):
            shutil.rmtree(path_value)
        elif os.path.exists(path_value):
            os.remove(path_value)


def _enterprise_intern_root(workspace_id: str, name: str) -> str:
    return os.fspath(intern_runtime_dir(WORK_AGENTS_ROOT, workspace_id, name))


def _remove_enterprise_metadata(contract: dict, paths: list[str], *, name: str) -> None:
    root = _contract_checkout_root(contract)
    if root is None:
        _delete_local_paths(paths)
        return
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return
    rels = _relative_paths_under(root, existing)
    if not os.path.isdir(os.path.join(root, ".git")):
        raise RuntimeError(f"metadata checkout is not a git repo: {root}")
    if contract.get("metadata_mode") == "metadata_branch":
        ensure_metadata_branch_checkout(
            contract,
            workspace_id=str(contract.get("workspace_id") or ""),
            checkout_path=root,
            branch=str(contract.get("metadata_branch") or ""),
        )
    push_metadata = not (
        contract.get("metadata_mode") == "repo_dotdir"
        and contract.get("repo_provider") == "local"
        and not run_git(["config", "--get", "remote.origin.url"], cwd=root, check=False).stdout.strip()
    )
    remove_and_push(
        repo_path=root,
        paths=rels,
        message=f"[{name}] intern: 删除",
        branch=contract.get("metadata_branch") or None,
        push=push_metadata,
    )


def _clear_session_registry(*, name: str, project: str, workspace_id: str, enterprise_mode: bool) -> None:
    sessions_file = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.json")
    if not os.path.isfile(sessions_file):
        return
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            sessions = json.load(f)
        session_key = _session_registry_key(name, project, workspace_id) if enterprise_mode else name
        if isinstance(sessions, dict) and session_key in sessions:
            sessions.pop(session_key, None)
            tmp = sessions_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, sessions_file)
            print(f"🗑️  已删除 session registry: {session_key}")
    except Exception as e:
        print(f"⚠️  session registry 清理失败: {e}", file=sys.stderr)


def _delete_feishu_group(*, name: str, project: str) -> None:
    addr_path = daemon_addr_file()
    try:
        with open(addr_path, "r", encoding="utf-8") as fp:
            port = int(json.load(fp)["http_port"])
        body = json.dumps({"intern_name": name, "project": project}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/group/delete",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print(f"🗑️  已删除飞书群/relay registry: {project}:{name}")
    except Exception as e:
        print(f"⚠️  飞书群/relay registry 清理失败: {e}", file=sys.stderr)


def _run_enterprise_delete(args: argparse.Namespace, *, name: str, project: str, workspace_id: str, force: bool) -> int:
    try:
        contract = resolve_metadata_for_workspace_id(workspace_id, name, "")
        status_path = _require_contract_path(contract, "status_path")
        knowledge_path = _require_contract_path(contract, "knowledge_path")
    except Exception as exc:
        if force:
            print(f"⚠️  无法解析企业 metadata contract: {exc}", file=sys.stderr)
            print("⚠️  --force 已指定，继续清理本地目录、session registry 和飞书群", file=sys.stderr)
            intern_root = _enterprise_intern_root(workspace_id, name)
            if os.path.isdir(intern_root):
                shutil.rmtree(intern_root)
                print(f"🗑️  已删除本地目录: {intern_root}")
            _clear_session_registry(name=name, project=project, workspace_id=workspace_id, enterprise_mode=True)
            _delete_feishu_group(name=name, project=project)
            print(f"\n✅ intern '{name}' 已删除")
            return 0
        print(f"❌ 无法解析企业 metadata contract: {exc}", file=sys.stderr)
        return 1

    if not os.path.exists(status_path):
        if not force:
            print(f"❌ intern '{name}' 不存在（project={project}, workspace={workspace_id}）", file=sys.stderr)
            return 1
        print(f"⚠️  intern '{name}' metadata 不存在，继续清理本地目录", file=sys.stderr)

    meta = parse_status_md(status_path)
    status = meta.get("status", "Unknown")
    if os.path.exists(status_path) and status not in ("Idle", "Unknown", "") and not force:
        print(
            f"❌ intern '{name}' 正在工作中（status={status}），"
            f"请先停止任务再删除",
            file=sys.stderr,
        )
        return 1

    intern_root = _enterprise_intern_root(workspace_id, name)
    task_id = meta.get("task", "")
    delete_paths = [os.path.dirname(status_path)]
    task_contract = contract
    if task_id:
        try:
            task_contract = resolve_metadata_for_workspace_id(workspace_id, name, task_id)
            readme_path = _require_contract_path(task_contract, "task_readme_path")
            delete_paths.append(os.path.dirname(readme_path))
        except Exception as exc:
            print(f"⚠️  无法解析 task metadata contract，跳过 task metadata 删除: {exc}", file=sys.stderr)

    if not args.confirm:
        print(f"⚠️  即将删除 intern '{name}'（project={project}, workspace={workspace_id}）：")
        print(f"   - metadata {status_path}")
        if task_id:
            print(f"   - task metadata {task_id}")
        print(f"   - 本地目录 {intern_root}/")
        answer = input("确认删除？(y/N) ").strip().lower()
        if answer != "y":
            print("已取消")
            return 0

    try:
        _remove_enterprise_metadata(task_contract, delete_paths, name=name)
        print("🗑️  已删除企业 metadata")
    except Exception as e:
        print(f"⚠️  企业 metadata 删除失败: {e}", file=sys.stderr)
        if not force:
            return 1
        print("⚠️  --force 已指定，继续清理本地目录和 session registry", file=sys.stderr)

    if os.path.isdir(intern_root):
        shutil.rmtree(intern_root)
        print(f"🗑️  已删除本地目录: {intern_root}")

    _clear_session_registry(name=name, project=project, workspace_id=workspace_id, enterprise_mode=True)
    _delete_feishu_group(name=name, project=project)

    print(f"\n✅ intern '{name}' 已删除")
    return 0


def run(args: argparse.Namespace) -> int:
    """执行 delete 命令。"""
    name: str = args.name
    project: str = args.project
    force = getattr(args, "force", False) is True
    local_project = getattr(args, "local_project", False) is True
    branch = getattr(args, "branch", None)
    if not isinstance(branch, str) or not branch.strip():
        branch = None

    if not validate_name(name):
        print(f"❌ 名称无效: '{name}'", file=sys.stderr)
        return 1

    enterprise_mode = enterprise_policy_exists(WORK_AGENTS_ROOT) and not local_project
    workspace_id = _find_workspace_id_for_project(project) if enterprise_mode else ""
    if enterprise_mode:
        return _run_enterprise_delete(args, name=name, project=project, workspace_id=workspace_id, force=force)

    # 根据 project 参数决定 master repo 路径和 interns 目录
    master_repo = os.path.join(WORK_AGENTS_ROOT, project)
    interns_dir = os.path.join(master_repo, "workspace", "interns")
    info = get_intern(name, interns_dir=interns_dir)
    if info is None:
        if not force:
            print(f"❌ intern '{name}' 不存在（project={project}）", file=sys.stderr)
            return 1
        print(f"⚠️  intern '{name}' 不存在于 repo，继续清理本地目录", file=sys.stderr)

    # 安全检查：正在工作中拒绝删除
    if info is not None and info.status not in ("Idle", "Unknown", "") and not force:
        print(
            f"❌ intern '{name}' 正在工作中（status={info.status}），"
            f"请先停止任务再删除",
            file=sys.stderr,
        )
        return 1

    # 交互确认
    intern_root = _enterprise_intern_root(workspace_id, name) if enterprise_mode else os.path.join(WORK_AGENTS_ROOT, name)
    if not args.confirm:
        print(f"⚠️  即将删除 intern '{name}'（project={project}）：")
        print(f"   - repo 中的 workspace/interns/{name}/")
        print(f"   - 本地目录 {intern_root}/")
        answer = input("确认删除？(y/N) ").strip().lower()
        if answer != "y":
            print("已取消")
            return 0

    # 1. 从 project repo 删除 workspace/interns/<name>/ 并 push
    intern_ws_rel = os.path.join("workspace", "interns", name)
    intern_ws_abs = os.path.join(master_repo, intern_ws_rel)
    if info is not None and os.path.isdir(intern_ws_abs):
        try:
            remove_and_push(
                repo_path=master_repo,
                paths=[intern_ws_rel],
                message=f"[{name}] intern: 删除",
                branch=branch,
            )
            print(f"🗑️  已从 repo 删除 {intern_ws_rel} 并推送")
        except Exception as e:
            print(f"⚠️  Git 删除失败: {e}", file=sys.stderr)
            # 继续清理本地目录

    # 2. 删除本地 <WORK_AGENTS_ROOT>/<name>/ 目录
    if os.path.isdir(intern_root):
        shutil.rmtree(intern_root)
        print(f"🗑️  已删除本地目录: {intern_root}")

    _clear_session_registry(name=name, project=project, workspace_id=workspace_id, enterprise_mode=False)
    _delete_feishu_group(name=name, project=project)

    print(f"\n✅ intern '{name}' 已删除")
    return 0
