"""internctl init <name> — 初始化 intern 本地工作目录（intern 已在 master repo 中注册）。

用于在新的 WORK_AGENTS_ROOT 下首次使用已有 intern 时，自动 clone 工作 repo 并创建辅助目录。
不创建 status.md，不 push to master。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from lib.intern_registry import WORK_AGENTS_ROOT, validate_name
from lib.git_ops import clone

DEFAULT_REPO_URL: str = "git@codeup.aliyun.com:finalsystems/chlxydl/axis_intern_agents.git"


def _ensure_hook_state(intern_root: str, project: str) -> None:
    """确保 .hook_state.json 存在且包含 project 字段。"""
    state_path = os.path.join(intern_root, ".hook_state.json")
    state: dict = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    if state.get("project") == project:
        return
    state["project"] = project
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.rename(tmp, state_path)


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("init", help="初始化 intern 本地工作目录（已注册的 intern）")
    p.add_argument("name", help="intern 名称")
    p.add_argument("--project", default="axis_intern_agents", help="项目名称")
    p.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Git repo URL")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    name: str = args.name
    project: str = args.project
    repo_url: str = args.repo_url

    if not validate_name(name):
        print(f"❌ 名称无效: '{name}'", file=sys.stderr)
        return 1

    intern_root = os.path.join(WORK_AGENTS_ROOT, name)
    intern_repo = os.path.join(intern_root, project)

    # 已存在则跳过（但仍补写缺失的 .hook_state.json）
    if os.path.exists(intern_repo):
        _ensure_hook_state(intern_root, project)
        print(f"✅ 工作目录已存在: {intern_root}")
        return 0

    # 1. 创建辅助目录
    for sub in ("debug", "outputs"):
        os.makedirs(os.path.join(intern_root, sub), exist_ok=True)

    # 2. Clone 工作 repo
    print(f"📦 Cloning {repo_url} → {intern_repo} ...")
    try:
        clone(repo_url, intern_repo)
    except Exception as e:
        print(f"❌ Clone 失败: {e}", file=sys.stderr)
        return 1

    # 3. 预创建 .hook_state.json（含 project 信息，hooks 只读写不重建）
    _ensure_hook_state(intern_root, project)

    print(f"✅ intern '{name}' 工作目录已就绪: {intern_root}")
    return 0
