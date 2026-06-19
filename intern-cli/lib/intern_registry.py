"""Intern registry — 扫描/解析 status.md 获取 intern 信息。

数据源：workspace/interns/<name>/status.md（无独立 registry 文件）。
"""

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── 常量 ──────────────────────────────────────
WORK_AGENTS_ROOT: str = os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()
MASTER_REPO: str = os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents")
INTERNS_DIR: str = os.path.join(MASTER_REPO, "workspace", "interns")

# intern name 白名单
NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*$")

# 新建 intern 严格前缀：必须 intern_xxx（历史非 intern_ 前缀的 intern 仅读取/删除，不受此校验影响）
NEW_NAME_PATTERN: re.Pattern[str] = re.compile(r"^intern_[a-z0-9_]+$")

# METADATA 行正则  <!-- METADATA:KEY=VALUE,... -->
_METADATA_RE: re.Pattern[str] = re.compile(r"<!--\s*METADATA:(?P<body>.+?)\s*-->")

INTERN_ROLES: tuple[str, ...] = ("independent", "coordinator", "team_lead", "worker")
DEFAULT_INTERN_ROLE = "independent"


@dataclass
class InternInfo:
    """单个 intern 的注册信息。"""

    name: str
    status: str = "Unknown"
    task: str = ""
    role: str = DEFAULT_INTERN_ROLE
    team_id: str = ""
    type: str = "copilot"
    hook_state_exists: bool = False
    coordinator_id: str = ""
    anchor_project: str = ""
    anchor_repo_path: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def validate_name(name: str) -> bool:
    """校验 intern 名称是否合法（用于读取/删除路径，兼容历史名）。"""
    return bool(NAME_PATTERN.match(name))


def validate_new_name(name: str) -> bool:
    """新建 intern 名称校验：必须以 intern_ 开头，仅含小写字母/数字/下划线。

    历史非 intern_ 前缀的 intern（如 cela/bob/yang）仍可通过 validate_name 被读取/删除，
    但新建必须走此严格校验。
    """
    return bool(NEW_NAME_PATTERN.match(name))


def name_exists_in_repo(name: str, interns_dir: str | None = None) -> bool:
    """检查 master repo 内是否已存在同名 intern（repo 维度唯一性校验）。"""
    base = interns_dir or INTERNS_DIR
    return os.path.isdir(os.path.join(base, name))


