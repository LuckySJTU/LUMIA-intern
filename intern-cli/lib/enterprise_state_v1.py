"""Enterprise state v1 path helpers.

Relay remains authoritative for workspace identity and policy.  These helpers
only derive daemon-local execution/cache paths from relay workspace_id values.
"""

from __future__ import annotations

import re
from pathlib import Path


WORKSPACE_ID_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")
SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
LOCAL_REGISTRY_SCHEMA = "intern-agents.local-registry.v1"
WORKSPACE_SCHEMA = "intern-agents.workspace.v1"


def validate_workspace_id(workspace_id: str) -> str:
    value = (workspace_id or "").strip()
    if not WORKSPACE_ID_RE.fullmatch(value):
        raise ValueError("workspace_id must match [a-z0-9_][a-z0-9_.-]*")
    return value


def safe_segment(value: str) -> str:
    safe = SAFE_SEGMENT_RE.sub("_", (value or "").strip()).strip("._")
    if not safe:
        raise ValueError("name must contain at least one safe character")
    return safe


def state_root(work_root: str | Path) -> Path:
    return Path(work_root) / "state" / "v1"


def state_registry_path(work_root: str | Path) -> Path:
    return state_root(work_root) / "registry.json"


def daemon_workspace_cache_path(work_root: str | Path) -> Path:
    return state_root(work_root) / "daemon-workspaces.json"


def workspace_state_dir(work_root: str | Path, workspace_id: str) -> Path:
    return state_root(work_root) / validate_workspace_id(workspace_id)


def workspace_record_path(work_root: str | Path, workspace_id: str) -> Path:
    return workspace_state_dir(work_root, workspace_id) / "workspace.json"


def workspace_source_path(work_root: str | Path, workspace_id: str) -> Path:
    return workspace_state_dir(work_root, workspace_id) / "source"


def workspace_metadata_cache_path(work_root: str | Path, workspace_id: str) -> Path:
    return workspace_state_dir(work_root, workspace_id) / "metadata" / "branch"


def intern_runtime_dir(work_root: str | Path, workspace_id: str, intern_name: str) -> Path:
    return workspace_state_dir(work_root, workspace_id) / "interns" / safe_segment(intern_name)
