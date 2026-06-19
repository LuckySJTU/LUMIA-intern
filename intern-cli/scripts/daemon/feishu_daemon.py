#!/usr/bin/env python3
"""
Feishu Daemon — 全功能飞书服务端

功能：
1. HTTP API (localhost:<ephemeral>) — 供插件/hooks/CLI 调用
2. WebSocket server (localhost:<ephemeral>) — 向插件推送消息
3. 飞书 WebSocket — 接收飞书群消息
4. 群生命周期管理 — 创建/删除/同步
5. 红绿灯管理 — 更新群名 🟢/🔴
6. 消息发送/更新/回复

启动：插件 activate 时自动后台启动
停止：POST /api/shutdown 或 SIGTERM

PID file: /tmp/feishu_daemon.json (JSON: pid/instance_id/work_agents_root/http_port/ws_port/started_at/script_hash/version/bundle_dir)
"""

__version__ = "1.0.0"

import json
import os
import sys
import subprocess
import shutil
import signal
import logging
import time
import threading
import asyncio
import hashlib
import base64
import fcntl
import urllib.request
import urllib.error
import urllib.parse
import socket
import uuid
import faulthandler
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
import re

# task283: daemon-local per-chat detail_mode store; lives next to this script.
_DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
_INTERN_CLI_ROOT = os.path.abspath(os.path.join(_DAEMON_DIR, "..", ".."))
sys.path.insert(0, _DAEMON_DIR)
sys.path.insert(0, _INTERN_CLI_ROOT)
import daemon_chat_config
from lib import team_mailbox
from lib.enterprise_state_v1 import (
    LOCAL_REGISTRY_SCHEMA,
    WORKSPACE_SCHEMA,
    daemon_workspace_cache_path,
    state_registry_path,
    validate_workspace_id,
    workspace_metadata_cache_path,
    workspace_record_path,
    workspace_state_dir,
    workspace_source_path,
)
from lib.git_ops import add_commit_push, ensure_git_identity
from lib.metadata_checkout import ensure_metadata_branch_checkout

# ── 配置 ──────────────────────────────────

WORK_AGENTS_ROOT = os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()
# HTTP_PORT/WS_PORT default to 0 → OS assigns ephemeral port at bind time.
# Actual ports are written to PID_FILE after bind, and read by hooks/CLI/extension.
HTTP_PORT = int(os.environ.get("FEISHU_HTTP_PORT", "0"))
WS_PORT = int(os.environ.get("FEISHU_WS_PORT", "0"))
PID_FILE = "/tmp/feishu_daemon.json"
OLD_PID_FILE = os.path.join(WORK_AGENTS_ROOT, ".feishu_daemon.pid")  # legacy, unlinked on startup
LOG_DIR = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", "_daemon")
LOG_FILE = os.path.join(LOG_DIR, "feishu_daemon.log")
BASE_URL = "https://open.feishu.cn/open-apis"
OWNER_JSON_PATH = os.path.join(WORK_AGENTS_ROOT, ".feishu_registry", "_owner.json")

_MACHINE_HELPER_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MACHINE_HELPER_FORBIDDEN_ENDPOINT_CHARS = set(" \t\r\n;&|$`<>\\")
_MACHINE_HELPER_SECRET_HINT_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)


def _safe_machine_helper_slug(machine_id):
    slug = _MACHINE_HELPER_SLUG_RE.sub("_", (machine_id or "").strip().lower()).strip("_")
    if not slug:
        raise ValueError("machine_id required")
    return slug


def _machine_helper_id_for_machine(machine_id):
    return f"machine_helper_{_safe_machine_helper_slug(machine_id)}"


def _machine_helper_task_id(machine_id):
    return f"task_machine_helper_{_safe_machine_helper_slug(machine_id)}"


def _machine_helper_workspace_key(machine_id):
    return f"machine_helper_{_safe_machine_helper_slug(machine_id)}"


def _machine_helper_workspace_display(machine_id):
    return f"machine-helper-{_safe_machine_helper_slug(machine_id)}"


def _machine_helper_state_root(machine_id, work_root=None):
    return os.path.join(work_root or WORK_AGENTS_ROOT, "state", "v1", _machine_helper_workspace_key(machine_id))


def _machine_helper_dir(machine_id, work_root=None):
    return os.path.join(
        _machine_helper_state_root(machine_id, work_root),
        "interns",
        _machine_helper_id_for_machine(machine_id),
    )


def _machine_helper_source_dir(machine_id, work_root=None):
    return os.path.join(_machine_helper_state_root(machine_id, work_root), "source")


def _machine_helper_metadata_root(machine_id, work_root=None):
    return os.path.join(
        _machine_helper_state_root(machine_id, work_root),
        "metadata",
        "local",
        ".intern_workspace",
    )


def _machine_helper_workspace_id(machine_id):
    return f"local-machine-helper:{_safe_machine_helper_slug(machine_id)}"


def parse_machine_helper_endpoint(endpoint):
    """Parse and validate a helper migration target endpoint.

    Accepts IPv4/DNS as ``host:port`` and IPv6 as ``[addr]:port``. Returns a
    normalized dict so migration helpers do not need to parse shell-like text.
    """
    raw = (endpoint or "").strip()
    if not raw:
        raise ValueError("endpoint required")
    if any(ch in _MACHINE_HELPER_FORBIDDEN_ENDPOINT_CHARS for ch in raw):
        raise ValueError("endpoint contains forbidden characters")
    if _MACHINE_HELPER_SECRET_HINT_RE.search(raw):
        raise ValueError("endpoint must not contain credential material")

    bracketed_ipv6 = raw.startswith("[")
    if bracketed_ipv6:
        end = raw.find("]")
        if end < 0 or len(raw) <= end + 2 or raw[end + 1] != ":":
            raise ValueError("IPv6 endpoint must be [host]:port")
        host = raw[1:end]
        port_text = raw[end + 2:]
    else:
        if raw.count(":") != 1:
            raise ValueError("endpoint must be host:port")
        host, port_text = raw.rsplit(":", 1)

    if not host:
        raise ValueError("host required")
    if not port_text.isdigit():
        raise ValueError("port must be numeric")
    port = int(port_text)
    if port < 1 or port > 65535:
        raise ValueError("port out of range")
    normalized = f"[{host}]:{port}" if bracketed_ipv6 else f"{host}:{port}"
    return {"host": host, "port": port, "endpoint": normalized}


def _write_json_file_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _register_machine_helper_session(helper_id, runtime, work_root=None, helper_dir="", project="",
                                     workspace_id=""):
    sessions_file = os.path.join(work_root or WORK_AGENTS_ROOT, ".intern_sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data[helper_id] = {
        "type": runtime,
        "role": "machine_helper",
        "intern_name": helper_id,
        "project": project,
        "workspace_id": workspace_id,
        "intern_dir": helper_dir,
    }
    _write_json_file_atomic(sessions_file, data)


def _unregister_machine_helper_session(helper_id, work_root=None):
    sessions_file = os.path.join(work_root or WORK_AGENTS_ROOT, ".intern_sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    changed = False
    for key, value in list(data.items()):
        if key == helper_id or (isinstance(value, dict) and value.get("intern_name") == helper_id and value.get("role") == "helper"):
            data.pop(key, None)
            changed = True
    if changed:
        _write_json_file_atomic(sessions_file, data)


def _delete_machine_helper_relay_chat(helper_id, project):
    if _registry:
        _registry.unregister(helper_id)
    if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
        return
    try:
        payload = {"intern_name": helper_id, "project": project or helper_id}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_relay_client._relay_http_base}/api/chat/delete",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:
        log.warning(f"failed to delete helper relay chat for {helper_id}: {exc}")


def _create_machine_helper_relay_chat(machine_id, helper_id, runtime, operator_open_id=""):
    if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
        return ""
    payload = {
        "machine_id": machine_id,
        "helper_id": helper_id,
        "runtime": runtime,
        "operator_open_id": operator_open_id,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_relay_client._relay_http_base}/api/helper/chat/create",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read() or b"{}")
    chat_id = result.get("chat_id") or ""
    if not chat_id:
        raise RuntimeError(f"relay /api/helper/chat/create returned no chat_id: {result}")
    return chat_id


def _metadata_root_for_workspace(workspace):
    mode = workspace.get("metadata_mode") or ""
    local_path = workspace.get("local_path") or ""
    metadata_cache = workspace.get("metadata_cache_path") or ""
    if mode == "repo_dotdir":
        return os.path.join(local_path, ".intern_workspace"), local_path
    if mode == "metadata_branch":
        return os.path.join(metadata_cache, ".intern_workspace"), metadata_cache
    if mode == "local_only":
        return os.path.join(metadata_cache, "local", ".intern_workspace"), ""
    raise ValueError(f"invalid metadata mode for workspace {workspace.get('workspace_id', '')}: {mode!r}")


def _run_machine_helper_git(repo, args, *, check=False):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check, timeout=30)


def _ensure_machine_helper_source_repo(machine_id, work_root):
    repo = Path(_machine_helper_source_dir(machine_id, work_root))
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-b", "master"], cwd=repo, capture_output=True, text=True, check=True)
    ensure_git_identity(str(repo))
    readme = repo / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {_machine_helper_workspace_display(machine_id)}\n\n"
            "This local repository is the machine helper workspace for enterprise Intern Agents.\n",
            encoding="utf-8",
        )
    guide = repo / "MACHINE_HELPER.md"
    guide.write_text(
        f"""# Machine Helper Knowledge

Machine: `{machine_id}`
Workspace: `{_machine_helper_workspace_display(machine_id)}`

Use this local repository for machine-level diagnosis and repair. The helper is a normal local-only intern:
- metadata mode is `local_only`;
- task, status, history, and knowledge files live under the local `.intern_workspace`;
- enterprise permissions, owner invite, and machine migration are handled by the relay/daemon helper surface;
- do not require any business workspace to be enabled before starting.
""",
        encoding="utf-8",
    )
    if _run_machine_helper_git(repo, ["status", "--short"]).stdout.strip():
        _run_machine_helper_git(repo, ["add", "README.md", "MACHINE_HELPER.md"], check=True)
        diff = _run_machine_helper_git(repo, ["diff", "--cached", "--quiet"])
        if diff.returncode != 0:
            _run_machine_helper_git(repo, ["commit", "-m", "Initialize machine helper workspace"], check=True)
    return str(repo)


def _write_machine_helper_state_workspace(machine_id, work_root):
    root = Path(_machine_helper_state_root(machine_id, work_root))
    repo = _ensure_machine_helper_source_repo(machine_id, work_root)
    workspace = {
        "schema": "intern-agents.workspace.v1",
        "workspace_id": _machine_helper_workspace_id(machine_id),
        "workspace_key": _machine_helper_workspace_key(machine_id),
        "display_name": _machine_helper_workspace_display(machine_id),
        "repo_url": repo,
        "local_path": repo,
        "default_branch": "master",
        "metadata": {
            "mode": "local_only",
            "repo_relative_path": ".intern_workspace",
            "branch": "intern_workspace",
            "local_path": _machine_helper_metadata_root(machine_id, work_root),
        },
    }
    _write_json_file_atomic(str(root / "workspace.json"), workspace)
    registry_path = state_registry_path(work_root)
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        registry = {}
    if not isinstance(registry, dict) or registry.get("schema") != LOCAL_REGISTRY_SCHEMA:
        registry = {"schema": LOCAL_REGISTRY_SCHEMA, "workspaces": {}}
    workspaces = registry.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    registry["schema"] = LOCAL_REGISTRY_SCHEMA
    registry["workspaces"] = workspaces
    workspaces[_machine_helper_workspace_id(machine_id)] = (
        f"{_machine_helper_workspace_key(machine_id)}/workspace.json"
    )
    _write_json_file_atomic(str(registry_path), registry)
    return workspace


def _resolve_machine_helper_metadata(work_root, helper_id, task_id, machine_id="", workspace_id="", metadata_resolver=None):
    if metadata_resolver:
        resolver = dict(metadata_resolver)
        resolver.setdefault("intern_name", helper_id)
        resolver.setdefault("task_id", task_id)
        return resolver
    machine_id = machine_id or helper_id.removeprefix("machine_helper_")
    workspace = _write_machine_helper_state_workspace(machine_id, work_root)
    metadata_root = workspace["metadata"]["local_path"]
    metadata_checkout = os.path.join(_machine_helper_state_root(machine_id, work_root), "metadata", "local")
    workspace_key = workspace["workspace_key"]
    workspace_id = workspace["workspace_id"]
    tasks_dir = os.path.join(metadata_root, "tasks")
    task_dir = os.path.join(tasks_dir, task_id)
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "workspace_key": workspace_key,
        "project": workspace["display_name"],
        "projectless": True,
        "intern_name": helper_id,
        "task_id": task_id,
        "metadata_mode": "local_only",
        "metadata_branch": None,
        "repo_provider": "local",
        "runtime_provider": "local",
        "default_branch": "master",
        "code_repo_path": workspace["local_path"],
        "code_worktree_path": workspace["local_path"],
        "metadata_checkout_path": metadata_checkout,
        "metadata_root": metadata_root,
        "workspace_source_path": workspace["local_path"],
        "project_rule_path": os.path.join(metadata_root, "project_rule.txt"),
        "error_book_path": os.path.join(metadata_root, "ERROR_BOOK.md"),
        "tasks_dir": tasks_dir,
        "task_readme_path": os.path.join(task_dir, "README.md"),
        "history_log_path": os.path.join(task_dir, "history_log.md"),
        "task_knowledge_path": os.path.join(task_dir, "task_knowledge.md"),
        "status_path": os.path.join(metadata_root, "interns", helper_id, "status.md"),
        "knowledge_path": os.path.join(metadata_root, "interns", helper_id, "knowledge.md"),
    }