def parse_status_md(path: str | Path) -> dict[str, str]:
    """解析 status.md，返回 METADATA 字段字典。

    Returns:
        {"status": "...", "task": "...", "role": "...", "team_id": "..."}  解析失败返回空 dict。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _METADATA_RE.search(line)
                if m:
                    result: dict[str, str] = {}
                    for pair in m.group("body").split(","):
                        if "=" not in pair:
                            continue
                        key, value = pair.split("=", 1)
                        result[key.strip().lower()] = value.strip()
                    role = result.get("role", DEFAULT_INTERN_ROLE)
                    result["role"] = role if role in INTERN_ROLES else DEFAULT_INTERN_ROLE
                    return result
    except OSError:
        pass
    return {}


def _repo_root_from_interns_dir(interns_dir: str) -> Path:
    return Path(interns_dir).resolve().parent.parent


def _load_coordinator_for_intern(interns_dir: str, intern_name: str) -> dict[str, str]:
    repo_root = _repo_root_from_interns_dir(interns_dir)
    coordinators_dir = repo_root / "workspace" / "coordinators"
    if not coordinators_dir.is_dir():
        return {}

    for metadata_path in coordinators_dir.glob("*/coordinator.json"):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("intern_name") != intern_name:
            continue
        anchor = data.get("anchor") if isinstance(data.get("anchor"), dict) else {}
        return {
            "coordinator_id": str(data.get("coordinator_id") or ""),
            "anchor_project": str(anchor.get("project") or ""),
            "anchor_repo_path": str(anchor.get("repo_path") or ""),
        }
    return {}


def _attach_coordinator_metadata(info: InternInfo, interns_dir: str) -> InternInfo:
    if info.role != "coordinator":
        return info
    metadata = _load_coordinator_for_intern(interns_dir, info.name)
    info.coordinator_id = metadata.get("coordinator_id", "")
    info.anchor_project = metadata.get("anchor_project", "")
    info.anchor_repo_path = metadata.get("anchor_repo_path", "")
    return info


def _enterprise_sessions_path() -> Path:
    return Path(WORK_AGENTS_ROOT) / ".intern_sessions.json"


def _load_enterprise_sessions() -> dict[str, dict]:
    path = _enterprise_sessions_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _enterprise_status_path(entry: dict) -> str:
    intern_dir = entry.get("intern_dir") or ""
    if intern_dir:
        state_path = Path(intern_dir) / ".hook_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            state = {}
        resolver = state.get("metadata_resolver") if isinstance(state.get("metadata_resolver"), dict) else {}
        status_path = resolver.get("status_path") or ""
        if status_path:
            return str(status_path)
    return ""


def list_enterprise_interns() -> list[InternInfo]:
    result: list[InternInfo] = []
    seen: set[tuple[str, str]] = set()
    for key, entry in sorted(_load_enterprise_sessions().items()):
        intern_dir = str(entry.get("intern_dir") or "")
        intern_name = str(entry.get("intern_name") or key)
        project = str(entry.get("project") or "")
        workspace_id = str(entry.get("workspace_id") or "")
        if not intern_dir or not validate_name(intern_name):
            continue
        identity = (workspace_id or project, intern_name)
        if identity in seen:
            continue
        seen.add(identity)
        meta = parse_status_md(_enterprise_status_path(entry))
        role = str(entry.get("role") or meta.get("role") or DEFAULT_INTERN_ROLE)
        if role == "helper":
            role = "helper"
        elif role not in INTERN_ROLES:
            role = DEFAULT_INTERN_ROLE
        info = InternInfo(
            name=intern_name,
            status=meta.get("status", "Unknown"),
            task=meta.get("task", ""),
            role=role,
            team_id=meta.get("team_id", ""),
            type=str(entry.get("type") or "copilot"),
            hook_state_exists=os.path.isfile(os.path.join(intern_dir, ".hook_state.json")),
            extra={
                "project": project,
                "workspace_id": workspace_id,
                "intern_dir": intern_dir,
                "session_key": key,
            },
        )
        result.append(info)
    return result


def list_interns(interns_dir: str | None = None) -> list[InternInfo]:
    """扫描 interns 目录，返回所有已注册 intern 的信息列表。"""
    base = interns_dir or INTERNS_DIR
    result: list[InternInfo] = []
    if interns_dir is None:
        result.extend(list_enterprise_interns())
    if not os.path.isdir(base):
        return result

    for name in sorted(os.listdir(base)):
        entry = os.path.join(base, name)
        if not os.path.isdir(entry):
            continue
        status_file = os.path.join(entry, "status.md")
        meta = parse_status_md(status_file)
        info = InternInfo(
            name=name,
            status=meta.get("status", "Unknown"),
            task=meta.get("task", ""),
            role=meta.get("role", DEFAULT_INTERN_ROLE),
            team_id=meta.get("team_id", ""),
        )
        result.append(_attach_coordinator_metadata(info, base))
    return result


def get_intern(name: str, interns_dir: str | None = None, project: str | None = None) -> Optional[InternInfo]:
    """获取单个 intern 的信息，不存在返回 None。"""
    if interns_dir is None:
        enterprise_matches = [
            item for item in list_enterprise_interns()
            if item.name == name and (not project or item.extra.get("project") == project or item.extra.get("workspace_id") == project)
        ]
        if len(enterprise_matches) == 1:
            return enterprise_matches[0]
    base = interns_dir or INTERNS_DIR
    entry = os.path.join(base, name)
    if not os.path.isdir(entry):
        return None
    status_file = os.path.join(entry, "status.md")
    meta = parse_status_md(status_file)
    return _attach_coordinator_metadata(InternInfo(
        name=name,
        status=meta.get("status", "Unknown"),
        task=meta.get("task", ""),
        role=meta.get("role", DEFAULT_INTERN_ROLE),
        team_id=meta.get("team_id", ""),
    ), base)
