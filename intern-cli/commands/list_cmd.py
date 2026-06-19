"""internctl list [--json] — 列出所有已注册 intern。"""

from __future__ import annotations

import argparse
import json
import sys

from lib.intern_registry import list_interns, INTERNS_DIR


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """注册 list 子命令。"""
    p = subparsers.add_parser("list", help="列出所有已注册 intern")
    p.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON 格式")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """执行 list 命令。"""
    interns = list_interns()

    if args.as_json:
        data = [
            {
                "name": i.name,
                "status": i.status,
                "task": i.task or None,
                "type": i.type or "copilot",
                "role": i.role,
                "team_id": i.team_id or None,
                "coordinator_id": i.coordinator_id or None,
                "anchor_project": i.anchor_project or None,
                "project": i.extra.get("project") or None,
                "workspace_id": i.extra.get("workspace_id") or None,
                "intern_dir": i.extra.get("intern_dir") or None,
            }
            for i in interns
        ]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    # 表格输出
    if not interns:
        print("（无已注册 intern）")
        return 0

    name_w = max(len("NAME"), max(len(i.name) for i in interns))
    status_w = max(len("STATUS"), max(len(i.status) for i in interns))
    role_w = max(len("ROLE"), max(len(i.role) for i in interns))
    project_w = max(len("PROJECT"), max(len(i.extra.get("project") or "-") for i in interns))
    team_w = max(len("TEAM"), max(len(i.team_id or "-") for i in interns))
    anchor_w = max(len("ANCHOR"), max(len(i.anchor_project or "-") for i in interns))
    task_w = max(len("TASK"), max(len(i.task or "-") for i in interns))

    header = f"{'NAME':<{name_w}}  {'STATUS':<{status_w}}  {'ROLE':<{role_w}}  {'PROJECT':<{project_w}}  {'TEAM':<{team_w}}  {'ANCHOR':<{anchor_w}}  {'TASK':<{task_w}}"
    print(header)
    for i in interns:
        task_display = i.task or "-"
        team_display = i.team_id or "-"
        anchor_display = i.anchor_project or "-"
        project_display = i.extra.get("project") or "-"
        print(f"{i.name:<{name_w}}  {i.status:<{status_w}}  {i.role:<{role_w}}  {project_display:<{project_w}}  {team_display:<{team_w}}  {anchor_display:<{anchor_w}}  {task_display:<{task_w}}")

    return 0