def ensure_machine_helper_profile(machine_id, runtime="codex", chat_id="", work_root=None,
                                  workspace_id="", metadata_resolver=None):
    """Create a helper profile that can be launched by the normal Codex intern script."""
    if runtime != "codex":
        raise ValueError("machine helper only supports codex runtime")
    work_root = work_root or WORK_AGENTS_ROOT
    helper_id = _machine_helper_id_for_machine(machine_id)
    helper_dir = _machine_helper_dir(machine_id, work_root)
    task_id = _machine_helper_task_id(machine_id)
    resolver = _resolve_machine_helper_metadata(
        work_root, helper_id, task_id,
        machine_id=machine_id,
        workspace_id=workspace_id,
        metadata_resolver=metadata_resolver,
    )
    project_name = resolver["project"]
    os.makedirs(helper_dir, exist_ok=True)
    os.makedirs(os.path.join(helper_dir, "debug"), exist_ok=True)
    os.makedirs(os.path.join(helper_dir, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(helper_dir, ".feishu_inbox"), exist_ok=True)

    state = {
        "role": "machine_helper",
        "projectless": True,
        "intern_name": helper_id,
        "project": project_name,
        "workspace_id": resolver["workspace_id"],
        "workspace_key": resolver["workspace_key"],
        "metadata_mode": "local_only",
        "code_worktree_path": resolver["code_worktree_path"],
        "intern_dir": helper_dir,
        "metadata_resolver": resolver,
        "current_task": task_id,
        "feishu": {"chat_id": chat_id},
        "helper": {
            "machine_id": machine_id,
            "helper_id": helper_id,
            "runtime": runtime,
            "chat_id": chat_id,
        },
    }
    _write_json_file_atomic(os.path.join(helper_dir, ".hook_state.json"), state)
    _write_json_file_atomic(os.path.join(helper_dir, "helper_profile.json"), {
        "machine_id": machine_id,
        "helper_id": helper_id,
        "runtime": runtime,
        "task_id": task_id,
        "chat_id": chat_id,
        "workspace_id": resolver["workspace_id"],
        "workspace_key": resolver["workspace_key"],
        "projectless": True,
        "metadata_resolver": resolver,
    })
    helper_metadata_paths = _write_machine_helper_workspace_files(resolver, helper_id, task_id, machine_id)
    _commit_machine_helper_metadata_if_needed(resolver, helper_metadata_paths)
    with open(os.path.join(helper_dir, "prompt.md"), "w", encoding="utf-8") as f:
        f.write(
            f"# Machine Helper\n\n"
            f"You are `{helper_id}`, a machine-level helper for `{machine_id}`.\n"
            "You have the same file, attachment, AskUser, hook, and checklist capabilities as a normal intern.\n"
        )
    _register_machine_helper_session(
        helper_id,
        runtime,
        work_root,
        helper_dir=helper_dir,
        project=resolver.get("project") or "",
        workspace_id=resolver.get("workspace_id") or "",
    )
    return {"helper_id": helper_id, "helper_dir": helper_dir, "task_id": task_id, "hook_state": state}


def _write_machine_helper_workspace_files(resolver, helper_id, task_id, machine_id):
    status_path = resolver["status_path"]
    knowledge_path = resolver["knowledge_path"]
    readme_path = resolver["task_readme_path"]
    history_path = resolver["history_log_path"]
    task_knowledge_path = resolver["task_knowledge_path"]
    touched = []
    for path in (status_path, knowledge_path, readme_path, history_path, task_knowledge_path,
                 resolver["project_rule_path"], resolver["error_book_path"]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(status_path):
        with open(status_path, "w", encoding="utf-8") as f:
            f.write(
                f"# {helper_id} - 状态\n\n"
                f"<!-- METADATA:STATUS=Working,TASK={task_id},ROLE=machine_helper,TEAM_ID= -->\n\n"
                "| 字段 | 值 |\n|------|-----|\n"
                f"| Name | {helper_id} |\n"
                "| Status | Working |\n"
                "| Role | machine_helper |\n"
                "| Team | N/A |\n"
                f"| Current Task | {task_id} |\n"
                "| PR |  |\n"
            )
        touched.append(status_path)
    if not os.path.exists(knowledge_path):
        with open(knowledge_path, "w", encoding="utf-8") as f:
            f.write(f"# {helper_id} - 个人知识库\n\n<!-- METADATA:SESSION=0 -->\n\n---\n\n## 知识条目\n")
        touched.append(knowledge_path)
    if not os.path.exists(readme_path):
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(
                f"# {task_id} - Machine helper for {machine_id}\n\n"
                f"<!-- METADATA:STATUS=InProgress,ASSIGNEE={helper_id} -->\n\n"
                "## 目标\n\n协助用户排查机器问题、执行新机器迁移诊断，并保持普通 intern 的文件、附件、AskUser、hook 和 Checklist 能力。\n"
            )
        touched.append(readme_path)
    if not os.path.exists(history_path):
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(
                f"# {task_id} - 历史日志\n\n<!-- METADATA:SESSION=0 -->\n\n---\n\n"
                "## Session 0 - 初始化\n\n**执行人**: machine helper\n\nhelper runtime profile created.\n\n---\n"
            )
        touched.append(history_path)
    if not os.path.exists(task_knowledge_path):
        with open(task_knowledge_path, "w", encoding="utf-8") as f:
            f.write(f"# {task_id} - 任务知识\n\n<!-- METADATA:SESSION=0 -->\n\n---\n\n## 知识条目\n")
        touched.append(task_knowledge_path)
    if not os.path.exists(resolver["project_rule_path"]):
        with open(resolver["project_rule_path"], "w", encoding="utf-8") as f:
            f.write("# Project Rule\n")
        touched.append(resolver["project_rule_path"])
    if not os.path.exists(resolver["error_book_path"]):
        with open(resolver["error_book_path"], "w", encoding="utf-8") as f:
            f.write("# ERROR_BOOK\n")
        touched.append(resolver["error_book_path"])
    return touched


def _commit_machine_helper_metadata_if_needed(resolver, paths):
    if not paths or resolver.get("metadata_mode") != "metadata_branch":
        return
    checkout = resolver.get("metadata_checkout_path") or ""
    if not checkout or not os.path.isdir(os.path.join(checkout, ".git")):
        return
    rels = []
    for path in paths:
        try:
            rel = os.path.relpath(path, checkout)
        except ValueError:
            continue
        if not rel.startswith(".."):
            rels.append(rel)
    if not rels:
        return
    add_commit_push(
        checkout,
        rels,
        f"Update machine helper metadata for {resolver.get('intern_name') or 'helper'}",
        branch=resolver.get("metadata_branch") or None,
        push=True,
    )


def _machine_helper_launcher_env(work_root, chat_id, resolver, helper_dir=""):
    env = os.environ.copy()
    env["WORK_AGENTS_ROOT"] = work_root
    if helper_dir:
        env["INTERN_DIR"] = helper_dir
    env["INTERN_START_NO_ATTACH"] = "1"
    env["INTERN_START_SKIP_GROUP_CREATE"] = "1"
    env["INTERN_METADATA_ROOT"] = resolver["metadata_root"]
    env["INTERN_METADATA_INTERN_DIR"] = os.path.dirname(resolver["status_path"])
    env["INTERN_CODE_REPO_PATH"] = resolver["code_repo_path"]
    env["INTERN_WORKSPACE_ID"] = resolver["workspace_id"]
    if chat_id:
        env["FEISHU_CHAT_ID"] = chat_id
    return env


def start_machine_helper_runtime(machine_id, chat_id="", issue_summary="", operator_open_id="",
                                 work_root=None, launcher=None, workspace_id="", metadata_resolver=None):
    profile = ensure_machine_helper_profile(
        machine_id, chat_id=chat_id, work_root=work_root,
        workspace_id=workspace_id, metadata_resolver=metadata_resolver)
    work_root = work_root or WORK_AGENTS_ROOT
    helper_id = profile["helper_id"]
    resolver = profile["hook_state"]["metadata_resolver"]
    project_name = resolver["project"]
    if not chat_id:
        chat_id = _create_machine_helper_relay_chat(machine_id, helper_id, "codex", operator_open_id)
        if chat_id:
            profile = ensure_machine_helper_profile(
                machine_id, chat_id=chat_id, work_root=work_root,
                workspace_id=workspace_id, metadata_resolver=metadata_resolver)
    if _registry and chat_id:
        _registry.register(helper_id, chat_id)
    script = os.path.join(_INTERN_CLI_ROOT, "scripts", "intern_start_codex.sh")
    run = launcher or subprocess.run
    completed = run(
        [script, helper_id, project_name],
        cwd=work_root,
        env=_machine_helper_launcher_env(work_root, chat_id, resolver, helper_dir=profile["helper_dir"]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(stderr or f"helper runtime start failed with code {completed.returncode}")
    _send_machine_helper_context(helper_id, machine_id, issue_summary, operator_open_id)
    return {**profile, "status": "running"}


def stop_machine_helper_runtime(machine_id, work_root=None, workspace_id=""):
    helper_id = _machine_helper_id_for_machine(machine_id)
    entry = _get_intern_session_entry(helper_id)
    project = entry.get("project") if isinstance(entry, dict) else ""
    project = project or workspace_id
    if _check_tmux_session(helper_id):
        subprocess.run(["tmux", "kill-session", "-t", f"={helper_id}"], check=True, capture_output=True)
    _unregister_machine_helper_session(helper_id, work_root=work_root)
    _delete_machine_helper_relay_chat(helper_id, project or "")
    return {"helper_id": helper_id, "status": "stopped"}


def machine_helper_runtime_status(machine_id):
    helper_id = _machine_helper_id_for_machine(machine_id)
    running = _is_codex_process_running(helper_id)
    return {"helper_id": helper_id, "status": "running" if running else "stopped"}


def _ensure_machine_helper_runtime_running(machine_id, chat_id="", issue_summary="", operator_open_id="",
                                           work_root=None, workspace_id="", metadata_resolver=None):
    helper_id = _machine_helper_id_for_machine(machine_id)
    if _is_codex_process_running(helper_id):
        ensure_machine_helper_profile(
            machine_id,
            chat_id=chat_id,
            work_root=work_root,
            workspace_id=workspace_id,
            metadata_resolver=metadata_resolver,
        )
        return {"helper_id": helper_id, "status": "running"}
    return start_machine_helper_runtime(
        machine_id,
        chat_id=chat_id,
        issue_summary=issue_summary,
        operator_open_id=operator_open_id,
        work_root=work_root,
        workspace_id=workspace_id,
        metadata_resolver=metadata_resolver,
    )


def _send_machine_helper_context(helper_id, machine_id, issue_summary, operator_open_id=""):
    text = (
        f"你是机器 `{machine_id}` 的 machine helper。\n"
        f"触发用户 open_id: `{operator_open_id or 'unknown'}`。\n"
        f"当前诉求：{issue_summary or '请先询问用户需要排查的问题。'}\n"
        "请先复述机器、用户诉求和你将执行的排查/迁移步骤；需要用户提供凭据或确认时必须使用 AskUser/request_user_input。"
    )
    return _send_to_codex_tmux(helper_id, text, delivery_id=f"helper-context-{uuid.uuid4().hex}", require_ack=False)


def build_machine_migration_prompt(endpoint, source_machine_id="", operator_open_id=""):
    parsed = parse_machine_helper_endpoint(endpoint)
    return (
        f"请协助迁移到新机器 `{parsed['endpoint']}`。\n"
        f"源机器：`{source_machine_id or 'unknown'}`；触发用户 open_id：`{operator_open_id or 'unknown'}`。\n"
        "先检查目标机器连通性、安装方式、daemon 接入、workspace enable、intern 启动和回归验证步骤。"
        "不要自动复制 token、ssh key、cookie 或其他敏感凭据；需要用户动作时使用 AskUser/request_user_input 明确说明风险和操作。"
    )


def handle_machine_helper_action(msg):
    action = msg.get("helper_action") or ""
    machine_id = msg.get("machine_id") or ""
    request_id = msg.get("request_id") or ""
    chat_id = msg.get("chat_id") or ""
    if not action or not machine_id:
        raise ValueError("helper_action and machine_id required")
    workspace_id = msg.get("workspace_id") or ""
    metadata_resolver = msg.get("metadata_resolver") if isinstance(msg.get("metadata_resolver"), dict) else None
    if action == "start":
        result = start_machine_helper_runtime(
            machine_id,
            chat_id=chat_id,
            issue_summary=msg.get("issue_summary") or "",
            operator_open_id=msg.get("operator_open_id") or "",
            workspace_id=workspace_id,
            metadata_resolver=metadata_resolver,
        )
    elif action == "stop":
        result = stop_machine_helper_runtime(machine_id, workspace_id=workspace_id)
    elif action == "status":
        result = machine_helper_runtime_status(machine_id)
    elif action == "invite_owner":
        runtime = _ensure_machine_helper_runtime_running(
            machine_id,
            chat_id=chat_id,
            issue_summary=msg.get("issue_summary") or "app owner 已被邀请进群，请向 owner 说明当前问题上下文、已做排查和需要确认的事项。",
            operator_open_id=msg.get("operator_open_id") or "",
            workspace_id=workspace_id,
            metadata_resolver=metadata_resolver,
        )
        helper_id = runtime.get("helper_id") or msg.get("helper_id") or _machine_helper_id_for_machine(machine_id)
        ok, err = _send_machine_helper_context(
            helper_id,
            machine_id,
            msg.get("issue_summary") or "app owner 已被邀请进群，请向 owner 说明当前问题上下文、已做排查和需要确认的事项。",
            msg.get("operator_open_id") or "",
        )
        if not ok:
            raise RuntimeError(err or "failed to send owner context to helper runtime")
        result = {"helper_id": helper_id, "status": "owner_invited", "context_sent": ok, "context_error": err}
    elif action == "migrate":
        runtime = _ensure_machine_helper_runtime_running(
            machine_id,
            chat_id=chat_id,
            issue_summary=msg.get("issue_summary") or "准备发送新机器迁移诊断 prompt。",
            operator_open_id=msg.get("operator_open_id") or "",
            workspace_id=workspace_id,
            metadata_resolver=metadata_resolver,
        )
        helper_id = runtime.get("helper_id") or msg.get("helper_id") or _machine_helper_id_for_machine(machine_id)
        prompt = build_machine_migration_prompt(
            msg.get("endpoint") or "",
            source_machine_id=machine_id,
            operator_open_id=msg.get("operator_open_id") or "",
        )
        ok, err = _send_to_codex_tmux(helper_id, prompt, delivery_id=f"helper-migrate-{request_id}")
        if not ok:
            raise RuntimeError(err or "failed to send migration prompt to helper runtime")
        result = {"helper_id": helper_id, "status": "migration_prompt_sent", "context_sent": ok, "context_error": err}
    else:
        raise ValueError(f"unknown helper_action: {action!r}")
    result["request_id"] = request_id
    result["machine_id"] = machine_id
    result["helper_action"] = action
    return result


def _is_machine_helper_intern(intern_name):
    if not intern_name:
        return False
    sessions_file = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            entry = (json.load(f).get(intern_name) or {})
    except (FileNotFoundError, json.JSONDecodeError):
        entry = {}
    return intern_name.startswith("machine_helper_") or entry.get("role") == "helper"

# Script content hash at startup — used for auto-update detection
def _compute_script_hash():
    """Compute deterministic hash of all files in this script's directory (daemon folder)."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        h = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(script_dir)):
            dirs[:] = [d for d in sorted(dirs) if d != '__pycache__']
            for fname in sorted(files):
                if fname.endswith('.pyc'):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, script_dir)
                h.update(rel.encode())
                with open(fpath, "rb") as f:
                    h.update(f.read())
        return h.hexdigest()[:16]
    except Exception:
        return "unknown"

_script_hash = _compute_script_hash()


# ── Meta reported to relay ──
#
# 版本信息由 VSCode 插件随 WS register 窗口帧携带。
# 这样仍覆盖 daemon 重启/插件 Reload/VSCode 重启，同时不再绕 HTTP POST。

_extension_meta = {
    "extension_version": "",
    "hooks_version": "",
    "updated_at": None,
}
_extension_meta_lock = threading.Lock()


def _update_extension_meta(extension_version, hooks_version, source):
    ext_ver = (extension_version or "").strip()
    hooks_ver = (hooks_version or "").strip()
    now_iso = datetime.now().isoformat()
    with _extension_meta_lock:
        _extension_meta["extension_version"] = ext_ver
        _extension_meta["hooks_version"] = hooks_ver
        _extension_meta["updated_at"] = now_iso
    log.info(f"[META] {source}: ext={ext_ver}, hooks={hooks_ver}")
    if _relay_client and _relay_client.connected:
        _relay_client.send({
            "type": "meta_update",
            "extension_version": ext_ver,
            "hooks_version": hooks_ver,
        })
    return now_iso


def _collect_static_meta():
    """Auth-time static meta: CLI versions only. Extension/hooks versions flow in
    later via plugin WS register → stored separately in _extension_meta."""
    import shutil as _shutil
    import subprocess as _sp

    def _probe(cmd):
        exe = _shutil.which(cmd)
        if not exe:
            return ""
        try:
            out = _sp.run([exe, "--version"], capture_output=True, timeout=3, text=True)
            return (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
        except (_sp.TimeoutExpired, OSError):
            return ""

    return {
        "cli_versions": {
            "python": sys.version.split()[0],
            "claude": _probe("claude"),
            "codex": _probe("codex"),
        },
    }


_static_meta = _collect_static_meta()


# ── Daemon self-check warnings reported to relay/admin ──
_daemon_warnings = {}
_daemon_warnings_lock = threading.Lock()
_daemon_warning_last_log = {}
_DAEMON_WARNING_LOG_INTERVAL = 300


def _set_daemon_warning(code, detail):
    now_ts = time.time()
    now_iso = datetime.now().isoformat()
    should_log = False
    changed = False
    with _daemon_warnings_lock:
        existing = _daemon_warnings.get(code)
        if existing is None:
            _daemon_warnings[code] = {"code": code, "detail": detail, "since": now_iso}
            changed = True
            should_log = True
        elif existing.get("detail") != detail:
            existing["detail"] = detail
            changed = True
            should_log = True
        elif now_ts - _daemon_warning_last_log.get(code, 0) >= _DAEMON_WARNING_LOG_INTERVAL:
            should_log = True
        if should_log:
            _daemon_warning_last_log[code] = now_ts
    if should_log:
        log.warning(f"[WARN] {code}: {detail}")
    return changed


def _clear_daemon_warning(code):
    with _daemon_warnings_lock:
        existed = code in _daemon_warnings
        if existed:
            del _daemon_warnings[code]
            _daemon_warning_last_log.pop(code, None)
    if existed:
        log.info(f"[WARN] cleared {code}")
    return existed


def _collect_daemon_warnings():
    with _daemon_warnings_lock:
        return [dict(_daemon_warnings[code]) for code in sorted(_daemon_warnings)]


def _count_feishu_daemon_processes():
    script_name = os.path.basename(__file__)
    count = 0
    pids = []
    proc_dir = "/proc"
    try:
        pid_names = os.listdir(proc_dir)
    except OSError:
        return 1, [str(os.getpid())]
    for pid_text in pid_names:
        if not pid_text.isdigit():
            continue
        cmdline_path = os.path.join(proc_dir, pid_text, "cmdline")
        try:
            with open(cmdline_path, "rb") as cmdline_file:
                raw_parts = [part for part in cmdline_file.read().split(b"\0") if part]
        except OSError:
            continue
        args = [part.decode(errors="ignore") for part in raw_parts]
        if "py_compile" in args:
            continue
        if any(os.path.basename(arg) == script_name for arg in args):
            count += 1
            pids.append(pid_text)
    return count, pids


def _check_multi_daemon_warning():
    count, pids = _count_feishu_daemon_processes()
    if count > 1:
        shown_pids = ",".join(pids[:8])
        suffix = "" if len(pids) <= 8 else ",..."
        return _set_daemon_warning(
            "multi_daemon",
            f"发现 {count} 个 feishu_daemon.py 进程 (pids={shown_pids}{suffix})",
        )
    return _clear_daemon_warning("multi_daemon")


def _sync_warnings_if_changed(changed):
    if changed and _relay_client and _relay_client.connected:
        threading.Thread(target=_refresh_lights, daemon=True).start()


def _collect_resources():
    """Dynamic machine resources reported on each sync_online."""
    import shutil as _shutil
    res = {}
    try:
        res["loadavg"] = list(os.getloadavg())
    except OSError:
        pass
    try:
        du = _shutil.disk_usage(WORK_AGENTS_ROOT)
        res["disk_free_gb"] = round(du.free / (1024 ** 3), 1)
    except OSError:
        pass
    return res


_STATUS_MD_METADATA_RE = re.compile(r"<!--\s*METADATA:(.+?)\s*-->")


def _parse_status_metadata(status_md_path):
    """Read line 3 (METADATA) of status.md. Returns {STATUS, TASK, ROLE} or {}."""
    try:
        with open(status_md_path) as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, OSError):
        return {}
    if len(lines) < 3:
        return {}
    m = _STATUS_MD_METADATA_RE.search(lines[2])
    if not m:
        return {}
    result = {}
    for pair in m.group(1).split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    result.setdefault("ROLE", "independent")
    return result


def _load_local_enterprise_sessions():
    sessions_file = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _session_status_path(entry):
    intern_dir = entry.get("intern_dir") or ""
    if intern_dir:
        state_file = os.path.join(intern_dir, ".hook_state.json")
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        resolver = state.get("metadata_resolver") if isinstance(state.get("metadata_resolver"), dict) else {}
        if resolver.get("status_path"):
            return resolver["status_path"]
    return _get_status_md_path(entry.get("intern_name") or "", entry.get("project") or "")


def _local_workspace_mode_switch_guard(workspace_id, mode):
    reasons = []
    warnings = []
    actions = []
    workspace = _workspace_cache.get_workspace(workspace_id) if _workspace_cache else None
    if not workspace:
        return {
            "workspace_id": workspace_id,
            "mode": mode,
            "available": False,
            "reasons": ["workspace not found in local daemon cache"],
            "warnings": [],
            "required_actions": [],
        }
    current_mode = workspace.get("metadata_mode") or ""
    if current_mode == mode:
        return {
            "workspace_id": workspace_id,
            "mode": mode,
            "available": True,
            "reasons": [],
            "warnings": [],
            "required_actions": [],
        }
    if current_mode == "local_only" and mode != "local_only":
        reasons.append("local_only workspaces cannot switch to repo_dotdir or metadata_branch")
    idle_seen = False
    for entry in _load_local_enterprise_sessions().values():
        if entry.get("workspace_id") != workspace_id:
            continue
        intern_name = entry.get("intern_name") or "<unknown>"
        intern_type = entry.get("type") or _get_intern_type(intern_name)
        if intern_type == "codex" and _is_codex_process_running(intern_name):
            reasons.append(f"active intern {intern_name} tmux=codex")
            continue
        if intern_type == "claude" and _is_claude_process_running(intern_name):
            reasons.append(f"active intern {intern_name} tmux=claude")
            continue
        if intern_type not in ("codex", "claude") and _check_tmux_session(intern_name):
            reasons.append(f"active intern {intern_name} tmux=session")
            continue
        meta = _parse_status_metadata(_session_status_path(entry))
        status = (meta.get("STATUS") or meta.get("status") or "").strip()
        task = (meta.get("TASK") or meta.get("task") or "").strip()
        if task or (status and status.lower() != "idle"):
            detail = f"active intern {intern_name}"
            if status:
                detail += f" status={status}"
            if task:
                detail += f" task={task}"
            reasons.append(detail)
        elif status.lower() == "idle":
            idle_seen = True
    if idle_seen:
        warnings.append("existing idle intern records will be refreshed for the new metadata mode on next session start")
    return {
        "workspace_id": workspace_id,
        "mode": mode,
        "available": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "required_actions": actions,
    }


def _merge_workspace_mode_reports(relay_report, local_report):
    report = dict(relay_report or {})
    report.setdefault("workspace_id", local_report.get("workspace_id", ""))
    report.setdefault("mode", local_report.get("mode", ""))
    report["reasons"] = list(report.get("reasons") or []) + list(local_report.get("reasons") or [])
    report["warnings"] = list(report.get("warnings") or []) + list(local_report.get("warnings") or [])
    report["required_actions"] = list(report.get("required_actions") or []) + list(local_report.get("required_actions") or [])
    report["available"] = bool(report.get("available", True)) and bool(local_report.get("available", True))
    return report


def _refresh_idle_workspace_resolvers(workspace_id):
    """Refresh idle enterprise hook_state metadata resolvers after a mode switch."""
    if _workspace_cache is None:
        return {"refreshed": 0, "errors": ["workspace cache unavailable"]}
    workspace = next(
        (item for item in _workspace_cache.list().get("workspaces", []) if item.get("workspace_id") == workspace_id),
        None,
    )
    if not workspace:
        return {"refreshed": 0, "errors": [f"workspace not found in local daemon cache: {workspace_id}"]}
    try:
        from commands.metadata import bind_repo_dotdir_metadata_to_code_repo, resolve_metadata_from_workspace
    except Exception as exc:
        return {"refreshed": 0, "errors": [f"metadata resolver import failed: {exc}"]}

    refreshed = 0
    errors = []
    for entry in _load_local_enterprise_sessions().values():
        if entry.get("workspace_id") != workspace_id:
            continue
        intern_dir = entry.get("intern_dir") or ""
        intern_name = entry.get("intern_name") or ""
        project = entry.get("project") or workspace.get("display_name") or workspace_id
        if not intern_dir or not intern_name:
            continue
        meta = _parse_status_metadata(_session_status_path(entry))
        status = (meta.get("STATUS") or meta.get("status") or "").strip()
        task_id = (meta.get("TASK") or meta.get("task") or "").strip()
        if task_id or (status and status.lower() != "idle"):
            continue
        state_path = os.path.join(intern_dir, ".hook_state.json")
        try:
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {}
            old_resolver = state.get("metadata_resolver") if isinstance(state.get("metadata_resolver"), dict) else {}
            task_id = task_id or str(old_resolver.get("task_id") or "")
            resolver = resolve_metadata_from_workspace(
                workspace,
                workspace_id=workspace_id,
                intern_name=intern_name,
                task_id=task_id,
            )
            code_repo = (
                old_resolver.get("code_worktree_path")
                or old_resolver.get("code_repo_path")
                or os.path.join(intern_dir, project)
            )
            resolver["code_repo_path"] = code_repo
            resolver["code_worktree_path"] = code_repo
            resolver = bind_repo_dotdir_metadata_to_code_repo(resolver, code_repo, intern_name, task_id)
            copied = []
            for key in ("status_path", "knowledge_path"):
                old_path = str(old_resolver.get(key) or "")
                new_path = str(resolver.get(key) or "")
                if old_path and new_path and old_path != new_path and os.path.isfile(old_path) and not os.path.exists(new_path):
                    os.makedirs(os.path.dirname(new_path), exist_ok=True)
                    shutil.copy2(old_path, new_path)
                    copied.append(new_path)
            if copied and resolver.get("metadata_mode") != "local_only":
                checkout = str(resolver.get("metadata_checkout_path") or "")
                if checkout and os.path.isdir(os.path.join(checkout, ".git")):
                    rels = []
                    checkout_abs = os.path.abspath(checkout)
                    for path_value in copied:
                        path_abs = os.path.abspath(path_value)
                        try:
                            if os.path.commonpath([checkout_abs, path_abs]) == checkout_abs:
                                rels.append(os.path.relpath(path_abs, checkout_abs))
                        except ValueError:
                            pass
                    if rels:
                        add_commit_push(
                            repo_path=checkout,
                            paths=sorted(set(rels)),
                            message=f"[{intern_name}] metadata: refresh after workspace mode switch",
                            branch=resolver.get("metadata_branch") or None,
                        )
            state["project"] = project
            state["workspace_id"] = workspace_id
            state["metadata_resolver"] = resolver
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, state_path)
            refreshed += 1
        except Exception as exc:
            errors.append(f"{intern_name}: {exc}")
    return {"refreshed": refreshed, "errors": errors}


def _online_key(intern_name, project=""):
    return f"{project}:{intern_name}" if project else intern_name


def _is_turn_active(intern_name, online_names, project=""):
    """True when the intern is online AND its feishu turn is not finalized.

    Used to drive the dashboard blue-light (mid-turn) vs green-light (online but idle).
    A turn is "active" only when the feishu module has an outstanding message for
    the current turn (``message_id`` set) and Stop APPROVE has not yet flipped
    ``finalized``. Dormant tmux sessions that have never run a turn have neither
    field set — they must stay green (idle), not blue.
    """
    if _online_key(intern_name, project) not in online_names and intern_name not in online_names:
        return False
    state_file = os.path.join(_get_intern_dir(intern_name, project=project), ".hook_state.json")
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    fs = state.get("feishu", {})
    if not fs.get("message_id"):
        return False
    return not bool(fs.get("finalized", False))


def _collect_interns_dynamic(online_names=None):
    """Per-intern dynamic state: status, current_task, last_active, turn_active.
    Scans each registered intern's <intern_dir>/<project>/workspace/interns/<name>/status.md.
    Silently skips interns without a status.md (unmanaged / legacy)."""
    if not _registry:
        return []
    if online_names is None:
        online_names = set()
    result = []
    all_interns = _iter_registry_entries(_registry)
    for item in all_interns:
        name = item["name"]
        project = _get_intern_project_scoped(name, project=item.get("project") or "")
        status_md = _get_status_md_path(name, project)
        if not os.path.isfile(status_md):
            continue
        meta = _parse_status_metadata(status_md)
        try:
            mtime = os.path.getmtime(status_md)
            last_active = datetime.fromtimestamp(mtime).isoformat()
        except OSError:
            last_active = ""
        result.append({
            "name": name,
            "project": project,
            "status": meta.get("STATUS", ""),
            "current_task": meta.get("TASK", ""),
            "role": meta.get("ROLE", "independent"),
            "team_id": meta.get("TEAM_ID", "") or meta.get("TEAM", ""),
            "last_active": last_active,
            "turn_active": _is_turn_active(name, online_names, project=project),
        })
    return result

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("feishu_daemon")


# ══════════════════════════════════════════
# 凭据 & Registry
# ══════════════════════════════════════════

def fetch_credentials_from_relay(relay_cfg):
    relay_http_url = relay_cfg.get("relay_http_url") or _relay_http_url_from_ws(relay_cfg["relay_url"])
    req = urllib.request.Request(
        relay_http_url.rstrip("/") + "/api/enterprise/daemon-credentials",
        headers={"Authorization": f"Bearer {relay_cfg['relay_token']}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"relay credentials fetch failed: HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"relay credentials fetch failed: {exc}") from exc
    credentials = result.get("credentials") or {}
    app_id = str(credentials.get("app_id") or "")
    app_secret = str(credentials.get("app_secret") or "")
    if not app_id or not app_secret:
        raise RuntimeError("relay credentials response missing app_id/app_secret")
    return app_id, app_secret


def load_credentials(relay_cfg):
    return fetch_credentials_from_relay(relay_cfg)


def _relay_http_url_from_ws(relay_url):
    parsed = urllib.parse.urlparse(relay_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    port = parsed.port
    if port is not None and port > 1:
        port -= 1
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunparse((scheme, netloc, "", "", "", ""))


# ══════════════════════════════════════════
# 飞书 API
# ══════════════════════════════════════════

class FeishuAPI:
    def __init__(self, app_id, app_secret, credential_loader=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self._credential_loader = credential_loader
        self._token = None
        self._token_expires = 0

    def _ensure_credentials(self):
        if self.app_id and self.app_secret:
            return True
        if not self._credential_loader:
            return False
        try:
            self.app_id, self.app_secret = self._credential_loader()
            log.info("Credentials refreshed from relay into daemon memory")
            return bool(self.app_id and self.app_secret)
        except Exception as exc:
            log.error(f"refresh credentials from relay failed: {exc}")
            return False

    def _get_token(self):
        now = time.time()
        if self._token and now < self._token_expires - 300:
            return self._token
        if not self._ensure_credentials():
            return None
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        req = urllib.request.Request(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                                     data=payload, headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            if result.get("code") == 0:
                self._token = result["tenant_access_token"]
                self._token_expires = now + result.get("expire", 7200)
                return self._token
        except Exception as e:
            log.error(f"get_token failed: {e}")
        return None

    def _request(self, method, path, body=None):
        token = self._get_token()
        if not token:
            return None, "no token"
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())
            if result.get("code") == 0:
                return result.get("data"), None
            return None, f"code={result.get('code')}, msg={result.get('msg')}"
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return None, str(e)

    def send_message(self, chat_id, text):
        lines = text.split("\n")
        content_lines = [[{"tag": "text", "text": line}] for line in lines]
        content = json.dumps({"zh_cn": {"content": content_lines}})
        data, err = self._request("POST", "/im/v1/messages?receive_id_type=chat_id", {
            "receive_id": chat_id, "msg_type": "post", "content": content})
        if err:
            return None, err
        return data.get("message_id") if data else None, None

    def update_message(self, message_id, text):
        lines = text.split("\n")
        content_lines = [[{"tag": "text", "text": line}] for line in lines]
        content = json.dumps({"zh_cn": {"content": content_lines}})
        _, err = self._request("PUT", f"/im/v1/messages/{message_id}", {
            "msg_type": "post", "content": content})
        return err

    def reply_message(self, message_id, text):
        content = json.dumps({"text": text})
        _, err = self._request("POST", f"/im/v1/messages/{message_id}/reply", {
            "msg_type": "text", "content": content})
        return err

    def send_to_user(self, open_id, text):
        """通过 open_id 直接给用户发消息"""
        lines = text.split("\n")
        content_lines = [[{"tag": "text", "text": line}] for line in lines]
        content = json.dumps({"zh_cn": {"content": content_lines}})
        data, err = self._request("POST", "/im/v1/messages?receive_id_type=open_id", {
            "receive_id": open_id, "msg_type": "post", "content": content})
        if err:
            return None, err
        return data.get("message_id") if data else None, None

    def mobile_to_open_id(self, mobile):
        """通过手机号反查飞书 open_id，返回 (open_id, error)"""
        data, err = self._request(
            "POST", "/contact/v3/users/batch_get_id?user_id_type=open_id",
            {"mobiles": [mobile]}
        )
        if err:
            return None, err
        user_list = (data or {}).get("user_list", [])
        if user_list and user_list[0].get("user_id"):
            return user_list[0]["user_id"], None
        return None, f"mobile '{mobile}' not found in this tenant"

    def get_user_info(self, open_id):
        """Resolve open_id to basic Feishu user info."""
        if not open_id:
            return None, "empty open_id"
        data, err = self._request("GET", f"/contact/v3/users/{open_id}?user_id_type=open_id")
        if err or not data:
            return None, err or "empty response"
        user = data.get("user") or {}
        return {
            "name": user.get("name", ""),
            "mobile": user.get("mobile", ""),
            "avatar_url": (user.get("avatar") or {}).get("avatar_72", ""),
        }, None

    def create_chat(self, name, description="", owner_open_id=""):
        body = {"name": name, "description": description or f"Intern agent: {name}",
                "chat_type": "private"}
        if owner_open_id:
            body["user_id_list"] = [owner_open_id]
        data, err = self._request("POST", "/im/v1/chats?user_id_type=open_id", body)
        if err:
            return None, err
        return data.get("chat_id") if data else None, None

    def add_chat_managers(self, chat_id, open_ids):
        """把成员设置为群管理员。open_ids 必须已经是群成员。"""
        _, err = self._request(
            "POST", f"/im/v1/chats/{chat_id}/managers/add_managers?member_id_type=open_id",
            {"manager_ids": open_ids})
        return err

    def delete_chat(self, chat_id):
        _, err = self._request("DELETE", f"/im/v1/chats/{chat_id}")
        return err

    def list_chats(self):
        chats = []
        page_token = ""
        while True:
            path = f"/im/v1/chats?page_size=100"
            if page_token:
                path += f"&page_token={page_token}"
            data, err = self._request("GET", path)
            if err:
                log.error(f"list_chats: {err}")
                break
            items = data.get("items", []) if data else []
            for item in items:
                chats.append({"chat_id": item.get("chat_id", ""), "name": item.get("name", "")})
            if not data or not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
        return chats

    def update_chat(self, chat_id, name=None, avatar=None):
        # avatar 字段保留：task204 回滚后当前不主动设置头像（类型由群名 emoji 区分），
        # avatar="" 可用于重置为飞书默认头像。
        body = {}
        if name is not None:
            body["name"] = name
        if avatar is not None:
            body["avatar"] = avatar
        if not body:
            return None
        _, err = self._request("PUT", f"/im/v1/chats/{chat_id}", body)
        return err

    def upload_avatar_image(self, data_bytes, filename="avatar.png"):
        """POST /im/v1/images with image_type=avatar. Returns (image_key, err).

        task204: 当前未被主动调用（类型区分改为群名 emoji 方案）。
        保留以便未来重启头像区分方案时直接复用。
        """
        import uuid
        token = self._get_token()
        if not token:
            return None, "no token"
        boundary = "----avatar" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"image_type\"\r\n\r\n"
            f"avatar\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + data_bytes + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/images", data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            if result.get("code") == 0:
                return (result.get("data") or {}).get("image_key"), None
            return None, f"code={result.get('code')}, msg={result.get('msg')}"
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return None, str(e)

    def send_interactive_card(self, chat_id, card_json):
        """Send an interactive card message. Returns (message_id, error)."""
        content = json.dumps(card_json)
        data, err = self._request("POST", "/im/v1/messages?receive_id_type=chat_id", {
            "receive_id": chat_id, "msg_type": "interactive", "content": content})
        if err:
            return None, err
        return data.get("message_id") if data else None, None

    def update_interactive_card(self, message_id, card_json):
        """Update an existing interactive card message via PATCH."""
        content = json.dumps(card_json)
        _, err = self._request("PATCH", f"/im/v1/messages/{message_id}", {
            "msg_type": "interactive", "content": content})
        return err

    def upload_file(self, file_path, file_type="stream"):
        """POST /im/v1/files multipart upload. Returns (file_key, err)。

        飞书文件上限 30MB；超过返回 err。file_type 默认 stream（通用二进制），
        .md 等文本走 stream 即可，飞书 IM 客户端会渲染 markdown 预览。
        """
        import os as _os
        import uuid as _uuid
        if not _os.path.isfile(file_path):
            return None, f"file not found: {file_path}"
        size = _os.path.getsize(file_path)
        if size > 30 * 1024 * 1024:
            return None, f"file {file_path} is {size / 1024 / 1024:.1f} MB, exceeds 30 MB"
        token = self._get_token()
        if not token:
            return None, "no token"
        filename = _os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data = f.read()
        boundary = "----file" + _uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file_type"\r\n\r\n'
            f"{file_type}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file_name"\r\n\r\n'
            f"{filename}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/files", data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            result = json.loads(resp.read())
            if result.get("code") == 0:
                return (result.get("data") or {}).get("file_key"), None
            return None, f"code={result.get('code')}, msg={result.get('msg')}"
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return None, str(e)

    def send_file(self, chat_id, file_key):
        """Send a previously uploaded file as msg_type=file. Returns (message_id, err)."""
        content = json.dumps({"file_key": file_key})
        data, err = self._request("POST", "/im/v1/messages?receive_id_type=chat_id", {
            "receive_id": chat_id, "msg_type": "file", "content": content})
        if err:
            return None, err
        return data.get("message_id") if data else None, None


# ══════════════════════════════════════════
# Registry
# ══════════════════════════════════════════

class RegistryManager:
    def __init__(self, registry_dir):
        self.registry_dir = registry_dir
        self._cache = {}
        self._intern_to_chat = {}
        self.reload()

    def reload(self):
        self._cache = {}
        self._intern_to_chat = {}
        if not os.path.isdir(self.registry_dir):
            return
        for fname in os.listdir(self.registry_dir):
            if not fname.endswith(".json"):
                continue
            try:
                data = json.loads(Path(os.path.join(self.registry_dir, fname)).read_text())
                chat_id = data.get("chatId", "")
                intern_name = data.get("internName") or fname.replace(".json", "")
                project = data.get("project") or ""
                if chat_id and intern_name:
                    key = self._key(intern_name, project)
                    self._cache[chat_id] = {
                        "intern_name": intern_name,
                        "project": project,
                        "chat_id": chat_id,
                    }
                    self._intern_to_chat[key] = chat_id
            except Exception:
                continue

    @staticmethod
    def _key(intern_name, project=""):
        return f"{project}:{intern_name}" if project else intern_name

    @staticmethod
    def _safe_file_part(value):
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._") or "default"

    def _registry_path(self, intern_name, project=""):
        if project:
            fname = f"{self._safe_file_part(project)}__{self._safe_file_part(intern_name)}.json"
        else:
            fname = f"{self._safe_file_part(intern_name)}.json"
        return os.path.join(self.registry_dir, fname)

    def find_intern(self, chat_id):
        entry = self._cache.get(chat_id)
        if isinstance(entry, dict):
            return entry.get("intern_name")
        return entry

    def find_intern_info(self, chat_id):
        entry = self._cache.get(chat_id)
        if isinstance(entry, dict):
            return dict(entry)
        if entry:
            return {"intern_name": entry, "project": "", "chat_id": chat_id}
        return {}

    def find_chat_id(self, intern_name, project=""):
        if project:
            return self._intern_to_chat.get(self._key(intern_name, project))
        legacy = self._intern_to_chat.get(intern_name)
        matches = [
            chat_id for key, chat_id in self._intern_to_chat.items()
            if key == intern_name or key.endswith(f":{intern_name}")
        ]
        if legacy and len(set(matches)) <= 1:
            return legacy
        if len(set(matches)) == 1:
            return matches[0]
        if len(set(matches)) > 1:
            log.warning(f"[REGISTRY] ambiguous chat lookup for intern={intern_name}; project required")
        return None

    def get_all(self):
        return dict(self._intern_to_chat)

    def get_all_entries(self):
        return [dict(entry) for entry in self._cache.values() if isinstance(entry, dict)]

    def register(self, intern_name, chat_id, project=""):
        os.makedirs(self.registry_dir, exist_ok=True)
        reg_path = self._registry_path(intern_name, project)
        with open(reg_path, "w") as f:
            data = {"internName": intern_name, "chatId": chat_id}
            if project:
                data["project"] = project
            json.dump(data, f, indent=2)
        self.reload()

    def unregister(self, intern_name, project=""):
        paths = [self._registry_path(intern_name, project)]
        if not project:
            for fname in os.listdir(self.registry_dir) if os.path.isdir(self.registry_dir) else []:
                if not fname.endswith(".json"):
                    continue
                try:
                    data = json.loads(Path(os.path.join(self.registry_dir, fname)).read_text())
                except Exception:
                    continue
                if data.get("internName") == intern_name and not data.get("project"):
                    paths.append(os.path.join(self.registry_dir, fname))
        for reg_path in set(paths):
            if os.path.exists(reg_path):
                os.remove(reg_path)
        self.reload()

    def sync_from_chats(self, chats):
        os.makedirs(self.registry_dir, exist_ok=True)
        count = 0
        for chat in chats:
            name = chat.get("name", "")
            chat_id = chat.get("chat_id", "")
            clean = re.sub(r'^[🟢🔴⚪🤖\s]+', '', name).strip()
            clean = re.sub(r'^\[(?:Claude🤖|Claude)\]\s*', '', clean).strip()
            intern_name = clean.split("/")[0].strip() if "/" in clean else clean.strip()
            if not intern_name or not re.match(r'^[a-z][a-z0-9_]*$', intern_name):
                continue
            project = clean.split("/", 1)[1].strip() if "/" in clean else ""
            self.register(intern_name, chat_id, project=project)
            count += 1
        return count

    # ── owner mobile 持久化 ──

    def load_owner_mobile(self):
        try:
            path_ = os.path.join(self.registry_dir, "_owner.json")
            if os.path.exists(path_):
                return json.loads(Path(path_).read_text()).get("mobile")
        except Exception:
            pass
        return None


def _load_owner_open_id():
    try:
        path_ = Path(OWNER_JSON_PATH)
        if path_.exists():
            owner = json.loads(path_.read_text(encoding="utf-8"))
            return owner.get("owner_open_id") or owner.get("open_id") or ""
    except Exception:
        pass
    return ""


class WorkspaceCache:
    """Daemon-local workspace cache and enable state.

    Relay is authoritative for workspace existence. This cache stores the last
    relay snapshot plus per-machine enable/local path state.
    """

    SCHEMA = "intern-agents.daemon-workspaces.v1"
    SCHEMA_VERSION = 1

    def __init__(self, work_root, cache_path=None):
        self.work_root = work_root
        self.cache_path = cache_path or os.fspath(daemon_workspace_cache_path(work_root))
        self._lock = threading.RLock()
        self._data = {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "relay_synced_at": "",
            "workspaces": {},
            "enabled": {},
        }
        self._load()

    def _load(self):
        candidates = [self.cache_path]
        if not os.path.exists(self.cache_path):
            candidates.extend([
                os.fspath(state_registry_path(self.work_root)),
                os.path.join(self.work_root, ".enterprise_state", "workspaces.json"),
            ])
        for load_path in candidates:
            if not os.path.exists(load_path):
                continue
            try:
                with open(load_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("schema") != self.SCHEMA:
                    continue
                data.setdefault("schema_version", self.SCHEMA_VERSION)
                data.setdefault("workspaces", {})
                data.setdefault("enabled", {})
                data.setdefault("relay_synced_at", "")
                self._data = data
                return
            except Exception as e:
                log.error(f"[WORKSPACE] failed to load daemon cache {load_path}: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.cache_path)

    def _save_workspace_records(self):
        for workspace_id, item in self._data.get("workspaces", {}).items():
            record_path = workspace_record_path(self.work_root, workspace_id)
            record = dict(item)
            record.setdefault("schema", WORKSPACE_SCHEMA)
            record.setdefault("workspace_key", workspace_id)
            os.makedirs(os.path.dirname(os.fspath(record_path)), exist_ok=True)
            tmp = os.fspath(record_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, record_path)

    def _save_state_registry_mapping(self):
        registry_path = state_registry_path(self.work_root)
        data = {}
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            data = {}
        if data.get("schema") != LOCAL_REGISTRY_SCHEMA:
            data = {}
        data.setdefault("schema", LOCAL_REGISTRY_SCHEMA)
        data.setdefault("work_agents_root", os.fspath(Path(self.work_root)))
        data.setdefault("default_metadata_mode", "repo_dotdir")
        workspaces = data.get("workspaces")
        if not isinstance(workspaces, dict):
            workspaces = {}
        data["workspaces"] = workspaces
        for workspace_id in self._data.get("workspaces", {}):
            workspaces[workspace_id] = f"{workspace_id}/workspace.json"
        os.makedirs(os.path.dirname(os.fspath(registry_path)), exist_ok=True)
        tmp = os.fspath(registry_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, registry_path)

    def validate_workspace_id(self, workspace_id):
        return validate_workspace_id(workspace_id)

    def sync_from_relay_payload(self, payload):
        workspaces = payload.get("workspaces")
        if not isinstance(workspaces, list):
            raise ValueError("relay workspace response missing workspaces list")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._lock:
            self._data["relay_synced_at"] = now
            synced = {}
            for item in workspaces:
                if not isinstance(item, dict) or not item.get("workspace_id"):
                    continue
                workspace_id = self.validate_workspace_id(item["workspace_id"])
                synced[workspace_id] = dict(item)
            self._data["workspaces"] = synced
            self._save()
            self._save_workspace_records()
            self._save_state_registry_mapping()
            return self.list()

    def list(self):
        with self._lock:
            result = []
            for workspace_id, item in self._data.get("workspaces", {}).items():
                local = dict(self._data.get("enabled", {}).get(workspace_id, {}))
                merged = dict(item)
                merged["local_enabled"] = bool(local.get("enabled", False))
                merged["local_path"] = local.get("local_path", self.default_local_path(item))
                merged["metadata_cache_path"] = local.get("metadata_cache_path", self.default_metadata_cache_path(workspace_id))
                merged["state_path"] = os.fspath(workspace_state_dir(self.work_root, workspace_id))
                merged["last_checked_at"] = local.get("last_checked_at", "")
                result.append(merged)
            result.sort(key=lambda x: (x.get("display_name") or "", x.get("workspace_id") or ""))
            return {
                "schema": self.SCHEMA,
                "schema_version": self.SCHEMA_VERSION,
                "relay_synced_at": self._data.get("relay_synced_at", ""),
                "workspaces": result,
            }

    def get_workspace(self, workspace_id):
        workspace_id = self.validate_workspace_id(workspace_id)
        with self._lock:
            item = self._data.get("workspaces", {}).get(workspace_id)
            return dict(item) if item else None

    def default_local_path(self, workspace):
        workspace_id = self.validate_workspace_id(workspace.get("workspace_id") or "")
        return os.fspath(workspace_source_path(self.work_root, workspace_id))

    def default_metadata_cache_path(self, workspace_id):
        return os.fspath(workspace_metadata_cache_path(self.work_root, self.validate_workspace_id(workspace_id)))

    def _ensure_code_checkout(self, workspace, local_path):
        provider = workspace.get("provider") or ""
        repo_url = workspace.get("repo_url") or ""
        if provider == "local":
            if not os.path.isdir(os.path.join(local_path, ".git")):
                raise RuntimeError(f"local workspace path is not a git repo: {local_path}")
            return
        if not repo_url:
            raise RuntimeError("repo_url is required to enable remote workspace")
        if os.path.isdir(os.path.join(local_path, ".git")):
            return
        target = Path(local_path)
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"local workspace path is not a git repo and is not empty: {local_path}")
        if target.exists():
            target.rmdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        ssh_command = env.get("GIT_SSH_COMMAND", "ssh")
        if "BatchMode" not in ssh_command:
            ssh_command = f"{ssh_command} -o BatchMode=yes"
        if "ConnectTimeout" not in ssh_command:
            ssh_command = f"{ssh_command} -o ConnectTimeout=10"
        env["GIT_SSH_COMMAND"] = ssh_command
        result = subprocess.run(
            ["git", "clone", repo_url, local_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=int(os.environ.get("INTERN_CODE_GIT_TIMEOUT", "120")),
        )
        if result.returncode != 0:
            shutil.rmtree(local_path, ignore_errors=True)
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"code repo clone failed: {detail}")

    def enable(self, workspace_id, local_path=None):
        workspace_id = self.validate_workspace_id(workspace_id)
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            raise KeyError(f"workspace not found: {workspace_id}")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if not local_path and workspace.get("provider") == "local" and workspace.get("repo_url"):
            local_path = workspace.get("repo_url")
        local_path = local_path or self.default_local_path(workspace)
        self._ensure_code_checkout(workspace, local_path)
        metadata_cache_path = self.default_metadata_cache_path(workspace_id)
        if workspace.get("metadata_mode") == "metadata_branch":
            os.makedirs(metadata_cache_path, exist_ok=True)
            checkout_workspace = dict(workspace)
            checkout_workspace["local_path"] = local_path
            checkout_workspace["metadata_cache_path"] = metadata_cache_path
            ensure_metadata_branch_checkout(checkout_workspace, workspace_id=workspace_id)
        with self._lock:
            self._data.setdefault("enabled", {})[workspace_id] = {
                "enabled": True,
                "local_path": local_path,
                "metadata_cache_path": metadata_cache_path,
                "last_checked_at": now,
            }
            self._save()
        return {"ok": True, "workspace_id": workspace_id, "enabled": True, "local_path": local_path}

    def disable(self, workspace_id):
        workspace_id = self.validate_workspace_id(workspace_id)
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            raise KeyError(f"workspace not found: {workspace_id}")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._lock:
            current = self._data.setdefault("enabled", {}).get(workspace_id, {})
            current["enabled"] = False
            current["last_checked_at"] = now
            current.setdefault("local_path", self.default_local_path(workspace))
            current.setdefault("metadata_cache_path", self.default_metadata_cache_path(workspace_id))
            self._data["enabled"][workspace_id] = current
            self._save()
        return {"ok": True, "workspace_id": workspace_id, "enabled": False, "local_path": current["local_path"]}

    def doctor(self, workspace_id):
        workspace_id = self.validate_workspace_id(workspace_id)
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            raise KeyError(f"workspace not found: {workspace_id}")
        with self._lock:
            local = dict(self._data.get("enabled", {}).get(workspace_id, {}))
        local_path = local.get("local_path", self.default_local_path(workspace))
        checks = {
            "registered_in_relay_cache": True,
            "local_enabled": bool(local.get("enabled", False)),
            "local_clone_exists": os.path.isdir(os.path.join(local_path, ".git")),
            "metadata_mode": workspace.get("metadata_mode", ""),
            "metadata_cache_path": local.get("metadata_cache_path", self.default_metadata_cache_path(workspace_id)),
        }
        return {"ok": True, "workspace_id": workspace_id, "checks": checks, "workspace": workspace}


# ══════════════════════════════════════════
# WebSocket server (push to plugin)
# ══════════════════════════════════════════

class WSServer:
    """WebSocket server for pushing messages to VS Code plugin.
    
    Maintains a window_registry for targeted routing:
    - Each plugin sends a 'register' frame on connect with {window_id, active_intern}
    - 'update_active' frames update the active intern for a window
    - feishu_message is routed only to the window whose active_intern matches
    - status_changed is broadcast to all (every window may care)
    """

    def __init__(self, port):
        self.port = port  # requested port (0 = ephemeral)
        self.actual_port = None  # filled after bind
        self.clients = set()
        # window_id → { "ws": websocket, "active": intern_name|None }
        self.windows = {}
        self._loop = None
        self._server = None
        self._bound = threading.Event()

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        # Wait for the server to actually bind so caller can read actual_port
        if not self._bound.wait(timeout=10):
            raise RuntimeError("WSServer failed to bind within 10s")
        log.info(f"WebSocket server starting on ws://localhost:{self.actual_port}")

    def _run(self):
        try:
            import websockets
        except ImportError:
            log.error("[WS_SERVER] FATAL: 'websockets' package not installed! Run: pip3 install websockets lark-oapi")
            raise
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        def _parse_window_started_at(window_id, raw_started_at):
            try:
                if raw_started_at is not None:
                    return int(raw_started_at)
            except (TypeError, ValueError):
                pass

            if isinstance(window_id, str):
                match = re.search(r"(\d{10,})$", window_id)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        pass

            return 0

        async def handler(websocket):
            self.clients.add(websocket)
            window_id = None
            log.info(f"WS client connected ({len(self.clients)} total)")
            try:
                async for raw in websocket:
                    try:
                        msg = json.loads(raw)
                        msg_type = msg.get("type")
                        if msg_type == "register":
                            window_id = msg.get("window_id")
                            active = msg.get("active_intern")
                            started_at = _parse_window_started_at(window_id, msg.get("window_started_at"))

                            stale_ws = self._register_window(window_id, websocket, active, started_at)
                            if stale_ws:
                                log.info(f"[WS_REG] Replacing previous socket for window {window_id}")
                                self.clients.discard(stale_ws)
                                asyncio.ensure_future(stale_ws.close(1000, "replaced by same window"))
                            log.info(f"[WS_REG] Window {window_id} registered, active={active}, started_at={started_at}")
                            if "extension_version" in msg or "hooks_version" in msg:
                                _update_extension_meta(
                                    msg.get("extension_version", ""),
                                    msg.get("hooks_version", ""),
                                    "ws_register",
                                )
                            else:
                                log.warning("[META] WS register missing extension/hooks version")
                            threading.Thread(target=_refresh_lights, daemon=True).start()
                        elif msg_type == "update_active":
                            if window_id and window_id in self.windows:
                                old = self.windows[window_id].get("active")
                                self.windows[window_id]["active"] = msg.get("active_intern")
                                log.info(f"[WS_REG] Window {window_id} active: {old} → {msg.get('active_intern')}")
                                threading.Thread(target=_refresh_lights, daemon=True).start()
                    except json.JSONDecodeError:
                        pass
            finally:
                self.clients.discard(websocket)
                if window_id and window_id in self.windows and self.windows[window_id].get("ws") is websocket:
                    del self.windows[window_id]
                    log.info(f"[WS_REG] Window {window_id} unregistered")
                    threading.Thread(target=_refresh_lights, daemon=True).start()
                log.info(f"WS client disconnected ({len(self.clients)} total)")

        async def serve():
            self._server = await websockets.serve(handler, "localhost", self.port)
            # Capture actual bound port (matters when self.port == 0 → ephemeral)
            sock = list(self._server.sockets)[0]
            self.actual_port = sock.getsockname()[1]
            self._bound.set()
            log.info(f"WebSocket server listening on ws://localhost:{self.actual_port}")
            await self._server.wait_closed()

        self._loop.run_until_complete(serve())

    def _register_window(self, window_id, websocket, active, started_at):
        """Register one VS Code plugin connection without evicting other windows.

        Multiple users may open the extension from the same machine and same
        WORK_AGENTS_ROOT. They intentionally share this daemon, so distinct
        window_id values must coexist. Only a reconnect/reload of the same
        window_id replaces its previous socket.
        """
        existing_entry = self.windows.get(window_id)
        stale_ws = None
        if existing_entry and existing_entry.get("ws") is not websocket:
            stale_ws = existing_entry["ws"]

        self.windows[window_id] = {
            "ws": websocket,
            "active": active,
            "started_at": started_at,
        }
        return stale_ws

    def push(self, data):
        """Broadcast message to all connected clients (used for status_changed)."""
        if not self.clients or not self._loop:
            return
        msg = json.dumps(data)

        async def _send():
            disconnected = set()
            for ws in self.clients.copy():
                try:
                    await ws.send(msg)
                except Exception:
                    disconnected.add(ws)
            self.clients -= disconnected

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def route_to_active(self, intern_name, data):
        """Send message only to the window whose active_intern matches. Returns True if delivered."""
        if not self._loop:
            return False
        target_ws = None
        for wid, info in self.windows.items():
            if info.get("active") == intern_name:
                target_ws = info["ws"]
                break
        if not target_ws:
            return False
        msg = json.dumps(data)
        asyncio.run_coroutine_threadsafe(target_ws.send(msg), self._loop)
        return True

    def get_active_interns(self):
        """Return set of all active intern names across all windows."""
        return {info["active"] for info in self.windows.values() if info.get("active")}


# ══════════════════════════════════════════
# Local Config & Relay Client
# ══════════════════════════════════════════

def load_relay_config():
    """Load relay config from _owner.json. Returns dict with relay_url, relay_token.
    
    Raises SystemExit if _owner.json is missing or lacks relay fields.
    """
    if not os.path.exists(OWNER_JSON_PATH):
        log.error(f"_owner.json not found: {OWNER_JSON_PATH}")
        sys.exit(1)
    try:
        with open(OWNER_JSON_PATH, "r") as f:
            owner = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.error(f"Failed to load {OWNER_JSON_PATH}: {e}")
        sys.exit(1)
    relay_url = owner.get("relay_url", "")
    relay_token = owner.get("relay_token", "")
    relay_http_url = owner.get("relay_http_url") or _relay_http_url_from_ws(relay_url)
    if not relay_url or not relay_token:
        log.error(f"_owner.json missing relay_url or relay_token")
        sys.exit(1)
    import socket
    # Get local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = socket.gethostbyname(socket.gethostname())
    # Get SSH port from SSH_CONNECTION env var (format: client_ip client_port server_ip server_port)
    ssh_port = 22
    ssh_conn = os.environ.get("SSH_CONNECTION", "")
    if ssh_conn:
        parts = ssh_conn.split()
        if len(parts) >= 4:
            ssh_port = int(parts[3])
    return {
        "relay_url": relay_url,
        "relay_http_url": relay_http_url,
        "relay_token": relay_token,
        "machine_id": _build_instance_id(),
        "owner_mobile": owner.get("mobile", ""),
        "owner_open_id": owner.get("owner_open_id") or owner.get("open_id") or "",
        "ip": local_ip,
        "ssh_port": ssh_port,
    }


def enrich_owner_identity_at_startup(api):
    """Resolve owner mobile once at daemon startup and persist display fields.

    `_owner.json` is the local source VS Code can read without making Feishu API
    calls. Some deployed files only contain `mobile`; resolving the name here lets
    UI defaults use the supervisor's name instead of falling back to `user`.
    """
    try:
        with open(OWNER_JSON_PATH, "r", encoding="utf-8") as f:
            owner = json.load(f)
    except FileNotFoundError:
        log.warning(f"[OWNER] _owner.json not found: {OWNER_JSON_PATH}")
        return False
    except Exception as exc:
        log.warning(f"[OWNER] failed to read _owner.json: {exc}")
        return False

    mobile = str(owner.get("mobile") or "").strip()
    if not mobile:
        log.warning("[OWNER] _owner.json missing mobile; skip owner identity enrichment")
        return False

    open_id = str(owner.get("owner_open_id") or owner.get("open_id") or "").strip()
    if not open_id:
        open_id, err = api.mobile_to_open_id(mobile)
        if err or not open_id:
            log.warning(f"[OWNER] mobile_to_open_id failed for owner mobile: {err}")
            return False

    user_info, err = api.get_user_info(open_id)
    if err or not user_info:
        log.warning(f"[OWNER] get_user_info failed for owner open_id: {err}")
        return False

    owner_name = str(user_info.get("name") or "").strip()
    updated = False
    updates = {
        "owner_open_id": open_id,
        "open_id": open_id,
        "owner_name": owner_name,
        "name": owner_name,
        "display_name": owner_name,
        "avatar_url": str(user_info.get("avatar_url") or "").strip(),
    }
    if user_info.get("mobile"):
        updates["mobile"] = str(user_info.get("mobile") or "").strip()

    for key, value in updates.items():
        if value and owner.get(key) != value:
            owner[key] = value
            updated = True

    if not updated:
        log.info(f"[OWNER] owner identity already present: {owner_name or open_id}")
        return True

    owner["owner_identity_updated_at"] = datetime.now().isoformat()
    owner_dir = os.path.dirname(OWNER_JSON_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix="_owner.", suffix=".json.tmp", dir=owner_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(owner, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, OWNER_JSON_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    log.info(f"[OWNER] enriched owner identity: name={owner_name or '-'}, open_id={open_id}")
    return True


def _build_instance_id():
    """Globally unique identifier for THIS daemon = hostname:ssh_port.

    Multiple dockers on the same physical host that share host network namespace
    will all return the same hostname (e.g. 'dev4infer'), so we MUST disambiguate
    by SSH port — each docker is reachable on its own SSH port (the user's entry).

    SSH port resolution order:
      1) FEISHU_INSTANCE_SSH_PORT env (extension-injected at spawn)
      2) parse from SSH_CONNECTION env (if daemon was started inside an SSH session)
      3) fallback '22'
    """
    hostname = socket.gethostname()
    ssh_port = os.environ.get("FEISHU_INSTANCE_SSH_PORT")
    if not ssh_port:
        ssh_conn = os.environ.get("SSH_CONNECTION", "")
        if ssh_conn:
            parts = ssh_conn.split()
            if len(parts) >= 4:
                ssh_port = parts[3]
    if not ssh_port:
        ssh_port = "22"
    return f"{hostname}:{ssh_port}"


class RelayClient:
    """WebSocket client connecting to the Relay Server in relay mode.

    Handles: auth, register_interns, heartbeat, receiving feishu_message.
    """

    def __init__(self, relay_url, relay_token, machine_id, registry, ws_server,
                 owner_mobile="", owner_open_id="", ip="", ssh_port=22):
        self.relay_url = relay_url
        self.relay_token = relay_token
        self.machine_id = machine_id
        self.registry = registry
        self.ws_server = ws_server
        self.owner_mobile = owner_mobile
        self.owner_open_id = owner_open_id
        self.ip = ip
        self.ssh_port = ssh_port
        self._loop = None
        self._ws = None
        self._connected = False
        self._conn_lock = threading.Lock()
        self._stop = False
        self._check_online_handler = None
        # task213: peer-send request/response correlation via request_id.
        self._peer_pending = {}            # request_id → {"event": Event, "result": dict}
        self._peer_pending_lock = threading.Lock()
        # Derive relay HTTP base URL from WS URL (ws://host:28081 → http://host:28080)
        import re as _re
        m = _re.match(r'wss?://([^:]+):(\d+)', relay_url)
        if m:
            self._relay_http_base = f"http://{m.group(1)}:{int(m.group(2)) - 1}"
        else:
            self._relay_http_base = None

    @property
    def connected(self):
        with self._conn_lock:
            return self._connected

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self):
        import websockets.sync.client as ws_sync
        while not self._stop:
            try:
                self._connect_and_listen(ws_sync)
            except Exception as e:
                self._clear_connection()
                if self._stop:
                    break
                log.warning(f"[RELAY_CLIENT] Connection lost: {e}, reconnecting in 5s...")
                time.sleep(5)

    def _set_connection(self, ws):
        with self._conn_lock:
            self._ws = ws
            self._connected = True

    def _clear_connection(self, ws=None):
        with self._conn_lock:
            if ws is not None and self._ws is not ws:
                return
            self._ws = None
            self._connected = False

    def _current_connection(self):
        with self._conn_lock:
            return self._ws

    def _mark_connection_broken(self, ws, reason):
        if ws is None:
            self._clear_connection()
            return
        with self._conn_lock:
            if self._ws is not ws:
                return
            self._ws = None
            self._connected = False
        log.warning(f"[RELAY_CLIENT] {reason}; closing relay websocket to trigger reconnect")
        try:
            ws.close()
        except Exception as close_err:
            log.warning(f"[RELAY_CLIENT] failed to close broken relay websocket: {close_err}")

    def _connect_and_listen(self, ws_sync):
        log.info(f"[RELAY_CLIENT] Connecting to {self.relay_url}...")
        with ws_sync.connect(self.relay_url) as ws:
            # Auth
            with _extension_meta_lock:
                ext_ver = _extension_meta.get("extension_version", "")
                hooks_ver = _extension_meta.get("hooks_version", "")
            workspaces = []
            if _workspace_cache is not None:
                try:
                    workspaces = _workspace_cache.list().get("workspaces", [])
                except Exception as e:
                    log.warning(f"[WORKSPACE] failed to include workspace state in auth: {e}")
            ws.send(json.dumps({
                "type": "auth",
                "token": self.relay_token,
                "machine_id": self.machine_id,
                "owner_mobile": self.owner_mobile,
                "owner_open_id": self.owner_open_id,
                "ip": self.ip,
                "ssh_port": self.ssh_port,
                "script_hash": _script_hash,
                "extension_version": ext_ver,
                "hooks_version": hooks_ver,
                "cli_versions": _static_meta.get("cli_versions", {}),
                "workspaces": workspaces,
                # task228: 本 daemon 版本支持入站附件落盘 + pending_attachments state。
                # relay 根据该列表决定要不要把附件下载并转发。老 daemon 不上报 →
                # relay 看到空列表 → reply_message 提示主管升级插件。详见 task238。
                # task261: "peer" 表示本 daemon 识别 intern_peer_message WS msg_type；
                # 老 daemon 不上报 → relay forward 前 gate → A 端立即拿 target_outdated
                # reason，避免 10s 超时后误报 relay_unreachable + 静默丢消息。
                # task283: "detail_mode" 表示本 daemon 支持 detail_mode_get/set RPC；
                # 老 daemon 无此 cap → relay 明确报"daemon 需升级"，不回落到 relay 本地
                # （旧路径已删，无 fallback 可言）。
                "capabilities": [
                    "attachments", "peer", "peer_modes", "detail_mode", "goal_api", "team_contract", "mailbox",
                    "machine_helper"
                ],
            }))
            resp = json.loads(ws.recv(timeout=10))
            if resp.get("type") != "auth_result" or not resp.get("ok"):
                log.error(f"[RELAY_CLIENT] Auth failed: {resp.get('error', 'unknown')}")
                time.sleep(10)
                return

            self._set_connection(ws)
            log.info(f"[RELAY_CLIENT] Authenticated as '{self.machine_id}'")

            # Register all local interns
            self._registered_interns = set()  # Reset on reconnect
            self._register_local_interns(ws)

            # Sync current online states to relay server after connect
            _check_multi_daemon_warning()
            threading.Thread(target=_refresh_lights, daemon=True).start()

            # Start heartbeat thread
            hb_stop = threading.Event()
            hb_thread = threading.Thread(target=self._heartbeat_loop, args=(ws, hb_stop), daemon=True)
            hb_thread.start()

            try:
                # Listen for messages from relay server
                while not self._stop:
                    try:
                        raw = ws.recv(timeout=30)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    self._handle_relay_message(msg)
            finally:
                hb_stop.set()
                self._clear_connection(ws)

    def _register_local_interns(self, ws):
        """Register locally-owned interns with the relay.

        task216 clarification: register == "this machine owns this intern"
        (local dir exists). It is intentionally broader than "online":
        relay routes incoming feishu messages to the machine that owns the
        intern even when the CLI is not running at that instant — the daemon
        will start/resume it on demand. "online" status (sync_online, below)
        is the live CLI check.

          - Claude/Codex interns and machine helpers: registered iff local intern dir exists.
          - Copilot interns: only registered when the VS Code window is the
            active owner (via _ws_server.get_active_interns). Non-active
            Copilot interns are NOT registered because they are not running
            on this machine at all.
        """
        all_interns = _iter_registry_entries(self.registry)
        if not all_interns:
            log.info("[RELAY_CLIENT] No local interns to register")
            return
        active_copilot = _ws_server.get_active_interns() if _ws_server else set()
        interns = []
        skipped = []
        for item in all_interns:
            name = item["name"]
            chat_id = item["chat_id"]
            project = item.get("project") or ""
            intern_dir = _get_intern_dir(name, project=project)
            if not os.path.isdir(intern_dir):
                skipped.append(name)
                continue
            intern_type = _get_intern_type_scoped(name, project=project)
            if _is_tmux_intern_type(intern_type):
                # Register all tmux-based interns (Claude/Codex) that have local dirs
                interns.append({"name": name, "type": intern_type, "chat_id": chat_id, "project": _get_intern_project_scoped(name, project=project)})
            elif name in active_copilot:
                # Only register active Copilot interns
                interns.append({"name": name, "type": intern_type, "chat_id": chat_id, "project": _get_intern_project_scoped(name, project=project)})
            else:
                skipped.append(name)
        if skipped:
            log.info(f"[RELAY_CLIENT] Skipped {len(skipped)} non-active interns: {skipped}")
        if interns:
            ws.send(json.dumps({"type": "register_interns", "interns": interns}))
            self._registered_interns.update(i["name"] for i in interns)
            log.info(f"[RELAY_CLIENT] Registered {len(interns)} local targets: {[i['name'] for i in interns]}")
        else:
            log.info("[RELAY_CLIENT] No local targets to register")

    def send(self, data):
        """Send a message to the relay server. Thread-safe."""
        with self._conn_lock:
            ws = self._ws
        if not ws:
            return
        try:
            ws.send(json.dumps(data))
        except Exception as e:
            self._mark_connection_broken(ws, f"send failed: {e}")

    # Track which interns have been registered with relay in this connection
    _registered_interns = set()

    def _ensure_registered(self, intern_name, project=None):
        """Ensure an intern is registered with relay. Used when Copilot becomes active after startup."""
        project = project or _get_intern_project_scoped(intern_name) or ""
        key = _online_key(intern_name, project)
        if key in self._registered_interns:
            return
        chat_id = self.registry.find_chat_id(intern_name, project=project)
        if not chat_id:
            return
        intern_type = _get_intern_type_scoped(intern_name, project=project)
        self.send({"type": "register_interns", "interns": [
            {"name": intern_name, "type": intern_type, "chat_id": chat_id, "project": project}
        ]})
        self._registered_interns.add(key)
        log.info(f"[RELAY_CLIENT] Late-registered Copilot '{intern_name}' project={project} with relay")

    def send_intern_online(self, intern_name, project=None):
        """Notify relay that an intern went online on this machine.
        Includes chat_id and type so relay can auto-register if needed."""
        project = project or _get_intern_project_scoped(intern_name) or ""
        self._ensure_registered(intern_name, project=project)
        chat_id = self.registry.find_chat_id(intern_name, project=project) if self.registry else None
        intern_type = _get_intern_type_scoped(intern_name, project=project)
        self.send({"type": "intern_online", "intern_name": intern_name,
                   "chat_id": chat_id, "intern_type": intern_type,
                   "project": project})

    def send_intern_offline(self, intern_name, project=None):
        """Notify relay that an intern went offline on this machine."""
        project = project or _get_intern_project_scoped(intern_name) or ""
        self.send({"type": "intern_offline", "intern_name": intern_name,
                   "project": project})

    def check_online(self, intern_name, timeout=5):
        """Ask relay server if intern is online. Uses HTTP API for reliability."""
        if not self._relay_http_base:
            return None
        try:
            import urllib.request
            from urllib.parse import quote
            project = _get_intern_project(intern_name) or ""
            url = (f"{self._relay_http_base}/api/intern/check_online"
                   f"?intern_name={quote(intern_name)}&project={quote(project)}")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning(f"[RELAY_CLIENT] check_online HTTP failed: {e}")
            return None

    def resolve_peer_target(self, to_intern_name, timeout=5):
        """task213: ask relay for all (project, name) candidates. Returns list or None on timeout/disconnect."""
        if not self._connected:
            return None
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {}
        with self._peer_pending_lock:
            self._peer_pending[request_id] = {"event": event, "result": holder}
        try:
            self.send({
                "type": "peer_resolve_target",
                "request_id": request_id,
                "to_intern_name": to_intern_name,
            })
            if not event.wait(timeout=timeout):
                return None
            return holder.get("candidates", [])
        finally:
            with self._peer_pending_lock:
                self._peer_pending.pop(request_id, None)

    def forward_peer_message(self, payload, timeout=10):
        """task213: send peer message via relay to target machine. Returns {status, reason?, ...}."""
        if not self._connected:
            return {"status": "undeliverable", "reason": "relay_unreachable"}
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {}
        with self._peer_pending_lock:
            self._peer_pending[request_id] = {"event": event, "result": holder}
        try:
            msg = dict(payload)
            msg["type"] = "intern_peer_message"
            msg["request_id"] = request_id
            self.send(msg)
            if not event.wait(timeout=timeout):
                return {"status": "undeliverable", "reason": "relay_unreachable"}
            return {k: v for k, v in holder.items() if k not in ("type", "request_id")}
        finally:
            with self._peer_pending_lock:
                self._peer_pending.pop(request_id, None)

    def forward_goal_command(self, payload, timeout=10):
        """task320: send goal command via relay to target machine. Returns {status, reason?, ...}."""
        if not self._connected:
            return {"status": "undeliverable", "reason": "relay_unreachable"}
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {}
        with self._peer_pending_lock:
            self._peer_pending[request_id] = {"event": event, "result": holder}
        try:
            msg = dict(payload)
            msg["type"] = "intern_goal_command"
            msg["request_id"] = request_id
            self.send(msg)
            if not event.wait(timeout=timeout):
                return {"status": "undeliverable", "reason": "relay_unreachable"}
            return {k: v for k, v in holder.items() if k not in ("type", "request_id")}
        finally:
            with self._peer_pending_lock:
                self._peer_pending.pop(request_id, None)

    def forward_mail_message(self, payload, timeout=10):
        """task309: send mail-to message via relay to target daemon. Returns {status, reason?, ...}."""
        if not self._connected:
            return {"status": "undeliverable", "reason": "relay_unreachable"}
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {}
        with self._peer_pending_lock:
            self._peer_pending[request_id] = {"event": event, "result": holder}
        try:
            msg = dict(payload)
            msg["type"] = "intern_mail_message"
            msg["request_id"] = request_id
            self.send(msg)
            if not event.wait(timeout=timeout):
                return {"status": "undeliverable", "reason": "relay_unreachable"}
            return {k: v for k, v in holder.items() if k not in ("type", "request_id")}
        finally:
            with self._peer_pending_lock:
                self._peer_pending.pop(request_id, None)

    def _heartbeat_loop(self, ws, stop_event):
        while not stop_event.is_set():
            stop_event.wait(timeout=30)
            if stop_event.is_set():
                break
            try:
                _sync_warnings_if_changed(_check_multi_daemon_warning())
                ws.send(json.dumps({"type": "heartbeat"}))
            except Exception as e:
                self._mark_connection_broken(ws, f"heartbeat failed: {e}")
                break

    def _handle_relay_message(self, msg):
        """Handle a message received from relay server."""
        msg_type = msg.get("type")

        if msg_type == "feishu_message":
            # Route to local intern (same logic as direct feishu message handler)
            intern_name = msg.get("intern_name", "")
            text = msg.get("text", "")
            message_id = msg.get("message_id", "")
            chat_id = msg.get("chat_id", "")
            project = msg.get("project", "")
            attachments = msg.get("attachments") or []

            if not intern_name or (not text and not attachments):
                return

            log.info(f"[RELAY_CLIENT] Feishu msg for '{intern_name}': "
                     f"text={text[:80]!r} atts={len(attachments)}")

            # task228: 附件 → 落盘 + 写 intern state.pending_attachments。
            # 失败（解码/写盘/字段缺失）→ reply_message 明确告诉主管，不再走 text
            # 路径（避免"AI 看到图"的假象，项目规则 6）。
            if attachments:
                try:
                    _persist_inbound_attachments(intern_name, message_id, attachments, project=project)
                except Exception as e:
                    log.error(f"[RELAY_CLIENT] persist attachments failed for {intern_name}: {e}",
                              exc_info=True)
                    if _api:
                        _api.reply_message(message_id, f"⚠️ 附件落盘失败：{e}")
                    return

            # 纯附件无 text：pending_attachments 已累积，等下一条 text 的 UPS hook 消费。
            # 不把空 text 发到 tmux —— 不唤醒 AI，此条消息不产生 AI 轮次。
            # relay 侧已经 reply_message 提示主管"请再发 text 触发 intern 查看"。
            if not text:
                return

            # ── 检查是否有 pending question 等待回答 ──
            if _try_answer_pending_question(intern_name, text):
                if _api:
                    _api.reply_message(message_id, f"✅ 已收到回复")
                return

            # Check if this is a command (starts with /)
            if text.strip().startswith("/"):
                _handle_feishu_command(intern_name, text.strip(), message_id, project=project)
                return

            # ── Route to intern (Claude/Codex via tmux, Copilot via WS) ──
            intern_type = _get_intern_type_scoped(intern_name, project=project)

            if _is_tmux_intern_type(intern_type):
                if intern_type == "codex":
                    success, err = _send_to_codex_tmux(intern_name, text, delivery_id=message_id)
                else:
                    success, err = _send_to_claude_tmux(intern_name, text, delivery_id=message_id)
                if success:
                    log.info(f"[RELAY_CLIENT] Sent to {intern_type.capitalize()} '{intern_name}' via tmux")
                else:
                    if err in _TMUX_SUBMIT_UNCONFIRMED_ERRORS:
                        if _api and _should_reply_tmux_unconfirmed(err):
                            _api.reply_message(message_id, _format_tmux_unconfirmed_message(intern_name, err))
                        log.warning(f"[RELAY_CLIENT] Codex submit unconfirmed for '{intern_name}': {err}")
                        return
                    if _api:
                        _api.reply_message(message_id, f"⚠️ {intern_name} 当前离线")
                    self.send_intern_offline(intern_name, project=project)
                    _notify_intern_status_changed(intern_name)
            else:
                payload = {
                    "type": "feishu_message",
                    "intern_name": intern_name,
                    "text": text,
                    "message_id": message_id,
                    "chat_id": chat_id,
                }
                delivered = self.ws_server.route_to_active(intern_name, payload)
                if not delivered:
                    if _api:
                        _api.reply_message(message_id, f"⚠️ {intern_name} 当前不在线")
                    self.send_intern_offline(intern_name, project=project)
                    _notify_intern_status_changed(intern_name)
                    log.info(f"[RELAY_CLIENT] '{intern_name}' not active in any window, sent offline")

        elif msg_type == "helper_action":
            request_id = msg.get("request_id", "")
            try:
                result = handle_machine_helper_action(msg)
                result.update({"type": "helper_action_result", "ok": True})
            except Exception as e:
                log.error(f"[HELPER] action failed: {e}", exc_info=True)
                result = {
                    "type": "helper_action_result",
                    "ok": False,
                    "request_id": request_id,
                    "machine_id": msg.get("machine_id", ""),
                    "helper_action": msg.get("helper_action", ""),
                    "error": str(e),
                }
            self.send(result)

        elif msg_type == "heartbeat_ack":
            machine_known = msg.get("machine_known", True)
            if machine_known is False:
                changed = _set_daemon_warning(
                    "machine_unknown_on_relay",
                    f"relay 未识别当前 machine_id={self.machine_id} 的 ws 连接",
                )
                _sync_warnings_if_changed(changed)
                log.warning("[WARN] machine_unknown_on_relay: relay 未识别当前 ws，关闭连接触发重连")
                self._mark_connection_broken(self._current_connection(), "relay no longer knows this machine")
            else:
                _sync_warnings_if_changed(_clear_daemon_warning("machine_unknown_on_relay"))

        elif msg_type == "card_callback":
            # Card interaction from relay → answer pending question
            intern_name = msg.get("intern_name", "")
            if not intern_name:
                return

            with _pq_lock:
                entry = _pending_questions.get(intern_name)
            if not entry or entry["answer"] is not None:
                log.warning(f"[RELAY_CLIENT] No pending question for '{intern_name}' (card callback)")
                return

            questions = entry["questions"]

            if msg.get("is_form"):
                # Form submission (free text or multi-question form)
                form_value = msg.get("form_value", {})
                question_keys = msg.get("question_keys", [])
                answers = {}
                for i, qk in enumerate(question_keys):
                    # multiSelect 优先：有勾选则直接用 list（保留 list 类型给 Claude）
                    multi = form_value.get(f"q_{i}_multiselect")
                    if isinstance(multi, list) and multi:
                        answers[qk] = multi
                        continue
                    # 自由文本 > 单选下拉
                    custom = form_value.get(f"q_{i}_input", "")
                    selected = form_value.get(f"q_{i}_select", "")
                    val = (custom.strip() if custom else "") or selected or ""
                    if val:
                        answers[qk] = val

                if not answers:
                    log.warning(f"[RELAY_CLIENT] Empty form answers for '{intern_name}'")
                    return

                log.info(f"[RELAY_CLIENT] Card form answers for '{intern_name}': {answers}")
            else:
                # Button click (single answer)
                answer = msg.get("answer", "")
                if not answer:
                    return

                log.info(f"[RELAY_CLIENT] Card callback for '{intern_name}': {answer[:80]}")

                if len(questions) == 1:
                    question_key = questions[0].get("question", questions[0].get("header", "Q1"))
                    answers = {question_key: answer}
                else:
                    # Shouldn't happen for multi-q (uses form), but handle gracefully
                    answers = {}
                    for q in questions:
                        qk = q.get("question", q.get("header", "Q"))
                        answers[qk] = answer

            with _pq_lock:
                entry = _pending_questions.get(intern_name)
                if entry:
                    entry["answer"] = answers
            log.info(f"[RELAY_CLIENT] Card answer set for '{intern_name}': {answers}")
            _update_question_card(intern_name, answers, "飞书卡片")
            with _pq_lock:
                entry = _pending_questions.get(intern_name)
                if entry:
                    entry["event"].set()
            return

        elif msg_type == "heartbeat_ack":
            pass  # Expected response to heartbeat

        elif msg_type == "check_online_result":
            if self._check_online_handler:
                self._check_online_handler(msg)

        elif msg_type == "intern_online_rejected":
            intern_name = msg.get("intern_name", "")
            existing_machine = msg.get("machine_id", "")
            log.warning(f"[RELAY_CLIENT] Online rejected for '{intern_name}': already on '{existing_machine}'")

        elif msg_type == "peer_resolve_target_result":
            # task213: relay replied with candidates for to_intern_name
            request_id = msg.get("request_id", "")
            with self._peer_pending_lock:
                entry = self._peer_pending.get(request_id)
            if entry:
                entry["result"]["candidates"] = msg.get("candidates", [])
                entry["event"].set()

        elif msg_type == "intern_peer_message":
            # task213: relay forwarded a peer message destined for one of our local interns.
            request_id = msg.get("request_id", "")
            sender_mid = msg.get("sender_machine_id", "")
            if msg.get("mode") == "goal":
                result = {"status": "undeliverable", "reason": "goal_same_daemon_project_required"}
            else:
                result = _deliver_peer_locally(msg)
            reply = {
                "type": "intern_peer_message_result",
                "request_id": request_id,
                "sender_machine_id": sender_mid,
            }
            reply.update(result)
            self.send(reply)

        elif msg_type == "intern_goal_command":
            # task320: relay forwarded a goal set/cancel command to one local tmux intern.
            request_id = msg.get("request_id", "")
            sender_mid = msg.get("sender_machine_id", "")
            msg["via_relay"] = True
            result = _deliver_goal_locally(msg)
            reply = {
                "type": "intern_goal_command_result",
                "request_id": request_id,
                "sender_machine_id": sender_mid,
            }
            reply.update(result)
            self.send(reply)

        elif msg_type == "intern_peer_message_result":
            # task213: B daemon's reply came back via relay
            request_id = msg.get("request_id", "")
            with self._peer_pending_lock:
                entry = self._peer_pending.get(request_id)
            if entry:
                entry["result"].update({k: v for k, v in msg.items() if k != "type"})
                entry["event"].set()

        elif msg_type == "intern_goal_command_result":
            # task320: B daemon's goal delivery receipt came back via relay.
            request_id = msg.get("request_id", "")
            with self._peer_pending_lock:
                entry = self._peer_pending.get(request_id)
            if entry:
                entry["result"].update({k: v for k, v in msg.items() if k != "type"})
                entry["event"].set()

        elif msg_type == "intern_mail_message":
            # task309: relay forwarded a mail-to message for one local intern mailbox.
            request_id = msg.get("request_id", "")
            sender_mid = msg.get("sender_machine_id", "")
            result = _deliver_mail_locally(msg)
            reply = {
                "type": "intern_mail_message_result",
                "request_id": request_id,
                "sender_machine_id": sender_mid,
            }
            reply.update(result)
            self.send(reply)

        elif msg_type == "intern_mail_message_result":
            # task309: target daemon's mailbox write receipt came back via relay.
            request_id = msg.get("request_id", "")
            with self._peer_pending_lock:
                entry = self._peer_pending.get(request_id)
            if entry:
                entry["result"].update({k: v for k, v in msg.items() if k != "type"})
                entry["event"].set()

        elif msg_type == "detail_mode_get":
            # task283: relay asks this daemon for the current per-chat
            # detail_mode value. Truth source is daemon-local since the hook
            # filtering also runs on this machine — see daemon_chat_config.
            request_id = msg.get("request_id", "")
            chat_id = msg.get("chat_id", "")
            reply = {"type": "detail_mode_get_result", "request_id": request_id,
                     "chat_id": chat_id}
            try:
                reply["mode"] = daemon_chat_config.get_detail_mode(chat_id)
            except Exception as e:
                log.error(f"[DETAIL] detail_mode_get for chat={chat_id} failed: {e}", exc_info=True)
                reply["error"] = f"daemon_local_read_failed: {e}"
            self.send(reply)

        elif msg_type == "detail_mode_set":
            # task283: relay asks this daemon to write the per-chat detail_mode
            # value. ValueError (bad mode / empty chat_id) is reported as a
            # structured error string — relay surfaces it to the supervisor.
            request_id = msg.get("request_id", "")
            chat_id = msg.get("chat_id", "")
            mode = msg.get("mode", "")
            reply = {"type": "detail_mode_set_result", "request_id": request_id,
                     "chat_id": chat_id, "mode": mode}
            try:
                reply["changed"] = daemon_chat_config.set_detail_mode(chat_id, mode)
            except ValueError as e:
                # Bad input — caller-side error, log at INFO not ERROR.
                log.info(f"[DETAIL] detail_mode_set rejected: {e}")
                reply["error"] = f"invalid_argument: {e}"
            except Exception as e:
                log.error(f"[DETAIL] detail_mode_set for chat={chat_id} failed: {e}", exc_info=True)
                reply["error"] = f"daemon_local_write_failed: {e}"
            self.send(reply)

        elif msg_type == "request_logs":
            request_id = msg.get("request_id", "")
            intern_name = msg.get("intern_name")
            relay_upload_url = msg.get("relay_upload_url", "")
            if request_id and relay_upload_url:
                threading.Thread(
                    target=self._upload_logs,
                    args=(request_id, relay_upload_url, intern_name),
                    daemon=True,
                ).start()

    def _upload_logs(self, request_id, relay_upload_url, intern_name=None):
        """Tar logs and upload to relay via HTTP POST."""
        import tarfile
        import tempfile

        log_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs")
        if not os.path.isdir(log_dir):
            log.warning(f"[LOG_UPLOAD] Log directory not found: {log_dir}")
            return

        # If intern_name specified, only tar that intern's subdirectory
        if intern_name:
            target = os.path.join(log_dir, intern_name)
            if not os.path.isdir(target):
                log.warning(f"[LOG_UPLOAD] Intern log dir not found: {target}")
                return
            arcname_base = intern_name
        else:
            target = log_dir
            arcname_base = "llm_intern_logs"

        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(target, arcname=arcname_base)

            # Upload via HTTP POST
            file_size = os.path.getsize(tmp_path)
            log.info(f"[LOG_UPLOAD] Uploading {file_size} bytes to relay (request_id={request_id})")
            with open(tmp_path, "rb") as f:
                req = urllib.request.Request(
                    relay_upload_url,
                    data=f.read(),
                    method="POST",
                    headers={"Content-Type": "application/gzip", "Content-Length": str(file_size)},
                )
                resp = urllib.request.urlopen(req, timeout=120)
                result = json.loads(resp.read())
                log.info(f"[LOG_UPLOAD] Upload complete: {result}")
        except Exception as e:
            log.error(f"[LOG_UPLOAD] Failed (request_id={request_id}): {e}")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def stop(self):
        self._stop = True


# Global relay client reference (set in main if relay mode)
_relay_client = None


# ══════════════════════════════════════════
# Feishu Command Handling
# ══════════════════════════════════════════

# Commands supported per intern type
# Codex 支持 /clear、/compact、/stop（PreCompact hook 不触发，但 /compact slash command 本身可用，
# 见 https://developers.openai.com/codex/cli/slash-commands）；/btw 是 Claude 专属
_COMMANDS = {
    "claude": {
        "/clear": "清空对话历史",
        "/stop": "停止当前执行",
        "/compact": "压缩上下文",
        "/btw": "旁路提问（Claude Code 原生，答案回传飞书；用法：/btw <问题>）",
    },
    "codex": {
        "/clear": "清空对话历史",
        "/compact": "压缩上下文（Codex 原生 slash command；不触发 PreCompact hook）",
        "/goal": "设置/管理 Codex 原生 goal（用法：/goal follow the instructions in <file> 或 /goal clear）",
        "/stop": "停止当前执行（发送 Escape 键）",
    },
}


def _handle_feishu_command(intern_name, command, message_id, project=None):
    """Handle a /command from Feishu. Returns True if handled."""
    intern_type = _get_intern_type_scoped(intern_name, project=project)
    # Extract first word as command (e.g. "/clear foo" → "/clear")
    cmd = command.strip().split()[0].lower()

    supported = _COMMANDS.get(intern_type, {})
    if cmd not in supported:
        if supported:
            cmd_list = "\n".join(f"  {c} — {d}" for c, d in supported.items())
            reply = f"❌ 不支持的指令: {cmd}\n\n可用指令:\n{cmd_list}"
        else:
            reply = f"❌ {intern_name} ({intern_type}) 暂不支持任何指令"
        if _api:
            _api.reply_message(message_id, reply)
        return True

    if intern_type == "claude":
        if project:
            return _exec_claude_command(intern_name, cmd, message_id, command, project=project)
        return _exec_claude_command(intern_name, cmd, message_id, command)
    if intern_type == "codex":
        if project:
            return _exec_codex_command(intern_name, cmd, message_id, command, project=project)
        return _exec_codex_command(intern_name, cmd, message_id, command)
    return True


def _exec_codex_command(intern_name, cmd, message_id, raw_command=None, project=None):
    """Execute a command for Codex intern via tmux."""
    if not _check_tmux_session(intern_name):
        if _api:
            _api.reply_message(message_id, f"⚠️ {intern_name} tmux 会话不存在")
        return True
    if not _is_codex_process_running(intern_name):
        if _api:
            _api.reply_message(message_id, f"⚠️ {intern_name} Codex 进程未运行")
        return True

    try:
        if cmd in ("/clear", "/compact"):
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "-l", "--", cmd],
                check=True, capture_output=True
            )
            time.sleep(0.5)
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "Enter"],
                check=True, capture_output=True
            )
            log.info(f"[CMD] Sent {cmd} to Codex '{intern_name}'")
            if _api:
                _api.reply_message(message_id, f"✅ 已向 {intern_name} 发送 {cmd}")

        elif cmd == "/goal":
            command_text = (raw_command or cmd).strip()
            content = command_text[len("/goal"):].strip() if command_text.lower().startswith("/goal") else ""
            if not content:
                if _api:
                    _api.reply_message(
                        message_id,
                        "❌ /goal 需要目标内容，或使用 `/goal clear` 清除当前 goal",
                    )
                return True

            if content.lower() == "clear":
                success, err = _send_goal_cancel_to_codex_tmux(intern_name, message_id)
            else:
                success, err = _send_peer_goal_to_codex_tmux(intern_name, content, message_id)

            if success:
                log.info(f"[CMD] Sent {cmd} to Codex '{intern_name}'")
                if _api:
                    _api.reply_message(message_id, f"✅ 已向 {intern_name} 发送 /goal")
            else:
                log.warning(f"[CMD] Failed to send {cmd} to '{intern_name}': {err}")
                if _api:
                    _api.reply_message(message_id, f"❌ 发送 /goal 失败: {err}")

        elif cmd == "/stop":
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "Escape"],
                check=True, capture_output=True
            )
            log.info(f"[CMD] Sent Escape (stop) to Codex '{intern_name}'")
            ok, reason = _finalize_active_feishu_message_for_stop(intern_name, project=project)
            if ok:
                log.info(f"[CMD] Finalized active Feishu turn for Codex '{intern_name}' after stop ({reason})")
                _notify_intern_status_changed(intern_name)
                _push_interns_state_once()
            else:
                log.info(f"[CMD] No Codex Feishu turn finalized for '{intern_name}' after stop: {reason}")
            if _api:
                _api.reply_message(message_id, f"✅ 已向 {intern_name} 发送停止信号")

    except subprocess.CalledProcessError as e:
        log.error(f"[CMD] Failed to send {cmd} to '{intern_name}': {e}")
        if _api:
            _api.reply_message(message_id, f"❌ 发送 {cmd} 失败: {e}")
    return True


def _exec_claude_command(intern_name, cmd, message_id, raw_command=None, project=None):
    """Execute a command for Claude intern via tmux."""
    if not _check_tmux_session(intern_name):
        if _api:
            _api.reply_message(message_id, f"⚠️ {intern_name} tmux 会话不存在")
        return True
    if not _is_claude_process_running(intern_name):
        if _api:
            _api.reply_message(message_id, f"⚠️ {intern_name} Claude 进程未运行")
        return True

    try:
        if cmd in ("/clear", "/compact"):
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "-l", "--", cmd],
                check=True, capture_output=True
            )
            time.sleep(0.5)
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "Enter"],
                check=True, capture_output=True
            )
            log.info(f"[CMD] Sent {cmd} to Claude '{intern_name}'")
            if _api:
                _api.reply_message(message_id, f"✅ 已向 {intern_name} 发送 {cmd}")

        elif cmd == "/stop":
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "Escape"],
                check=True, capture_output=True
            )
            log.info(f"[CMD] Sent Escape (stop) to Claude '{intern_name}'")
            if _api:
                _api.reply_message(message_id, f"✅ 已向 {intern_name} 发送停止信号")

        elif cmd == "/btw":
            # 提取 /btw 之后的全文作为问题（保留中英文/换行/多空格）
            raw = (raw_command or "").strip()
            question = raw[len("/btw"):].strip() if raw.lower().startswith("/btw") else ""
            if not question:
                if _api:
                    _api.reply_message(message_id, "❌ /btw 需要问题文本，用法：/btw <问题>")
                return True
            line = f"/btw {question}"
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "-l", "--", line],
                check=True, capture_output=True
            )
            time.sleep(0.5)
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={intern_name}:", "Enter"],
                check=True, capture_output=True
            )
            log.info(f"[CMD] Sent /btw to Claude '{intern_name}': {question[:80]}")
            if _api:
                _api.reply_message(message_id, f"✅ 已发送 /btw 到 {intern_name}，答案稍后回传")

    except subprocess.CalledProcessError as e:
        log.error(f"[CMD] Failed to send {cmd} to '{intern_name}': {e}")
        if _api:
            _api.reply_message(message_id, f"❌ 发送 {cmd} 失败: {e}")
    return True


def _get_intern_session_entry(intern_name, project=None):
    """Return a .intern_sessions.json entry for intern/project.

    Plain-name fallback is allowed only when it resolves uniquely.
    """
    sessions_file = os.path.join(WORK_AGENTS_ROOT, ".intern_sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    entry = data.get(f"{project}:{intern_name}") if project else data.get(intern_name)
    if isinstance(entry, dict) and entry:
        return entry
    matches = []
    for key, candidate in data.items():
        if not isinstance(candidate, dict):
            continue
        if candidate.get("intern_name") == intern_name or key == intern_name or key.endswith(f":{intern_name}"):
            if project and candidate.get("project") != project and not key.startswith(f"{project}:"):
                continue
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.warning(f"[SESSION] ambiguous session lookup for intern={intern_name}; project required")
        return {"_ambiguous": True}
    return {}


def _get_intern_dir(intern_name, project=None):
    """Resolve legacy and enterprise intern directory layouts."""
    entry = _get_intern_session_entry(intern_name, project=project)
    if isinstance(entry, dict) and entry.get("_ambiguous"):
        return ""
    intern_dir = entry.get("intern_dir") if isinstance(entry, dict) else ""
    if intern_dir:
        return intern_dir
    return os.path.join(WORK_AGENTS_ROOT, intern_name)


def _get_status_md_path(intern_name, project=None):
    project = project or _get_intern_project(intern_name) or ""
    try:
        status_md = os.path.join(team_mailbox.team_registry.interns_dir(project), intern_name, "status.md")
        if os.path.isfile(status_md):
            return status_md
    except Exception:
        pass
    return os.path.join(
        _get_intern_dir(intern_name, project=project), project, "workspace",
        "interns", intern_name, "status.md")


def _get_intern_type(intern_name, project=None):
    """Read intern type from .intern_sessions.json. Returns 'copilot' / 'claude' / 'codex'."""
    entry = _get_intern_session_entry(intern_name, project=project)
    intern_type = entry.get("type") if isinstance(entry, dict) else ""
    if intern_type in ("copilot", "claude", "codex"):
        return intern_type
    inferred_type = _infer_local_tmux_intern_type(intern_name)
    if inferred_type:
        return inferred_type
    return "copilot"


def _get_intern_type_scoped(intern_name, project=None):
    try:
        return _get_intern_type(intern_name, project=project)
    except TypeError:
        return _get_intern_type(intern_name)


def _check_tmux_session(intern_name):
    """Check if a tmux session exists for the given intern. Returns True/False."""
    try:
        subprocess.run(
            ["tmux", "has-session", "-t", f"={intern_name}"],
            check=True, capture_output=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _is_claude_process_running(intern_name):
    """Check if Claude CLI is actually running in the tmux pane (not just bash).

    Returns True if the current command in the pane is 'claude', False otherwise.
    A tmux session can exist but Claude may have /exit'd, leaving only bash.
    """
    return _is_tmux_cli_process_running(intern_name, "claude")


def _is_codex_process_running(intern_name):
    """Check if Codex CLI is actually running in the tmux pane.

    Older Codex CLI builds reported `node` in tmux's pane_current_command and
    needed a child-process cmdline scan. New native builds report names such as
    `codex-aarch64-a`, so first trust the pane command itself.
    """
    if not _check_tmux_session(intern_name):
        return False
    try:
        pane_cmd = subprocess.run(
            ["tmux", "list-panes", "-t", f"={intern_name}", "-F", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        cmd = pane_cmd.stdout.strip().splitlines()[0] if pane_cmd.stdout.strip() else ""
        if _is_codex_command_name(cmd):
            return True

        result = subprocess.run(
            ["tmux", "list-panes", "-t", f"={intern_name}", "-F", "#{pane_pid}"],
            capture_output=True, text=True
        )
        pane_pid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not pane_pid:
            return False
        return _child_cmdline_contains(pane_pid, "codex")
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return False


def _is_codex_command_name(cmd):
    base = os.path.basename((cmd or "").strip()).lower()
    return bool(base and "codex" in base)


def _child_cmdline_contains(parent_pid, needle):
    needle = (needle or "").lower()
    if not parent_pid or not needle:
        return False
    commands = [
        ["ps", "--ppid", str(parent_pid), "-o", "args="],
        ["pgrep", "-P", str(parent_pid), "-fl", "."],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if result.returncode in (0, 1) and needle in (result.stdout or "").lower():
            return True
    return False


def _infer_local_tmux_intern_type(intern_name):
    """Infer live local tmux CLI type when session registry is stale/missing.

    This is intentionally narrow: only a local intern dir plus a live CLI process
    can recover type. It prevents a stale .intern_sessions.json from making an
    active local Codex target look like an unreachable remote relay target.
    """
    if not intern_name:
        return None
    intern_dir = os.path.join(WORK_AGENTS_ROOT, intern_name)
    if not os.path.isdir(intern_dir):
        return None
    if _is_codex_process_running(intern_name):
        return "codex"
    if _is_claude_process_running(intern_name):
        return "claude"
    return None


def _is_tmux_cli_process_running(intern_name, expected_cmd):
    """Generic helper: check if tmux pane is running the expected CLI command."""
    if not _check_tmux_session(intern_name):
        return False
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", f"={intern_name}", "-F", "#{pane_current_command}"],
            capture_output=True, text=True
        )
        cmd = result.stdout.strip().splitlines()[0].lower() if result.stdout.strip() else ""
        return cmd == expected_cmd
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return False


def _is_tmux_intern_type(intern_type):
    """tmux-based intern types (claude, codex) — opposed to Copilot which runs in VS Code Chat."""
    return intern_type in ("claude", "codex")


def _iter_registry_entries(registry):
    """Return normalized registry entries with bare intern names.

    Project-scoped registry entries are internally keyed as ``project:intern``;
    online detection and tmux lookup must use the bare ``intern_name``.
    """
    if not registry:
        return []
    if hasattr(registry, "get_all_entries"):
        entries = []
        for entry in registry.get_all_entries():
            if not isinstance(entry, dict):
                continue
            name = entry.get("intern_name") or entry.get("name")
            chat_id = entry.get("chat_id") or entry.get("chatId")
            if name and chat_id:
                entries.append({
                    "name": name,
                    "chat_id": chat_id,
                    "project": entry.get("project") or "",
                })
        return entries
    return [
        {"name": name, "chat_id": chat_id, "project": ""}
        for name, chat_id in registry.get_all().items()
    ]


def _owns_local_peer_target(intern_name, project):
    """True only when this daemon really owns the peer target locally.

    ``RegistryManager`` is a Feishu chat mapping. It can contain stale or
    imported chat entries for interns owned by other machines, so peer routing
    must not treat registry membership alone as local ownership.
    """
    if not intern_name or not project:
        return False

    intern_type = _get_intern_type_scoped(intern_name, project=project)
    intern_dir = _get_intern_dir(intern_name, project=project)
    if _is_tmux_intern_type(intern_type):
        if not os.path.isdir(intern_dir):
            return False
    elif intern_type == "copilot":
        active_set = _ws_server.get_active_interns() if _ws_server else set()
        if intern_name not in active_set:
            return False
    else:
        return False

    return (_get_intern_project_scoped(intern_name, project=project) or "") == project


def _owns_local_mail_target(intern_name, project):
    if not intern_name or not project:
        return False
    intern_workspace = os.path.join(team_mailbox.team_registry.interns_dir(project), intern_name)
    return os.path.isdir(intern_workspace)


def _is_intern_online(intern_name, project=None):
    """Claude/Codex online depends on CLI process running in tmux; Copilot online depends on any active VS Code window."""
    intern_type = _get_intern_type_scoped(intern_name, project=project)
    if intern_type == "claude":
        return _is_claude_process_running(intern_name)
    if intern_type == "codex":
        return _is_codex_process_running(intern_name)
    active_set = _ws_server.get_active_interns() if _ws_server else set()
    return intern_name in active_set


def _get_intern_project(intern_name, project=None):
    if project:
        return project
    session_entry = _get_intern_session_entry(intern_name)
    if isinstance(session_entry, dict) and session_entry.get("_ambiguous"):
        return ""
    """Read intern project from .hook_state.json. Returns project name string.
    Falls back to auto-detection from directory structure if not set."""
    intern_dir = _get_intern_dir(intern_name, project=project)
    state_file = os.path.join(intern_dir, ".hook_state.json")
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
        project = state.get("project")
        if project:
            return project
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Auto-detect: find subdirectory that is a git repo (exclude standard dirs)
    _skip = {"debug", "outputs", "llm_intern_logs", ".claude"}
    try:
        for entry in os.listdir(intern_dir):
            if entry.startswith(".") or entry in _skip:
                continue
            candidate = os.path.join(intern_dir, entry)
            if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, ".git")):
                return entry
    except OSError:
        pass
    entry = session_entry if isinstance(session_entry, dict) else _get_intern_session_entry(intern_name)
    if isinstance(entry, dict) and entry.get("project"):
        return entry["project"]
    return "axis_intern_agents"


def _get_intern_project_scoped(intern_name, project=None):
    try:
        return _get_intern_project(intern_name, project=project)
    except TypeError:
        return project or _get_intern_project(intern_name)


# task228: 入站附件 inbox 目录名（repo 外，$WORK_AGENTS_ROOT/<intern>/.feishu_inbox/<mid>/）。
# 与 hook 侧 `common/utils.STATE_FILE`/`LOCK_FILE` 对齐复用同一把 fcntl 锁。
_FEISHU_INBOX_DIR = ".feishu_inbox"
_HOOK_STATE_FILE = ".hook_state.json"
_HOOK_STATE_LOCK = ".hook_state.lock"


def _persist_inbound_attachments(intern_name, message_id, attachments, project=None):
    """task228: 把 relay 下发的 base64 附件落盘并 append 到 intern state.pending_attachments。

    - 落盘目录：`$WORK_AGENTS_ROOT/<intern_name>/.feishu_inbox/<message_id>/<filename>`
    - 文件名 basename 二次保护（relay 已做过一次，双防穿越）。
    - state.pending_attachments: list of `{"kind":..., "path": abs_path, "filename":...}`
      原子追加（fcntl 互斥 `.hook_state.lock`，与 hook `state_lock` 共用）。

    失败（缺字段、base64 解码错、写盘错）统一 raise；caller 负责向主管 reply_message。
    不允许"写了一半又回退"——附件原子性一次 try；中途 IO 错 raise 时前面已落盘的文件
    不清理（下一次看到仍可处理 / 后续清理任务负责，项目规则 6 重点是不隐藏错误）。
    """
    if not intern_name or not message_id:
        raise ValueError(f"intern_name/message_id 必须非空: {intern_name!r} {message_id!r}")
    if not isinstance(attachments, list) or not attachments:
        raise ValueError("attachments 必须是非空 list")

    intern_dir = _get_intern_dir(intern_name, project=project)
    if not os.path.isdir(intern_dir):
        raise FileNotFoundError(f"intern_dir 不存在: {intern_dir}")

    # 每条消息一个子目录；os.makedirs 幂等。basename 防 message_id 被构造成 '..'.
    safe_mid = os.path.basename(str(message_id)) or "_unknown"
    inbox_dir = os.path.join(intern_dir, _FEISHU_INBOX_DIR, safe_mid)
    os.makedirs(inbox_dir, exist_ok=True)

    new_items = []
    for idx, a in enumerate(attachments):
        if not isinstance(a, dict):
            raise ValueError(f"attachments[{idx}] 不是 dict: {type(a)}")
        kind = a.get("kind")
        filename = os.path.basename(str(a.get("filename") or "")) or f"att_{idx}.bin"
        b64 = a.get("bytes_b64") or ""
        if kind not in ("image", "file") or not b64:
            raise ValueError(f"attachments[{idx}] 字段非法: kind={kind!r} bytes_b64_len={len(b64)}")
        data = base64.b64decode(b64, validate=True)
        dest = os.path.join(inbox_dir, filename)
        with open(dest, "wb") as f:
            f.write(data)
        new_items.append({"kind": kind, "path": dest, "filename": filename})
        log.info(f"[INBOX] {intern_name} {safe_mid} {kind} → {dest} ({len(data)} bytes)")

    # append pending_attachments 到 intern state（与 hook 共享 fcntl LOCK_EX）。
    lock_path = os.path.join(intern_dir, _HOOK_STATE_LOCK)
    state_path = os.path.join(intern_dir, _HOOK_STATE_FILE)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                with open(state_path, "r") as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            pending = state.get("pending_attachments") or []
            if not isinstance(pending, list):
                pending = []
            pending.extend(new_items)
            state["pending_attachments"] = pending
            tmp_path = state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.rename(tmp_path, state_path)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _build_group_name(intern_name, is_online=None, project=None):
    """Name format: `🟢 🤖 rule_bob/axis_intern_agents`.

    task204: 类型由群名 emoji 表达（claude=🤖 / codex=🚀 / copilot=空），
    相关头像 API 保留但当前不主动设置。
    """
    project = _get_intern_project_scoped(intern_name, project=project)
    online = _is_intern_online(intern_name, project=project) if is_online is None else is_online
    prefix = "🟢" if online else "🔴"
    stripped = intern_name[len("intern_"):] if intern_name.startswith("intern_") else intern_name
    intern_type = _get_intern_type_scoped(intern_name, project=project) or "copilot"
    badge = {"claude": "🤖 ", "codex": "🚀 ", "copilot": ""}.get(intern_type, "")
    return f"{prefix} {badge}{stripped}/{project}"


def _is_transient_feishu_error(err):
    detail = str(err or "").lower()
    return any(token in detail for token in (
        "http 500",
        "internal error",
        "1663",
        "name or service not known",
        "temporary failure in name resolution",
        "could not resolve",
        "timed out",
        "connection reset",
        "connection refused",
        "network is unreachable",
    ))


def _mobile_to_open_id_with_retry(owner_mobile, *, attempts=3, delay=1.0):
    last_err = None
    for attempt in range(attempts):
        owner_open_id, err = _api.mobile_to_open_id(owner_mobile)
        if owner_open_id and not err:
            return owner_open_id, None
        last_err = err
        if not _is_transient_feishu_error(err) or attempt == attempts - 1:
            break
        time.sleep(delay)
    return None, last_err


def _add_chat_managers_with_retry(chat_id, open_ids, *, attempts=3, delay=1.0):
    last_err = None
    for attempt in range(attempts):
        err = _api.add_chat_managers(chat_id, open_ids)
        if not err:
            return None
        last_err = err
        if not _is_transient_feishu_error(err) or attempt == attempts - 1:
            break
        time.sleep(delay)
    return last_err


def _ensure_group_creator_manager(chat_id, owner_mobile, intern_name, owner_open_id=""):
    owner_open_id = str(owner_open_id or "").strip()
    if not owner_open_id:
        if not owner_mobile:
            return "owner_mobile or owner_open_id required"
        owner_open_id, err = _mobile_to_open_id_with_retry(owner_mobile)
    else:
        err = None
    if err or not owner_open_id:
        return f"mobile lookup failed: {err}"
    err = _add_chat_managers_with_retry(chat_id, [owner_open_id])
    if err:
        return err
    log.info(f"Ensured group creator is manager for {intern_name}: {chat_id}")
    return None


def _relay_lookup_chat(intern_name, project, timeout=5):
    if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
        return {}
    query = urllib.parse.urlencode({"intern": intern_name, "project": project or ""})
    url = f"{_relay_client._relay_http_base}/api/chat/lookup?{query}"
    resp = urllib.request.urlopen(url, timeout=timeout)
    return json.loads(resp.read())


def _normalize_group_lookup_name(name):
    clean = re.sub(r"^[🟢🔴⚪🤖🚀\s]+", "", name or "").strip()
    return re.sub(r"^\[(?:Claude🤖|Claude)\]\s*", "", clean).strip()


def _find_existing_chat_via_feishu(intern_name, project):
    stripped = intern_name[len("intern_"):] if intern_name.startswith("intern_") else intern_name
    targets = {f"{intern_name}/{project}", f"{stripped}/{project}"}
    try:
        for chat in _api.list_chats():
            chat_id = chat.get("chat_id", "")
            if chat_id and _normalize_group_lookup_name(chat.get("name", "")) in targets:
                log.info(f"Recovered existing Feishu group for {intern_name} via list_chats: {chat_id}")
                return {"intern_name": intern_name, "chat_id": chat_id}
    except Exception as exc:
        log.warning(f"Feishu list_chats recovery failed for {intern_name}: {exc}")
    return {}


def _finalize_group_create(intern_name, chat_id, owner_mobile, result, recovered=False, project="", owner_open_id=""):
    manager_err = _ensure_group_creator_manager(chat_id, owner_mobile, intern_name, owner_open_id=owner_open_id)
    if manager_err:
        return None, {"error": f"ensure_chat_manager failed: {manager_err}"}
    _registry.register(intern_name, chat_id, project=project)
    response = dict(result or {})
    response["chat_id"] = chat_id
    if project:
        response["project"] = project
    if recovered:
        response["existing"] = True
        response["recovered"] = True
    log.info(f"Registered group for {intern_name}: {chat_id} recovered={recovered}")
    threading.Thread(target=_refresh_lights, daemon=True).start()
    return response, None


def _recover_group_create_after_proxy_error(intern_name, project, owner_mobile, original_error, owner_open_id=""):
    log.warning(f"Recovering /api/group/create after proxy error for {intern_name}: {original_error}")
    last_error = None
    for attempt in range(3):
        try:
            result = _relay_lookup_chat(intern_name, project, timeout=5)
            chat_id = result.get("chat_id", "")
            if chat_id:
                return _finalize_group_create(
                    intern_name,
                    chat_id,
                    owner_mobile,
                    result,
                    recovered=True,
                    project=project,
                    owner_open_id=owner_open_id,
                )
        except Exception as exc:
            last_error = exc
            log.warning(f"Relay lookup recovery attempt {attempt + 1} failed for {intern_name}: {exc}")
        result = _find_existing_chat_via_feishu(intern_name, project)
        chat_id = result.get("chat_id", "")
        if chat_id:
            return _finalize_group_create(
                intern_name,
                chat_id,
                owner_mobile,
                result,
                recovered=True,
                project=project,
                owner_open_id=owner_open_id,
            )
        time.sleep(1)
    detail = f"{original_error}; recovery lookup failed"
    if last_error:
        detail += f": {last_error}"
    return None, {"error": f"relay proxy failed: {detail}"}


def _notify_intern_status_changed(intern_name):
    if _ws_server:
        _ws_server.push({"type": "intern_status_changed", "intern_name": intern_name})


def _compose_feishu_timeline(buffer_lines, spinner=True, footer=""):
    text = "\n".join(buffer_lines)
    if footer:
        text += "\n" + footer
    if spinner:
        text += "\n\n⏳ 处理中..."
    return text


def _write_hook_state_atomic(state_path, state):
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, state_path)


def _finalize_active_feishu_message_for_stop(intern_name, stop_note="\n⛔ 已停止", project=None):
    """Finalize Codex's active Feishu timeline when ESC does not fire Stop hook.

    Codex interrupt currently shows "Conversation interrupted" in the TUI but
    does not reliably emit our Stop hook. The hook state remains the relay
    dashboard's turn_active source, so daemon-side /stop must close the active
    Feishu message and flip feishu.finalized itself.
    """
    if not _api:
        return False, "no_api"

    intern_dir = _get_intern_dir(intern_name, project=project)
    state_path = os.path.join(intern_dir, _HOOK_STATE_FILE)
    lock_path = os.path.join(intern_dir, _HOOK_STATE_LOCK)

    try:
        if not os.path.isdir(intern_dir):
            return False, "no_intern_dir"
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    return False, "no_state"

                fs = state.get("feishu") or {}
                msg_id = fs.get("message_id")
                if not msg_id:
                    return False, "no_active_message"
                if fs.get("finalized"):
                    return True, "already_finalized"

                buffer_lines = fs.get("buffer_lines") or []
                if not isinstance(buffer_lines, list):
                    buffer_lines = []
                if stop_note and (not buffer_lines or buffer_lines[-1] != stop_note):
                    buffer_lines.append(stop_note)
                if not any(str(line).strip().startswith("✅") for line in buffer_lines[-2:]):
                    buffer_lines.append("\n✅ 完成")

                final_text = _compose_feishu_timeline(buffer_lines, spinner=False)
                err = _api.update_message(msg_id, final_text)
                if err:
                    return False, f"update_failed: {err}"

                fs["buffer_lines"] = buffer_lines
                fs["finalized"] = True
                fs["update_count"] = fs.get("update_count", 0) + 1

                transcript_path = state.get("log", {}).get("transcript_path", "")
                if transcript_path and os.path.exists(transcript_path):
                    try:
                        fs["transcript_offset"] = os.path.getsize(transcript_path)
                    except OSError:
                        pass

                state["feishu"] = fs
                _write_hook_state_atomic(state_path, state)
                return True, "finalized"
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as e:
        return False, f"state_io_error: {e}"


def _push_interns_state_once():
    if _registry and _relay_client and _relay_client.connected:
        try:
            _relay_client.send(_build_state_payload("interns_state"))
        except Exception as e:
            log.warning(f"[STATE] interns_state immediate push failed: {e}")


# Claude Code TUI 在 prompt 为空时将这些字符解释为快捷键（打开 help / bash 模式 / memorize 等），
# 字符不会进对话流。短消息或以这些字符开头的消息需要占位包装。'/' 不在此集合——
# daemon 已在 _handle_feishu_command 截走 slash command。
_CLAUDE_SHORTCUT_FIRST_CHARS = {'?', '!', '@', '&', '#'}
_TMUX_PASTE_MIN_CHARS = 512
_TMUX_ENTER_DELAY_SECONDS = 1.0
_TMUX_ACK_TIMEOUT_SECONDS = 5.0
_CODEX_GOAL_ACK_TIMEOUT_SECONDS = 3.0
_TMUX_ACK_POLL_SECONDS = 0.2
_TMUX_SUBMIT_UNCONFIRMED_ERROR = "prompt submit unconfirmed"
_TMUX_SUBMIT_RETRY_UNCONFIRMED_ERROR = "prompt submit unconfirmed after enter retry"
_TMUX_SUBMIT_UNCONFIRMED_ERRORS = {
    _TMUX_SUBMIT_UNCONFIRMED_ERROR,
    _TMUX_SUBMIT_RETRY_UNCONFIRMED_ERROR,
}


def _format_tmux_unconfirmed_message(intern_name, err):
    if err == _TMUX_SUBMIT_RETRY_UNCONFIRMED_ERROR:
        return (
            f"⚠️ 已写入 {intern_name} 的 tmux，并补发过一次 Enter，"
            "但仍未确认 Codex 提交。请查看 tmux。"
        )
    return (
        f"⚠️ 已写入 {intern_name} 的 tmux，但未确认 Codex 提交；"
        "未补发 Enter（未确认输入框可提交）。请查看 tmux。"
    )


def _should_reply_tmux_unconfirmed(err):
    return err == _TMUX_SUBMIT_RETRY_UNCONFIRMED_ERROR


def _send_to_claude_tmux(intern_name, text, delivery_id=""):
    """Send message to Claude CLI via tmux send-keys. Returns (success, error).

    Two-step send: literal text first, then Enter after a short delay.
    Uses -l (literal) flag to prevent escape sequence injection.
    """
    return _send_to_tmux_cli(intern_name, text, _is_claude_process_running, "Claude", delivery_id)


def _send_to_codex_tmux(intern_name, text, delivery_id="", require_ack=True):
    """Send message to Codex CLI via tmux send-keys. Returns (success, error)."""
    return _send_to_tmux_cli(
        intern_name, text, _is_codex_process_running, "Codex", delivery_id,
        require_codex_ack=require_ack)


def _tmux_send_enter(target):
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        check=True, capture_output=True
    )


def _tmux_send_literal(target, text):
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "-l", "--", text],
        check=True, capture_output=True
    )


def _tmux_clear_input_line(target):
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "C-u"],
        check=True, capture_output=True
    )


def _tmux_paste_text(target, text):
    buffer_name = f"feishu-prompt-{os.getpid()}-{int(time.time() * 1000)}"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(text)
            tmp_path = f.name
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, tmp_path],
            check=True, capture_output=True
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-d", "-p", "-b", buffer_name, "-t", target],
            check=True, capture_output=True
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _load_hook_state_for_intern(intern_name, project=None):
    state_path = os.path.join(_get_intern_dir(intern_name, project=project), ".hook_state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _codex_transcript_matches_intern(path, intern_name, project=None):
    intern_dir = os.path.abspath(_get_intern_dir(intern_name, project=project))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        obj = json.loads(first) if first else {}
    except (OSError, json.JSONDecodeError):
        return False
    if obj.get("type") != "session_meta":
        return False
    cwd = obj.get("payload", {}).get("cwd", "")
    if not cwd:
        return False
    cwd_abs = os.path.abspath(cwd)
    return cwd_abs == intern_dir or cwd_abs.startswith(intern_dir + os.sep)


def _discover_codex_transcript_path(intern_name, project=None):
    state = _load_hook_state_for_intern(intern_name, project=project)
    candidates = []
    state_path = state.get("log", {}).get("transcript_path", "")
    if state_path and os.path.exists(state_path):
        candidates.append(state_path)

    sessions_dir = Path(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))) / "sessions"
    if sessions_dir.exists():
        try:
            recent = sorted(
                sessions_dir.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:80]
        except OSError:
            recent = []
        candidates.extend(str(p) for p in recent)

    seen = set()
    matched = []
    for path in candidates:
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        if _codex_transcript_matches_intern(path, intern_name, project=project):
            try:
                matched.append((os.path.getmtime(path), path))
            except OSError:
                pass
    if not matched:
        return ""
    return max(matched)[1]


def _normalise_prompt_for_ack(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _codex_user_text_from_entry(obj):
    payload = obj.get("payload", {})
    if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
        return payload.get("message", "")
    if obj.get("type") == "response_item":
        if payload.get("type") == "message" and payload.get("role") == "user":
            parts = []
            for item in payload.get("content", []) or []:
                if isinstance(item, dict) and item.get("type") in ("input_text", "text"):
                    parts.append(item.get("text", ""))
            return "\n".join(parts)
    return ""


def _codex_goal_objective_from_entry(obj):
    payload = obj.get("payload", {})
    if obj.get("type") != "event_msg" or payload.get("type") != "thread_goal_updated":
        return ""
    goal = payload.get("goal") or {}
    return goal.get("objective", "")


def _codex_transcript_has_user_prompt(transcript_path, start_offset, text):
    expected = _normalise_prompt_for_ack(text)
    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return False
    offset = start_offset if 0 <= start_offset <= file_size else 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                actual = _normalise_prompt_for_ack(_codex_user_text_from_entry(obj))
                if actual == expected:
                    return True
    except OSError:
        return False
    return False


def _codex_transcript_has_goal_update(transcript_path, start_offset, objective):
    expected = _normalise_prompt_for_ack(objective)
    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return False
    offset = start_offset if 0 <= start_offset <= file_size else 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                actual = _normalise_prompt_for_ack(_codex_goal_objective_from_entry(obj))
                if actual == expected:
                    return True
    except OSError:
        return False
    return False


def _get_codex_ack_start(intern_name):
    transcript_path = _discover_codex_transcript_path(intern_name)
    if not transcript_path:
        return "", 0
    try:
        return transcript_path, os.path.getsize(transcript_path)
    except OSError:
        return "", 0


def _wait_for_codex_prompt_ack(transcript_path, start_offset, text, timeout=_TMUX_ACK_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _codex_transcript_has_user_prompt(transcript_path, start_offset, text):
            return True
        time.sleep(_TMUX_ACK_POLL_SECONDS)
    return _codex_transcript_has_user_prompt(transcript_path, start_offset, text)


def _wait_for_codex_goal_ack(transcript_path, start_offset, objective, timeout=_TMUX_ACK_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _codex_transcript_has_goal_update(transcript_path, start_offset, objective):
            return True
        time.sleep(_TMUX_ACK_POLL_SECONDS)
    return _codex_transcript_has_goal_update(transcript_path, start_offset, objective)


_CODEX_NON_IDLE_MARKERS = (
    "action required",
    "question ",
    "enter to confirm",
    "esc to cancel",
    "ctrl+c to cancel",
    "ctrl+c to interrupt",
    "esc to interrupt",
    "working (",
)


def _visible_prompt_fragment(text):
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 12:
        return ""
    return compact[-80:]


def _codex_prompt_pending_submit(target, text):
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-80"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"[TMUX_SEND] capture-pane failed for {target}: {e}")
        return False

    capture = result.stdout or ""
    lines = capture.splitlines()
    bottom = "\n".join(lines[-12:])
    marker_scope = "\n".join(lines[-20:]).lower()
    if any(marker in marker_scope for marker in _CODEX_NON_IDLE_MARKERS):
        return False
    fragment = _visible_prompt_fragment(text)
    if not fragment:
        return False
    return fragment in re.sub(r"\s+", "", bottom)


def _codex_prompt_visible_in_pane(target, text):
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-80"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"[TMUX_SEND] capture-pane failed for visible prompt check {target}: {e}")
        return False

    fragment = _visible_prompt_fragment(text)
    if not fragment:
        return False
    return fragment in re.sub(r"\s+", "", result.stdout or "")


def _codex_goal_confirmation_pending(target, objective):
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-80"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"[PEER] capture-pane failed for goal confirmation {target}: {e}")
        return False

    capture = result.stdout or ""
    compact = re.sub(r"\s+", "", capture)
    fragment = _visible_prompt_fragment(objective)
    has_goal_prompt = "Replacegoal?" in compact or "Setgoal?" in compact or "Newobjective:" in compact
    return has_goal_prompt and bool(fragment) and fragment in compact


def _codex_goal_visible_in_panel(target, objective):
    """Best-effort confirmation that Codex visibly accepted the goal.

    Transcript ``thread_goal_updated`` is the authoritative ack, but Codex can
    accept the slash command while the transcript watcher misses the event. The
    panel check is intentionally narrow: the objective fragment must be visible
    together with goal/objective UI wording, and the visible text must not simply
    be the still-pending ``/goal ...`` input line.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-120"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"[PEER] capture-pane failed for goal panel check {target}: {e}")
        return False

    capture = result.stdout or ""
    compact = re.sub(r"\s+", "", capture)
    lower_compact = compact.lower()
    fragment = _visible_prompt_fragment(objective)
    if not fragment or fragment.lower() not in lower_compact:
        return False
    if f"/goal{fragment.lower()}" in lower_compact:
        return False
    goal_markers = (
        "goal:",
        "currentgoal",
        "pressinggoal",
        "objective:",
        "newobjective:",
        "目标:",
        "当前目标",
    )
    return any(marker in lower_compact for marker in goal_markers)


def _delivery_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _send_to_tmux_cli(
    intern_name, text, process_check, label, delivery_id="", require_codex_ack=True
):
    """Generic tmux send-keys for CLI-type interns (claude/codex).

    Claude 分支：短消息（≤2 字符）或首字符在快捷键集合里的消息走占位包装路径，
    避免被 Claude TUI 当作快捷键吞掉。长消息与 Codex 走原路径。
    """
    if not _check_tmux_session(intern_name):
        return False, "tmux session not found"
    if not process_check(intern_name):
        return False, f"{label} has exited (tmux session exists but {label} is not running)"

    target = f"={intern_name}:"
    delivery_hash = _delivery_hash(text)
    ack_path, ack_offset = ("", 0)
    if label == "Codex" and require_codex_ack:
        ack_path, ack_offset = _get_codex_ack_start(intern_name)
    needs_wrap = (
        label == "Claude"
        and text
        and (len(text) <= 2 or text[0] in _CLAUDE_SHORTCUT_FIRST_CHARS)
    )
    use_paste_buffer = (
        label == "Codex"
        and bool(text)
        and not needs_wrap
        and ("\n" in text or len(text) >= _TMUX_PASTE_MIN_CHARS)
    )
    try:
        if needs_wrap:
            reason = "short_msg" if len(text) <= 2 else "shortcut_first_char"
            # 先发一个空格占住 prompt，避免首字符触发 TUI 快捷键
            _tmux_send_literal(target, " ")
            _tmux_send_literal(target, text)
            time.sleep(0.05)
            # 抹掉占位空格：Home 回行首 + Delete 删一字符
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Home"],
                check=True, capture_output=True
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Delete"],
                check=True, capture_output=True
            )
            time.sleep(_TMUX_ENTER_DELAY_SECONDS)
            _tmux_send_enter(target)
            method = "wrapped"
            log.info(f"[TMUX_SEND] wrapped (reason={reason}) intern={intern_name}, text={text[:30]!r}")
        else:
            if use_paste_buffer:
                _tmux_paste_text(target, text)
                method = "paste-buffer"
            else:
                _tmux_send_literal(target, text)
                method = "send-keys"
            time.sleep(_TMUX_ENTER_DELAY_SECONDS)
            _tmux_send_enter(target)

        if label == "Codex":
            log_ctx = f"intern={intern_name}, delivery={delivery_id or '-'}, hash={delivery_hash}"
            if not require_codex_ack:
                log.info(f"[TMUX_SEND] success {log_ctx}, len={len(text)}, method={method}, ack=skipped")
                return True, None
            if not ack_path:
                if _codex_prompt_visible_in_pane(target, text):
                    log.info(f"[TMUX_SEND] success {log_ctx}, len={len(text)}, method={method}, ack=pane")
                    return True, None
                log.warning(f"[TMUX_SEND] codex ack unavailable {log_ctx}, len={len(text)}, method={method}")
                return False, _TMUX_SUBMIT_UNCONFIRMED_ERROR
            if _wait_for_codex_prompt_ack(ack_path, ack_offset, text):
                log.info(f"[TMUX_SEND] success {log_ctx}, len={len(text)}, method={method}, ack=ok")
                return True, None
            if not _codex_prompt_pending_submit(target, text):
                log.warning(f"[TMUX_SEND] ack timeout without enter retry {log_ctx}, len={len(text)}, method={method}")
                return False, _TMUX_SUBMIT_UNCONFIRMED_ERROR
            log.warning(f"[TMUX_SEND] ack timeout {log_ctx}, len={len(text)}; retrying Enter once")
            _tmux_send_enter(target)
            if _wait_for_codex_prompt_ack(ack_path, ack_offset, text):
                log.info(f"[TMUX_SEND] success {log_ctx}, len={len(text)}, method={method}, ack=ok_after_retry")
                return True, None
            log.warning(f"[TMUX_SEND] ack failed after enter retry {log_ctx}, transcript={ack_path}")
            return False, _TMUX_SUBMIT_RETRY_UNCONFIRMED_ERROR

        log.info(f"[TMUX_SEND] success intern={intern_name}, len={len(text)}, method={method}")
        return True, None
    except subprocess.CalledProcessError as e:
        return False, str(e)


def _notify_peer_target_outdated(from_name, to_name, to_project):
    """task261: A 端拿到 reason=target_outdated 后给 A 飞书群发 systemMessage 提示
    主管升级 B 所在机器的插件。target_outdated 是版本兼容问题，LLM 自己看不懂也
    不会主动汇报，必须主动飞书可见；其他 undeliverable reason（busy/offline/
    unknown_target/400 类）由 LLM 自行处理，不发飞书避免噪音。

    任何失败仅 warn-only 日志 anchor [PEER_VISIBILITY]，不嵌套提示。
    """
    if _api is None:
        log.warning(f"[PEER_VISIBILITY] _api not initialized, skip target_outdated alert for {from_name}")
        return
    chat_id = _registry.find_chat_id(from_name)
    if not chat_id:
        log.warning(f"[PEER_VISIBILITY] no chat_id for {from_name}, skip target_outdated alert")
        return
    text = (
        f"⚠️ peer 投递失败：{to_project}/{to_name} 所在机器的插件版本太旧，"
        f"daemon 未声明本次投递需要的 peer capability。请升级该机器的 "
        f"intern-agent-helper 插件后重试。（reason=target_outdated）"
    )
    _, err = _api.send_message(chat_id, text)
    if err:
        log.warning(f"[PEER_VISIBILITY] send target_outdated alert failed for {from_name}: {err}")


_PEER_DELIVERY_MODES = {"default", "next", "goal", "stop"}
_GOAL_API_ACTIONS = {"set", "replace", "cancel"}
_TEAM_CONTRACT_ROLES = {"coordinator", "team_lead", "worker"}
_INDEPENDENT_ROLE = "independent"
_TEAM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TEAM_SUPERVISOR_ONLY_REASON = "team_only_accepts_supervisor_tasks_via_coordinator"
_TEAM_SUPERVISOR_ONLY_MESSAGE = "team只允许coordinator从主管接受任务"
_PEER_NEXT_QUEUE = []
_PEER_NEXT_QUEUE_LOCK = threading.Lock()


def _format_peer_text(payload, content):
    from_intern = payload.get("from_intern_name", "")
    from_project = payload.get("from_project", "")
    mode = payload.get("original_mode") or payload.get("mode") or "default"
    delivery = payload.get("delivery_kind") or "direct"
    msg_id = payload.get("msg_id") or payload.get("request_id") or "-"
    source = f"{from_project}/{from_intern}" if from_project else from_intern
    prefix = f"【peer mode={mode} delivery={delivery} from {source} msg_id={msg_id}】"
    return prefix + "\n" + content


def _send_peer_text_to_tmux(intern_name, intern_type, text, msg_id):
    if intern_type == "codex":
        return _send_to_codex_tmux(intern_name, text, delivery_id=msg_id, require_ack=True)
    return _send_to_claude_tmux(intern_name, text, delivery_id=msg_id)


def _normalize_contract_role(role):
    role = (role or _INDEPENDENT_ROLE).strip()
    if not role or role == "plain_intern":
        return _INDEPENDENT_ROLE
    if role in _TEAM_CONTRACT_ROLES or role == _INDEPENDENT_ROLE:
        return role
    return _INDEPENDENT_ROLE


def _get_local_contract_meta(intern_name, project):
    if not intern_name:
        return {"role": _INDEPENDENT_ROLE, "team_id": ""}
    project = project or _get_intern_project(intern_name) or ""
    status_md = _get_status_md_path(intern_name, project)
    meta = _parse_status_metadata(status_md)
    return {
        "role": _normalize_contract_role(meta.get("ROLE", _INDEPENDENT_ROLE)),
        "team_id": meta.get("TEAM_ID", "") or meta.get("TEAM", ""),
    }


def _contract_roles_from_payload(payload):
    from_name = payload.get("from_intern_name", "")
    from_project = payload.get("from_project", "")
    to_name = payload.get("to_intern_name", "")
    to_project = payload.get("to_project", "")
    from_role = _normalize_contract_role(payload.get("from_role"))
    to_role = _normalize_contract_role(payload.get("to_role"))
    from_team_id = payload.get("from_team_id", "")
    to_team_id = payload.get("to_team_id", "")

    if "from_role" not in payload or "from_team_id" not in payload:
        meta = _get_local_contract_meta(from_name, from_project)
        if "from_role" not in payload:
            from_role = meta["role"]
        if "from_team_id" not in payload:
            from_team_id = meta["team_id"]
    if "to_role" not in payload or "to_team_id" not in payload:
        meta = _get_local_contract_meta(to_name, to_project)
        if "to_role" not in payload:
            to_role = meta["role"]
        if "to_team_id" not in payload:
            to_team_id = meta["team_id"]

    return {
        "from_role": from_role,
        "to_role": to_role,
        "from_team_id": from_team_id,
        "to_team_id": to_team_id,
    }


def _same_contract_team(payload, roles):
    request_team_id = payload.get("team_id", "")
    from_team_id = roles.get("from_team_id", "")
    to_team_id = roles.get("to_team_id", "")
    if request_team_id and from_team_id and request_team_id != from_team_id:
        return False
    if request_team_id and to_team_id and request_team_id != to_team_id:
        return False
    if from_team_id and to_team_id and from_team_id != to_team_id:
        return False
    return True


def _contract_reject(reason, message=None):
    result = {"status": "undeliverable", "reason": reason}
    if message:
        result["message"] = message
    return result


def _team_supervisor_only_reject():
    return _contract_reject(_TEAM_SUPERVISOR_ONLY_REASON, _TEAM_SUPERVISOR_ONLY_MESSAGE)


def _validate_independent_team_boundary(roles):
    from_role = roles.get("from_role")
    to_role = roles.get("to_role")
    if from_role == _INDEPENDENT_ROLE and to_role in _TEAM_CONTRACT_ROLES:
        return _team_supervisor_only_reject()
    if to_role == _INDEPENDENT_ROLE and from_role in _TEAM_CONTRACT_ROLES:
        return _team_supervisor_only_reject()
    return None


def _is_safe_team_id(team_id):
    return (
        isinstance(team_id, str)
        and bool(_TEAM_ID_PATTERN.fullmatch(team_id))
        and os.path.basename(team_id) == team_id
        and team_id not in (".", "..")
    )


def _load_team_contract(project, team_id):
    if not project or not _is_safe_team_id(team_id):
        return None
    path = os.path.join(WORK_AGENTS_ROOT, project, "workspace", "teams", team_id, "team.json")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _active_team_member_names(team_data):
    names = set()
    lead = team_data.get("team_lead")
    if isinstance(lead, dict) and lead.get("status", "active") != "deleted":
        lead_name = str(lead.get("intern_name") or "")
        if lead_name:
            names.add(lead_name)
    workers = team_data.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict) or worker.get("status", "active") == "deleted":
                continue
            worker_name = str(worker.get("intern_name") or "")
            if worker_name:
                names.add(worker_name)
    return names


