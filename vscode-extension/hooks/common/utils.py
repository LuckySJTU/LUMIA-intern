"""
Hooks 共享工具函数（VS Code + Claude CLI 通用）。
- get_intern_dir() 优先使用 INTERN_DIR 环境变量（Claude CLI），回退到 sessionId 映射（VS Code）
"""
import os
import json
import hashlib
import re
import fcntl
import subprocess
import sys
from datetime import datetime
from contextlib import contextmanager

# ============================================================
# 路径解析
# ============================================================

WORK_AGENTS_ROOT = os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()
SHARED_REPO = os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents")

# Session → Intern 映射文件
SESSION_MAP_FILE = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.json")
SESSION_MAP_LOCK = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.lock")


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return safe or value


def _split_scoped_intern_key(value: str) -> tuple[str, str]:
    if isinstance(value, str) and ":" in value:
        project, name = value.split(":", 1)
        return project, name
    return "", value


def _session_entry_for_intern(identifier: str) -> dict:
    data = load_session_map()
    entry = data.get(identifier)
    if isinstance(entry, dict):
        return entry
    project, name = _split_scoped_intern_key(identifier)
    for key, candidate in data.items():
        if not isinstance(candidate, dict):
            continue
        if key == identifier:
            return candidate
        if candidate.get("intern_name") != name:
            continue
        if project and candidate.get("project") not in {"", project, None}:
            continue
        return candidate
    return {}


def _state_intern_dir(identifier: str) -> str:
    """Resolve a state-v1 intern runtime directory from a session map value."""
    if not identifier:
        return ""
    project, name = _split_scoped_intern_key(identifier)
    entry = _session_entry_for_intern(identifier)
    intern_dir = entry.get("intern_dir") if isinstance(entry, dict) else ""
    if intern_dir and os.path.isdir(intern_dir):
        return intern_dir

    state_root = os.path.join(WORK_AGENTS_ROOT, "state", "v1")
    safe = _safe_segment(name)
    if os.path.isdir(state_root):
        for workspace_key in os.listdir(state_root):
            record_path = os.path.join(state_root, workspace_key, "interns", safe, "intern.json")
            if not os.path.isfile(record_path):
                continue
            try:
                with open(record_path, "r", encoding="utf-8") as fp:
                    record = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            record_dir = record.get("intern_dir")
            if record_dir and os.path.isdir(record_dir):
                return record_dir
            candidate = os.path.dirname(record_path)
            if os.path.isdir(candidate):
                return candidate
    return ""


def get_intern_dir(cwd: str, session_id: str = "") -> str:
    """获取 intern 根目录。

    优先级：
    1. INTERN_DIR 环境变量（Claude CLI 模式）
    2. sessionId 映射文件（VS Code 模式）

    Returns:
        intern 根目录路径（如 <WORK_AGENTS_ROOT>/intern_rule_cela）

    Raises:
        ValueError: 映射不存在（该 session 未绑定 intern）
    """
    # 优先使用 INTERN_DIR 环境变量（Claude CLI 模式）。测试和 VS Code 场景
    # 可能继承到外层 shell 的 INTERN_DIR；只有它属于当前 WORK_AGENTS_ROOT
    # 时才视为本轮 hook 的权威来源。
    env_dir = os.environ.get("INTERN_DIR")
    if env_dir and os.path.isdir(env_dir):
        try:
            common = os.path.commonpath([os.path.realpath(WORK_AGENTS_ROOT), os.path.realpath(env_dir)])
        except ValueError:
            common = ""
        if common == os.path.realpath(WORK_AGENTS_ROOT):
            return env_dir

    # 回退到 session_id 映射（VS Code 模式）
    if session_id:
        intern_key = get_session_intern(session_id)
        if intern_key:
            intern_dir = _state_intern_dir(intern_key)
            if intern_dir:
                return intern_dir
            _, intern_name = _split_scoped_intern_key(intern_key)
            intern_dir = os.path.join(WORK_AGENTS_ROOT, intern_name)
            if os.path.isdir(intern_dir):
                return intern_dir

    raise ValueError(
        f"No intern mapped for session {session_id!r}."
    )


def get_intern_name(intern_dir: str) -> str:
    """从 intern 目录提取 intern name。"""
    return os.path.basename(intern_dir.rstrip("/"))


