"""Compatibility facade for personal-edition state_v1 helpers.

Enterprise stores workspace authority in the relay and daemon cache, but some
shared user-side commands are intentionally reused from the personal edition.
This module exposes the subset of the personal ``lib.state_v1`` contract those
commands need, backed by enterprise ``state/v1`` files.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.metadata_checkout import ensure_metadata_branch_checkout


METADATA_BRANCH = "intern_workspace"


def work_agents_root() -> Path:
    return Path(os.environ.get("WORK_AGENTS_ROOT") or os.getcwd())


def state_root(root: Path | None = None) -> Path:
    return (root or work_agents_root()) / "state" / "v1"


def safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._")
    if not safe:
        raise ValueError("name must contain at least one safe character")
    return safe


def intern_runtime_dir(root: Path, workspace_key: str, intern: str) -> Path:
    return state_root(root) / safe_segment(workspace_key) / "interns" / safe_segment(intern)


def workspace_metadata_checkout_path(root: Path, workspace_key: str) -> Path:
    return state_root(root) / safe_segment(workspace_key) / "metadata" / "branch"


@dataclass
class WorkspaceRef:
    workspace_key: str
    path: Path
    data: dict


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _daemon_cache(root: Path) -> dict:
    return _load_json(state_root(root) / "daemon-workspaces.json")


def _workspace_key_from_record(workspace_id: str, record: dict) -> str:
    explicit = str(record.get("workspace_key") or "").strip()
    if explicit:
        return explicit
    return workspace_id


def _personal_workspace_shape(root: Path, workspace_id: str, record: dict, record_path: Path) -> WorkspaceRef:
    cache = _daemon_cache(root)
    enabled = cache.get("enabled") if isinstance(cache.get("enabled"), dict) else {}
    local = enabled.get(workspace_id) if isinstance(enabled, dict) else {}
    local = local if isinstance(local, dict) else {}
    workspace_key = _workspace_key_from_record(workspace_id, record)
    mode = record.get("metadata_mode") or record.get("mode") or "repo_dotdir"
    local_path = local.get("local_path") or record.get("local_path") or str(state_root(root) / safe_segment(workspace_key) / "source")
    metadata_cache = local.get("metadata_cache_path") or record.get("metadata_cache_path") or str(
        state_root(root) / safe_segment(workspace_key) / "metadata" / "branch"
    )
    metadata = {
        "mode": mode,
        "branch": record.get("metadata_branch") or METADATA_BRANCH,
        "repo_relative_path": record.get("repo_relative_path") or ".intern_workspace",
    }
    if mode == "local_only":
        metadata["local_path"] = str(Path(metadata_cache) / "local" / ".intern_workspace")
    data = dict(record)
    data.update({
        "workspace_key": workspace_key,
        "workspace_id": workspace_id,
        "display_name": record.get("display_name") or record.get("name") or workspace_id,
        "local_path": local_path,
        "repo_url": record.get("repo_url") or local_path,
        "metadata": metadata,
    })
    return WorkspaceRef(workspace_key=workspace_key, path=record_path, data=data)


class StateStore:
    def __init__(self, work_root: Path | str | None = None):
        self.work_root = Path(work_root) if work_root is not None else work_agents_root()

    def _registry(self) -> dict:
        return _load_json(state_root(self.work_root) / "registry.json")

    def list_workspaces(self) -> list[WorkspaceRef]:
        registry = self._registry()
        workspaces = registry.get("workspaces") if isinstance(registry.get("workspaces"), dict) else {}
        refs: list[WorkspaceRef] = []
        for workspace_id, rel_path in sorted(workspaces.items()):
            record_path = state_root(self.work_root) / str(rel_path)
            record = _load_json(record_path)
            if not record:
                continue
            refs.append(_personal_workspace_shape(self.work_root, str(workspace_id), record, record_path))
        return refs

    def load_workspace(self, workspace_key: str) -> WorkspaceRef:
        for ref in self.list_workspaces():
            if workspace_key in {ref.workspace_key, ref.data.get("workspace_id"), ref.data.get("display_name")}:
                return ref
        raise KeyError(f"workspace not found: {workspace_key}")


def list_state_interns(store: StateStore, workspace_key: str | None = None) -> list[dict]:
    sessions = _load_json(store.work_root / ".intern_sessions.json")
    result: list[dict] = []
    for key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        # Legacy session maps were keyed only by intern name and carried no
        # workspace identity. Keep those out of the state-v1 projection so
        # callers can use their existing legacy fallback paths.
        if "workspace_id" not in value and ":" not in str(key):
            continue
        name = value.get("intern_name") or str(key).split(":", 1)[-1]
        ws_id = str(value.get("workspace_id") or str(key).split(":", 1)[0])
        try:
            ref = store.load_workspace(ws_id)
            ws_key = ref.workspace_key
            project = ref.data.get("display_name") or value.get("project") or ws_id
        except Exception:
            ws_key = ws_id
            project = value.get("project") or ws_id
        if workspace_key and workspace_key not in {ws_key, ws_id, project}:
            continue
        intern_dir = value.get("intern_dir") or str(intern_runtime_dir(store.work_root, ws_key, name))
        code_path = value.get("code_worktree_path") or value.get("code_repo_path") or str(Path(intern_dir) / str(project))
        result.append({
            "name": name,
            "type": value.get("type") or "codex",
            "workspace_key": ws_key,
            "workspace_id": ws_id,
            "project": project,
            "intern_dir": intern_dir,
            "code_worktree_path": code_path,
        })
    return result


def ensure_git_identity(repo: Path) -> None:
    try:
        subprocess.run(["git", "config", "user.name"], cwd=repo, check=True, capture_output=True, text=True)
    except Exception:
        subprocess.run(["git", "config", "user.name", "intern-agent"], cwd=repo, check=False, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "intern-agent@local"], cwd=repo, check=False, capture_output=True, text=True)


def ensure_metadata_base_branch(_repo_url: str, _branch: str) -> None:
    return None


def ensure_metadata_checkout(store: StateStore, workspace_key: str, intern: str = "") -> dict:
    ref = store.load_workspace(workspace_key)
    branch = ref.data.get("metadata", {}).get("branch") or METADATA_BRANCH
    checkout = workspace_metadata_checkout_path(store.work_root, ref.workspace_key)
    workspace = dict(ref.data)
    workspace["metadata_branch"] = branch
    workspace["metadata_cache_path"] = str(checkout)
    ensure_metadata_branch_checkout(workspace, workspace_id=ref.data.get("workspace_id") or ref.workspace_key, checkout_path=str(checkout))
    return {
        "metadata_checkout_path": str(checkout),
        "workspace_key": ref.workspace_key,
        "intern": intern,
    }