def _validate_coordinator_team_scope(payload, roles):
    """Limit coordinator control to teams explicitly bound to that coordinator.

    The target daemon performs this check for both local and relayed peer/goal
    deliveries, so cross-machine sends cannot bypass the team.json owner.
    """
    if roles.get("from_role") != "coordinator":
        return None

    to_role = roles.get("to_role")
    if to_role == _INDEPENDENT_ROLE:
        return _contract_reject("coordinator_target_not_in_assigned_team")
    if to_role not in {"team_lead", "worker"}:
        return _contract_reject("coordinator_target_not_in_assigned_team")

    team_id = roles.get("to_team_id") or payload.get("team_id") or ""
    if not team_id:
        return _contract_reject("coordinator_target_not_in_assigned_team")

    team_data = _load_team_contract(payload.get("to_project", ""), team_id)
    if not team_data:
        return _contract_reject("coordinator_target_not_in_assigned_team")

    coordinator = team_data.get("coordinator")
    if not isinstance(coordinator, dict):
        return _contract_reject("coordinator_target_not_in_assigned_team")
    if str(coordinator.get("intern_name") or "") != payload.get("from_intern_name", ""):
        return _contract_reject("coordinator_target_not_in_assigned_team")
    if payload.get("to_intern_name", "") not in _active_team_member_names(team_data):
        return _contract_reject("coordinator_target_not_in_assigned_team")
    return None


