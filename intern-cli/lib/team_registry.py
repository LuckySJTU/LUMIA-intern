"""Workspace team metadata registry."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.intern_registry import WORK_AGENTS_ROOT, get_intern, list_enterprise_interns, validate_name
from lib.enterprise_state_v1 import (
    LOCAL_REGISTRY_SCHEMA,
    daemon_workspace_cache_path,
    state_registry_path,
)

TEAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_TEAM_WORKERS = 5


def validate_team_name(team_name: str) -> bool:
    return (
        bool(TEAM_NAME_PATTERN.fullmatch(team_name))
        and os.path.basename(team_name) == team_name
        and team_name not in (".", "..")
    )


def default_team_lead_name(team_name: str) -> str:
    return f"intern_{team_name}_lead"


def team_lead_management_task_id(team_name: str) -> str:
    return f"{team_name}_lead"


def default_worker_names(team_name: str, worker_count: int) -> list[str]:
    if worker_count < 0 or worker_count > MAX_TEAM_WORKERS:
        raise ValueError(f"worker_count must be between 0 and {MAX_TEAM_WORKERS}")
    return [f"intern_{team_name}_worker_{idx}" for idx in range(1, worker_count + 1)]

def _enterprise_project_paths(project: str) -> dict[str, str]:
    workspace_id = _workspace_id_for_project_from_cache(project)
    if workspace_id:
        cache_paths = _enterprise_workspace_cache_paths(workspace_id)
        if cache_paths:
            return cache_paths
    workspace_id = ""
    for info in list_enterprise_interns():
        if info.extra.get("project") != project and info.extra.get("workspace_id") != project:
            continue
        workspace_id = info.extra.get("workspace_id") or project
        cache_paths = _enterprise_workspace_cache_paths(workspace_id)
        if cache_paths:
            return cache_paths
        intern_dir = info.extra.get("intern_dir") or ""
        state_path = Path(intern_dir) / ".hook_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        resolver = state.get("metadata_resolver") if isinstance(state.get("metadata_resolver"), dict) else {}
        metadata_root = resolver.get("metadata_root") or ""
        checkout = resolver.get("metadata_checkout_path") or ""
        if metadata_root and checkout:
            return {"repo": str(checkout), "metadata_root": str(metadata_root)}
        if metadata_root:
            return {"repo": str(Path(metadata_root).parent), "metadata_root": str(metadata_root)}
    return {}


def _workspace_id_for_project_from_cache(project: str) -> str:
    for workspace_id, workspace in _load_workspace_records_from_state().items():
        if project in {workspace_id, workspace.get("display_name") or ""}:
            return str(workspace_id)
    for workspace_id, workspace in _load_workspace_records_from_daemon_cache().items():
        if project in {workspace_id, workspace.get("display_name") or ""}:
            return str(workspace_id)
    return ""


def _enterprise_workspace_cache_paths(workspace_id: str) -> dict[str, str]:
    if not workspace_id:
        return {}
    workspace = (
        _load_workspace_records_from_state().get(workspace_id)
        or _load_workspace_records_from_daemon_cache().get(workspace_id)
        or {}
    )
    local = _load_enabled_workspaces().get(workspace_id) or {}
    if not workspace or not local.get("enabled"):
        return {}
    mode = workspace.get("metadata_mode") or ""
    local_path = local.get("local_path") or ""
    metadata_cache = local.get("metadata_cache_path") or ""
    if mode == "repo_dotdir" and local_path:
        return {"repo": local_path, "metadata_root": os.path.join(local_path, ".intern_workspace")}
    if mode == "metadata_branch" and metadata_cache:
        return {"repo": metadata_cache, "metadata_root": os.path.join(metadata_cache, ".intern_workspace")}
    if mode == "local_only" and metadata_cache:
        return {"repo": metadata_cache, "metadata_root": os.path.join(metadata_cache, "local", ".intern_workspace")}
    return {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_workspace_records_from_state() -> dict[str, dict[str, Any]]:
    registry = _read_json(state_registry_path(WORK_AGENTS_ROOT))
    if registry.get("schema") != LOCAL_REGISTRY_SCHEMA:
        return {}
    result: dict[str, dict[str, Any]] = {}
    workspaces = registry.get("workspaces") if isinstance(registry.get("workspaces"), dict) else {}
    for workspace_id, rel_path in workspaces.items():
        if not isinstance(rel_path, str) or not rel_path:
            continue
        path = Path(WORK_AGENTS_ROOT) / "state" / "v1" / rel_path
        workspace = _read_json(path)
        if workspace:
            result[str(workspace_id)] = workspace
    return result


def _load_workspace_records_from_daemon_cache() -> dict[str, dict[str, Any]]:
    candidates = [
        daemon_workspace_cache_path(WORK_AGENTS_ROOT),
        Path(WORK_AGENTS_ROOT) / ".enterprise_state" / "workspaces.json",
    ]
    result: dict[str, dict[str, Any]] = {}
    for cache_path in candidates:
        data = _read_json(cache_path)
        workspaces = data.get("workspaces") if isinstance(data.get("workspaces"), dict) else {}
        for workspace_id, workspace in workspaces.items():
            if isinstance(workspace, dict):
                result[str(workspace_id)] = workspace
    return result


def _load_enabled_workspaces() -> dict[str, dict[str, Any]]:
    candidates = [
        daemon_workspace_cache_path(WORK_AGENTS_ROOT),
        Path(WORK_AGENTS_ROOT) / ".enterprise_state" / "workspaces.json",
    ]
    for cache_path in candidates:
        data = _read_json(cache_path)
        enabled = data.get("enabled") if isinstance(data.get("enabled"), dict) else {}
        if enabled:
            return enabled
    return {}


def project_repo_path(project: str) -> str:
    enterprise = _enterprise_project_paths(project)
    if enterprise:
        return enterprise["repo"]
    return os.path.join(WORK_AGENTS_ROOT, project)


def project_metadata_prefix(project: str) -> str:
    enterprise = _enterprise_project_paths(project)
    if not enterprise:
        return "workspace"
    try:
        rel = os.path.relpath(enterprise["metadata_root"], enterprise["repo"])
    except ValueError:
        rel = ".intern_workspace"
    return rel if rel and rel != "." else "."


def project_metadata_rel_path(project: str, *parts: str) -> str:
    prefix = project_metadata_prefix(project)
    if prefix == ".":
        return os.path.join(*parts)
    return os.path.join(prefix, *parts)


def project_metadata_abs_path(project: str, *parts: str) -> str:
    return os.path.join(project_repo_path(project), project_metadata_rel_path(project, *parts))


def teams_dir(project: str) -> str:
    return project_metadata_abs_path(project, "teams")


def coordinators_dir(project: str) -> str:
    return project_metadata_abs_path(project, "coordinators")


def interns_dir(project: str) -> str:
    return project_metadata_abs_path(project, "interns")


def tasks_dir(project: str) -> str:
    return project_metadata_abs_path(project, "tasks")


def team_json_path(project: str, team_name: str) -> str:
    return os.path.join(teams_dir(project), team_name, "team.json")


def coordinator_json_path(project: str, coordinator_id: str) -> str:
    return os.path.join(coordinators_dir(project), coordinator_id, "coordinator.json")


def validate_team_id(team_id: str) -> bool:
    return validate_team_name(team_id)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_member(intern_name: str, project: str, created_at: str) -> dict[str, str]:
    return {
        "intern_name": intern_name,
        "project": project,
        "workspace_root": os.path.join(WORK_AGENTS_ROOT, intern_name, project),
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
    }


def list_teams(project: str) -> list[dict[str, Any]]:
    base = teams_dir(project)
    if not os.path.isdir(base):
        return []
    result: list[dict[str, Any]] = []
    for team_name in sorted(os.listdir(base)):
        path = team_json_path(project, team_name)
        if not os.path.isfile(path):
            continue
        result.append(read_team(project, team_name))
    return result


def read_team(project: str, team_name: str) -> dict[str, Any]:
    path = team_json_path(project, team_name)
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"team metadata must be an object: {path}")
    return data


def read_coordinator(project: str, coordinator_id: str) -> dict[str, Any]:
    path = coordinator_json_path(project, coordinator_id)
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"coordinator metadata must be an object: {path}")
    return data


def write_team(project: str, team_name: str, data: dict[str, Any]) -> None:
    path = team_json_path(project, team_name)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp, path)


def write_coordinator(project: str, coordinator_id: str, data: dict[str, Any]) -> None:
    path = coordinator_json_path(project, coordinator_id)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp, path)


def validate_member_role(project: str, intern_name: str, expected_role: str, team_name: str) -> None:
    if not validate_name(intern_name):
        raise ValueError(f"invalid intern name: {intern_name}")
    info = get_intern(intern_name, interns_dir=interns_dir(project))
    if info is None:
        raise ValueError(f"intern '{intern_name}' does not exist in this project")
    if info.role != expected_role:
        raise ValueError(f"intern '{intern_name}' role is {info.role}, expected {expected_role}")
    if info.team_id != team_name:
        raise ValueError(f"intern '{intern_name}' TEAM_ID is {info.team_id}, expected {team_name}")


def build_team_metadata(
    *,
    project: str,
    team_id: str,
    team_lead: str,
    workers: list[str],
    coordinator: dict[str, str] | None,
) -> dict[str, Any]:
    created_at = utc_now()
    data: dict[str, Any] = {
        "schema_version": 1,
        "team_name": team_id,
        "team_id": team_id,
        "project": project,
        "workspace_root": project_repo_path(project),
        "team_lead": {
            **make_member(team_lead, project, created_at),
            "management_task": {
                "task_id": team_lead_management_task_id(team_id),
                "status": "InProgress",
                "lifecycle": "exists_while_team_exists",
                "completion_policy": "never_complete_while_team_exists",
            },
        },
        "workers": [make_member(worker, project, created_at) for worker in workers],
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if coordinator:
        data["coordinator"] = coordinator
    return data