def get_project_repo(intern_dir: str) -> str:
    """获取项目 repo 路径。从 hook_state.json 读 project 字段。"""
    env_repo = os.environ.get("PROJECT_REPO")
    if env_repo and os.path.isdir(env_repo):
        return env_repo
    # 从 hook_state 读 project name（缺失则抛错，不 fallback）
    state_path = os.path.join(intern_dir, STATE_FILE)
    with open(state_path, "r") as f:
        state = json.load(f)
    resolver = state.get("metadata_resolver") if isinstance(state.get("metadata_resolver"), dict) else {}
    for key in ("code_worktree_path", "code_repo_path"):
        repo_path = resolver.get(key)
        if repo_path and os.path.isdir(repo_path):
            return repo_path
    for key in ("code_worktree_path", "project_repo", "repo_dir"):
        repo_path = state.get(key)
        if repo_path and os.path.isdir(repo_path):
            return repo_path
    project = state.get("project")
    if not project:
        raise ValueError(f".hook_state.json missing 'project' field in {intern_dir}")
    return os.path.join(intern_dir, project)


def get_workspace_dir(intern_dir: str) -> str:
    """获取 metadata workspace 根目录。"""
    return get_metadata_context(intern_dir)["metadata_root"]


def _first_dict(*values):
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_string(*values):
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def is_machine_helper_state(state: dict, intern_name: str = "") -> bool:
    """Return True for projectless machine helper/debugger hook state."""
    if not isinstance(state, dict):
        return False
    helper = state.get("helper") if isinstance(state.get("helper"), dict) else {}
    role = _first_string(state.get("role"), helper.get("role"))
    name = _first_string(state.get("intern_name"), helper.get("helper_id"), intern_name)
    if role == "machine_helper":
        return True
    return bool(
        state.get("projectless") is True
        and (
            name.startswith("machine_helper_")
            or bool(helper)
            or role in ("machine_debugger", "debugger")
        )
    )


def machine_helper_chat_id_from_state(state: dict) -> str:
    """Return the dedicated helper chat id from hook state, if present."""
    if not isinstance(state, dict):
        return ""
    helper = state.get("helper") if isinstance(state.get("helper"), dict) else {}
    feishu = state.get("feishu") if isinstance(state.get("feishu"), dict) else {}
    return _first_string(helper.get("chat_id"), feishu.get("chat_id"))


class MetadataResolverError(RuntimeError):
    """Raised when the enterprise metadata resolver contract is unavailable."""


_RESOLVER_CONTRACT_KEYS = (
    "metadata_root",
    "metadata_root_path",
    "status_path",
    "knowledge_path",
    "tasks_dir",
    "project_rule_path",
    "error_book_path",
)