_DELIVERY_HEALTH_REASONS = {
    "offline": "Target intern is offline or not active on this daemon.",
    "tmux_session_missing": "Target tmux session is missing.",
    "session_not_running": "Target tmux session exists but the CLI process is not running.",
    "tmux_send_failed": "Target tmux session rejected the delivery command.",
}


def _augment_delivery_health_response(result, to_name, same_machine):
    if not isinstance(result, dict) or result.get("status") != "undeliverable":
        return result
    reason = result.get("reason", "")
    if reason not in _DELIVERY_HEALTH_REASONS:
        return result

    response = dict(result)
    response.setdefault("message", _DELIVERY_HEALTH_REASONS[reason])
    if same_machine:
        response["remediation"] = {
            "same_machine": True,
            "action": "restart_session_via_daemon",
            "message": (
                f"{to_name} is on the same machine as the sender. "
                "Call the local daemon/session restart entry point to try restarting the session, then retry."
            ),
        }
    else:
        response["remediation"] = {
            "same_machine": False,
            "action": "notify_supervisor",
            "message": (
                f"{to_name} is not on the sender's machine. "
                "Notify the supervisor to restart or repair the target intern session on its host."
            ),
        }
    return response


def _augment_goal_unconfirmed_response(result):
    if not isinstance(result, dict) or result.get("reason") != "unconfirmed":
        return result

    response = dict(result)
    response.setdefault(
        "message",
        (
            "Codex did not confirm this content as an active goal. "
            "The goal content may be too complex or multi-line for `/goal [content]` handling."
        ),
    )
    response.setdefault("detail", _TMUX_SUBMIT_UNCONFIRMED_ERROR)
    response["remediation"] = {
        "action": "rewrite_goal_content_single_line_and_retry",
        "message": (
            "Rewrite the goal content to fit `/goal [content]`, preferably as one concise line, "
            "then call the goal API again. For long instructions, put details in a file or task "
            "document and set a short one-line goal that points to it."
        ),
    }
    return response


