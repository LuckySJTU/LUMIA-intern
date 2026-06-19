"""internctl status <name> [--json] — 显示 intern 详情。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from lib.intern_registry import (
    WORK_AGENTS_ROOT,
    get_intern,
    validate_name,
)


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """注册 status 子命令。"""
    p = subparsers.add_parser("status", help="显示 intern 详情")
    p.add_argument("name", help="intern 名称")
    p.add_argument("--project", default="", help="enterprise 模式下用于消除同名 intern 歧义")
    p.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON 格式")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """执行 status 命令。"""
    name: str = args.name

    if not validate_name(name):
        print(f"❌ 名称无效: '{name}'", file=sys.stderr)
        return 1

    info = get_intern(name, project=args.project or None)
    if info is None:
        print(f"❌ intern '{name}' 不存在", file=sys.stderr)
        return 1

    # 检查 .hook_state.json
    if not info.hook_state_exists:
        hook_state_path = os.path.join(WORK_AGENTS_ROOT, name, ".hook_state.json")
        info.hook_state_exists = os.path.isfile(hook_state_path)

    if args.as_json:
        data = {
            "name": info.name,
            "status": info.status,
            "task": info.task or None,
            "role": info.role,
            "team_id": info.team_id or None,
            "coordinator_id": info.coordinator_id or None,
            "anchor_project": info.anchor_project or None,
            "anchor_repo_path": info.anchor_repo_path or None,
            "project": info.extra.get("project") or None,
            "workspace_id": info.extra.get("workspace_id") or None,
            "intern_dir": info.extra.get("intern_dir") or None,
            "hook_state_exists": info.hook_state_exists,
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"Name:              {info.name}")
    print(f"Status:            {info.status}")
    print(f"Role:              {info.role}")
    print(f"Team:              {info.team_id or '-'}")
    if info.extra.get("project"):
        print(f"Project:           {info.extra.get('project')}")
    if info.extra.get("workspace_id"):
        print(f"Workspace:         {info.extra.get('workspace_id')}")
    if info.role == "coordinator":
        print(f"Coordinator ID:    {info.coordinator_id or '-'}")
        print(f"Anchor project:    {info.anchor_project or '-'}")
        print(f"Anchor repo:       {info.anchor_repo_path or '-'}")
    print(f"Task:              {info.task or '-'}")
    print(f"Hook state file:   {'✓ exists' if info.hook_state_exists else '✗ not found'}")

    return 0