def _read_hook_state_strict(intern_dir: str) -> dict:
    state_path = os.path.join(intern_dir, STATE_FILE)
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise MetadataResolverError(f"missing hook state: {state_path}") from exc
    except json.JSONDecodeError as exc:
        raise MetadataResolverError(f"invalid hook state JSON: {state_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MetadataResolverError(f"{state_path} must contain a JSON object")
    return data


def _metadata_contract_from_state(state: dict) -> dict:
    return _first_dict(
        state.get("metadata_resolver"),
        state.get("metadata_context"),
        state.get("workspace_metadata"),
    )


def _workspace_id_from_state(state: dict) -> str:
    contract = _metadata_contract_from_state(state)
    workspace = _first_dict(state.get("workspace"), contract.get("workspace"))
    return _first_string(
        contract.get("workspace_id"),
        contract.get("workspace_key"),
        workspace.get("workspace_id"),
        workspace.get("workspace_key"),
        state.get("workspace_id"),
        state.get("workspace_key"),
    )


def _repo_path_from_state(intern_dir: str, state: dict) -> str:
    contract = _metadata_contract_from_state(state)
    for key in ("code_worktree_path", "code_repo_path"):
        value = contract.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("code_worktree_path", "project_repo", "repo_dir"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    project = state.get("project")
    if isinstance(project, str) and project:
        return os.path.join(intern_dir, project)
    return ""


def _runtime_repo_path_from_state(intern_dir: str, state: dict) -> str:
    for key in ("code_worktree_path", "project_repo", "repo_dir"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    project = state.get("project")
    if isinstance(project, str) and project:
        return os.path.join(intern_dir, project)
    return _repo_path_from_state(intern_dir, state)


def _metadata_mode_from_state(state: dict) -> str:
    contract = _metadata_contract_from_state(state)
    workspace = _first_dict(state.get("workspace"), contract.get("workspace"))
    metadata = _first_dict(
        contract.get("metadata"),
        workspace.get("metadata"),
        state.get("metadata"),
    )
    return _first_string(
        contract.get("metadata_mode"),
        contract.get("mode"),
        metadata.get("metadata_mode"),
        metadata.get("mode"),
        workspace.get("metadata_mode"),
        state.get("metadata_mode"),
        state.get("workspace_mode"),
    )


def _has_resolver_paths(data: dict) -> bool:
    return any(data.get(key) for key in _RESOLVER_CONTRACT_KEYS)


def _has_enterprise_metadata_marker(state: dict) -> bool:
    contract = _metadata_contract_from_state(state)
    workspace = _first_dict(state.get("workspace"), contract.get("workspace"))
    metadata = _first_dict(contract.get("metadata"), workspace.get("metadata"), state.get("metadata"))
    return bool(
        contract
        or workspace
        or metadata
        or _workspace_id_from_state(state)
        or _metadata_mode_from_state(state)
        or state.get("metadata_branch")
        or state.get("metadata_checkout_path")
        or state.get("local_metadata_root")
    )


def _daemon_bundle_dir() -> str:
    pid_file = os.environ.get("FEISHU_DAEMON_ADDR_FILE")
    if not pid_file:
        import hashlib

        root = os.path.abspath(os.environ.get("WORK_AGENTS_ROOT") or os.getcwd())
        getuid = getattr(os, "getuid", None)
        uid = int(getuid()) if callable(getuid) else 0
        digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]
        pid_file = f"/tmp/feishu_daemon_{uid}_{digest}.json"
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataResolverError(f"failed to read daemon pid file {pid_file}: {exc}") from exc
    bundle_dir = data.get("bundle_dir") if isinstance(data, dict) else ""
    return bundle_dir if isinstance(bundle_dir, str) else ""


def _find_internctl_path() -> str:
    candidates = []
    env_path = os.environ.get("INTERNCTL_PATH")
    if env_path:
        candidates.append(env_path)
    bundle_dir = _daemon_bundle_dir()
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, "internctl.py"))
    current = os.path.abspath(__file__)
    candidates.append(os.path.abspath(os.path.join(current, "..", "..", "..", "..", "intern-cli", "internctl.py")))
    candidates.append(os.path.abspath(os.path.join(current, "..", "..", "..", "intern-cli", "internctl.py")))
    candidates.append(os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents", "intern-cli", "internctl.py"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise MetadataResolverError("internctl.py not found for metadata resolver")


def _run_internctl_json(args: list[str], timeout: int = 10) -> dict:
    cmd = [
        sys.executable,
        _find_internctl_path(),
        *args,
    ]
    env = os.environ.copy()
    env["WORK_AGENTS_ROOT"] = WORK_AGENTS_ROOT
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MetadataResolverError(f"internctl {' '.join(args)} failed: {detail}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MetadataResolverError(f"internctl {' '.join(args)} returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MetadataResolverError(f"internctl {' '.join(args)} returned non-object JSON")
    return data


def _discover_workspace_id(intern_dir: str, state: dict) -> str:
    repo = os.path.realpath(_repo_path_from_state(intern_dir, state))
    if not repo:
        raise MetadataResolverError(
            f"metadata resolver contract missing workspace_id/workspace_key and code repo path in {os.path.join(intern_dir, STATE_FILE)}"
        )
    data = _run_internctl_json(["workspace", "list", "--json"])
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, list):
        raise MetadataResolverError("workspace list payload missing workspaces array")
    matches = []
    for item in workspaces:
        if not isinstance(item, dict):
            continue
        local_path = item.get("local_path") or item.get("code_repo_path") or ""
        if local_path and os.path.realpath(str(local_path)) == repo and item.get("workspace_id"):
            matches.append(str(item["workspace_id"]))
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise MetadataResolverError(f"multiple relay workspaces match repo path {repo}: {', '.join(matches)}")
    raise MetadataResolverError(f"workspace_id not found for repo path {repo}; run internctl workspace enable first")


def _run_metadata_resolver(intern_dir: str, task_id: str = "") -> dict:
    state = _read_hook_state_strict(intern_dir)
    workspace_id = _workspace_id_from_state(state) or _discover_workspace_id(intern_dir, state)
    args = [
        "metadata",
        "resolve",
        "--workspace",
        workspace_id,
        "--intern",
        get_intern_name(intern_dir),
        "--json",
    ]
    if task_id:
        args.extend(["--task", task_id])
    data = _run_internctl_json(args)
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise MetadataResolverError(f"metadata resolver returned non-ok payload: {data!r}")
    return data


def _bind_repo_dotdir_resolver_to_runtime_repo(data: dict, intern_dir: str, state: dict, task_id: str = "") -> dict:
    """Keep repo_dotdir metadata in the intern runtime worktree.

    The daemon resolver returns the workspace cache checkout. A running intern
    edits its own cloned worktree, and repo_dotdir metadata must travel with
    that worktree/MR. Personal state v1 has a single repo root, so this binding
    preserves the same invariant for enterprise deployments.
    """
    if not isinstance(data, dict) or data.get("metadata_mode") != "repo_dotdir":
        return data
    code_repo = _runtime_repo_path_from_state(intern_dir, state)
    if not code_repo:
        return data
    metadata_root = os.path.join(code_repo, ".intern_workspace")
    tasks_dir = os.path.join(metadata_root, "tasks")
    task_dir = os.path.join(tasks_dir, task_id) if task_id else ""
    updated = dict(data)
    updated.update({
        "code_repo_path": code_repo,
        "code_worktree_path": code_repo,
        "metadata_checkout_path": code_repo,
        "metadata_root": metadata_root,
        "workspace_source_path": code_repo,
        "project_rule_path": os.path.join(metadata_root, "project_rule.txt"),
        "error_book_path": os.path.join(metadata_root, "ERROR_BOOK.md"),
        "tasks_dir": tasks_dir,
        "task_readme_path": os.path.join(task_dir, "README.md") if task_dir else None,
        "history_log_path": os.path.join(task_dir, "history_log.md") if task_dir else None,
        "task_knowledge_path": os.path.join(task_dir, "task_knowledge.md") if task_dir else None,
        "status_path": os.path.join(metadata_root, "interns", get_intern_name(intern_dir), "status.md"),
        "knowledge_path": os.path.join(metadata_root, "interns", get_intern_name(intern_dir), "knowledge.md"),
    })
    return updated


def _contract_value(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _normalize_metadata_context(data: dict, intern_dir: str, task_id: str = "") -> dict:
    if not isinstance(data, dict):
        raise MetadataResolverError("metadata resolver output must be a JSON object")
    intern_name = get_intern_name(intern_dir)
    metadata_root = _contract_value(data, "metadata_root", "metadata_root_path")
    code_repo_path = _contract_value(data, "code_repo_path", "code_worktree_path", "repo_dir")
    workspace_id = _contract_value(data, "workspace_id", "workspace_key")
    metadata_mode = _contract_value(data, "metadata_mode", "mode")
    required = {
        "workspace_id": workspace_id,
        "metadata_mode": metadata_mode,
        "metadata_root": metadata_root,
        "status_path": _contract_value(data, "status_path"),
        "knowledge_path": _contract_value(data, "knowledge_path"),
        "tasks_dir": _contract_value(data, "tasks_dir"),
        "project_rule_path": _contract_value(data, "project_rule_path"),
        "error_book_path": _contract_value(data, "error_book_path"),
        "code_repo_path": code_repo_path,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        suffix = f" for task {task_id}" if task_id else ""
        raise MetadataResolverError(
            f"metadata resolver output missing required fields{suffix}: {', '.join(missing)}"
        )

    task_readme = _contract_value(data, "task_readme_path")
    history_log = _contract_value(data, "history_log_path")
    task_knowledge = _contract_value(data, "task_knowledge_path")
    if task_id:
        missing_task = [
            key for key, value in (
                ("task_readme_path", task_readme),
                ("history_log_path", history_log),
                ("task_knowledge_path", task_knowledge),
            ) if not value
        ]
        if missing_task:
            raise MetadataResolverError(
                f"metadata resolver output missing required fields for task {task_id}: {', '.join(missing_task)}"
            )

    project = _contract_value(data, "project")
    if not project:
        project = os.path.basename(code_repo_path.rstrip(os.sep)) if code_repo_path else ""
    workspace_id = str(workspace_id)
    return {
        "workspace_id": workspace_id,
        "workspace_key": workspace_id,
        "project": project,
        "repo_dir": str(code_repo_path),
        "code_worktree_path": str(code_repo_path),
        "workspace_source_path": _contract_value(data, "workspace_source_path") or str(code_repo_path),
        "metadata_mode": str(metadata_mode),
        "metadata_root": str(metadata_root),
        "metadata_branch": _contract_value(data, "metadata_branch"),
        "metadata_checkout_path": _contract_value(data, "metadata_checkout_path"),
        "repo_provider": _contract_value(data, "repo_provider", "provider"),
        "runtime_provider": _contract_value(data, "runtime_provider"),
        "default_branch": _contract_value(data, "default_branch"),
        "status_path": str(required["status_path"]),
        "knowledge_path": str(required["knowledge_path"]),
        "intern_dir": os.path.dirname(str(required["status_path"])),
        "tasks_dir": str(required["tasks_dir"]),
        "project_rule_path": str(required["project_rule_path"]),
        "error_book_path": str(required["error_book_path"]),
        "shared_repo": _contract_value(data, "shared_repo") or str(code_repo_path),
        "task_dir": os.path.dirname(task_readme) if task_id else "",
        "task_readme_path": task_readme,
        "history_log_path": history_log,
        "task_knowledge_path": task_knowledge,
    }


def _legacy_metadata_context(intern_dir: str, state: dict) -> dict:
    name = get_intern_name(intern_dir)
    repo = get_project_repo(intern_dir)
    project = state.get("project") or os.path.basename(repo)
    metadata_root = os.path.join(repo, "workspace")
    log_debug(intern_dir, "metadata_resolver", f"legacy metadata layout: {metadata_root}")
    return {
        "workspace_id": project,
        "workspace_key": project,
        "project": project,
        "repo_dir": repo,
        "code_worktree_path": repo,
        "workspace_source_path": repo,
        "metadata_mode": "legacy",
        "metadata_root": metadata_root,
        "metadata_branch": "",
        "metadata_checkout_path": "",
        "repo_provider": _first_string(state.get("repo_provider"), state.get("provider"), ""),
        "runtime_provider": _first_string(state.get("runtime_provider"), get_intern_type(name)),
        "default_branch": _first_string(state.get("default_branch"), "master"),
        "status_path": os.path.join(metadata_root, "interns", name, "status.md"),
        "knowledge_path": os.path.join(metadata_root, "interns", name, "knowledge.md"),
        "intern_dir": os.path.join(metadata_root, "interns", name),
        "tasks_dir": os.path.join(metadata_root, "tasks"),
        "project_rule_path": os.path.join(metadata_root, "project_rule.txt"),
        "error_book_path": os.path.join(metadata_root, "ERROR_BOOK.md"),
        "shared_repo": _first_string(state.get("shared_repo"), os.path.join(WORK_AGENTS_ROOT, project)),
        "task_dir": "",
        "task_readme_path": "",
        "history_log_path": "",
        "task_knowledge_path": "",
    }


def get_metadata_context(intern_dir: str) -> dict:
    """Resolve hook metadata paths through task342 resolver contract.

    Enterprise workspaces use either a full resolver payload already written to
    `.hook_state.json` or `internctl metadata resolve` via the daemon. The old
    repo/workspace layout is kept only for states with no enterprise metadata
    marker, so missing enterprise contract is visible instead of silently
    changing target files.
    """
    state = _read_hook_state_strict(intern_dir)
    contract = _metadata_contract_from_state(state)
    if contract and _has_resolver_paths(contract):
        return _normalize_metadata_context(contract, intern_dir)
    workspace_id = _workspace_id_from_state(state)
    if workspace_id:
        resolved = _bind_repo_dotdir_resolver_to_runtime_repo(_run_metadata_resolver(intern_dir), intern_dir, state)
        return _normalize_metadata_context(resolved, intern_dir)
    if _has_enterprise_metadata_marker(state):
        resolved = _bind_repo_dotdir_resolver_to_runtime_repo(_run_metadata_resolver(intern_dir), intern_dir, state)
        return _normalize_metadata_context(resolved, intern_dir)
    return _legacy_metadata_context(intern_dir, state)


def get_task_metadata_paths(intern_dir: str, task_id: str) -> dict:
    state = _read_hook_state_strict(intern_dir)
    contract = _metadata_contract_from_state(state)
    if contract and _has_resolver_paths(contract):
        has_task_paths = all(
            _contract_value(contract, key)
            for key in ("task_readme_path", "history_log_path", "task_knowledge_path")
        )
        contract_task = _contract_value(contract, "task_id")
        if has_task_paths and (not task_id or not contract_task or contract_task == task_id):
            bound = _bind_repo_dotdir_resolver_to_runtime_repo(contract, intern_dir, state, task_id)
            return _normalize_metadata_context(bound, intern_dir, task_id=task_id)
    workspace_id = _workspace_id_from_state(state)
    if workspace_id or _has_enterprise_metadata_marker(state):
        resolved = _bind_repo_dotdir_resolver_to_runtime_repo(
            _run_metadata_resolver(intern_dir, task_id),
            intern_dir,
            state,
            task_id,
        )
        return _normalize_metadata_context(resolved, intern_dir, task_id=task_id)
    ctx = get_metadata_context(intern_dir)
    task_dir = os.path.join(ctx["tasks_dir"], task_id) if task_id else ""
    return {
        **ctx,
        "task_dir": task_dir,
        "task_readme_path": os.path.join(task_dir, "README.md") if task_dir else "",
        "history_log_path": os.path.join(task_dir, "history_log.md") if task_dir else "",
        "task_knowledge_path": os.path.join(task_dir, "task_knowledge.md") if task_dir else "",
    }


# ============================================================
# 文件读取
# ============================================================

def read_file_safe(path: str, max_chars: int = 8000) -> str:
    """安全读文件，超长截断。"""
    try:
        with open(path, "r") as f:
            content = f.read()
        if len(content) > max_chars:
            return content[:max_chars] + "\n\n... (截断)"
        return content
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"[读取失败: {e}]"


def read_file_head_tail(path: str, head_chars: int = 8000, tail_chars: int = 8000) -> str:
    """读文件头 head_chars + 尾 tail_chars，中间截断。
    用于检查需要覆盖文件首尾（如 history_log.md：METADATA 在头，最新 Session 在尾）
    但又不希望随文件无限增长占内存的场景。
    """
    try:
        with open(path, "r") as f:
            content = f.read()
        if len(content) <= head_chars + tail_chars:
            return content
        return content[:head_chars] + "\n\n... (中段截断) ...\n\n" + content[-tail_chars:]
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"[读取失败: {e}]"


def file_hash(path: str) -> str:
    """计算文件 MD5 hash。"""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return ""


# ============================================================
# METADATA 解析
# ============================================================

def parse_status_metadata(content: str) -> dict:
    """从 status.md 解析 METADATA 行。"""
    m = re.search(r'METADATA:(.+?)\s*-->', content)
    if not m:
        return {"status": "Unknown", "task": "", "role": "independent", "team_id": ""}
    meta = {}
    for pair in m.group(1).split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        meta[key.strip().lower()] = value.strip()
    return {
        "status": meta.get("status", "Unknown"),
        "task": meta.get("task", ""),
        "role": meta.get("role", "independent"),
        "team_id": meta.get("team_id", ""),
    }


def parse_session_metadata(content: str) -> int:
    """从 history_log/task_knowledge 解析 SESSION 号。"""
    m = re.search(r'METADATA:SESSION=(\d+)', content)
    return int(m.group(1)) if m else 0


# ============================================================
# history_log 截断
# ============================================================

def truncate_history_log(content: str, max_sessions: int = 3) -> str:
    """只保留最近 N 个 session 的 history_log 内容。"""
    parts = re.split(r'(?=## Session \d+)', content)
    header = parts[0] if parts else ""
    sessions = [p for p in parts[1:] if p.strip()]
    if len(sessions) <= max_sessions:
        return content
    return header + "\n".join(sessions[:max_sessions]) + "\n\n... (更早的 session 已省略)\n"


# ============================================================
# 飞书凭据和 chatId
# ============================================================

def _get_config_dir() -> str:
    """获取配置目录。"""
    env = os.environ.get("CONFIG_DIR")
    if env:
        return env
    repo = os.environ.get("PROJECT_REPO")
    if repo:
        return os.path.join(repo, "claude-runtime", "config")
    return os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents", "claude-runtime", "config")


KEY_FILE = os.environ.get("FEISHU_KEY_FILE", os.path.join(WORK_AGENTS_ROOT, "key.txt"))
REGISTRY_DIR = os.environ.get("FEISHU_REGISTRY_DIR", os.path.join(WORK_AGENTS_ROOT, ".feishu_registry"))


def load_feishu_credentials() -> tuple:
    """读取飞书 app_id 和 app_secret。"""
    key_file = KEY_FILE
    if not os.path.exists(key_file):
        config_key = os.path.join(_get_config_dir(), "feishu_key.txt")
        if os.path.exists(config_key):
            key_file = config_key
    with open(key_file) as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    if len(lines) < 2:
        raise ValueError(f"key file {key_file} needs 2 lines: app_id and app_secret")
    return lines[0], lines[1]


def get_chat_id(intern_name: str) -> str:
    """从 registry 获取 intern 的飞书 chatId。返回 chat_id 或空串（不 raise）。"""
    registry_dir = REGISTRY_DIR
    if not os.path.isdir(registry_dir):
        config_reg = os.path.join(_get_config_dir(), "feishu_registry")
        if os.path.isdir(config_reg):
            registry_dir = config_reg
    registry_file = os.path.join(registry_dir, f"{intern_name}.json")
    if not os.path.exists(registry_file):
        return ""
    try:
        with open(registry_file) as f:
            data = json.load(f)
        return data.get("chatId", "")
    except Exception:
        return ""


# ============================================================
# State 文件读写
# ============================================================

STATE_FILE = ".hook_state.json"
LOCK_FILE = ".hook_state.lock"


def load_state(intern_dir: str) -> dict:
    """从 state 文件读取状态。"""
    path = os.path.join(intern_dir, STATE_FILE)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(intern_dir: str, state: dict):
    """原子写入 state 文件。"""
    path = os.path.join(intern_dir, STATE_FILE)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.rename(tmp_path, path)


@contextmanager
def state_lock(intern_dir: str):
    """文件锁，保护 state 读写的原子性。"""
    lock_path = os.path.join(intern_dir, LOCK_FILE)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ============================================================
# 日志
# ============================================================

def log_debug(intern_dir: str, hook_name: str, message: str):
    """写 debug 日志到 llm_intern_logs/<intern_name>/hooks.log。"""
    name = get_intern_name(intern_dir)
    log_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", name)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "hooks.log")
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            archive = log_path + f".{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.rename(log_path, archive)
    except Exception:
        pass
    with open(log_path, "a") as f:
        f.write(f"[{ts}] [{hook_name}] {message}\n")


def dump_hook_stdin(intern_dir: str, hook_name: str, hook_input: dict):
    """任务 task200 事件分类调研工具：dump 完整 stdin JSON。

    门禁：仅当 `{intern_dir}/debug/stdin_dump_enabled` 标志文件存在时 dump。
    输出：`{WORK_AGENTS_ROOT}/llm_intern_logs/<intern>/hook_dumps/<hook>_<ts>.json`
    轮转：保留最近 100 个文件。
    """
    marker = os.path.join(intern_dir, "debug", "stdin_dump_enabled")
    if not os.path.exists(marker):
        return
    try:
        name = get_intern_name(intern_dir)
        dump_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", name, "hook_dumps")
        os.makedirs(dump_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(dump_dir, f"{hook_name}_{ts}.json")
        with open(path, "w") as f:
            json.dump(hook_input, f, ensure_ascii=False, indent=2)
        files = sorted(fn for fn in os.listdir(dump_dir) if fn.endswith(".json"))
        for old in files[:-100]:
            try:
                os.remove(os.path.join(dump_dir, old))
            except OSError:
                pass
    except Exception:
        pass


# ============================================================
# Session → Intern 映射
# ============================================================

def _session_map_lock():
    """获取映射文件的文件锁。"""
    os.makedirs(os.path.dirname(SESSION_MAP_LOCK), exist_ok=True)
    fd = open(SESSION_MAP_LOCK, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _session_map_unlock(fd):
    """释放映射文件的文件锁。"""
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def load_session_map() -> dict:
    """读取 .intern_sessions.json。格式：{intern: {sessionResource, sessionId}}"""
    try:
        with open(SESSION_MAP_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_session_map(data: dict):
    """原子写入 .intern_sessions.json。"""
    os.makedirs(os.path.dirname(SESSION_MAP_FILE) or ".", exist_ok=True)
    tmp = SESSION_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.rename(tmp, SESSION_MAP_FILE)


def set_session_intern(session_id: str, intern_name: str):
    """带锁写入 sessionId 到 intern entry（保留插件侧已写的 sessionResource）。"""
    fd = _session_map_lock()
    try:
        data = load_session_map()
        target_key = intern_name
        if ":" not in intern_name:
            matches = [
                key for key, value in data.items()
                if isinstance(value, dict)
                and (value.get("intern_name") == intern_name or key == intern_name)
            ]
            if len(matches) == 1:
                target_key = matches[0]
        entry = data.get(target_key, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["sessionId"] = session_id
        if ":" in target_key:
            _, scoped_name = _split_scoped_intern_key(target_key)
            entry.setdefault("intern_name", scoped_name)
        data[target_key] = entry
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        sessions[session_id] = target_key
        data["sessions"] = sessions
        save_session_map(data)
    finally:
        _session_map_unlock(fd)


def get_session_intern(session_id: str) -> str:
    """遍历查找 sessionId 对应的 intern name。"""
    data = load_session_map()
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        value = sessions.get(session_id, "")
        return value if isinstance(value, str) else ""
    for intern_name, entry in data.items():
        if isinstance(entry, dict) and entry.get("sessionId") == session_id:
            return intern_name
    return ""


def get_intern_type(intern_name: str) -> str:
    """从 .intern_sessions.json 获取 intern 类型，默认 'copilot'。"""
    data = load_session_map()
    entry = data.get(intern_name, {})
    if isinstance(entry, dict):
        intern_type = entry.get("type")
        if intern_type:
            return intern_type
    for key, candidate in data.items():
        if (
            isinstance(candidate, dict)
            and (candidate.get("intern_name") == intern_name or key == intern_name)
            and candidate.get("intern_dir")
            and candidate.get("type")
        ):
            return candidate["type"]
    for key, candidate in data.items():
        if (
            isinstance(candidate, dict)
            and (candidate.get("intern_name") == intern_name or key == intern_name)
            and candidate.get("type")
        ):
            return candidate["type"]
    return "copilot"


def set_intern_type(intern_name: str, intern_type: str):
    """带锁写入 intern 类型到 .intern_sessions.json。"""
    fd = _session_map_lock()
    try:
        data = load_session_map()
        entry = data.get(intern_name, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["type"] = intern_type
        data[intern_name] = entry
        save_session_map(data)
    finally:
        _session_map_unlock(fd)


# ============================================================
# Transcript 解析（VS Code 专用：Stop hook 提取 last_assistant_message）
# ============================================================

def extract_last_assistant_message(transcript_path: str, intern_type: str = "copilot") -> str:
    """从 transcript JSONL 文件提取最后一条 assistant 消息。

    VS Code Stop hook stdin 没有 last_assistant_message 字段，
    需要从 transcript_path 文件中自行提取。

    Args:
        transcript_path: JSONL transcript 文件路径
        intern_type: "copilot" / "claude" / "codex"，决定解析格式

    Copilot (VS Code) transcript 格式：
    - type: "assistant.message" → data.content 包含文本

    Claude Code transcript 格式：
    - role: "assistant" → content 包含文本或 [{type: "text", text: "..."}]

    Codex CLI rollout-*.jsonl 格式（基于 codex-rs/protocol 的 RolloutItem schema）：
    - 每行: {"timestamp": "...", "type": "response_item",
              "payload": {"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": "..."}]}}

    Returns:
        最后一条 assistant 消息文本，找不到返回空字符串。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        last_assistant = ""
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if intern_type == "claude":
                    # Claude Code format: role=assistant → content
                    role = entry.get("role", "")
                    if role == "assistant":
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            texts = []
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                                elif isinstance(part, str):
                                    texts.append(part)
                            last_assistant = "\n".join(texts)
                        elif isinstance(content, str):
                            last_assistant = content
                elif intern_type == "codex":
                    # Codex rollout format: RolloutLine wraps RolloutItem (tag=type, content=payload).
                    # Assistant message lives at payload.type=message, payload.role=assistant.
                    if entry.get("type") != "response_item":
                        continue
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "message" or payload.get("role") != "assistant":
                        continue
                    content = payload.get("content", [])
                    if isinstance(content, list):
                        texts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                texts.append(part.get("text", ""))
                        if texts:
                            last_assistant = "\n".join(texts)
                else:
                    # Copilot (VS Code) format: type=assistant.message → data.content
                    entry_type = entry.get("type", "")
                    data = entry.get("data", {})
                    if entry_type == "assistant.message" and isinstance(data, dict):
                        content = data.get("content", "")
                        if content and isinstance(content, str) and content.strip():
                            last_assistant = content.strip()
                    elif entry.get("role") == "assistant":
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            texts = []
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                                elif isinstance(part, str):
                                    texts.append(part)
                            if texts:
                                last_assistant = "\n".join(texts)
                        elif isinstance(content, str):
                            last_assistant = content

        return last_assistant
    except Exception:
        return ""