def _validate_peer_contract(payload):
    mode = payload.get("mode") or "default"
    roles = _contract_roles_from_payload(payload)
    from_role = roles["from_role"]
    to_role = roles["to_role"]

    boundary_result = _validate_independent_team_boundary(roles)
    if boundary_result:
        return boundary_result

    scope_result = _validate_coordinator_team_scope(payload, roles)
    if scope_result:
        return scope_result

    # independent-to-independent keeps the broad peer-send compatibility surface.
    if from_role == _INDEPENDENT_ROLE or to_role == _INDEPENDENT_ROLE:
        return None

    if mode == "goal":
        return _contract_reject("coordinator_goal_requires_goal_api")
    if from_role == to_role:
        return _contract_reject("same_role_team_channel_not_supported")
    if from_role == "coordinator" and to_role == "team_lead":
        if mode in {"default", "next", "stop"}:
            return None
        return _contract_reject("unsupported_mode_for_team")
    if from_role == "team_lead" and to_role == "coordinator":
        if mode == "default":
            return None
        return _contract_reject("unsupported_mode_for_team")
    if from_role == "team_lead" and to_role == "worker":
        if mode not in {"default", "next", "stop"}:
            return _contract_reject("unsupported_mode_for_team")
        if not _same_contract_team(payload, roles):
            return _contract_reject("not_same_team")
        return None
    if from_role == "worker" and to_role == "team_lead":
        return _contract_reject("worker_to_team_lead_use_mailbox")
    if from_role == "coordinator" and to_role == "worker":
        return _contract_reject("coordinator_to_worker_use_team_lead")
    if from_role == "worker" and to_role == "coordinator":
        return _contract_reject("worker_to_coordinator_use_team_lead")
    return _contract_reject("role_not_allowed")


def _validate_goal_contract(payload, same_daemon):
    roles = _contract_roles_from_payload(payload)
    from_role = roles["from_role"]
    to_role = roles["to_role"]
    same_project = (payload.get("from_project", "") == payload.get("to_project", ""))

    boundary_result = _validate_independent_team_boundary(roles)
    if boundary_result:
        return boundary_result

    scope_result = _validate_coordinator_team_scope(payload, roles)
    if scope_result:
        return scope_result

    if from_role == _INDEPENDENT_ROLE or to_role == _INDEPENDENT_ROLE:
        if from_role == _INDEPENDENT_ROLE and to_role == _INDEPENDENT_ROLE and same_daemon and same_project:
            return None
        return _contract_reject("goal_independent_same_daemon_required")
    if from_role == "coordinator" and to_role == "team_lead":
        return None
    if from_role == "coordinator" and to_role == "worker":
        return _contract_reject("coordinator_to_worker_use_team_lead")
    if from_role == "worker" and to_role == "coordinator":
        return _contract_reject("worker_to_coordinator_use_team_lead")
    if from_role == "worker" and to_role == "team_lead":
        return _contract_reject("worker_to_team_lead_use_mailbox")
    if from_role == to_role:
        return _contract_reject("same_role_team_channel_not_supported")
    return _contract_reject("unsupported_goal_target")


def _attach_local_sender_contract(payload):
    meta = _get_local_contract_meta(payload.get("from_intern_name", ""), payload.get("from_project", ""))
    payload["from_role"] = meta["role"]
    payload["from_team_id"] = meta["team_id"]


def _send_codex_goal_set_attempt(intern_name, target, content, log_ctx):
    text = "/goal " + content
    ack_path, ack_offset = _get_codex_ack_start(intern_name)
    try:
        _tmux_clear_input_line(target)
        time.sleep(0.05)
        _tmux_paste_text(target, "/goal clear")
        time.sleep(_TMUX_ENTER_DELAY_SECONDS)
        _tmux_send_enter(target)
        time.sleep(0.2)
        _tmux_clear_input_line(target)
        time.sleep(0.05)
        _tmux_paste_text(target, text)
        time.sleep(_TMUX_ENTER_DELAY_SECONDS)
        _tmux_send_enter(target)
    except subprocess.CalledProcessError as e:
        return False, str(e)

    if ack_path and _wait_for_codex_goal_ack(ack_path, ack_offset, content, timeout=1.0):
        log.info(f"[PEER] codex goal delivered {log_ctx}, ack=ok")
        return True, None
    if _codex_goal_confirmation_pending(target, content):
        log.info(f"[PEER] codex goal confirmation pending {log_ctx}; confirming")
        try:
            _tmux_send_enter(target)
        except subprocess.CalledProcessError as e:
            return False, str(e)
    if ack_path and _wait_for_codex_goal_ack(
        ack_path, ack_offset, content, timeout=_CODEX_GOAL_ACK_TIMEOUT_SECONDS
    ):
        log.info(f"[PEER] codex goal delivered {log_ctx}, ack=ok")
        return True, None
    if _codex_goal_visible_in_panel(target, content):
        log.info(f"[PEER] codex goal delivered {log_ctx}, ack=panel")
        return True, None
    log.warning(f"[PEER] codex goal ack failed {log_ctx}, transcript={ack_path or '-'}")
    return False, _TMUX_SUBMIT_UNCONFIRMED_ERROR


def _send_peer_goal_to_codex_tmux(intern_name, content, msg_id):
    if not _check_tmux_session(intern_name):
        return False, "tmux session not found"
    if not _is_codex_process_running(intern_name):
        return False, "Codex has exited (tmux session exists but Codex is not running)"

    target = f"={intern_name}:"
    text = "/goal " + content
    log_ctx = f"intern={intern_name}, delivery={msg_id or '-'}, hash={_delivery_hash(text)}"
    return _send_codex_goal_set_attempt(intern_name, target, content, log_ctx)


def _send_goal_cancel_to_codex_tmux(intern_name, msg_id):
    if not _check_tmux_session(intern_name):
        return False, "tmux session not found"
    if not _is_codex_process_running(intern_name):
        return False, "Codex has exited (tmux session exists but Codex is not running)"

    target = f"={intern_name}:"
    text = "/goal clear"
    ack_path, ack_offset = _get_codex_ack_start(intern_name)
    try:
        _tmux_clear_input_line(target)
        time.sleep(0.05)
        _tmux_paste_text(target, text)
        time.sleep(_TMUX_ENTER_DELAY_SECONDS)
        _tmux_send_enter(target)
    except subprocess.CalledProcessError as e:
        return False, str(e)

    log_ctx = f"intern={intern_name}, delivery={msg_id or '-'}, hash={_delivery_hash(text)}"
    if not ack_path:
        log.warning(f"[GOAL_API] codex goal cancel ack unavailable {log_ctx}")
        return False, _TMUX_SUBMIT_UNCONFIRMED_ERROR
    if _wait_for_codex_goal_ack(ack_path, ack_offset, "", timeout=_CODEX_GOAL_ACK_TIMEOUT_SECONDS):
        log.info(f"[GOAL_API] codex goal canceled {log_ctx}, ack=ok")
        return True, None
    log.warning(f"[GOAL_API] codex goal cancel ack failed {log_ctx}, transcript={ack_path}")
    return False, _TMUX_SUBMIT_UNCONFIRMED_ERROR


def _notify_peer_goal_visible(payload, content):
    """Send a target-group Feishu marker for slash-command goal delivery.

    Codex/Claude slash commands do not produce a normal UserPromptSubmit turn, so
    the target supervisor group otherwise only sees the later goal continuation.
    """
    if _api is None:
        log.warning("[PEER_VISIBILITY] _api not initialized, skip peer goal marker")
        return
    to_name = payload.get("to_intern_name", "")
    chat_id = _registry.find_chat_id(to_name) if _registry else ""
    if not chat_id:
        log.warning(f"[PEER_VISIBILITY] no chat_id for {to_name}, skip peer goal marker")
        return
    from_intern = payload.get("from_intern_name", "")
    from_project = payload.get("from_project", "")
    source = f"{from_project}/{from_intern}" if from_project else from_intern
    msg_id = payload.get("msg_id") or payload.get("request_id") or "-"
    text = (
        "🎯 peer goal set\n"
        f"【peer mode=goal delivery=goal from {source} msg_id={msg_id}】\n"
        "Goal:\n"
        f"{content}"
    )
    _, err = _api.send_message(chat_id, text)
    if err:
        log.warning(f"[PEER_VISIBILITY] send peer goal marker failed for {to_name}: {err}")


def _notify_goal_api_visible(payload, action, content):
    """Send a target-group Feishu marker for direct goal API delivery."""
    if _api is None:
        log.warning("[GOAL_API_VISIBILITY] _api not initialized, skip goal marker")
        return
    to_name = payload.get("to_intern_name", "")
    chat_id = _registry.find_chat_id(to_name) if _registry else ""
    if not chat_id:
        log.warning(f"[GOAL_API_VISIBILITY] no chat_id for {to_name}, skip goal marker")
        return
    from_intern = payload.get("from_intern_name", "")
    from_project = payload.get("from_project", "")
    source = f"{from_project}/{from_intern}" if from_project else from_intern
    msg_id = payload.get("goal_id") or payload.get("msg_id") or payload.get("request_id") or "-"
    title = "🛑 goal canceled" if action == "cancel" else "🎯 goal set"
    text = (
        f"{title}\n"
        f"【goal api action={action} delivery=goal from {source} msg_id={msg_id}】"
    )
    if action != "cancel":
        text += "\nGoal:\n" + content
    _, err = _api.send_message(chat_id, text)
    if err:
        log.warning(f"[GOAL_API_VISIBILITY] send goal marker failed for {to_name}: {err}")


def _deliver_goal_locally(payload):
    """Deliver a same-daemon/same-project goal API command to a tmux intern."""
    to_name = payload.get("to_intern_name", "")
    to_project = payload.get("to_project", "")
    from_project = payload.get("from_project", "")
    action = payload.get("action") or "set"
    content = payload.get("content", "")
    goal_id = payload.get("goal_id") or payload.get("msg_id") or payload.get("request_id") or uuid.uuid4().hex

    if action not in _GOAL_API_ACTIONS:
        return {"status": "undeliverable", "reason": "unsupported_action"}
    contract_result = _validate_goal_contract(payload, same_daemon=not bool(payload.get("via_relay")))
    if contract_result:
        return contract_result
    intern_type = _get_intern_type_scoped(to_name, project=to_project)
    if not _is_tmux_intern_type(intern_type):
        return {"status": "undeliverable", "reason": "unsupported_target"}
    if not _owns_local_peer_target(to_name, to_project):
        return {"status": "undeliverable", "reason": "offline"}
    if not _check_tmux_session(to_name):
        return {"status": "undeliverable", "reason": "tmux_session_missing"}
    process_check = _is_claude_process_running if intern_type == "claude" else _is_codex_process_running
    if not process_check(to_name):
        return {"status": "undeliverable", "reason": "session_not_running"}

    if action == "cancel":
        if intern_type == "codex":
            success, err = _send_goal_cancel_to_codex_tmux(to_name, goal_id)
        else:
            success, err = _send_peer_text_to_tmux(to_name, intern_type, "/goal clear", goal_id)
    elif intern_type == "codex":
        success, err = _send_peer_goal_to_codex_tmux(to_name, content, goal_id)
    else:
        success, err = _send_peer_text_to_tmux(to_name, intern_type, "/goal " + content, goal_id)

    if not success:
        log.warning(f"[GOAL_API] tmux goal send failed for {to_name}: {err}")
        if err in _TMUX_SUBMIT_UNCONFIRMED_ERRORS:
            return {"status": "undeliverable", "reason": "unconfirmed", "detail": err}
        return {"status": "undeliverable", "reason": "tmux_send_failed", "detail": err}

    _notify_goal_api_visible(payload, action, content)
    if action == "cancel":
        return {"status": "delivered", "kind": "goal_cancel", "goal_id": goal_id}
    return {"status": "delivered", "kind": "goal", "goal_id": goal_id}


def _goal_api_http_status(result):
    """Map goal delivery result to HTTP status.

    Goal API callers must be able to rely on transport status for the common
    success/failure split. Detailed handling still uses the JSON ``reason``.
    """
    if result.get("status") == "delivered":
        return 200
    reason = result.get("reason")
    if reason == "relay_unreachable":
        return 503
    if reason == "unknown_target":
        return 404
    return 409


def _deliver_mail_locally(payload):
    to_name = payload.get("to_intern_name", "")
    to_project = payload.get("to_project", "")
    from_name = payload.get("from_intern_name", "")
    from_project = payload.get("from_project", "")
    content = payload.get("content", "")
    if not _owns_local_mail_target(to_name, to_project):
        return {"status": "undeliverable", "reason": "offline"}
    try:
        message = team_mailbox.append_message(
            target_project=to_project,
            from_intern_name=from_name,
            from_project=from_project,
            to_intern_name=to_name,
            content=content,
            team_id=payload.get("team_id", ""),
            kind=payload.get("kind", "progress"),
            related_task=payload.get("related_task", ""),
            related_pr=payload.get("related_pr", ""),
            client_message_id=payload.get("client_message_id", ""),
        )
    except PermissionError as exc:
        return {"status": "undeliverable", "reason": str(exc)}
    except ValueError as exc:
        return {"status": "undeliverable", "reason": str(exc)}
    return {
        "status": "stored",
        "kind": "mail",
        "message_id": message["message_id"],
        "team_id": message["team_id"],
        "read_state": "unread",
    }


def _send_peer_stop_to_tmux(intern_name, intern_type, project=None):
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"={intern_name}:", "Escape"],
            check=True, capture_output=True
        )
        if intern_type == "codex":
            ok, reason = _finalize_active_feishu_message_for_stop(intern_name, project=project)
            if ok:
                log.info(f"[PEER] finalized active Feishu turn for Codex '{intern_name}' after peer stop ({reason})")
                _notify_intern_status_changed(intern_name)
                _push_interns_state_once()
        return {"status": "delivered", "kind": "stop"}
    except subprocess.CalledProcessError as e:
        log.warning(f"[PEER] stop tmux failed for {intern_name}: {e}")
        return {"status": "undeliverable", "reason": "tmux_send_failed", "detail": str(e)}


def _enqueue_peer_next(payload):
    queued = dict(payload)
    queued["original_mode"] = payload.get("mode") or "next"
    queued["delivery_kind"] = "queued"
    queued["mode"] = "default"
    with _PEER_NEXT_QUEUE_LOCK:
        _PEER_NEXT_QUEUE.append(queued)
    return len(_PEER_NEXT_QUEUE)


def _peer_next_queue_worker(stop_event):
    while not stop_event.is_set():
        stop_event.wait(1)
        if stop_event.is_set():
            break
        with _PEER_NEXT_QUEUE_LOCK:
            queued = list(_PEER_NEXT_QUEUE)
        if not queued:
            continue
        for payload in queued:
            to_name = payload.get("to_intern_name", "")
            to_project = payload.get("to_project", "")
            if not to_name or _is_turn_active(to_name, {_online_key(to_name, to_project), to_name}, project=to_project):
                continue
            result = _deliver_peer_locally(payload)
            with _PEER_NEXT_QUEUE_LOCK:
                if payload in _PEER_NEXT_QUEUE:
                    _PEER_NEXT_QUEUE.remove(payload)
            log.info(f"[PEER] next queued delivery to {to_name}: {result}")


def _deliver_peer_locally(payload):
    """task213: deliver a peer message to a target intern owned by this daemon.

    Returns ``{"status": "delivered"|"undeliverable", "reason"?: str, "kind"?: str}``.
    Caller (HTTP handler for same-machine, RelayClient WS handler for cross-machine)
    is responsible for relay communication and HTTP response shaping. This function
    only encapsulates the local delivery rules (tmux/copilot/esc/busy/attachments).

    mode=next queues while the target turn is active; default intentionally
    sends to the CLI so its pending input behavior can take over.
    """
    to_name = payload.get("to_intern_name", "")
    content = payload.get("content", "")
    attachments = payload.get("attachments") or []
    to_project = payload.get("to_project", "")
    msg_id = payload.get("msg_id") or payload.get("request_id") or uuid.uuid4().hex
    mode = payload.get("mode") or "default"
    if mode not in _PEER_DELIVERY_MODES:
        return {"status": "undeliverable", "reason": "unsupported_mode"}
    contract_result = _validate_peer_contract(payload)
    if contract_result:
        return contract_result
    if mode == "goal" and payload.get("from_project", "") != to_project:
        return {"status": "undeliverable", "reason": "goal_same_daemon_project_required"}

    intern_type = _get_intern_type_scoped(to_name, project=to_project)

    if not _is_tmux_intern_type(intern_type) and intern_type != "copilot":
        return {"status": "undeliverable", "reason": "unsupported_target"}
    if not _owns_local_peer_target(to_name, to_project):
        return {"status": "undeliverable", "reason": "offline"}

    if intern_type == "copilot":
        if mode != "default":
            return {"status": "undeliverable", "reason": "unsupported_mode"}
        if attachments:
            return {"status": "undeliverable", "reason": "unsupported_attachment_target"}
        push_payload = {
            "type": "peer_message",
            "intern_name": to_name,
            "text": _format_peer_text(payload, content),
            "message_id": msg_id,
            "from_intern_name": payload.get("from_intern_name", ""),
            "from_project": payload.get("from_project", ""),
        }
        delivered = _ws_server.route_to_active(to_name, push_payload) if _ws_server else False
        if not delivered:
            return {"status": "undeliverable", "reason": "offline"}
        return {"status": "delivered"}

    if not _check_tmux_session(to_name):
        return {"status": "undeliverable", "reason": "tmux_session_missing"}
    process_check = _is_claude_process_running if intern_type == "claude" else _is_codex_process_running
    if not process_check(to_name):
        return {"status": "undeliverable", "reason": "session_not_running"}

    # Legacy /esc maps to the new stop control mode.
    if mode == "stop" or content == "/esc":
        result = _send_peer_stop_to_tmux(to_name, intern_type, project=to_project)
        if content == "/esc" and result.get("status") == "delivered":
            result = {"status": "delivered", "kind": "esc"}
        return result

    if mode == "next" and _is_turn_active(to_name, {_online_key(to_name, to_project), to_name}, project=to_project):
        queue_depth = _enqueue_peer_next(payload)
        log.info(f"[PEER] queued next message for '{to_name}', depth={queue_depth}")
        return {"status": "delivered", "kind": "queued"}

    if attachments:
        try:
            _persist_inbound_attachments(to_name, msg_id, attachments, project=to_project)
        except Exception as e:
            log.error(f"[PEER] persist attachments failed for {to_name}: {e}", exc_info=True)
            return {"status": "undeliverable", "reason": "offline"}

    if mode == "goal" and intern_type == "codex":
        success, err = _send_peer_goal_to_codex_tmux(to_name, content, msg_id)
    else:
        text = "/goal " + content if mode == "goal" else _format_peer_text(payload, content)
        success, err = _send_peer_text_to_tmux(to_name, intern_type, text, msg_id)
    if not success:
        log.warning(f"[PEER] tmux send failed for {to_name}: {err}")
        if err in _TMUX_SUBMIT_UNCONFIRMED_ERRORS:
            return {"status": "undeliverable", "reason": "unconfirmed", "detail": err}
        return {"status": "undeliverable", "reason": "tmux_send_failed", "detail": err}
    if mode == "goal":
        _notify_peer_goal_visible(payload, content)
        return {"status": "delivered", "kind": "goal"}
    return {"status": "delivered"}


def _build_state_payload(msg_type):
    """Build the full state payload sent to the relay.

    Same shape for ``sync_online`` (event-driven, triggers light control) and
    ``interns_state`` (5s periodic, memory-only). The only difference is ``type``.
    """
    all_interns = _iter_registry_entries(_registry)
    active_copilot = _ws_server.get_active_interns() if _ws_server else set()
    current_online = []
    online_names = set()
    for item in all_interns:
        name = item["name"]
        chat_id = item["chat_id"]
        project = item.get("project") or ""
        intern_type = _get_intern_type_scoped(name, project=project)
        project = _get_intern_project_scoped(name, project=project)
        if _is_tmux_intern_type(intern_type):
            if _is_intern_online(name, project=project):
                current_online.append({"name": name, "chat_id": chat_id, "type": intern_type, "project": project})
                online_names.add(_online_key(name, project))
                online_names.add(name)
        elif name in active_copilot:
            current_online.append({"name": name, "chat_id": chat_id, "type": intern_type, "project": project})
            online_names.add(_online_key(name, project))
            online_names.add(name)
    return {
        "type": msg_type,
        "online_interns": current_online,
        "resources": _collect_resources(),
        "interns_dynamic": _collect_interns_dynamic(online_names),
        "warnings": _collect_daemon_warnings(),
    }


def _refresh_lights():
    """Send full online set to relay (stateless, no local diff).

    Scans tmux for Claude + WS active set for Copilot, sends complete list to relay.
    Sends extended format with chat_id+type so relay can auto-register if needed.
    Relay computes diff and updates feishu group lights.
    """
    if not _registry:
        return

    if _relay_client and _relay_client.connected:
        msg = _build_state_payload("sync_online")
        _relay_client.send(msg)
        log.info(f"[LIGHT] sync_online sent: {[i['name'] for i in msg['online_interns']]}")
    else:
        log.warning("[LIGHT] relay client not connected, skipping light sync")


def _refresh_lights_for_intern(intern_name, project=None):
    """Refresh global online set, then repair one confirmed-online intern route."""
    _refresh_lights()
    if not intern_name:
        return
    if not _relay_client or not _relay_client.connected:
        return
    online = _is_intern_online(intern_name, project=project) if project else _is_intern_online(intern_name)
    if not online:
        log.info(f"[LIGHT] request_refresh skip online repair for '{intern_name}': not live")
        return
    if project:
        _relay_client.send_intern_online(intern_name, project=project)
    else:
        _relay_client.send_intern_online(intern_name)
    _notify_intern_status_changed(intern_name)
    log.info(f"[LIGHT] request_refresh sent intern_online repair for '{intern_name}'")


def _report_interns_state(stop_event, interval=5):
    """Periodic (default 5s) interns_state push to relay.

    Same payload as ``_refresh_lights`` but ``type=interns_state`` — relay updates
    its in-memory registry only and does not touch the feishu API, so the
    dashboard stays fresh without amplifying light-control calls.
    """
    while not stop_event.is_set():
        try:
            if _registry and _relay_client and _relay_client.connected:
                msg = _build_state_payload("interns_state")
                _relay_client.send(msg)
        except Exception as e:
            log.debug(f"[STATE] interns_state push failed: {e}")
        stop_event.wait(interval)


# ══════════════════════════════════════════
# HTTP API server
# ══════════════════════════════════════════

# Global references set in main()
_api = None
_registry = None
_workspace_cache = None
_ws_server = None
_shutdown_event = None

# ── 交互式问答队列（AskUserQuestion / ExitPlanMode） ──
import threading as _q_threading
_pending_questions = {}   # intern_name → {"questions": [...], "tool_name": str, "answer": None|dict, "event": Event}
_pq_lock = _q_threading.Lock()
_codex_rui_watchers = {}  # (intern_name, transcript_path) → Thread
_codex_rui_seen_calls = set()
_codex_rui_lock = _q_threading.Lock()
_CODEX_RUI_WATCH_TIMEOUT = 6 * 3600


def _options_description_md(options):
    """把 options[].description 拼成 markdown bullet list。
    返回 None 表示没有任何 option 带 description（调用方可省略渲染）。
    """
    lines = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        desc = opt.get("description", "")
        if not desc:
            continue
        label = opt.get("label", "")
        star = " ⭐" if opt.get("recommended", False) else ""
        lines.append(f"- **{label}**{star} — {desc}")
    return "\n".join(lines) if lines else None


def _format_question_card(intern_name, tool_name, questions):
    """Build interactive card JSON for AskUserQuestion / ExitPlanMode.

    Single question with options → buttons for quick select + form for free text.
    Single question without options → form with input.
    Multi question → form with select/input per question + submit.
    """
    if tool_name == "ExitPlanMode":
        title = f"📋 {intern_name} 的方案已完成规划"
        template = "blue"
    elif tool_name == "request_user_input":
        # Codex CLI 的等价工具，标题区分一下来源便于主管识别
        title = f"❓ {intern_name}（Codex）有问题需要确认"
        template = "purple"
    else:
        title = f"❓ {intern_name} 有问题需要确认"
        template = "purple"

    # Build question_keys for form submission metadata
    question_keys = []
    for q in questions:
        qk = q.get("question", q.get("header", f"Q{len(question_keys)+1}"))
        question_keys.append(qk)

    elements = []

    if len(questions) == 1:
        # ── Single question ──
        q = questions[0]
        header = q.get("header", "")
        question = q.get("question", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)
        q_text = f"【{header}】{question}" if header else question

        # Question text
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{q_text}**"}
        })

        # Options descriptions — 独立可见 text block，不被 button/下拉宽度截断
        desc_md = _options_description_md(options)
        if desc_md:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": desc_md}
            })

        if options and not multi_select:
            # Quick-select buttons (outside form) — 按钮上只放 label，description 在上方 text block 里已展示
            actions = []
            for opt in options:
                label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                recommended = opt.get("recommended", False) if isinstance(opt, dict) else False
                desc = opt.get("description", "") if isinstance(opt, dict) else ""
                url = opt.get("url", "") if isinstance(opt, dict) else ""
                btn_text = f"{label} — {desc}" if desc else label
                btn_type = "primary" if recommended else "default"

                btn = {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": btn_type,
                }
                if url:
                    btn["multi_url"] = {
                        "url": url,
                        "pc_url": "",
                        "android_url": "",
                        "ios_url": "",
                    }
                else:
                    btn["value"] = {
                        "intern_name": intern_name,
                        "question_key": question_keys[0],
                        "answer": label,
                        "question_title": q_text,
                    }
                actions.append(btn)
            elements.append({"tag": "action", "actions": actions})

        # Free text input form (multiSelect path puts multi_select_static inside form)
        elements.append({"tag": "hr"})
        form_elements = []

        if options and multi_select:
            # multi_select_static 放 form 里，提交时返回 list of labels
            select_options = []
            for opt in options:
                label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                recommended = opt.get("recommended", False) if isinstance(opt, dict) else False
                disp = label + (" ⭐" if recommended else "")
                select_options.append({
                    "text": {"tag": "plain_text", "content": disp},
                    "value": label,
                })
            form_elements.append({
                "tag": "multi_select_static",
                "name": "q_0_multiselect",
                "placeholder": {"tag": "plain_text", "content": "可勾选多项..."},
                "options": select_options,
            })

        form_elements.extend([
            {
                "tag": "input",
                "name": "q_0_input",
                "placeholder": {"tag": "plain_text", "content": "输入你的回答..."},
                "label": {
                    "tag": "plain_text",
                    "content": "✍️ 或输入自定义回答：" if options else "✍️ 输入回答：",
                },
                "label_position": "top",
            },
            {
                "tag": "button",
                "text": {"tag": "lark_md", "content": "提交"},
                "type": "primary",
                "action_type": "form_submit",
                "name": "submit",
                "value": {
                    "intern_name": intern_name,
                    "question_keys": question_keys,
                    "question_title": q_text,
                }
            }
        ])
        elements.append({
            "tag": "form",
            "name": "free_text_form",
            "elements": form_elements,
        })

    else:
        # ── Multi question → all in form ──
        form_elements = []
        for i, q in enumerate(questions):
            header = q.get("header", "")
            question = q.get("question", "")
            options = q.get("options", [])
            multi_select = q.get("multiSelect", False)
            q_text = f"【{header}】{question}" if header else question

            # Question text (div not allowed directly in form, use column_set>column>markdown)
            form_elements.append({
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [{
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{
                        "tag": "markdown",
                        "content": f"**{q_text}**"
                    }]
                }]
            })

            # Options descriptions — 独立可见，不被 select 下拉宽度截断
            desc_md = _options_description_md(options)
            if desc_md:
                form_elements.append({
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [{
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [{
                            "tag": "markdown",
                            "content": desc_md
                        }]
                    }]
                })

            if options:
                select_options = []
                for opt in options:
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    recommended = opt.get("recommended", False) if isinstance(opt, dict) else False
                    disp = label + (" ⭐" if recommended else "")
                    select_options.append({
                        "text": {"tag": "plain_text", "content": disp},
                        "value": label,
                    })
                if multi_select:
                    form_elements.append({
                        "tag": "multi_select_static",
                        "name": f"q_{i}_multiselect",
                        "placeholder": {"tag": "plain_text", "content": "可勾选多项..."},
                        "options": select_options,
                    })
                else:
                    form_elements.append({
                        "tag": "select_static",
                        "name": f"q_{i}_select",
                        "placeholder": {"tag": "plain_text", "content": "请选择..."},
                        "options": select_options,
                    })

            # Text input for custom answer (always present; multi-select 场景仅作 fallback)
            form_elements.append({
                "tag": "input",
                "name": f"q_{i}_input",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "或输入自定义回答（优先于下拉/多选）" if options else "输入回答...",
                },
            })

        # Submit button
        form_elements.append({
            "tag": "button",
            "text": {"tag": "lark_md", "content": "提交所有回答"},
            "type": "primary",
            "action_type": "form_submit",
            "name": "submit",
            "value": {
                "intern_name": intern_name,
                "question_keys": question_keys,
            }
        })

        elements.append({
            "tag": "form",
            "name": "multi_question_form",
            "elements": form_elements,
        })

    # Hint
    elements.append({"tag": "hr"})
    if len(questions) == 1 and questions[0].get("options"):
        hint = "💡 点击按钮/表单提交；群里文字请用 /answer 1 或 /answer <自定义内容>"
    elif len(questions) > 1:
        hint = "💡 选择或输入回答后提交；群里文字请用 /answer 1:答案 2:答案"
    else:
        hint = "💡 输入回答后提交；群里文字请用 /answer <回复内容>"

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": hint}]
    })

    return {
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title}
        },
        "elements": elements
    }


def _build_answered_card(intern_name, tool_name, answers, source="飞书"):
    """Build a read-only card showing the question has been answered."""
    if tool_name == "ExitPlanMode":
        title = f"📋 {intern_name} 的方案 — ✅ 已回答"
    elif tool_name == "request_user_input":
        title = f"❓ {intern_name}（Codex）的问题 — ✅ 已回答"
    else:
        title = f"❓ {intern_name} 的问题 — ✅ 已回答"

    # Format answers for display
    if "_local" in answers and len(answers) == 1:
        answer_text = answers["_local"]
    else:
        def _display(v):
            if isinstance(v, list):
                return "、".join(str(x) for x in v)
            return v
        answer_lines = [f"**{k}**: {_display(v)}" for k, v in answers.items()]
        answer_text = "\n".join(answer_lines) if answer_lines else str(answers)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"💬 **回答内容：**\n{answer_text}"}},
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"✅ 已通过{source}回答"}]},
    ]
    return {
        "header": {"template": "green", "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def _update_question_card(intern_name, answers, source="飞书"):
    """Update the Feishu card to show answered state (does NOT pop pending entry)."""
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
    if not entry:
        return
    message_id = entry.get("message_id")
    tool_name = entry.get("tool_name", "AskUserQuestion")
    if message_id and _api:
        card_json = _build_answered_card(intern_name, tool_name, answers, source)
        err = _api.update_interactive_card(message_id, card_json)
        if err:
            log.warning(f"[QUESTION] Failed to update card for {intern_name}: {err}")
        else:
            log.info(f"[QUESTION] Updated card for {intern_name} to answered state")


def _build_timeout_card(intern_name, tool_name, hours):
    """Build a read-only card showing the question has timed out."""
    if tool_name == "ExitPlanMode":
        title = f"📋 {intern_name} 的方案 — ⏰ 已超时"
    elif tool_name == "request_user_input":
        title = f"❓ {intern_name}（Codex）的问题 — ⏰ 已超时"
    else:
        title = f"❓ {intern_name} 的问题 — ⏰ 已超时"

    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"⏰ **{hours} 小时内未收到回复，问题已超时**\n\n请到 tmux 终端查看 intern 当前状态，或重新发起问题。"}},
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "该卡片已失效，点击不会再提交回答"}]},
    ]
    return {
        "header": {"template": "red", "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def _update_question_card_to_timeout(intern_name, hours):
    """Update the Feishu card to show timeout state (does NOT pop pending entry)."""
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
    if not entry:
        return
    message_id = entry.get("message_id")
    tool_name = entry.get("tool_name", "AskUserQuestion")
    if message_id and _api:
        card_json = _build_timeout_card(intern_name, tool_name, hours)
        err = _api.update_interactive_card(message_id, card_json)
        if err:
            log.warning(f"[QUESTION] Failed to update card to timeout for {intern_name}: {err}")
        else:
            log.info(f"[QUESTION] Updated card for {intern_name} to timeout state ({hours}h)")


def _format_question_feishu(intern_name, tool_name, questions):
    """将 AskUserQuestion / ExitPlanMode 的问题格式化为飞书消息文本。"""
    lines = []
    if tool_name == "ExitPlanMode":
        lines.append(f"📋 {intern_name} 的方案已完成规划，请选择执行方式：")
    elif tool_name == "request_user_input":
        lines.append(f"❓ {intern_name}（Codex）有问题需要确认：")
    else:
        lines.append(f"❓ {intern_name} 有问题需要确认：")
    lines.append("")

    for i, q in enumerate(questions, 1):
        header = q.get("header", "")
        question = q.get("question", "")
        options = q.get("options", [])

        if header:
            lines.append(f"【{header}】{question}")
        else:
            lines.append(f"{i}. {question}")

        if options:
            for j, opt in enumerate(options, 1):
                label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
                desc = opt.get("description", "") if isinstance(opt, dict) else ""
                recommended = opt.get("recommended", False) if isinstance(opt, dict) else False
                mark = " ⭐推荐" if recommended else ""
                if desc:
                    lines.append(f"  {j}. {label} — {desc}{mark}")
                else:
                    lines.append(f"  {j}. {label}{mark}")
            # 自由输入选项
            lines.append(f"  {len(options) + 1}. 自由输入（请用 /answer 写内容）")
        lines.append("")

    lines.append("💡 回复方式：")
    if len(questions) == 1 and questions[0].get("options"):
        lines.append("  - 回复 /answer 1 选择选项")
        lines.append("  - 或回复 /answer <自由内容>")
    elif len(questions) > 1:
        lines.append("  - 逐题回复，格式: /answer 1:2 2:是的")
        lines.append("  - 或回复 /answer <自由内容> 作为统一回复")
    else:
        lines.append("  - 回复 /answer <回复内容>")

    return "\n".join(lines)


_ANSWER_COMMAND_RE = re.compile(r"^/answer(?:\s+|\n)(.*)$", re.IGNORECASE | re.DOTALL)


def _extract_explicit_answer_text(text):
    stripped = (text or "").strip()
    if stripped.lower() == "/answer":
        return True, ""
    match = _ANSWER_COMMAND_RE.match(stripped)
    if match:
        return True, match.group(1).strip()
    return False, ""


def _try_answer_pending_question(intern_name, text):
    """尝试用飞书消息回答 pending question。

    Returns True if consumed, False if no pending question.
    Handles parse failure by sending retry hint via feishu.
    """
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
    if not entry or entry["answer"] is not None:
        return False

    questions = entry["questions"]
    is_answer, answer_text = _extract_explicit_answer_text(text)
    if not is_answer:
        return False
    if not answer_text:
        log.warning(f"[QUESTION] Empty /answer for {intern_name}")
        retry_text = "⚠️ 回复为空：请使用 /answer <内容>，或点击卡片按钮/表单提交。"
        chat_id = _registry.find_chat_id(intern_name) if _registry else None
        if chat_id and _api:
            _api.send_message(chat_id, retry_text)
        return True

    try:
        answers = _parse_answer(questions, answer_text)
    except ValueError as e:
        # 解析失败 → 发送提示让主管重新回复
        error_msg = str(e)
        log.warning(f"[QUESTION] Parse failed for {intern_name}: {error_msg}")
        retry_text = f"⚠️ 回复格式有误：{error_msg}\n请重新回复。"
        chat_id = _registry.find_chat_id(intern_name) if _registry else None
        if chat_id and _api:
            _api.send_message(chat_id, retry_text)
        return True  # consumed the message, but not answered yet — wait for retry

    # 成功解析
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
        if entry:
            entry["answer"] = answers

    log.info(f"[QUESTION] Answered for {intern_name}: {answers}")
    _update_question_card(intern_name, answers, "飞书消息")
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
        if entry:
            entry["event"].set()
    return True


def _parse_answer(questions, text):
    """解析主管的回复文本为 answers dict。

    Raises ValueError on parse failure with human-readable reason.
    """
    answers = {}

    if len(questions) == 1:
        q = questions[0]
        question_key = q.get("question", q.get("header", "Q1"))
        options = q.get("options", [])

        if options:
            # 尝试数字选择
            try:
                choice = int(text)
                if 1 <= choice <= len(options):
                    opt = options[choice - 1]
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    answers[question_key] = label
                    return answers
                elif choice == len(options) + 1:
                    # 自由输入选项 — 需要更多文本
                    raise ValueError(f"你选了「自由输入」，请直接输入内容（不要只写数字 {choice}）")
                else:
                    raise ValueError(f"请输入 1-{len(options) + 1} 之间的数字")
            except ValueError as ve:
                if "请输入" in str(ve) or "自由输入" in str(ve):
                    raise
                # 不是纯数字 → 当自由文本
                pass

        # 自由文本回复
        answers[question_key] = text
        return answers

    # 多题模式
    # 尝试解析 "题号:答案" 格式
    parts = re.split(r'\s+', text)
    parsed_any = False
    for part in parts:
        if ':' in part or '：' in part:
            sep = ':' if ':' in part else '：'
            idx_str, ans_str = part.split(sep, 1)
            try:
                idx = int(idx_str.strip()) - 1
                if 0 <= idx < len(questions):
                    q = questions[idx]
                    question_key = q.get("question", q.get("header", f"Q{idx+1}"))
                    options = q.get("options", [])
                    if options:
                        try:
                            choice = int(ans_str.strip())
                            if 1 <= choice <= len(options):
                                opt = options[choice - 1]
                                label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                                answers[question_key] = label
                                parsed_any = True
                                continue
                        except ValueError:
                            pass
                    answers[question_key] = ans_str.strip()
                    parsed_any = True
            except ValueError:
                pass

    if parsed_any:
        # 填充未回答的题目默认值
        for i, q in enumerate(questions):
            question_key = q.get("question", q.get("header", f"Q{i+1}"))
            if question_key not in answers:
                answers[question_key] = ""  # 空字符串表示未回答
        return answers

    # 无法解析为结构化格式 → 当统一自由回复（所有题共用）
    for q in questions:
        question_key = q.get("question", q.get("header", "Q"))
        answers[question_key] = text

    return answers


def _register_pending_question(intern_name, tool_name, questions, prelude_file_path="", metadata=None):
    """Send a Feishu question card and register the pending answer state."""
    if not intern_name or not questions:
        return 400, {"error": "intern_name and questions required"}

    # 发送到飞书（优先发送交互卡片，失败时 fallback 到文本）
    chat_id = _registry.find_chat_id(intern_name) if _registry else None
    msg_id = None
    if chat_id and _api:
        # 若有 prelude_file_path（例如 ExitPlanMode 把完整 plan 写到 md 临时文件），
        # 先上传 + 发文件到群。利用飞书对 md 的渲染，主管点开即可阅读完整 plan，
        # 不再用普通消息污染聊天窗口。
        if prelude_file_path:
            file_key, up_err = _api.upload_file(prelude_file_path)
            if up_err:
                log.warning(f"[QUESTION] prelude file upload failed for {intern_name}: {up_err}")
            else:
                _, send_err = _api.send_file(chat_id, file_key)
                if send_err:
                    log.warning(f"[QUESTION] prelude file send failed for {intern_name}: {send_err}")
                else:
                    log.info(f"[QUESTION] prelude file sent for {intern_name}: {prelude_file_path}")
        # 尝试发送交互卡片
        card_json = _format_question_card(intern_name, tool_name, questions)
        msg_id, err = _api.send_interactive_card(chat_id, card_json)
        if err:
            log.warning(f"[QUESTION] Card send failed ({err}), falling back to text")
            feishu_text = _format_question_feishu(intern_name, tool_name, questions)
            msg_id, err = _api.send_message(chat_id, feishu_text)
            if err:
                log.warning(f"[QUESTION] Text fallback also failed: {err}")
        else:
            log.info(f"[QUESTION] Interactive card sent to {intern_name}, msg_id={msg_id}")

    codex_tui = (metadata or {}).get("codex_tui") if isinstance(metadata, dict) else None

    # 注册 pending（如果已有旧 pending，先更新旧卡片）
    with _pq_lock:
        old_entry = _pending_questions.get(intern_name)
        if old_entry and old_entry.get("message_id") and _api:
            old_msg_id = old_entry["message_id"]
            old_tool = old_entry.get("tool_name", "AskUserQuestion")
            supersede_card = _build_answered_card(intern_name, old_tool, {"_superseded": "已被新问题取代"}, "系统")
            err = _api.update_interactive_card(old_msg_id, supersede_card)
            if err:
                log.warning(f"[QUESTION] Failed to supersede old card for {intern_name}: {err}")
            else:
                log.info(f"[QUESTION] Superseded old card for {intern_name}")
        _pending_questions[intern_name] = {
            "questions": questions,
            "tool_name": tool_name,
            "answer": None,
            "event": _q_threading.Event(),
            "message_id": msg_id,
            "codex_tui": codex_tui,
        }

    log.info(f"[QUESTION] Registered pending question for {intern_name} ({tool_name})")
    if codex_tui:
        call_id = codex_tui.get("call_id", "")
        threading.Thread(
            target=_await_codex_tui_question_answer,
            args=(intern_name, call_id),
            daemon=True,
        ).start()
    return 200, {"ok": True, "message_id": msg_id}


def _codex_question_key(question):
    return question.get("question", question.get("header", "Q"))


def _codex_option_labels(question):
    labels = []
    for opt in question.get("options", []) or []:
        labels.append(opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt))
    return labels


def _send_codex_tui_answer(intern_name, questions, answers):
    """Submit Feishu answers into Codex 0.130 native request_user_input TUI."""
    if not _check_tmux_session(intern_name):
        return False, "tmux session not found"
    if not _is_codex_process_running(intern_name):
        return False, "Codex has exited"

    target = f"={intern_name}:"
    try:
        for question in questions:
            key = _codex_question_key(question)
            answer = answers.get(key, "")
            if isinstance(answer, list):
                answer = answer[0] if answer else ""
            answer = str(answer)
            labels = _codex_option_labels(question)

            if answer in labels:
                idx = labels.index(answer)
                if idx > 0:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", target, *("Down" for _ in range(idx))],
                        check=True,
                        capture_output=True,
                    )
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, capture_output=True)
            else:
                # Codex adds a final "None of the above" option with notes. Use it
                # for custom text answers that do not match an explicit option.
                if labels:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", target, *("Down" for _ in range(len(labels))), "Tab"],
                        check=True,
                        capture_output=True,
                    )
                if answer:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", target, "-l", "--", answer],
                        check=True,
                        capture_output=True,
                    )
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, capture_output=True)
            time.sleep(0.25)
        log.info(f"[CODEX_RUI] Submitted TUI answer for {intern_name}")
        return True, None
    except subprocess.CalledProcessError as e:
        return False, str(e)


def _await_codex_tui_question_answer(intern_name, call_id):
    with _pq_lock:
        entry = _pending_questions.get(intern_name)
        if not entry or not entry.get("codex_tui") or entry["codex_tui"].get("call_id") != call_id:
            return
        event = entry["event"]

    if not event.wait(timeout=_CODEX_RUI_WATCH_TIMEOUT):
        log.warning(f"[CODEX_RUI] Timeout waiting Feishu answer for {intern_name} call_id={call_id}")
        _update_question_card_to_timeout(intern_name, _CODEX_RUI_WATCH_TIMEOUT // 3600)
        with _pq_lock:
            current = _pending_questions.get(intern_name)
            if current and current.get("codex_tui", {}).get("call_id") == call_id:
                _pending_questions.pop(intern_name, None)
        return

    with _pq_lock:
        entry = _pending_questions.get(intern_name)
        if not entry or not entry.get("codex_tui") or entry["codex_tui"].get("call_id") != call_id:
            return
        questions = entry.get("questions", [])
        answers = entry.get("answer") or {}

    success, err = _send_codex_tui_answer(intern_name, questions, answers)
    if not success:
        log.warning(f"[CODEX_RUI] Failed to submit TUI answer for {intern_name}: {err}")
        chat_id = _registry.find_chat_id(intern_name) if _registry else None
        if chat_id and _api:
            _api.send_message(chat_id, f"⚠️ 已收到回答，但回填 Codex TUI 失败：{err}")
        return

    with _pq_lock:
        current = _pending_questions.get(intern_name)
        if current and current.get("codex_tui", {}).get("call_id") == call_id:
            _pending_questions.pop(intern_name, None)


def _handle_codex_request_user_input_call(intern_name, transcript_path, payload):
    call_id = payload.get("call_id", "")
    if not call_id:
        return

    seen_key = (intern_name, transcript_path, call_id)
    with _codex_rui_lock:
        if seen_key in _codex_rui_seen_calls:
            return
        _codex_rui_seen_calls.add(seen_key)

    try:
        args = json.loads(payload.get("arguments") or "{}")
    except json.JSONDecodeError as e:
        log.warning(f"[CODEX_RUI] Invalid request_user_input args for {intern_name}: {e}")
        return

    questions = args.get("questions", [])
    if not isinstance(questions, list) or not questions:
        log.warning(f"[CODEX_RUI] Empty request_user_input questions for {intern_name}")
        return

    status, resp = _register_pending_question(
        intern_name,
        "request_user_input",
        questions,
        metadata={"codex_tui": {"call_id": call_id, "transcript_path": transcript_path}},
    )
    if status != 200:
        log.warning(f"[CODEX_RUI] Failed to register Feishu question for {intern_name}: {resp}")
    else:
        log.info(f"[CODEX_RUI] Feishu question registered for {intern_name} call_id={call_id}")


def _codex_request_user_input_watch_loop(intern_name, transcript_path, start_offset):
    log.info(f"[CODEX_RUI] Watcher start intern={intern_name} path={transcript_path} offset={start_offset}")
    offset = max(0, int(start_offset or 0))
    deadline = time.time() + _CODEX_RUI_WATCH_TIMEOUT

    while time.time() < deadline:
        try:
            if not os.path.exists(transcript_path):
                time.sleep(0.5)
                continue
            size = os.path.getsize(transcript_path)
            if size < offset:
                offset = 0
            if size == offset:
                time.sleep(0.5)
                continue
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                lines = f.readlines()
                offset = f.tell()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "response_item":
                    continue
                payload = item.get("payload") or {}
                if payload.get("type") == "function_call" and payload.get("name") == "request_user_input":
                    _handle_codex_request_user_input_call(intern_name, transcript_path, payload)
        except Exception as e:
            log.warning(f"[CODEX_RUI] Watcher error for {intern_name}: {e}", exc_info=True)
            time.sleep(1)

    log.info(f"[CODEX_RUI] Watcher stop intern={intern_name} path={transcript_path}")


def _register_codex_request_user_input_watcher(intern_name, transcript_path, start_offset=0):
    if not intern_name or not transcript_path:
        return 400, {"error": "intern_name and transcript_path required"}
    if _get_intern_type(intern_name) != "codex":
        return 200, {"ok": True, "skipped": "not_codex"}

    key = (intern_name, transcript_path)
    with _codex_rui_lock:
        existing = _codex_rui_watchers.get(key)
        if existing and existing.is_alive():
            return 200, {"ok": True, "already_running": True}
        t = threading.Thread(
            target=_codex_request_user_input_watch_loop,
            args=(intern_name, transcript_path, start_offset),
            daemon=True,
        )
        _codex_rui_watchers[key] = t
        t.start()
    return 200, {"ok": True}


class DaemonHTTPServer(ThreadingHTTPServer):
    # Python's TCPServer defaults request_queue_size=5 → socket.listen(5). On
    # VS Code window reload the extension fires 3–5 concurrent /api/status
    # probes (hash check + reachability poll + plugin status); multi-window
    # reload easily crosses 5 simultaneous SYNs and the kernel starts dropping
    # them, forcing 1s TCP SYN retransmit that blows past the extension's
    # 1.5s/3s timeouts and triggers a spurious daemon kill-and-restart.
    request_queue_size = 128


def _relay_http_base_required():
    if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
        raise RuntimeError("relay not connected")
    return _relay_client._relay_http_base


def _relay_workspace_request(method, path, payload=None, timeout=15):
    base = _relay_http_base_required()
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {"error": raw}
        return int(exc.code), body


class APIHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, format, *args):
        log.info(f"[HTTP] {format % args}")

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json_response(400, {"error": "invalid Content-Length"})
            return None
        if length > 1024 * 1024:
            self._json_response(413, {"error": "request body too large"})
            return None
        if length > 0:
            try:
                raw = self.rfile.read(length)
            except socket.timeout:
                self._json_response(408, {"error": "request body timeout"})
                return None
            if len(raw) != length:
                self._json_response(400, {"error": "incomplete request body"})
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                self._json_response(400, {"error": "invalid JSON body"})
                return None
        return {}

    def _workspace_path_parts(self):
        parsed = urllib.parse.urlparse(self.path)
        return [part for part in parsed.path.split("/") if part], urllib.parse.parse_qs(parsed.query)

    def _sync_workspaces_from_relay(self):
        if _workspace_cache is None:
            raise RuntimeError("workspace cache unavailable")
        status, payload = _relay_workspace_request("GET", "/api/workspaces")
        if status >= 400:
            raise RuntimeError(payload.get("error") or f"relay workspace sync failed: HTTP {status}")
        return _workspace_cache.sync_from_relay_payload(payload)

    def _handle_workspace_get(self):
        if _workspace_cache is None:
            return self._json_response(503, {"error": "workspace cache unavailable"})
        parts, _ = self._workspace_path_parts()
        if parts == ["api", "workspaces"]:
            try:
                self._sync_workspaces_from_relay()
            except Exception as e:
                log.warning(f"[WORKSPACE] relay sync before list failed: {e}")
            return self._json_response(200, _workspace_cache.list())
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "doctor":
            try:
                return self._json_response(200, _workspace_cache.doctor(parts[2]))
            except KeyError as e:
                return self._json_response(404, {"error": str(e)})
        return self._json_response(404, {"error": "not found"})

    def _handle_workspace_post(self, body):
        if _workspace_cache is None:
            return self._json_response(503, {"error": "workspace cache unavailable"})
        parts, _ = self._workspace_path_parts()
        try:
            if parts == ["api", "workspaces"]:
                status, payload = _relay_workspace_request("POST", "/api/workspaces", body, timeout=30)
                if status < 400:
                    self._sync_workspaces_from_relay()
                return self._json_response(status, payload)
            if parts == ["api", "workspaces", "sync"]:
                return self._json_response(200, self._sync_workspaces_from_relay())
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "enable":
                self._sync_workspaces_from_relay()
                return self._json_response(200, _workspace_cache.enable(parts[2], body.get("local_path") or None))
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "disable":
                return self._json_response(200, _workspace_cache.disable(parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "doctor":
                return self._json_response(200, _workspace_cache.doctor(parts[2]))
            if len(parts) == 5 and parts[:2] == ["api", "workspaces"] and parts[3:5] == ["mode", "validate"]:
                status, payload = _relay_workspace_request(
                    "POST", f"/api/workspaces/{urllib.parse.quote(parts[2])}/mode/validate", body)
                local_report = _local_workspace_mode_switch_guard(
                    parts[2], body.get("mode") or body.get("metadata_mode") or "")
                return self._json_response(status, _merge_workspace_mode_reports(payload, local_report))
            if len(parts) == 5 and parts[:2] == ["api", "workspaces"] and parts[3:5] == ["mode", "set"]:
                local_report = _local_workspace_mode_switch_guard(
                    parts[2], body.get("mode") or body.get("metadata_mode") or "")
                if not local_report.get("available"):
                    return self._json_response(409, local_report)
                status, payload = _relay_workspace_request(
                    "POST", f"/api/workspaces/{urllib.parse.quote(parts[2])}/mode/set", body)
                if status < 400:
                    self._sync_workspaces_from_relay()
                    refresh_report = _refresh_idle_workspace_resolvers(parts[2])
                    if isinstance(payload, dict):
                        payload["local_resolver_refresh"] = refresh_report
                        if refresh_report.get("errors"):
                            payload["warnings"] = list(payload.get("warnings") or []) + refresh_report["errors"]
                return self._json_response(status, payload)
        except KeyError as e:
            return self._json_response(404, {"error": str(e)})
        except Exception as e:
            return self._json_response(503, {"error": str(e)})
        return self._json_response(404, {"error": "not found"})

    def _handle_workspace_delete(self):
        if _workspace_cache is None:
            return self._json_response(503, {"error": "workspace cache unavailable"})
        parts, _ = self._workspace_path_parts()
        if len(parts) == 3 and parts[:2] == ["api", "workspaces"]:
            try:
                status, payload = _relay_workspace_request("DELETE", f"/api/workspaces/{urllib.parse.quote(parts[2])}")
                if status < 400:
                    self._sync_workspaces_from_relay()
                return self._json_response(status, payload)
            except Exception as e:
                return self._json_response(503, {"error": str(e)})
        return self._json_response(404, {"error": "not found"})

    def _handle_peer_send(self, body):
        """task213: intern A → intern B peer message.

        Synchronous delivery confirmation only (status: delivered|undeliverable);
        B's reply is asynchronous (reverse call to the same endpoint). See
        intern-cli/builtin/peer_send.md for the LLM behavior contract.
        """
        from_name = body.get("from_intern_name", "")
        to_name = body.get("to_intern_name", "")
        to_project = body.get("to_project") or ""
        content = body.get("content", "")
        attachments = body.get("attachments") or []
        mode = body.get("mode") or "default"

        if not from_name or not to_name:
            return self._json_response(400, {"error": "missing_field"})
        if not isinstance(content, str):
            return self._json_response(400, {"error": "missing_field"})
        if not isinstance(mode, str) or mode not in _PEER_DELIVERY_MODES:
            return self._json_response(400, {"error": "invalid_mode"})

        from_project = _get_intern_project(from_name) or ""
        if not (_registry.find_chat_id(from_name, project=from_project) or _registry.find_chat_id(from_name)):
            return self._json_response(400, {"error": "invalid_from"})

        # /esc and mode=stop are control commands that bypass content size/empty
        # checks (their purpose is to interrupt; message text is irrelevant).
        if content != "/esc" and mode != "stop":
            if content == "":
                return self._json_response(400, {"error": "content_empty"})
            if len(content.encode("utf-8")) > 4096:
                return self._json_response(400, {"error": "content_too_long"})

        resolved_project = to_project
        if not resolved_project:
            if not (_relay_client and _relay_client.connected):
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            candidates = _relay_client.resolve_peer_target(to_name, timeout=5)
            if candidates is None:
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            if len(candidates) == 0:
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "unknown_target"})
            if len(candidates) > 1:
                return self._json_response(200, {
                    "status": "undeliverable",
                    "reason": "ambiguous_target",
                    "candidates": candidates,
                })
            resolved_project = candidates[0]["project"]

        if resolved_project == from_project and to_name == from_name:
            return self._json_response(400, {"error": "self_send"})

        msg_id = uuid.uuid4().hex
        payload = {
            "from_intern_name": from_name,
            "from_project": from_project,
            "to_intern_name": to_name,
            "to_project": resolved_project,
            "content": content,
            "mode": mode,
            "msg_id": msg_id,
        }
        _attach_local_sender_contract(payload)
        if attachments:
            payload["attachments"] = attachments

        # Keep the same-machine fast path, but only when this daemon really
        # owns the target. Local chat registry files are just Feishu chat
        # mappings and may contain stale/imported entries.
        same_machine = _owns_local_peer_target(to_name, resolved_project)

        if mode == "goal" and (resolved_project != from_project or not same_machine):
            return self._json_response(200, {
                "status": "undeliverable",
                "reason": "goal_same_daemon_project_required",
            })

        if same_machine:
            result = _deliver_peer_locally(payload)
        else:
            if not (_relay_client and _relay_client.connected):
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            result = _relay_client.forward_peer_message(payload, timeout=10)

        # task261: 仅 target_outdated 触发飞书 systemMessage（主管诉求"明确感知"
        # 边界 align 结果）。其他 reason 由 LLM 自行处理避免噪音。same-machine
        # 路径 target_mid 是 A 自己，永远 has_capability=True 不会触发此分支。
        if result.get("reason") == "target_outdated":
            _notify_peer_target_outdated(from_name, to_name, resolved_project)

        result = _augment_delivery_health_response(result, to_name, same_machine)
        return self._json_response(200, result)

    def _handle_goal_api(self, body, path_action=None):
        """task320: direct same-daemon intern goal API with explicit cancel support."""
        from_name = body.get("from_intern_name", "")
        to_name = body.get("to_intern_name", "")
        to_project = body.get("to_project") or ""
        from_project = body.get("from_project") or ""
        body_action = body.get("action") or ""
        action = path_action or body_action or "set"
        content = body.get("content")
        if content is None:
            content = body.get("objective", "")

        if not from_name or not to_name:
            return self._json_response(400, {"error": "missing_field"})
        if body_action and path_action and body_action != path_action:
            return self._json_response(400, {"error": "invalid_action"})
        if not isinstance(action, str) or action not in _GOAL_API_ACTIONS:
            return self._json_response(400, {"error": "invalid_action"})
        if not isinstance(content, str):
            return self._json_response(400, {"error": "missing_field"})
        from_project = from_project or _get_intern_project(from_name) or ""
        if not (_registry.find_chat_id(from_name, project=from_project) or _registry.find_chat_id(from_name)):
            return self._json_response(400, {"error": "invalid_from"})
        if action != "cancel":
            if content == "":
                return self._json_response(400, {"error": "content_empty"})
            if len(content.encode("utf-8")) > 4096:
                return self._json_response(400, {"error": "content_too_long"})
        resolved_project = to_project
        if not resolved_project:
            if not (_relay_client and _relay_client.connected):
                result = {"status": "undeliverable", "reason": "relay_unreachable"}
                return self._json_response(_goal_api_http_status(result), result)
            candidates = _relay_client.resolve_peer_target(to_name, timeout=5)
            if candidates is None:
                result = {"status": "undeliverable", "reason": "relay_unreachable"}
                return self._json_response(_goal_api_http_status(result), result)
            if len(candidates) == 0:
                result = {"status": "undeliverable", "reason": "unknown_target"}
                return self._json_response(_goal_api_http_status(result), result)
            if len(candidates) > 1:
                result = {
                    "status": "undeliverable",
                    "reason": "ambiguous_target",
                    "candidates": candidates,
                }
                return self._json_response(_goal_api_http_status(result), result)
            resolved_project = candidates[0]["project"]

        if resolved_project == from_project and to_name == from_name:
            return self._json_response(400, {"error": "self_send"})

        goal_id = body.get("goal_id") or body.get("client_goal_id") or uuid.uuid4().hex
        payload = {
            "from_intern_name": from_name,
            "from_project": from_project,
            "to_intern_name": to_name,
            "to_project": resolved_project,
            "content": content,
            "action": action,
            "goal_id": goal_id,
            "msg_id": goal_id,
        }
        _attach_local_sender_contract(payload)
        same_machine = _owns_local_peer_target(to_name, resolved_project)
        if not same_machine and payload.get("from_role") == _INDEPENDENT_ROLE:
            result = {
                "status": "undeliverable",
                "reason": "goal_independent_same_daemon_required",
            }
            return self._json_response(_goal_api_http_status(result), result)
        if same_machine:
            result = _deliver_goal_locally(payload)
        else:
            if not (_relay_client and _relay_client.connected):
                result = {"status": "undeliverable", "reason": "relay_unreachable"}
                return self._json_response(_goal_api_http_status(result), result)
            result = _relay_client.forward_goal_command(payload, timeout=10)
        if result.get("reason") == "target_outdated":
            _notify_peer_target_outdated(from_name, to_name, resolved_project)
        result = _augment_delivery_health_response(result, to_name, same_machine)
        result = _augment_goal_unconfirmed_response(result)
        return self._json_response(_goal_api_http_status(result), result)

    def _mailbox_error_response(self, exc):
        reason = str(exc)
        if reason in {"content_empty", "content_too_long", "message_ids_required"}:
            return self._json_response(400, {"error": reason})
        if reason in {
            "invalid_intern_name",
            "invalid_team_id",
            "unknown_team",
            "not_managed_worker",
            "ambiguous_team",
        }:
            return self._json_response(200, {"status": "undeliverable", "reason": reason})
        return self._json_response(500, {"error": reason})

    def _handle_mailbox_send(self, body):
        from_name = body.get("from_intern_name", "")
        to_name = body.get("to_intern_name", "")
        to_project = body.get("to_project") or ""
        content = body.get("content", "")
        team_id = body.get("team_id") or ""
        if not from_name or not to_name:
            return self._json_response(400, {"error": "missing_field"})
        if not isinstance(content, str):
            return self._json_response(400, {"error": "missing_field"})
        from_project = body.get("from_project") or _get_intern_project(from_name) or ""
        if not (_registry.find_chat_id(from_name, project=from_project) or _registry.find_chat_id(from_name)):
            return self._json_response(400, {"error": "invalid_from"})
        resolved_project = to_project
        if not resolved_project:
            if not (_relay_client and _relay_client.connected):
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            candidates = _relay_client.resolve_peer_target(to_name, timeout=5)
            if candidates is None:
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            if len(candidates) == 0:
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "unknown_target"})
            if len(candidates) > 1:
                return self._json_response(200, {
                    "status": "undeliverable",
                    "reason": "ambiguous_target",
                    "candidates": candidates,
                })
            resolved_project = candidates[0]["project"]
        payload = {
            "from_intern_name": from_name,
            "from_project": from_project,
            "to_intern_name": to_name,
            "to_project": resolved_project,
            "team_id": team_id,
            "kind": body.get("kind") or "progress",
            "content": content,
            "related_task": body.get("related_task") or "",
            "related_pr": body.get("related_pr") or "",
            "client_message_id": body.get("client_message_id") or "",
        }
        if _owns_local_mail_target(to_name, resolved_project):
            result = _deliver_mail_locally(payload)
        else:
            if not (_relay_client and _relay_client.connected):
                return self._json_response(200, {
                    "status": "undeliverable", "reason": "relay_unreachable"})
            result = _relay_client.forward_mail_message(payload, timeout=10)
        return self._json_response(200, result)

    def _handle_mailbox_list(self, body):
        if self.path.startswith("/api/intern/mailbox/"):
            mailbox_intern_name = body.get("intern_name") or ""
        else:
            mailbox_intern_name = body.get("to_intern_name") or body.get("team_lead_name") or body.get("intern_name") or ""
        project = body.get("to_project") or body.get("project") or ""
        include_read = bool(body.get("include_read", False))
        if not mailbox_intern_name or not project:
            return self._json_response(400, {"error": "missing_field"})
        try:
            messages = team_mailbox.list_messages(
                project=project,
                intern_name=mailbox_intern_name,
                include_read=include_read,
            )
        except (ValueError, OSError) as exc:
            return self._mailbox_error_response(exc)
        return self._json_response(200, {
            "status": "ok",
            "messages": messages,
            "unread_count": len([message for message in messages if not message.get("read")]),
        })

    def _handle_mailbox_mark_read(self, body):
        if self.path.startswith("/api/intern/mailbox/"):
            mailbox_intern_name = body.get("intern_name") or ""
        else:
            mailbox_intern_name = body.get("to_intern_name") or body.get("team_lead_name") or body.get("intern_name") or ""
        project = body.get("to_project") or body.get("project") or ""
        message_ids = body.get("message_ids")
        if message_ids is None and body.get("message_id"):
            message_ids = [body.get("message_id")]
        if not mailbox_intern_name or not project:
            return self._json_response(400, {"error": "missing_field"})
        if not isinstance(message_ids, list) or not all(isinstance(item, str) for item in message_ids):
            return self._json_response(400, {"error": "message_ids_required"})
        try:
            marked = team_mailbox.mark_read(
                project=project,
                intern_name=mailbox_intern_name,
                message_ids=message_ids,
            )
        except (ValueError, OSError) as exc:
            return self._mailbox_error_response(exc)
        return self._json_response(200, {
            "status": "ok",
            "marked_read": marked,
            "marked_count": len(marked),
        })

    def do_GET(self):
        if self.path.startswith("/api/workspaces"):
            self._handle_workspace_get()
            return
        if self.path == "/api/status":
            status = {
                "running": True,
                "version": __version__,
                "script_hash": _script_hash,
                "uptime": time.time(),
                "registry_count": len(_registry.get_all()),
                "ws_clients": len(_ws_server.clients),
                "mode": "relay",
                "relay_connected": _relay_client.connected if _relay_client else False,
                "work_agents_root": WORK_AGENTS_ROOT,
                "instance_id": _relay_client.machine_id if _relay_client else None,
            }
            self._json_response(200, status)
        elif self.path == "/api/group/list":
            self._json_response(200, [
                {
                    "intern_name": entry.get("intern_name"),
                    "project": entry.get("project", ""),
                    "chat_id": entry.get("chat_id"),
                }
                for entry in _registry.get_all_entries()
            ])
        elif self.path.startswith("/api/intern/check_online"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            intern_name = params.get("intern_name", [""])[0]
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            if _relay_client and _relay_client.connected:
                # Ask relay server
                result = _relay_client.check_online(intern_name, timeout=5)
                if result:
                    self._json_response(200, {
                        "intern_name": intern_name,
                        "online": result.get("online", False),
                        "machine_id": result.get("machine_id"),
                    })
                else:
                    self._json_response(503, {"error": "relay server timeout"})
            else:
                self._json_response(503, {"error": "relay not connected"})
        elif self.path.startswith("/api/chat/lookup"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            intern_name = params.get("intern", [""])[0]
            project = params.get("project", [""])[0]
            if not intern_name:
                return self._json_response(400, {"error": "intern param required"})
            # First check local registry
            chat_id = _registry.find_chat_id(intern_name, project=project)
            if chat_id:
                return self._json_response(200, {"intern_name": intern_name, "project": project, "chat_id": chat_id})
            # Fallback: ask relay
            if _relay_client and _relay_client.connected and _relay_client._relay_http_base:
                try:
                    resolved_project = project or _get_intern_project(intern_name)
                    query = urllib.parse.urlencode({"intern": intern_name, "project": resolved_project})
                    url = f"{_relay_client._relay_http_base}/api/chat/lookup?{query}"
                    resp = urllib.request.urlopen(url, timeout=5)
                    result = json.loads(resp.read())
                    chat_id = result.get("chat_id", "")
                    if chat_id:
                        _registry.register(intern_name, chat_id, project=resolved_project)
                    self._json_response(200, result)
                except Exception as e:
                    self._json_response(502, {"error": f"relay lookup failed: {e}"})
            else:
                self._json_response(200, {"intern_name": intern_name, "chat_id": ""})
        elif self.path.startswith("/api/question/poll"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            intern_name = params.get("intern_name", [""])[0]
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            with _pq_lock:
                entry = _pending_questions.get(intern_name)
            if not entry:
                return self._json_response(200, {"status": "none"})
            if entry["answer"] is not None:
                answer_data = entry["answer"]
                with _pq_lock:
                    _pending_questions.pop(intern_name, None)
                return self._json_response(200, {"status": "answered", "answers": answer_data})
            return self._json_response(200, {"status": "pending"})

        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return

        if self.path.startswith("/api/workspaces"):
            self._handle_workspace_post(body)
            return

        if self.path == "/api/group/create":
            intern_name = body.get("intern_name", "")
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            # Proxy to relay server for centralized chat management
            if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
                return self._json_response(503, {"error": "relay not connected"})
            try:
                intern_type = body.get("type") or _get_intern_type(intern_name)
                project = body.get("project") or _get_intern_project(intern_name)
                if not project:
                    return self._json_response(409, {"error": "project required for group create"})
                # Include owner identity from local _owner.json so relay uses
                # the requesting machine's configured owner.
                owner_mobile = _registry.load_owner_mobile() if _registry else None
                owner_open_id = _load_owner_open_id()
                payload = {"intern_name": intern_name, "type": intern_type, "project": project}
                if body.get("workspace_id"):
                    payload["workspace_id"] = body.get("workspace_id")
                if owner_mobile:
                    payload["owner_mobile"] = owner_mobile
                if owner_open_id:
                    payload["owner_open_id"] = owner_open_id
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    f"{_relay_client._relay_http_base}/api/chat/create",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=60)
                result = json.loads(resp.read())
                chat_id = result.get("chat_id", "")
                if chat_id:
                    response, err_response = _finalize_group_create(
                        intern_name,
                        chat_id,
                        owner_mobile,
                        result,
                        project=project,
                        owner_open_id=owner_open_id,
                    )
                    if err_response:
                        return self._json_response(500, err_response)
                    return self._json_response(200, response)
                return self._json_response(200, result)
            except Exception as e:
                log.error(f"Relay proxy /api/chat/create failed: {e}")
                response, err_response = _recover_group_create_after_proxy_error(
                    intern_name,
                    project,
                    owner_mobile,
                    e,
                    owner_open_id=owner_open_id,
                )
                if response:
                    return self._json_response(200, response)
                return self._json_response(502, err_response)

        elif self.path == "/api/group/delete":
            intern_name = body.get("intern_name", "")
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            # Proxy to relay server
            if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
                return self._json_response(503, {"error": "relay not connected"})
            try:
                project = body.get("project") or _get_intern_project(intern_name)
                if not project:
                    return self._json_response(409, {"error": "project required for group delete"})
                payload = {"intern_name": intern_name, "project": project}
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    f"{_relay_client._relay_http_base}/api/chat/delete",
                    data=data, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=15)
                result = json.loads(resp.read())
                _registry.unregister(intern_name, project=project)
                log.info(f"Deleted group for {intern_name} via relay")
                self._json_response(200, result)
            except Exception as e:
                log.error(f"Relay proxy /api/chat/delete failed: {e}")
                return self._json_response(502, {"error": f"relay proxy failed: {e}"})

        elif self.path == "/api/group/sync":
            chats = _api.list_chats()
            count = _registry.sync_from_chats(chats)
            log.info(f"Synced {count} from {len(chats)} chats")
            self._json_response(200, {"synced": count, "total": len(chats)})

        elif self.path == "/api/group/trigger_mode":
            # task252: VS Code 右键 → 切换该 intern 群的 trigger_mode（all|at_only）。
            # daemon 把 intern_name 配上 project 后代理到 relay /api/chat/trigger_mode。
            intern_name = body.get("intern_name", "")
            mode = body.get("mode", "")
            if not intern_name or not mode:
                return self._json_response(400, {"error": "intern_name and mode required"})
            if not (_relay_client and _relay_client.connected and _relay_client._relay_http_base):
                return self._json_response(503, {"error": "relay not connected"})
            try:
                project = _get_intern_project(intern_name)
                payload = {"intern_name": intern_name, "project": project, "mode": mode}
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    f"{_relay_client._relay_http_base}/api/chat/trigger_mode",
                    data=data, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=15)
                result = json.loads(resp.read())
                log.info(f"[TRIGGER] proxied /api/chat/trigger_mode for {intern_name}: {result}")
                self._json_response(200, result)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
                log.error(f"Relay proxy /api/chat/trigger_mode HTTP {e.code}: {err_body}")
                self._json_response(e.code, {"error": err_body})
            except Exception as e:
                log.error(f"Relay proxy /api/chat/trigger_mode failed: {e}")
                return self._json_response(502, {"error": f"relay proxy failed: {e}"})

        elif self.path == "/api/group/detail_mode":
            # task283: daemon owns detail_mode truth source (hook reads on this
            # machine, not relay's). VS Code 右键调本接口 → daemon 直接写本机
            # `$WORK_AGENTS_ROOT/.feishu_registry/_chat_config.json`，relay 不
            # 参与。task258 旧版会把请求 HTTP 转发到 relay `/api/chat/detail_mode`
            # ——那条路径在 relay-client 部署下导致写到远端 relay、本机 hook
            # 读不到（README 复现链路）。
            intern_name = body.get("intern_name", "")
            mode = body.get("mode", "")
            if not intern_name or not mode:
                return self._json_response(400, {"error": "intern_name and mode required"})
            if mode not in daemon_chat_config.valid_detail_modes():
                return self._json_response(400, {"error": (
                    f"invalid mode {mode!r}; must be one of "
                    f"{list(daemon_chat_config.valid_detail_modes())}")})
            chat_id = _registry.find_chat_id(intern_name) if _registry else None
            if not chat_id:
                return self._json_response(404, {"error": (
                    f"no chat for intern={intern_name!r}")})
            try:
                changed = daemon_chat_config.set_detail_mode(chat_id, mode)
            except Exception as e:
                log.error(f"[DETAIL] daemon-local set failed for {intern_name}: {e}", exc_info=True)
                return self._json_response(500, {"error": str(e)})
            log.info(f"[DETAIL] daemon-local /api/group/detail_mode "
                     f"intern={intern_name} chat={chat_id} mode={mode} changed={changed}")
            self._json_response(200, {"ok": True, "chat_id": chat_id,
                                       "mode": mode, "changed": changed})

        elif self.path == "/api/light/set":
            # 灯控完全由 WS 注册表驱动，HTTP 只是手动触发刷新
            _refresh_lights()
            self._json_response(200, {"ok": True})

        elif self.path == "/api/message/send":
            intern_name = body.get("intern_name", "")
            text = body.get("text", "")
            chat_id = _registry.find_chat_id(intern_name)
            if not chat_id:
                return self._json_response(404, {"error": f"no chat for {intern_name}"})
            msg_id, err = _api.send_message(chat_id, text)
            if err:
                return self._json_response(500, {"error": err})
            self._json_response(200, {"message_id": msg_id})

        elif self.path == "/api/message/update":
            msg_id = body.get("message_id", "")
            text = body.get("text", "")
            err = _api.update_message(msg_id, text)
            if err:
                return self._json_response(500, {"error": err})
            self._json_response(200, {"ok": True})

        elif self.path == "/api/message/finalize":
            msg_id = body.get("message_id", "")
            text = body.get("text", "")
            err = _api.update_message(msg_id, text)
            if err:
                return self._json_response(500, {"error": err})
            self._json_response(200, {"ok": True})

        elif self.path == "/api/message/reply":
            msg_id = body.get("message_id", "")
            text = body.get("text", "")
            err = _api.reply_message(msg_id, text)
            if err:
                return self._json_response(500, {"error": err})
            self._json_response(200, {"ok": True})

        elif self.path == "/api/intern/offline":
            intern_name = body.get("intern_name", "")
            project = body.get("project", "")
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            log.info(f"[HTTP] Intern offline notification: {intern_name}")
            if _relay_client and _relay_client.connected:
                _relay_client.send_intern_offline(intern_name, project=project)
            _notify_intern_status_changed(intern_name)
            self._json_response(200, {"ok": True})

        elif self.path == "/api/intern/request_refresh":
            # task223: Claude resume 等场景下，插件侧调这里触发一次 light 重新扫描。
            # daemon 不信任请求者声称的 online 状态，仅通过 _is_claude_process_running 扫 tmux pane 后再上报。
            intern_name = body.get("intern_name", "")
            project = body.get("project", "")
            log.info(f"[HTTP] Request light refresh (intern={intern_name or '-'})")
            threading.Thread(target=_refresh_lights_for_intern, args=(intern_name, project), daemon=True).start()
            self._json_response(200, {"ok": True})

        elif self.path == "/api/intern/status_changed":
            intern_name = body.get("intern_name", "")
            if not intern_name:
                return self._json_response(400, {"error": "intern_name required"})
            log.info(f"Intern status changed: {intern_name}")
            _ws_server.push({"type": "intern_status_changed", "intern_name": intern_name})
            self._json_response(200, {"ok": True})

        elif self.path in ("/api/intern/goal/set", "/api/intern/goal/cancel"):
            path_action = None
            if self.path.endswith("/set"):
                path_action = "set"
            elif self.path.endswith("/cancel"):
                path_action = "cancel"
            return self._handle_goal_api(body, path_action=path_action)

        elif self.path == "/api/helper/action":
            action = body.get("helper_action") or body.get("action") or ""
            machine_id = body.get("machine_id") or (_relay_client.machine_id if _relay_client else "")
            local_machine_id = _relay_client.machine_id if _relay_client else ""
            if not action:
                return self._json_response(400, {"ok": False, "error": "action required"})
            if not machine_id:
                return self._json_response(503, {"ok": False, "error": "local machine_id unavailable"})
            if local_machine_id and machine_id != local_machine_id:
                return self._json_response(400, {
                    "ok": False,
                    "error": "machine_id must match this daemon",
                    "local_machine_id": local_machine_id,
                })
            msg = dict(body)
            msg["helper_action"] = action
            msg["machine_id"] = machine_id
            try:
                result = handle_machine_helper_action(msg)
                result["ok"] = True
                return self._json_response(200, result)
            except Exception as e:
                log.error(f"[HELPER] local API action failed: {e}", exc_info=True)
                return self._json_response(500, {
                    "ok": False,
                    "error": str(e),
                    "helper_action": action,
                    "machine_id": machine_id,
                })

        elif self.path == "/api/team/mailbox/send":
            return self._handle_mailbox_send(body)

        elif self.path == "/api/intern/mail/to":
            return self._handle_mailbox_send(body)

        elif self.path == "/api/team/mailbox/list":
            return self._handle_mailbox_list(body)

        elif self.path == "/api/intern/mailbox/list":
            return self._handle_mailbox_list(body)

        elif self.path == "/api/team/mailbox/mark-read":
            return self._handle_mailbox_mark_read(body)

        elif self.path == "/api/intern/mailbox/mark-read":
            return self._handle_mailbox_mark_read(body)

        elif self.path == "/api/intern/peer/send":
            return self._handle_peer_send(body)

        elif self.path == "/api/question/ask":
            intern_name = body.get("intern_name", "")
            tool_name = body.get("tool_name", "AskUserQuestion")
            questions = body.get("questions", [])
            prelude_file_path = body.get("prelude_file_path", "")
            status, resp = _register_pending_question(intern_name, tool_name, questions, prelude_file_path)
            self._json_response(status, resp)

        elif self.path == "/api/codex/request_user_input/register":
            intern_name = body.get("intern_name", "")
            transcript_path = body.get("transcript_path", "")
            start_offset = body.get("offset", 0)
            status, resp = _register_codex_request_user_input_watcher(
                intern_name,
                transcript_path,
                start_offset,
            )
            self._json_response(status, resp)

        elif self.path == "/api/question/cancel":
            intern_name = body.get("intern_name", "")
            with _pq_lock:
                _pending_questions.pop(intern_name, None)
            log.info(f"[QUESTION] Cancelled pending question for {intern_name}")
            self._json_response(200, {"ok": True})

        elif self.path == "/api/question/timeout":
            intern_name = body.get("intern_name", "")
            hours = body.get("hours", 6)
            _update_question_card_to_timeout(intern_name, hours)
            with _pq_lock:
                _pending_questions.pop(intern_name, None)
            log.info(f"[QUESTION] Timeout notified for {intern_name} ({hours}h)")
            self._json_response(200, {"ok": True})

        elif self.path == "/api/message/send_to_owner":
            text = body.get("text", "")
            if not text:
                return self._json_response(400, {"error": "text required"})
            owner_open_id = _load_owner_open_id()
            owner_mobile = _registry.load_owner_mobile() if _registry else ""
            if not owner_open_id and not owner_mobile:
                return self._json_response(400, {"error": "owner identity missing in _owner.json"})
            if owner_open_id:
                open_id, err = owner_open_id, None
            else:
                open_id, err = _api.mobile_to_open_id(owner_mobile)
            if err or not open_id:
                return self._json_response(500, {"error": f"mobile_to_open_id failed: {err}"})
            msg_id, err = _api.send_to_user(open_id, text)
            if err:
                return self._json_response(500, {"error": f"send_to_user failed: {err}"})
            log.info("[HTTP] Sent message to owner")
            self._json_response(200, {"message_id": msg_id})

        elif self.path == "/api/shutdown":
            log.info("Shutdown requested via API")
            self._json_response(200, {"ok": True})
            threading.Thread(target=lambda: (_shutdown_event.set()), daemon=True).start()

        else:
            self._json_response(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/workspaces"):
            self._handle_workspace_delete()
            return
        self._json_response(404, {"error": "not found"})


# ══════════════════════════════════════════
# 飞书消息接收 + 路由
# ══════════════════════════════════════════

def parse_text(content, msg_type):
    try:
        data = json.loads(content)
        if msg_type == "text":
            return data.get("text", "").strip()
        elif msg_type == "post":
            texts = []
            # Feishu API v2 flat format: {"title":"...","content":[[{"tag":"text","text":"..."}]]}
            content_lines = data.get("content", [])
            if isinstance(content_lines, list) and content_lines and isinstance(content_lines[0], list):
                for line in content_lines:
                    for elem in line:
                        if isinstance(elem, dict) and elem.get("tag") == "text":
                            texts.append(elem.get("text", ""))
            else:
                # Legacy format: {"zh_cn": {"title":"...","content":[...]}}
                for lang_content in data.values():
                    if isinstance(lang_content, dict):
                        for line in lang_content.get("content", []):
                            for elem in line:
                                if elem.get("tag") == "text":
                                    texts.append(elem.get("text", ""))
            return " ".join(texts).strip()
    except Exception:
        pass
    return content.strip() if isinstance(content, str) else ""


def create_message_handler(api, registry, ws_server):
    start_time_ms = str(int(time.time() * 1000))

    def handle_message(data):
        try:
            msg = data.event.message
            sender = data.event.sender
            chat_id = msg.chat_id
            message_id = msg.message_id
            msg_type = msg.message_type
            content = msg.content

            if sender and sender.sender_type == "app":
                return

            # 忽略 daemon 启动前的旧消息（Lark SDK 可能在 WebSocket 连接时投递积压消息）
            create_time = getattr(msg, 'create_time', '') or ''
            if create_time and create_time < start_time_ms:
                log.info(f"Ignoring old message {message_id} (create_time={create_time} < start={start_time_ms})")
                return

            text = parse_text(content, msg_type)
            if not text:
                return

            intern_info = registry.find_intern_info(chat_id)
            intern_name = intern_info.get("intern_name", "")
            project = intern_info.get("project", "")
            if not intern_name:
                log.warning(f"No intern for chatId={chat_id}")
                return

            log.info(f"Feishu msg for {intern_name}: {text[:80]}")

            intern_type = _get_intern_type_scoped(intern_name, project=project)

            # ── 检查是否有 pending question 等待回答 ──
            if _try_answer_pending_question(intern_name, text):
                api.reply_message(message_id, f"✅ 已收到回复")
                return

            if _is_tmux_intern_type(intern_type):
                # Claude/Codex intern: route via tmux send-keys
                if intern_type == "codex":
                    success, err = _send_to_codex_tmux(intern_name, text, delivery_id=message_id)
                else:
                    success, err = _send_to_claude_tmux(intern_name, text, delivery_id=message_id)
                if success:
                    log.info(f"[ROUTE] Sent to {intern_type.capitalize()} intern {intern_name} via tmux")
                else:
                    if err in _TMUX_SUBMIT_UNCONFIRMED_ERRORS:
                        if _should_reply_tmux_unconfirmed(err):
                            api.reply_message(message_id, _format_tmux_unconfirmed_message(intern_name, err))
                        log.warning(f"[ROUTE] Codex submit unconfirmed for {intern_name}: {err}")
                        return
                    # tmux session not found / process not running → offline
                    api.reply_message(message_id, f"⚠️ {intern_name} 当前离线")
                    log.info(f"[ROUTE] {intern_type.capitalize()} intern {intern_name} offline: {err}")
                    target_chat = registry.find_chat_id(intern_name, project=project)
                    if target_chat:
                        api.update_chat(target_chat, name=_build_group_name(intern_name, is_online=False, project=project))
                    _notify_intern_status_changed(intern_name)
            else:
                # Copilot intern: route via WebSocket to VS Code plugin
                payload = {
                    "type": "feishu_message",
                    "intern_name": intern_name,
                    "project": project,
                    "text": text,
                    "message_id": message_id,
                    "chat_id": chat_id,
                }
                delivered = ws_server.route_to_active(intern_name, payload)
                if not delivered:
                    api.reply_message(message_id, f"⚠️ {intern_name} 当前不在线")
                    # Notify relay that Copilot went offline (align with Claude failure path)
                    if _relay_client and _relay_client.connected:
                        _relay_client.send_intern_offline(intern_name, project=project)
                    _notify_intern_status_changed(intern_name)
                    log.info(f"[ROUTE] {intern_name} not active in any window, sent offline")

        except Exception as e:
            log.error(f"Message handler error: {e}", exc_info=True)

    return handle_message


# ══════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════

def main():
    global _api, _registry, _workspace_cache, _ws_server, _shutdown_event, _relay_client

    log.info("=" * 60)
    log.info(f"Feishu Daemon v{__version__} starting...")

    # ── 诊断 hook：SIGUSR1 → 把所有线程 Python 栈打到 daemon log ──
    # 容器无 cap_sys_ptrace 时 py-spy/gdb 都用不了；这是唯一能在不重启进程的
    # 前提下取得运行时栈的办法。卡死时直接 `kill -USR1 <pid>`。
    # 同时启用 faulthandler 让 segfault 也能出栈。
    fault_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", "_daemon")
    os.makedirs(fault_dir, exist_ok=True)
    fault_log = open(os.path.join(fault_dir, "feishu_daemon_faults.log"), "a")
    faulthandler.enable(file=fault_log, all_threads=True)
    faulthandler.register(signal.SIGUSR1, file=fault_log, all_threads=True, chain=False)
    log.info(f"faulthandler registered: SIGUSR1 → {fault_log.name}")

    # 一次性迁移：如果老版本的文本 PID file 还在，unlink 它（不会被还原）
    if os.path.exists(OLD_PID_FILE):
        try:
            os.remove(OLD_PID_FILE)
            log.info(f"Removed legacy PID file: {OLD_PID_FILE}")
        except Exception as e:
            log.warning(f"Failed to remove legacy PID file {OLD_PID_FILE}: {e}")

    _shutdown_event = threading.Event()

    # 0. 加载 relay 配置（从 _owner.json）
    relay_cfg = load_relay_config()
    log.info(f"Mode: relay (url={relay_cfg['relay_url']}, instance_id={relay_cfg['machine_id']})")

    # 1. 凭据 + API
    credential_loader = lambda: fetch_credentials_from_relay(relay_cfg)
    app_id, app_secret = load_credentials(relay_cfg)
    _api = FeishuAPI(app_id, app_secret, credential_loader=credential_loader)
    log.info(f"Credentials: app_id={app_id[:8]}...")
    enrich_owner_identity_at_startup(_api)

    # 2. Registry
    registry_dir = os.path.join(WORK_AGENTS_ROOT, ".feishu_registry")
    _registry = RegistryManager(registry_dir)
    _workspace_cache = WorkspaceCache(WORK_AGENTS_ROOT)
    log.info(f"Registry: {len(_registry.get_all())} interns")

    # 3. 同步
    chats = _api.list_chats()
    if chats:
        count = _registry.sync_from_chats(chats)
        log.info(f"Synced {count} from {len(chats)} chats")

    # 4. WebSocket server (binds to ephemeral port; actual_port populated after start)
    _ws_server = WSServer(WS_PORT)
    _ws_server.start()

    # 5. HTTP server (bind to ephemeral port if HTTP_PORT == 0)
    http_server = DaemonHTTPServer(("localhost", HTTP_PORT), APIHandler)
    actual_http_port = http_server.server_address[1]
    actual_ws_port = _ws_server.actual_port
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    log.info(f"HTTP API on http://localhost:{actual_http_port}")

    # 现在两个服务均已 bind 到实际端口 → 写 PID file (JSON)
    # task267: bundle_dir = daemon 自身所在 bundled-cli 根目录（__file__ 上溯 3 层：
    # <install>/bundled-cli/scripts/daemon/feishu_daemon.py → <install>/bundled-cli）；
    # context_loader 等 consumer 用 `<bundle_dir>/builtin/peer_send.md` 拼路径
    # 指向与协议版本绑定的 doc，避免随 intern PR 漂移。
    _bundle_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pid_payload = {
        "pid": os.getpid(),
        "instance_id": relay_cfg["machine_id"],
        "work_agents_root": WORK_AGENTS_ROOT,
        "http_port": actual_http_port,
        "ws_port": actual_ws_port,
        "started_at": datetime.now().isoformat(),
        "script_hash": _script_hash,
        "version": __version__,
        "bundle_dir": _bundle_dir,
    }
    with open(PID_FILE, "w") as f:
        json.dump(pid_payload, f, indent=2)
    log.info(f"PID file written: {PID_FILE} (pid={os.getpid()}, http={actual_http_port}, ws={actual_ws_port})")

    # 6. 入站消息来源（relay 模式）
    _relay_client = RelayClient(
        relay_url=relay_cfg["relay_url"],
        relay_token=relay_cfg["relay_token"],
        machine_id=relay_cfg["machine_id"],
        registry=_registry,
        ws_server=_ws_server,
        owner_mobile=relay_cfg.get("owner_mobile", ""),
        owner_open_id=relay_cfg.get("owner_open_id", ""),
        ip=relay_cfg.get("ip", ""),
        ssh_port=relay_cfg.get("ssh_port", 22),
    )
    _relay_client.start()
    log.info(f"Relay client connecting to {relay_cfg['relay_url']} as '{relay_cfg['machine_id']}'")

    # 6b. 周期状态上报（interns_state，5s 一次，只更新 registry，不触发飞书灯控）
    threading.Thread(
        target=_report_interns_state,
        args=(_shutdown_event,),
        name="interns_state_reporter",
        daemon=True,
    ).start()
    log.info("interns_state reporter started (5s interval)")

    threading.Thread(
        target=_peer_next_queue_worker,
        args=(_shutdown_event,),
        name="peer_next_queue_worker",
        daemon=True,
    ).start()
    log.info("peer next queue worker started")

    # 7. 信号处理
    def signal_handler(sig, frame):
        log.info("Received signal, shutting down...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log.info("Daemon ready. Waiting for shutdown...")
    _shutdown_event.wait()

    # 清理：仅删除指向自己的 pid 文件，避免误删后起 daemon 写入的新文件
    http_server.shutdown()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                current = json.load(f)
            if current.get("pid") == os.getpid():
                os.remove(PID_FILE)
        except (json.JSONDecodeError, OSError):
            os.remove(PID_FILE)
    log.info("Daemon stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
