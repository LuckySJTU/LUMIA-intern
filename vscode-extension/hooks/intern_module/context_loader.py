"""
读取 intern workspace 文件并构建 prompt 各段落。
完全对齐 VS Code 插件的 promptBuilder.ts / workingPrompt.ts / idlePrompt.ts。
"""
import os
import re
import json
from common.utils import (
    get_intern_name, get_project_repo, get_workspace_dir,
    read_file_safe, parse_status_metadata, parse_session_metadata,
    truncate_history_log, WORK_AGENTS_ROOT, get_metadata_context,
    get_task_metadata_paths, load_state, is_machine_helper_state,
)
from common.i18n import get_locale


def load_intern_files(intern_dir):
    """读取所有 intern 相关文件，返回 dict。"""
    name = get_intern_name(intern_dir)
    hook_state = load_state(intern_dir)
    metadata_ctx = get_metadata_context(intern_dir)
    ws = metadata_ctx["metadata_root"]
    repo = metadata_ctx.get("repo_dir") or metadata_ctx.get("code_worktree_path") or get_project_repo(intern_dir)

    status_raw = read_file_safe(metadata_ctx["status_path"])
    meta = parse_status_metadata(status_raw)
    role = meta.get("role", "independent")
    if is_machine_helper_state(hook_state, name):
        role = "machine_helper"

    # 解析 PR 链接
    pr_match = re.search(r'PR\s*\|\s*(https?://\S+)', status_raw)
    pr_url = pr_match.group(1) if pr_match else ""
    pr_number = ""
    if pr_url:
        m = re.search(r'/(?:pull|change|merge_requests)/(\d+)', pr_url)
        pr_number = m.group(1) if m else ""

    # 检测当前项目 provider
    provider = _detect_provider(intern_dir, metadata_ctx)

    # task_id resolve：短名 → 目录全名
    task_id_raw = meta.get("task", "")
    task_id = task_id_raw
    tasks_dir = metadata_ctx["tasks_dir"]
    if task_id and os.path.isdir(tasks_dir) and not os.path.isdir(os.path.join(tasks_dir, task_id)):
        for d in os.listdir(tasks_dir):
            if d.startswith(task_id + "_") and os.path.isdir(os.path.join(tasks_dir, d)):
                task_id = d
                break

    task_paths = get_task_metadata_paths(intern_dir, task_id) if task_id else metadata_ctx

    files = {
        "intern_name": name,
        "intern_dir": intern_dir,
        "repo_dir": repo,
        "workspace_dir": ws,
        "metadata": metadata_ctx,
        "hook_state": hook_state,
        "metadata_mode": metadata_ctx["metadata_mode"],
        "workspace_id": metadata_ctx["workspace_id"],
        "workspace_key": metadata_ctx["workspace_key"],
        "metadata_root": metadata_ctx["metadata_root"],
        "metadata_branch": metadata_ctx["metadata_branch"],
        "default_branch": metadata_ctx["default_branch"],
        "runtime_provider": metadata_ctx["runtime_provider"],
        "status_path": metadata_ctx["status_path"],
        "knowledge_path": metadata_ctx["knowledge_path"],
        "project_rule_path": metadata_ctx["project_rule_path"],
        "error_book_path": metadata_ctx["error_book_path"],
        "tasks_dir": metadata_ctx["tasks_dir"],
        "shared_repo": metadata_ctx["shared_repo"],
        "status_raw": status_raw,
        "status": meta.get("status", "Idle"),
        "role": role,
        "projectless": bool(hook_state.get("projectless")) or is_machine_helper_state(hook_state, name),
        "helper": hook_state.get("helper") if isinstance(hook_state.get("helper"), dict) else {},
        "team_id": meta.get("team_id", ""),
        "task_id": task_id,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "provider": provider,
        "knowledge": read_file_safe(metadata_ctx["knowledge_path"]),
        "error_book": read_file_safe(metadata_ctx["error_book_path"]),
        "project_rule": read_file_safe(metadata_ctx["project_rule_path"]),
    }
    files["mailbox_unread"] = _load_unread_mailbox(os.path.join(os.path.dirname(metadata_ctx["status_path"]), "mailbox.json"))

    # Session number
    files["session_number"] = 0

    # Task-specific files
    if files["status"] == "Working" and files["task_id"]:
        files["task_dir"] = task_paths["task_dir"]
        files["task_readme_path"] = task_paths["task_readme_path"]
        files["history_log_path"] = task_paths["history_log_path"]
        files["task_knowledge_path"] = task_paths["task_knowledge_path"]
        files["task_readme"] = read_file_safe(files["task_readme_path"])
        raw_log = read_file_safe(files["history_log_path"], max_chars=16000)
        files["session_number"] = parse_session_metadata(raw_log)
        files["history_log"] = truncate_history_log(raw_log, max_sessions=3)
        files["task_knowledge"] = read_file_safe(files["task_knowledge_path"])
    else:
        files["task_dir"] = ""
        files["task_readme_path"] = ""
        files["history_log_path"] = ""
        files["task_knowledge_path"] = ""
        files["task_readme"] = ""
        files["history_log"] = ""
        files["task_knowledge"] = ""

    files["team_context"] = _load_team_context(files)

    return files


def _load_unread_mailbox(path, limit=8):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return []
    messages = data.get("messages") if isinstance(data, dict) else []
    if not isinstance(messages, list):
        return []
    unread = [m for m in messages if isinstance(m, dict) and not m.get("read")]
    unread = sorted(unread, key=lambda m: m.get("created_at", ""))
    return unread[:limit]


# ================================================================
# Provider 检测 & PR 命令生成
# ================================================================

def _detect_provider(intern_dir, metadata_ctx=None):
    """检测当前项目的 provider 类型（github | codeup）。

    读取优先级：
    1. .hook_state.json 的 project → .intern-config.json 的 projects[].provider
    2. git remote URL 模式匹配
    3. 默认 github
    """
    import json
    from common.utils import load_state

    if metadata_ctx and metadata_ctx.get("repo_provider"):
        provider = metadata_ctx["repo_provider"]
        if provider in ("github", "codeup", "gitlab", "local"):
            return provider

    # 从 hook_state 获取项目名
    state = load_state(intern_dir)
    project_name = state.get("project")
    if not project_name:
        raise ValueError(f"hook_state missing 'project' field in {intern_dir}")

    # 从 .intern-config.json 查找 provider
    config_path = os.path.join(
        WORK_AGENTS_ROOT, "axis_intern_agents", "workspace", ".intern-config.json"
    )
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                data = json.load(f)
            for proj in data.get("projects", []):
                if proj.get("name") == project_name or proj.get("projectId") == project_name:
                    provider = proj.get("provider", "")
                    if provider in ("github", "codeup", "gitlab", "local"):
                        return provider
        except (json.JSONDecodeError, OSError):
            pass

    # 从 git remote URL 检测
    repo_dir = os.path.join(intern_dir, project_name)
    if os.path.isdir(repo_dir):
        try:
            import subprocess
            remote_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_dir, stderr=subprocess.DEVNULL,
            ).decode().strip()
            if "codeup.aliyun.com" in remote_url:
                return "codeup"
        except Exception:
            pass

    return "github"


def _build_pr_create_cmd(provider, name):
    """根据 provider + 当前 locale 生成 PR/MR 创建命令段落。"""
    if get_locale() == "en":
        return _build_pr_create_cmd_en(provider, name)
    return _build_pr_create_cmd_zh(provider, name)


def _build_pr_create_cmd_zh(provider, name):
    if provider == "codeup":
        return f'''```bash
codeup_pr create --title "【<task_id>】【{name}】<描述>" --base <target_branch> --body "## 任务
<task_id>

## Owner
{name}

## 状态
进行中"
```'''
    return f'''```bash
gh pr create --title "【<task_id>】【{name}】<描述>" --base <target_branch> --body "## 任务
<task_id>

## Owner
{name}

## 状态
进行中"
```'''


def _build_pr_create_cmd_en(provider, name):
    if provider == "codeup":
        return f'''```bash
codeup_pr create --title "[<task_id>][{name}] <description>" --base <target_branch> --body "## Task
<task_id>

## Owner
{name}

## Status
In progress"
```'''
    return f'''```bash
gh pr create --title "[<task_id>][{name}] <description>" --base <target_branch> --body "## Task
<task_id>

## Owner
{name}

## Status
In progress"
```'''


def _build_pr_view_cmd(provider, repo_path, pr_number):
    """根据 provider 生成 PR/MR 查看命令段落。"""
    if provider == "codeup":
        return f'''```bash
cd {repo_path}
codeup_pr view {pr_number} --json status,mergedRevision
```'''
    else:
        return f'''```bash
cd {repo_path}
gh pr view {pr_number} --json state,mergedAt --jq '{{{{ state, mergedAt }}}}'
```'''


# ================================================================
# Prompt 构建 — 统一轻量 context + playbook 磁盘文件
# ================================================================

def build_system_prompt(intern_dir):
    """构建轻量 system prompt，返回 (prompt_text, status_str)。
    所有 intern 统一使用轻量 context + playbook 写磁盘。
    """
    f = load_intern_files(intern_dir)
    return _build_prompt(f)


# ================================================================
# 统一轻量 Context + Playbook 磁盘文件
# ================================================================

def _build_prompt(f):
    """构建轻量 context + 写 playbook 到磁盘。"""
    name = f["intern_name"]
    repo = f["repo_dir"]
    project_name = os.path.basename(repo)
    task_id = f["task_id"]
    status = f["status"]
    next_session = f["session_number"] + 1
    pr_display = f"#{f['pr_number']}" if f["pr_number"] else "<PR#xx>"

    # 每次 UPS 写 playbook 到磁盘（debug 同级，repo 外）
    _write_playbook(f)

    locale = get_locale()
    if status == "Working" and task_id:
        if locale == "en":
            prompt = _build_working_context_en(
                name, repo, project_name, task_id, next_session, pr_display, f)
        else:
            prompt = _build_working_context(
                name, repo, project_name, task_id, next_session, pr_display, f)
    else:
        if locale == "en":
            prompt = _build_idle_context_en(name, repo, project_name, f)
        else:
            prompt = _build_idle_context(name, repo, project_name, f)

    _builtin_ref = _resolve_builtin_doc_ref(repo, f.get("role", "independent"))
    if _builtin_ref is not None:
        prompt += _builtin_ref
    if _is_machine_helper_context(f):
        prompt += _build_machine_helper_injection(f, locale)

    return prompt, status


def _is_machine_helper_context(f):
    name = f.get("intern_name", "")
    state = f.get("hook_state") if isinstance(f.get("hook_state"), dict) else {}
    if is_machine_helper_state(state, name):
        return True
    return bool(
        f.get("projectless")
        and f.get("role") == "machine_helper"
        and name.startswith("machine_helper_")
    )


def _build_machine_helper_injection(f, locale):
    repo = f.get("repo_dir", "")
    intern_dir = f.get("intern_dir", "")
    metadata_root = f.get("metadata_root", "")
    workspace_key = f.get("workspace_key", "") or f.get("workspace_id", "")
    helper = f.get("helper") if isinstance(f.get("helper"), dict) else {}
    machine_id = helper.get("machine_id") or workspace_key or "unknown"
    if locale == "en":
        return f"""

## Machine Helper Operating Model

You are the machine-local enterprise helper for `{machine_id}`, not a business-project intern. Keep the standard Working/task/status/history/knowledge flow, but scope investigation to this local machine, the enterprise Intern Agents plugin runtime, and migration/setup/debugging work.

- Local repo: `{repo}`; metadata is resolver-managed at `{metadata_root}`; intern runtime dir is `{intern_dir}`; workspace key is `{workspace_key}`.
- You have the same file, command, network, attachment, AskUser/request_user_input, hook, checklist, Log, and Feishu reporting capabilities as a normal intern. The helper role changes the prompt and task boundary, not tool capability.
- Diagnose VS Code extension code, `intern-cli`, daemon, relay, hooks, tmux startup, Feishu registry, state v1 records, Codeup/enterprise policy, packaging, and VSIX/install verification together.
- For Feishu or relay issues, trace relay -> daemon -> tmux -> hooks -> FeishuModule output before concluding.
- When a user asks from the helper group, perform real diagnosis in this session and reply with findings; do not only acknowledge receipt.
"""
    return f"""

## Machine Helper 工作模型

你是本机企业版 machine helper，服务对象是 `{machine_id}`，不是业务项目 intern。你仍然遵守标准 Working/task/status/history/knowledge 流程，但排障范围限定在本机、企业版 Intern Agents 插件运行时，以及迁移、初始化、排障工作。

- 本地 repo：`{repo}`；metadata 由 resolver 管理：`{metadata_root}`；intern runtime dir：`{intern_dir}`；workspace key：`{workspace_key}`。
- 你的文件、命令、网络、附件、AskUser/request_user_input、hook、checklist、Log、Feishu 回传能力与普通 intern 一致；helper 角色只改变 prompt 和任务边界，不降低工具能力。
- 排查时把 VS Code extension、`intern-cli`、daemon、relay、hooks、tmux 启动、Feishu registry、state v1 记录、Codeup/企业权限、打包和 VSIX/安装验证放在同一条链路里看。
- Feishu 或 relay 问题按 relay -> daemon -> tmux -> hooks -> FeishuModule 回传链路逐段定位。
- 用户从 helper 群发问题时，必须在本会话做真实排障并回复结论；不要只回复“已收到”。
"""


# ================================================================
# Team role context
# ================================================================

VALID_TEAM_ROLES = {"coordinator", "team_lead", "worker"}


def _load_team_context(f):
    role = f.get("role", "independent")
    if role not in VALID_TEAM_ROLES:
        return None
    if role == "coordinator":
        return _load_coordinator_context(f)
    return _load_workspace_team_context(f, role)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def _json_files_under(root):
    if not os.path.isdir(root):
        return []
    result = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename.endswith(".json"):
                result.append(os.path.join(dirpath, filename))
    return sorted(result)


def _load_coordinator_context(f):
    name = f["intern_name"]
    coord_root = os.path.join(f["workspace_dir"], "coordinators")
    matches = []
    errors = []
    for path in _json_files_under(coord_root):
        try:
            data = _read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if data.get("role") == "coordinator" and data.get("intern_name") == name:
            matches.append((data.get("created_at", ""), path, data))

    if not matches:
        detail = f"未找到 {coord_root} 下 intern_name={name} 的 coordinator metadata"
        if errors:
            detail += "；读取错误：" + "; ".join(errors)
        return {"role": "coordinator", "error": detail}

    _, path, data = sorted(matches, reverse=True)[0]
    return {
        "role": "coordinator",
        "metadata_path": path,
        "coordinator_id": data.get("coordinator_id", ""),
        "owner": data.get("owner") or {},
        "standing_goal": data.get("standing_goal") or {},
        "coordinator_task": data.get("coordinator_task") or {},
        "managed_workspaces": data.get("managed_workspaces") or [],
        "team_leads": data.get("team_leads") or [],
        "all_workspace_teams": _load_all_workspace_teams(f["workspace_dir"]),
    }


def _load_all_workspace_teams(current_workspace_dir):
    teams = []
    for workspace_dir in _discover_workspace_dirs(current_workspace_dir):
        teams.extend(_load_team_summaries_for_workspace(workspace_dir))
    return teams


def _discover_workspace_dirs(current_workspace_dir):
    workspace_dirs = [current_workspace_dir]
    config_paths = [
        os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents", "workspace", ".intern-config.json"),
        os.path.join(current_workspace_dir, ".intern-config.json"),
    ]
    for config_path in config_paths:
        if not os.path.isfile(config_path):
            continue
        try:
            config = _read_json(config_path)
        except (OSError, json.JSONDecodeError):
            continue
        for project in config.get("projects", []):
            if project.get("enabled") is False:
                continue
            repo_path = project.get("outerRepoPath")
            if not repo_path:
                project_name = project.get("projectId") or project.get("name")
                if not project_name:
                    continue
                repo_path = os.path.join(WORK_AGENTS_ROOT, project_name)
            workspace_dirs.append(os.path.join(repo_path, "workspace"))

    result = []
    seen = set()
    for workspace_dir in workspace_dirs:
        real = os.path.realpath(workspace_dir)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        result.append(real)
    return result


def _load_team_summaries_for_workspace(workspace_dir):
    team_root = os.path.join(workspace_dir, "teams")
    if not os.path.isdir(team_root):
        return []
    project_name = os.path.basename(os.path.dirname(workspace_dir))
    teams = []
    for path in _json_files_under(team_root):
        try:
            data = _read_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or data.get("status", "active") == "deleted":
            continue
        workers = [
            worker
            for worker in data.get("workers", [])
            if isinstance(worker, dict) and worker.get("status", "active") != "deleted"
        ]
        teams.append({
            "project": data.get("project") or project_name,
            "team_id": data.get("team_name") or data.get("team_id") or os.path.basename(os.path.dirname(path)),
            "team_lead": data.get("team_lead") or {},
            "workers": workers,
        })
    return sorted(teams, key=lambda item: (item.get("project", ""), item.get("team_id", "")))


def _load_workspace_team_context(f, role):
    name = f["intern_name"]
    team_root = os.path.join(f["workspace_dir"], "teams")
    matches = []
    errors = []
    for path in _json_files_under(team_root):
        try:
            data = _read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if data.get("status", "active") == "deleted":
            continue
        team_lead = data.get("team_lead") or {}
        workers = data.get("workers") or []
        if role == "team_lead" and team_lead.get("intern_name") == name:
            matches.append((data.get("updated_at", ""), path, data))
        elif role == "worker" and any(
            w.get("intern_name") == name and w.get("status", "active") != "deleted"
            for w in workers
        ):
            matches.append((data.get("updated_at", ""), path, data))

    if not matches:
        detail = f"未找到 {team_root} 下包含 {role}={name} 的 active team metadata"
        if errors:
            detail += "；读取错误：" + "; ".join(errors)
        return {"role": role, "error": detail}

    _, path, data = sorted(matches, reverse=True)[0]
    team_id = data.get("team_id", "")
    team_lead = data.get("team_lead") or {}
    team_lead_management_task = {}
    if role == "team_lead":
        team_lead_management_task = team_lead.get("management_task") if isinstance(team_lead.get("management_task"), dict) else {}
        if not team_lead_management_task and team_id:
            team_lead_management_task = {
                "task_id": f"{team_id}_lead",
                "status": "InProgress",
                "completion_policy": "never_complete_while_team_exists",
            }

    return {
        "role": role,
        "metadata_path": path,
        "team_id": team_id,
        "project": data.get("project", ""),
        "coordinator": data.get("coordinator") or {},
        "team_lead": team_lead,
        "team_lead_management_task": team_lead_management_task,
        "workers": data.get("workers") or [],
    }


def _format_names(items, empty="无"):
    names = [i.get("intern_name", "") for i in items if i.get("intern_name")]
    return ", ".join(names) if names else empty


def _format_team_summaries_zh(teams):
    if not teams:
        return "- 无"
    lines = []
    for team in teams:
        lead = (team.get("team_lead") or {}).get("intern_name") or "-"
        workers = _format_names(team.get("workers") or [], "-")
        lines.append(f"- {team.get('project')}/{team.get('team_id')}: lead={lead}; workers={workers}")
    return "\n".join(lines)


def _format_team_summaries_en(teams):
    if not teams:
        return "- none"
    lines = []
    for team in teams:
        lead = (team.get("team_lead") or {}).get("intern_name") or "-"
        workers = _format_names(team.get("workers") or [], "-")
        lines.append(f"- {team.get('project')}/{team.get('team_id')}: lead={lead}; workers={workers}")
    return "\n".join(lines)


def _format_mailbox_unread_zh(messages):
    if not messages:
        return "- 当前无未读 mail。"
    lines = []
    for message in messages:
        source = f"{message.get('from_project', '')}/{message.get('from_intern_name', '')}".strip("/")
        kind = message.get("kind") or "progress"
        mid = message.get("message_id") or "-"
        content = (message.get("content") or "").replace("\n", " ")
        if len(content) > 220:
            content = content[:217] + "..."
        lines.append(f"- [{kind}] {source} msg={mid}: {content}")
    return "\n".join(lines)


def _format_mailbox_unread_en(messages):
    if not messages:
        return "- no unread mail."
    lines = []
    for message in messages:
        source = f"{message.get('from_project', '')}/{message.get('from_intern_name', '')}".strip("/")
        kind = message.get("kind") or "progress"
        mid = message.get("message_id") or "-"
        content = (message.get("content") or "").replace("\n", " ")
        if len(content) > 220:
            content = content[:217] + "..."
        lines.append(f"- [{kind}] {source} msg={mid}: {content}")
    return "\n".join(lines)


def _team_role_context_zh(f):
    ctx = f.get("team_context")
    if not ctx:
        return ""
    role = ctx["role"]
    if ctx.get("error"):
        return f"""
## Team Role Context
- Role：{role}
- Metadata：{ctx["error"]}
- 行为：当前不启用 team 专属上下文；按普通 intern 规则工作，并把 metadata 缺失作为需要修复的问题报告。
"""
    if role == "coordinator":
        standing_goal = ctx.get("standing_goal") or {}
        coordinator_task = ctx.get("coordinator_task") or {}
        return f"""
## Team Role Context
- Role：coordinator（用户级长期协调者）
- Metadata：{ctx["metadata_path"]}
- Coordinator ID：{ctx.get("coordinator_id") or "未设置"}
- 长期目标：{standing_goal.get("objective") or "未设置"}
- 永续任务：{coordinator_task.get("task_id") or "未设置"}；只要 coordinator 存在，该任务必须保持 InProgress，不允许标记为 Completed。
- Managed workspaces：{len(ctx.get("managed_workspaces") or [])} 个
- Team leads：{_format_names(ctx.get("team_leads") or [])}
- 职责：承接用户指令，创建/管理 team_lead，拆解和分配任务，持续监工和汇总，管理任务不主动结束。
- 边界：不直接写代码；对 team_lead 的目标下发走 coordinator→team_lead goal API，普通通知可用 peer send。

### 所有 Workspace Teams
{_format_team_summaries_zh(ctx.get("all_workspace_teams") or [])}
"""
    if role == "team_lead":
        management_task = ctx.get("team_lead_management_task") or {}
        return f"""
## Team Role Context
- Role：team_lead（workspace team 负责人）
- Metadata：{ctx["metadata_path"]}
- Team：{ctx.get("team_id") or "未设置"}
- Manage team 常驻任务：{management_task.get("task_id") or "未设置"}；只要 team 存在，该任务必须保持 InProgress，不允许标记为 Completed。
- Coordinator：{(ctx.get("coordinator") or {}).get("intern_name") or "未绑定"}
- Workers：{_format_names(ctx.get("workers") or [])}
- 职责：接收 coordinator/用户任务，拆解需求，分配 worker，监工，review 代码与 tester 报告，做 merge 决策。
- 调度原则：收到可拆分任务后，必须先评估所有 active workers；任务足够大或可并行时，默认尽量用满所有 active workers。若未用满，必须在回报中说明原因（例如任务太小、worker 离线、职责不适合或存在冲突）。
- Worker task 分配：给 worker 分配实现任务时，必须先创建 `workspace/tasks/<task_id>/` 标准 task 文档，写清背景、目标、实现范围、验收标准和分配信息；推荐使用 `internctl team assign-worker-task ...`。peer send 只通知 worker 接受指定 task，不承载完整任务正文。
- 强制边界：禁止修改业务代码；禁止创建实现 PR；禁止执行实现测试/验证；禁止执行 merge。允许的动作只有：创建/更新 worker task 文档、peer send 通知 worker、阅读 PR/diff、阅读并处理 mailbox、review 汇总信息、做 approve/request changes/block 决策、向 coordinator/用户汇报。
- 测试职责：team_lead 不亲自跑测试；必须预留/指派一个 worker 作为 tester，由 tester 执行必要测试/验证并用 mailbox 回报命令、结果、环境和未覆盖风险。
- Merge 流程：approve 后通知实现 worker self merge；team_lead 自己不执行 merge。
- 最终交付：回报 coordinator/用户时必须是完成态（已 merge/已验证，或未 merge/阻塞及原因），不要把最终交付写成 pending review。
- Mailbox：每一次 peer send 前必须先阅读并处理完本段列出的未读 mail；处理后调用 `/api/intern/mailbox/mark-read`，用自己的 `intern_name` 标记 read。
- 未读 mail：
{_format_mailbox_unread_zh(f.get("mailbox_unread") or [])}
- 边界：不直接写代码，不创建实现 PR，不亲自跑测试/验证，不执行 merge；对上使用 peer send，对 worker 使用 peer send。
"""
    return f"""
## Team Role Context
- Role：worker（workspace team 执行者）
- Metadata：{ctx["metadata_path"]}
- Team：{ctx.get("team_id") or "未设置"}
- Team lead：{(ctx.get("team_lead") or {}).get("intern_name") or "未设置"}
- 职责：接受 team_lead 分配，执行代码/测试/提交，按授权执行 merge。
- 任务来源：team_lead 分配任务时会指定 `workspace/tasks/<task_id>/`；必须先接受该 task，并以 task 文档作为任务权威来源。
- Merge 后完成：PR merge 后必须把当前 task 标记为 Completed，状态切回 Idle，并通过 mailbox 向 team_lead 汇报 merge 结果。
- 汇报：可以使用 `POST /api/intern/mail/to` mail to lead；不要用 peer send 打断 team_lead。
- 边界：worker 不主动联系其他 intern。
"""


def _team_role_context_en(f):
    ctx = f.get("team_context")
    if not ctx:
        return ""
    role = ctx["role"]
    if ctx.get("error"):
        return f"""
## Team Role Context
- Role: {role}
- Metadata: {ctx["error"]}
- Behavior: team-specific context is disabled; work as a normal intern and report the missing metadata as a fixable issue.
"""
    if role == "coordinator":
        standing_goal = ctx.get("standing_goal") or {}
        coordinator_task = ctx.get("coordinator_task") or {}
        return f"""
## Team Role Context
- Role: coordinator (user-scoped long-running coordinator)
- Metadata: {ctx["metadata_path"]}
- Coordinator ID: {ctx.get("coordinator_id") or "unset"}
- Standing goal: {standing_goal.get("objective") or "unset"}
- Permanent task: {coordinator_task.get("task_id") or "unset"}; while the coordinator exists, this task must stay InProgress and must not be marked Completed.
- Managed workspaces: {len(ctx.get("managed_workspaces") or [])}
- Team leads: {_format_names(ctx.get("team_leads") or [], "none")}
- Duties: accept user instructions, create/manage team_leads, break down work, assign tasks, monitor progress, and keep the management task running.
- Boundaries: do not write code directly; coordinator-to-team_lead goal changes use the dedicated goal API, while ordinary notices may use peer send.

### All Workspace Teams
{_format_team_summaries_en(ctx.get("all_workspace_teams") or [])}
"""
    if role == "team_lead":
        management_task = ctx.get("team_lead_management_task") or {}
        return f"""
## Team Role Context
- Role: team_lead (workspace team lead)
- Metadata: {ctx["metadata_path"]}
- Team: {ctx.get("team_id") or "unset"}
- Manage team permanent task: {management_task.get("task_id") or "unset"}; while the team exists, this task must stay InProgress and must not be marked Completed.
- Coordinator: {(ctx.get("coordinator") or {}).get("intern_name") or "unbound"}
- Workers: {_format_names(ctx.get("workers") or [], "none")}
- Duties: receive coordinator/user tasks, break them down, assign workers, monitor execution, review code plus the tester report, and make merge decisions.
- Worker task assignment: before assigning implementation work to a worker, create a standard `workspace/tasks/<task_id>/` task with background, goal, scope, acceptance criteria, and assignment metadata; prefer `internctl team assign-worker-task ...`. Peer send should only tell the worker to accept the task, not carry the full task body.
- Hard boundary: do not modify product/source code; do not create implementation PRs; do not run implementation tests or verification; do not merge. Allowed actions are only: create/update worker task docs, peer send workers, read PRs/diffs, read and handle mailbox, review summaries, decide approve/request changes/block, and report to the coordinator/user.
- Testing duty: team_lead must not run tests personally; reserve/assign a worker as tester, and have the tester run required tests/verification and report commands, results, environment, and uncovered risks through mailbox.
- Merge flow: after approval, tell the implementation worker to self merge; team_lead does not merge.
- Final delivery: report to the coordinator/user in a completion state (merged/verified, or not merged/blocked with reasons), not as pending review.
- Mailbox: before every peer send, read and handle all unread mail listed here; after handling, call `/api/intern/mailbox/mark-read` with your own `intern_name`.
- Unread mail:
{_format_mailbox_unread_en(f.get("mailbox_unread") or [])}
- Boundaries: do not write code directly, do not create implementation PRs, do not run tests/verification personally, and do not merge; use peer send upward and to workers.
"""
    return f"""
## Team Role Context
- Role: worker (workspace team executor)
- Metadata: {ctx["metadata_path"]}
- Team: {ctx.get("team_id") or "unset"}
- Team lead: {(ctx.get("team_lead") or {}).get("intern_name") or "unset"}
- Duties: accept team_lead assignments, implement, test, commit, and merge when authorized.
- Task source: team_lead assignments point to `workspace/tasks/<task_id>/`; accept that task first and treat the task docs as the source of truth.
- After merge: once the PR is merged, mark the current task Completed, switch status back to Idle, and report the merge result to the team_lead through mailbox.
- Reporting: use `POST /api/intern/mail/to` to mail to lead; do not interrupt the team_lead through peer send.
- Boundaries: workers do not directly contact other interns.
"""


def _role_specific_context_zh(f):
    role = f.get("role", "independent")
    if role in VALID_TEAM_ROLES:
        return ""
    if role == "machine_helper":
        return """
## Role-specific Context
- Role：machine_helper（本机插件排障/迁移 helper）
- 职责：协助用户排查本机企业版插件、daemon、relay、hooks、tmux、Feishu 回传、Codeup/权限和迁移初始化问题。
- 工具能力：与普通 intern 一致，不因 helper 角色降级文件、命令、网络、附件、AskUser/request_user_input 或协作能力。
"""
    if role == "helper":
        return """
## Role-specific Context
- Role：helper（辅助执行者）
- 职责：承接明确的小范围辅助任务、整理资料、补充测试或做局部验证；遇到任务所有权不清晰时先回报边界。
- 工具能力：与普通 intern 一致，不因 helper 角色降级文件、命令、网络或协作能力。
"""
    return """
## Role-specific Context
- Role：independent（独立执行者）
- 职责：直接承接用户任务，独立完成调研、实现、测试、提交和回报。
"""


def _role_specific_context_en(f):
    role = f.get("role", "independent")
    if role in VALID_TEAM_ROLES:
        return ""
    if role == "machine_helper":
        return """
## Role-specific Context
- Role: machine_helper
- Duties: diagnose this machine's enterprise plugin, daemon, relay, hooks, tmux, Feishu reporting, Codeup/policy, migration, and setup issues.
- Tool capability: same as a normal intern; helper role does not downgrade file, command, network, attachment, AskUser/request_user_input, or collaboration capabilities.
"""
    if role == "helper":
        return """
## Role-specific Context
- Role: helper
- Duties: handle clearly scoped support work, organize findings, add tests, or run focused verification; report boundaries when ownership is unclear.
- Tool capability: same as a normal intern; helper role does not downgrade file, command, network, or collaboration capabilities.
"""
    return """
## Role-specific Context
- Role: independent
- Duties: accept user tasks directly and independently complete research, implementation, tests, commits, and reporting.
"""


# task213/267/320: daemon ↔ extension 共享的 PID file。daemon 启动时写
# http_port/ws_port/bundle_dir 等字段；context_loader 在每次 UPS 注入 peer/goal
# endpoint URL + builtin doc 路径到 system prompt 末尾。常量化便于单测 monkeypatch。
PEER_DAEMON_PID_FILE = "/tmp/feishu_daemon.json"


def _resolve_builtin_doc_ref(repo, role="independent"):
    """返回按 role 注入 system prompt 末尾的 builtin 文档引用行；daemon 未跑或 PID
    文件不可读时返 None（接口本来就不可用，跳过这一行不破坏 prompt 构建）。

    task267: 优先读 PID payload 的 ``bundle_dir`` 拼 VSIX bundle 路径（与协议
    版本绑定的稳定 doc）；老 daemon 未上报该字段时 fallback 到 intern worktree
    ``{repo}/intern-cli/builtin/*.md``——升级窗口期保留可读 doc，
    fallback 删除留给 task268 cleanup（task262 同款 follow-up）。
    """
    try:
        import json as _json
        with open(PEER_DAEMON_PID_FILE) as _f:
            _pid = _json.load(_f)
            _port = int(_pid["http_port"])
            _bundle = _pid.get("bundle_dir")
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return None
    if _bundle:
        _peer_doc_path = f"{_bundle}/builtin/peer_send.md"
        _goal_doc_path = f"{_bundle}/builtin/goal_send.md"
        _mailbox_doc_path = f"{_bundle}/builtin/mailbox.md"
        _team_roles_doc_path = f"{_bundle}/builtin/team_roles.md"
    else:
        _peer_doc_path = f"{repo}/intern-cli/builtin/peer_send.md"
        _goal_doc_path = f"{repo}/intern-cli/builtin/goal_send.md"
        _mailbox_doc_path = f"{repo}/intern-cli/builtin/mailbox.md"
        _team_roles_doc_path = f"{repo}/intern-cli/builtin/team_roles.md"

    if role == "worker":
        return (
            f"\n\n> Team builtin 文档：处理 worker mailbox 见 {_mailbox_doc_path}；"
            f"team role 边界见 {_team_roles_doc_path}\n"
        )
    if role == "team_lead":
        return (
            f"\n\n> 联系/分配 intern：`POST http://localhost:{_port}/api/intern/peer/send`"
            f"（请求体/响应见 {_peer_doc_path}）；处理 worker mailbox 见 {_mailbox_doc_path}；"
            f"team role 边界见 {_team_roles_doc_path}\n"
        )
    if role == "coordinator":
        return (
            f"\n\n> 联系 team_lead：`POST http://localhost:{_port}/api/intern/peer/send`"
            f"（请求体/响应见 {_peer_doc_path}）；设置/取消 team_lead pressing goal："
            f"`POST http://localhost:{_port}/api/intern/goal/set` / "
            f"`POST http://localhost:{_port}/api/intern/goal/cancel`（见 {_goal_doc_path}）；"
            f"team role 边界见 {_team_roles_doc_path}\n"
        )
    return _resolve_peer_send_doc_ref(repo)


def _resolve_peer_send_doc_ref(repo):
    """返回普通 intern 的 peer/goal endpoint 引用行；保留给旧测试和外部调用。"""
    try:
        import json as _json
        with open(PEER_DAEMON_PID_FILE) as _f:
            _pid = _json.load(_f)
            _port = int(_pid["http_port"])
            _bundle = _pid.get("bundle_dir")
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return None
    if _bundle:
        _peer_doc_path = f"{_bundle}/builtin/peer_send.md"
        _goal_doc_path = f"{_bundle}/builtin/goal_send.md"
    else:
        _peer_doc_path = f"{repo}/intern-cli/builtin/peer_send.md"
        _goal_doc_path = f"{repo}/intern-cli/builtin/goal_send.md"
    return (
        f"\n\n> 联系其他 intern：`POST http://localhost:{_port}/api/intern/peer/send`"
        f"（请求体/响应见 {_peer_doc_path}）；设置/取消 pressing goal："
        f"`POST http://localhost:{_port}/api/intern/goal/set` / "
        f"`POST http://localhost:{_port}/api/intern/goal/cancel`（见 {_goal_doc_path}）\n"
    )


# ================================================================
# ###################  ZH (zh-cn) builders  ######################
# ================================================================


def _write_rule_zh(f):
    mode = f.get("metadata_mode", "repo_dotdir")
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    metadata_branch = f.get("metadata_branch") or "intern_workspace"
    if mode == "local_only":
        return (
            f"- Workspace mode：local_only；status/task/history/knowledge 只写本机 metadata 路径 `{metadata_root}`\n"
            "- 纯 metadata 变化不 push 到代码仓库；如果任务修改代码，仍在代码 checkout 开任务分支并按 provider 走 PR/MR\n"
            f"- ⚠️ 禁止直接 push 代码默认分支 `{default_branch}`；只有主管明确批准 merge 才执行 merge"
        )
    if mode == "metadata_branch":
        return (
            f"- Workspace mode：metadata_branch；status/task/history/knowledge 写入 metadata checkout `{metadata_root}` 并 push 到 `{metadata_branch}`\n"
            "- 代码修改在代码 checkout 单独开任务分支，PR/MR 目标通常是代码默认分支或主管指定分支\n"
            f"- ⚠️ 禁止直接 push 代码默认分支 `{default_branch}`；只有主管明确批准 merge 才执行 merge"
        )
    return (
        f"- Workspace mode：repo_dotdir；status/task/history/knowledge 写入 metadata root `{metadata_root}`\n"
        "- metadata 与代码是否同分支提交由 playbook 和主管指定的目标分支决定；代码修改必须走 PR/MR\n"
        f"- ⚠️ 禁止直接 push 代码默认分支 `{default_branch}`；只有主管明确批准 merge 才执行 merge"
    )


def _write_rule_en(f):
    mode = f.get("metadata_mode", "repo_dotdir")
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    metadata_branch = f.get("metadata_branch") or "intern_workspace"
    if mode == "local_only":
        return (
            f"- Workspace mode: local_only; write status/task/history/knowledge only under local metadata `{metadata_root}`\n"
            "- Do not push metadata-only changes to the code repo; code changes still use a task branch and provider PR/MR when available\n"
            f"- Never push the code default branch `{default_branch}` directly; merge only after explicit supervisor approval"
        )
    if mode == "metadata_branch":
        return (
            f"- Workspace mode: metadata_branch; write status/task/history/knowledge under metadata checkout `{metadata_root}` and push `{metadata_branch}`\n"
            "- Code changes use a separate task branch in the code checkout; PR/MR targets the default branch or the supervisor-specified branch\n"
            f"- Never push the code default branch `{default_branch}` directly; merge only after explicit supervisor approval"
        )
    return (
        f"- Workspace mode: repo_dotdir; write status/task/history/knowledge under metadata root `{metadata_root}`\n"
        "- Whether metadata and code share a branch is defined by the playbook and supervisor target branch; code changes require PR/MR\n"
        f"- Never push the code default branch `{default_branch}` directly; merge only after explicit supervisor approval"
    )


def _workspace_context_zh(f):
    return f"""
## Workspace / Provider Context
- Workspace ID：{f.get("workspace_id") or f.get("workspace_key") or "未设置"}
- Workspace mode：{f.get("metadata_mode", "repo_dotdir")}
- Metadata root：{f.get("metadata_root", "")}
- Repo provider：{f.get("provider", "github")}
- Runtime provider：{f.get("runtime_provider", "")}
- Role：{f.get("role", "independent")}
- 写入规则：
{_write_rule_zh(f)}
"""


def _workspace_context_en(f):
    return f"""
## Workspace / Provider Context
- Workspace ID: {f.get("workspace_id") or f.get("workspace_key") or "unset"}
- Workspace mode: {f.get("metadata_mode", "repo_dotdir")}
- Metadata root: {f.get("metadata_root", "")}
- Repo provider: {f.get("provider", "github")}
- Runtime provider: {f.get("runtime_provider", "")}
- Role: {f.get("role", "independent")}
- Write rules:
{_write_rule_en(f)}
"""


def _build_working_context(name, repo, project_name, task_id, next_session, pr_display, f):
    """Working context (~1.5KB)。不读 playbook 也能正常工作。"""
    playbook_ref = f"{f['intern_dir']}/playbook.md"
    project_rule = f.get("project_rule", "")

    prompt = f"""你是 {name}，在项目 {project_name} 中工作。

## 路径速查
| 用途 | 路径 |
|------|------|
| 代码 | {repo}/ |
| 状态 | {f['status_path']} |
| 任务 | {f['task_dir']} |
| 调试 | {repo}/../debug/ |
| 输出 | {repo}/../outputs/ |

## 当前状态
任务：{task_id} | Session：{next_session} | PR：{pr_display} | 状态：Working
{_workspace_context_zh(f)}
{_team_role_context_zh(f)}
{_role_specific_context_zh(f)}

## 工作规则
- commit 后必须 push（自己分支）
- ⚠️ 禁止直接 push master，走 PR
- 状态文件在自己分支上更新
- 只有主管明确批准 merge 并完成 merge closeout 时，任务 README 的 METADATA 状态才写字面值 `Completed`；实现完成但未获批准时必须保持 `InProgress`/Working，不要写 `Done`、`Closed` 或中文状态词

## AskUser / request_user_input 规则
- 调用 Claude `AskUserQuestion` 或 Codex `request_user_input` 时，所有决策背景必须写进 tool payload 的 `question`，每个选项的取舍、风险、推荐理由必须写进 `options[].description`
- 不要只在工具调用前的普通文字里说明背景；飞书卡片在用户选择前只稳定显示 tool payload
"""
    if project_rule:
        prompt += f"""
## 项目规则
{project_rule}
"""

    prompt += f"""
## 参考文件（需要时 Read）
- 任务文档：{f['task_readme_path']}
- 历史日志：{f['history_log_path']}
- 任务知识：{f['task_knowledge_path']}
- 错题本：{f['error_book_path']}
- 个人知识库：{f['knowledge_path']}"""

    prompt += f"""

## 特定流程（需要时 Read）
- 继续当前任务 / 主管明确允许 merge / 创建新任务：{playbook_ref}
- 注意：用户只说“继续”时，只能继续实现、复核或汇报当前进展；这不是 merge 批准。只有用户明确说“允许 merge / 可以 merge / 批准 merge”等语义时，才能执行 merge 完结流程。

## 这次回复结束前必须更新的文件（绝对路径，照抄即可）
- {f['status_path']}：修改最近进展
- {f['history_log_path']}：追加 Session {next_session} 记录，METADATA:SESSION 替换为 {next_session}
- {f['task_knowledge_path']}：METADATA:SESSION 替换为 {next_session}（无新知识也要更新 SESSION 号）

## Session 结束 Checklist（必须包含在每次回复末尾）

📋 Checklist:
我是 {name}，当前任务：{task_id}，Session：{next_session}
场景：<C - 工作中 | D - Working→Idle PR 已 merge>

【Session 结束确认】
- [x] 已 push
- [x] {f['status_path']} 已更新：<一句话>
- [x] {f['history_log_path']} 已更新：<一句话>
- [x] {f['task_knowledge_path']} 已更新：<描述> / N/A

本次：
- <做了什么>

下步：
- <具体可执行>

⚠️ 格式检查：回复结束时 Stop hook 检查，不通过被打回消耗双倍费用。
⚠️ 禁止："待更新"、"稍后"、"下次"、"后续统一"
"""
    return prompt


def _build_idle_context(name, repo, project_name, f):
    """Idle context (~0.8KB)。"""
    playbook_ref = f"{f['intern_dir']}/playbook.md"
    project_rule = f.get("project_rule", "")

    prompt = f"""你是 {name}，在项目 {project_name} 中工作。

## 路径速查
| 用途 | 路径 |
|------|------|
| 代码 | {repo}/ |
| 状态 | {f['status_path']} |
| 任务 | {f['tasks_dir']} |

## 当前状态
Idle，无进行中任务。
{_workspace_context_zh(f)}
{_team_role_context_zh(f)}
{_role_specific_context_zh(f)}

## 工作规则
- commit 后必须 push
- ⚠️ 禁止直接 push master，走 PR
- 主管只要求“创建任务”时，只创建 task metadata，保持 Idle；禁止接受/分配给自己、禁止切任务分支、禁止改代码或实现，必须等待单独的分配指令

## AskUser / request_user_input 规则
- 调用 Claude `AskUserQuestion` 或 Codex `request_user_input` 时，所有决策背景必须写进 tool payload 的 `question`，每个选项的取舍、风险、推荐理由必须写进 `options[].description`
- 不要只在工具调用前的普通文字里说明背景；飞书卡片在用户选择前只稳定显示 tool payload
"""
    if project_rule:
        prompt += f"""
## 项目规则
{project_rule}
"""

    prompt += f"""
## 参考文件（需要时 Read）
- 错题本：{f['error_book_path']}
- 个人知识库：{f['knowledge_path']}

## 特定流程（需要时 Read）
- 创建任务 / 被分配任务（Idle→Working）：{playbook_ref}

## Session 结束 Checklist（必须包含在每次回复末尾）

📋 Checklist:
我是 {name}，当前任务：无
场景：<Idle - 空闲 | Idle - 创建任务 | A - Idle→Working>

本次：
- <做了什么>

下步：
- <具体可执行>

⚠️ 禁止："待更新"、"稍后"、"下次"、"后续统一"
"""
    return prompt


def _build_working_playbook(name, repo, shared_repo, task_id, pr_number, provider):
    """Working playbook：PR Merge 完结 + 创建新任务。"""
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. PR Merge 完结流程

只有主管明确说“允许 merge / 可以 merge / 批准 merge”等语义时，才按以下步骤执行。
如果主管只说“继续”，你必须继续实现、复核、补测试或汇报当前状态；禁止把“继续”解释为 merge 批准，禁止把任务标记 Completed，禁止执行 `codeup_pr merge`。

### 步骤 1-4：Merge 前状态更新

在**自己分支上**完成以下更新并 push：

1. 更新 `workspace/interns/{name}/status.md`：
   - METADATA 行（第三行）：`<!-- METADATA:STATUS=Idle,TASK=,ROLE=<保持原 ROLE，缺省 independent> -->`
   - 表格中的状态：Working → Idle
   - 表格中的当前任务：清空

2. 更新任务 README 的 METADATA（第三行）：`<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - worker 被 team_lead 分配的 task 也按本步骤完成；PR merge 后必须保持该 task 已 Completed，并通过 mailbox 向 team_lead 汇报 merge 结果。

3. 精炼 task_knowledge.md 中有价值的内容到个人知识库：
   `workspace/interns/{name}/knowledge.md`

4. 提交所有更新：
```bash
git add workspace/
git commit -m "完成任务 {task_id}"
git push
```

### 步骤 5：Merge PR

5. 由你自行执行 merge PR：`codeup_pr merge <pr_number>`（默认走 squash —— 分支多次 commit 会被压成单一 commit；如需保留分支 commit 历史，显式加 `--merge-type no-fast-forward`）。如果 merge 被拒绝（如 405 该状态不允许合并），通常是分支与 master 有 conflict：先 `git fetch origin && git merge origin/master` 解决冲突并 push，再重试 merge；仍失败则汇报主管。

### 步骤 6-8：Merge 后清理

6. 确认 PR 已 merge：
{pr_view_cmd}
- 如果 `state` 不是 `MERGED`，**停止清理**，向主管确认
- 确认 `mergedAt` 有值后再继续

7. 清理本地分支：
```bash
cd {repo}
git checkout master && git pull origin master
git branch -d {name}/{task_id}
```

8. 清理临时文件（**必须原文执行；禁止额外加 `ls`/`echo` 包装或 `;` 多语句拼接，否则会破坏 `Bash(rm:*)` allowlist 匹配触发 permission 弹窗**）：
```bash
rm -rf {repo}/../debug {repo}/../outputs && mkdir -p {repo}/../debug {repo}/../outputs
```

---

## 2. 创建新任务流程

复杂任务需要拆分子任务时使用。

> **`<new_task_id>` 约定**：完整目录名（格式 `taskNNN_描述` 或 `task_描述`），写入文件和提交信息都用完整目录名。

⚠️ **必须在共享 repo `{shared_repo}` 操作**。禁止在当前任务分支 (`{repo}`) 改 `workspace/tasks/` 后 push master——会污染任务分支或引起冲突。共享 repo 永远在 master，不要 checkout。

```bash
cd {shared_repo}
git pull --ff-only origin master
mkdir -p workspace/tasks/<new_task_id>
```

创建以下文件（必须包含 METADATA 头部）：

**README.md**：
```markdown
# <new_task_id> - 任务标题

<!-- METADATA:STATUS=Open,ASSIGNEE= -->

## 背景
...
## 任务目标
...
## 验收标准
- [ ] ...
```

**history_log.md**：
```markdown
# <new_task_id> - 历史日志

<!-- METADATA:SESSION=0 -->

---

## Session 0 - YYYY-MM-DD - 初始化

**执行人**: {name}

任务创建

---
```

**task_knowledge.md**：
```markdown
# <new_task_id> - 任务知识

<!-- METADATA:SESSION=0 -->

> **编写规则**：每条一句话，格式：`N. 类别：内容`
>
> 类别包括：主管要求、技术事实、文件修改、调研结论

---

## 知识条目

（任务未开始，暂无知识积累）

---
```

提交：
```bash
cd {shared_repo}
git add workspace/tasks/<new_task_id>/
git commit -m "[{name}] 创建子任务 <new_task_id>"
git push origin master
```
"""


def _build_idle_playbook(name, repo, shared_repo, provider):
    """Idle playbook：创建任务 + 接受任务 + 响应问题。"""
    pr_create_cmd = _build_pr_create_cmd(provider, name)

    return f"""# Idle Playbook

> **`<task_id>` 约定**：下文所有 `<task_id>` 均指 `workspace/tasks/` 下的**完整目录名**（如 `task152_cleanup_question_resolve_dead_code`）。主管可能口头说简称（如 `task152`），此时先 `ls {shared_repo}/workspace/tasks/` 找到对应完整目录名再用。写入 status.md METADATA 和表格的也必须是完整目录名。

## 1. 创建任务流程

主管要求创建新任务时执行。创建后仍为 Idle。
只要主管没有单独说“分配 <task_id> 给你”或“接受 <task_id>”，到创建任务后就停止；不要切任务分支，不要修改代码文件，不要把 status 改成 Working。
只要主管没有单独说“分配 <task_id> 给你”或“接受 <task_id>”，到创建任务并 push 后就停止；不要切任务分支，不要创建 PR/MR，不要修改代码文件，不要把 status 改成 Working。

⚠️ **必须在共享 repo `{shared_repo}` 操作**。禁止在自己 worktree (`{repo}`) 改 `workspace/tasks/` 后 push master——会污染当前任务分支或引起冲突。共享 repo 永远在 master，不要 checkout。

```bash
cd {shared_repo}
git pull --ff-only origin master
mkdir -p workspace/tasks/<task_id>
```

创建 README.md / history_log.md / task_knowledge.md（METADATA 在第三行）：

- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->` + 背景/目标/验收标准
- history_log.md：`<!-- METADATA:SESSION=0 -->` + Session 0 初始化记录
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->` + 编写规则 + 空知识条目

> 模板参考：{shared_repo}/workspace/tasks/ 下已有任务的同名文件

```bash
cd {shared_repo}
git add workspace/tasks/<task_id>/
git commit -m "[{name}] 创建任务 <task_id>"
git push origin master
```

到这里必须停止并回复 Checklist，场景写 `Idle - 创建任务`。

---

## 2. 接受任务流程

```bash
cd {repo}
git checkout master && git pull origin master
cat workspace/tasks/<task_id>/README.md
cat workspace/tasks/<task_id>/history_log.md
cat workspace/tasks/<task_id>/task_knowledge.md
```

创建分支：
```bash
git checkout -b {name}/<task_id>
```
如果主管指定了基础分支：`git checkout -b {name}/<task_id> origin/<base_branch>`

占位 commit 并推送：
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "【<task_id>】初始化"
git push -u origin {name}/<task_id>
```

创建 PR：
{pr_create_cmd}
如果主管指定了目标分支，替换 `<target_branch>`，否则默认 `master`。

更新状态：
- `workspace/interns/{name}/status.md` METADATA：`<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<保持原 ROLE，缺省 independent> -->`
- 表格：状态→Working、当前任务→<task_id>、PR→<pr_url>
- 任务 README METADATA：`<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
git add workspace/ && git commit -m "接受任务 <task_id>" && git push
```

---

## 3. 其他情况

正常响应主管的问题或指示。
"""


def _build_local_only_working_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    default_branch = f.get("default_branch") or "master"
    return f"""# Working Playbook

## 1. local_only Merge 完结流程

只有主管明确说“允许 merge / 可以 merge / 批准 merge”等语义时，才按以下步骤执行。local_only 的任务 metadata 只保存在本机，不要 commit 或 push 纯 metadata 变化。
如果主管只说“继续”，你必须继续实现、复核、补测试或汇报当前状态；禁止把“继续”解释为 merge 批准，禁止把任务标记 Completed，禁止合并任务分支。

### 步骤 1-3：更新本机 metadata

1. 更新状态文件：
   `{f['status_path']}`
   - METADATA 行改为：`<!-- METADATA:STATUS=Idle,TASK=,ROLE=<保持原 ROLE，缺省 independent> -->`
   - 表格中的状态改为 Idle，当前任务清空，PR 可写 N/A

2. 更新任务 README：
   `{f['task_readme_path']}`
   - METADATA 行改为：`<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - 这里的状态值必须是字面值 `Completed`，不能写 `Done`、`Closed` 或中文状态词

3. 追加历史日志、整理任务知识到个人知识库：
   - `{f['history_log_path']}`
   - `{f['task_knowledge_path']}`
   - `{f['knowledge_path']}`

### 步骤 4：只在批准后合并本地任务分支

```bash
cd {repo}
git status
git checkout {default_branch}
git merge --ff-only {name}/{task_id} || git merge --no-ff {name}/{task_id} -m "{task_id}: merge approved local work"
```

如果这个 repo 有可写 `origin`，可以再执行 `git push origin {default_branch}`；如果 origin 不可写，保留本地 master ahead 状态并在回复里说明。

### 步骤 5：回复主管

回复中必须说明：任务已 Completed、自己已回 Idle、代码已合并到本地 `{default_branch}`，以及是否成功 push 到 origin。

---

## 2. 创建新任务流程

复杂任务需要拆分子任务时使用。local_only 新任务写入本机 metadata root：`{f['metadata_root']}`。

```bash
mkdir -p {f['metadata_root']}/tasks/<new_task_id>
```

创建 README.md / history_log.md / task_knowledge.md，METADATA 行分别使用：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md：`<!-- METADATA:SESSION=0 -->`
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->`
"""


def _build_local_only_idle_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    default_branch = f.get("default_branch") or "master"
    metadata_root = f["metadata_root"]
    status_path = f["status_path"]
    return f"""# Idle Playbook

> **`<task_id>` 约定**：下文所有 `<task_id>` 均指 `{metadata_root}/tasks/` 下的完整目录名。

## 1. 创建任务流程

主管要求创建新任务时执行。创建后仍为 Idle。
创建任务和分配任务是两个独立动作。只要用户消息没有单独说“分配 <task_id> 给你”或“接受 <task_id>”，本轮只创建任务 metadata，必须保持 Idle。
禁止在创建任务流程中执行这些动作：不要切任务分支、不要写目标代码文件、不要提交代码、不要 push 代码分支、不要把 README 改成 InProgress、不要把 `{status_path}` 改成 Working。

```bash
mkdir -p {metadata_root}/tasks/<task_id>
```

创建 README.md / history_log.md / task_knowledge.md（METADATA 在第三行）：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->` + 背景/目标/验收标准
- history_log.md：`<!-- METADATA:SESSION=0 -->` + Session 0 初始化记录
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->` + 空知识条目

local_only metadata 只保存在本机；不要 commit 或 push 纯 metadata 变化。

到这里必须停止并回复 Checklist，场景写 `Idle - 创建任务`。如果任务内容要求新增或修改代码文件，也必须等下一条“分配/接受”指令后才实现。

---

## 2. 接受任务流程

仅当用户明确说“分配 <task_id> 给你”或“接受 <task_id>”时执行本节。不要因为自己刚创建了任务就自动进入本节。

```bash
cat {metadata_root}/tasks/<task_id>/README.md
cat {metadata_root}/tasks/<task_id>/history_log.md
cat {metadata_root}/tasks/<task_id>/task_knowledge.md
cd {repo}
git checkout {default_branch}
git checkout -b {name}/<task_id>
```

更新本机 metadata：
- `{status_path}` METADATA：`<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<保持原 ROLE，缺省 independent> -->`
- `{status_path}` 表格：状态改为 Working，当前任务改为 `<task_id>`
- `{metadata_root}/tasks/<task_id>/README.md` METADATA：`<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

执行代码修改时在 `{name}/<task_id>` 分支提交代码。实现完成后停在 Working，等待主管明确批准 merge；不要提前 merge，不要把任务标记 Completed，不要把自己切回 Idle。

---

## 3. 其他情况

正常响应主管的问题或指示。
"""


# ================================================================
# ###################  EN builders  ##############################
# ================================================================


def _build_working_context_en(name, repo, project_name, task_id, next_session, pr_display, f):
    """Working context (EN)."""
    playbook_ref = f"{f['intern_dir']}/playbook.md"
    project_rule = f.get("project_rule", "")

    prompt = f"""You are {name}, working in project {project_name}.

## Path quick-reference
| Use | Path |
|------|------|
| Code | {repo}/ |
| Status | {f['status_path']} |
| Task | {f['task_dir']} |
| Debug | {repo}/../debug/ |
| Output | {repo}/../outputs/ |

## Current state
Task: {task_id} | Session: {next_session} | PR: {pr_display} | Status: Working
{_workspace_context_en(f)}
{_team_role_context_en(f)}
{_role_specific_context_en(f)}

## Work rules
- Must push after commit (your own branch)
- ⚠️ Never push master directly; go through PR
- Update status files on your own branch
- Only after explicit supervisor merge approval and merge closeout may the task README METADATA status become the literal `Completed`; after implementation but before approval, keep `InProgress`/Working and do not write `Done`, `Closed`, or translated status words

## AskUser / request_user_input rules
- When calling Claude `AskUserQuestion` or Codex `request_user_input`, put all decision context in the tool payload `question`, and put each option's tradeoffs, risks, and recommendation reason in `options[].description`
- Do not rely only on ordinary prose immediately before the tool call; the Feishu card reliably shows only the tool payload before the user chooses
"""
    if project_rule:
        prompt += f"""
## Project rules
{project_rule}
"""

    prompt += f"""
## Files that MUST be updated before this reply ends
- status.md: latest progress
- history_log.md: append Session {next_session} entry; replace METADATA:SESSION with {next_session}
- task_knowledge.md: replace METADATA:SESSION with {next_session} (update SESSION number even when no new knowledge)

## Reference files (Read on demand)
- Task doc: {f['task_readme_path']}
- History log: {f['history_log_path']}
- Task knowledge: {f['task_knowledge_path']}
- Error book: {f['error_book_path']}
- Personal knowledge base: {f['knowledge_path']}"""

    prompt += f"""

## Specific procedures (Read on demand)
- Continue current task / supervisor explicitly allows merge / create new task: {playbook_ref}
- Note: when the user only says "continue", only continue implementation, verification, or progress reporting for the current task; it is not merge approval. Merge is allowed only when the user explicitly says "allow merge", "you can merge", "approved to merge", or equivalent.

## Session-end Checklist (must be included at the end of every reply)

📋 Checklist:
I am {name}, current task: {task_id}, Session: {next_session}
Scenario: <C - working | D - Working→Idle PR merged>

[Session end confirmation]
- [x] Pushed
- [x] status.md updated: <one line>
- [x] history_log.md updated: <one line>
- [x] task_knowledge.md updated: <description> / N/A

This turn:
- <what was done>

Next:
- <concrete actionable items>

⚠️ Format check: Stop hook validates at end of reply; failures cost 2x.
⚠️ Forbidden: "TBD", "later", "next time", "will batch later"
"""
    return prompt


def _build_idle_context_en(name, repo, project_name, f):
    """Idle context (EN, ~0.8KB)."""
    playbook_ref = f"{f['intern_dir']}/playbook.md"
    project_rule = f.get("project_rule", "")

    prompt = f"""You are {name}, working in project {project_name}.

## Path quick-reference
| Use | Path |
|------|------|
| Code | {repo}/ |
| Status | {f['status_path']} |
| Tasks | {f['tasks_dir']} |

## Current state
Idle, no task in progress.
{_workspace_context_en(f)}
{_team_role_context_en(f)}
{_role_specific_context_en(f)}

## Work rules
- Must push after commit
- ⚠️ Never push master directly; go through PR
- When the supervisor only asks you to create a task, create only task metadata and remain Idle; do not accept/assign it to yourself, create/switch a task branch, edit code, or implement until a separate assignment arrives

## AskUser / request_user_input rules
- When calling Claude `AskUserQuestion` or Codex `request_user_input`, put all decision context in the tool payload `question`, and put each option's tradeoffs, risks, and recommendation reason in `options[].description`
- Do not rely only on ordinary prose immediately before the tool call; the Feishu card reliably shows only the tool payload before the user chooses
"""
    if project_rule:
        prompt += f"""
## Project rules
{project_rule}
"""

    prompt += f"""
## Reference files (Read on demand)
- Error book: {f['error_book_path']}
- Personal knowledge base: {f['knowledge_path']}

## Specific procedures (Read on demand)
- Create task / accept task (Idle→Working): {playbook_ref}

## Session-end Checklist (must be included at the end of every reply)

📋 Checklist:
I am {name}, current task: none
Scenario: <Idle - idle | Idle - create task | A - Idle→Working>

This turn:
- <what was done>

Next:
- <concrete actionable items>

⚠️ Forbidden: "TBD", "later", "next time", "will batch later"
"""
    return prompt
    """每次 UPS 写 playbook 到 {intern_dir}/playbook.md（debug 同级，repo 外）。"""
    name = f["intern_name"]
    repo = f["repo_dir"]
    shared_repo = os.path.join(WORK_AGENTS_ROOT, os.path.basename(repo))
    task_id = f["task_id"]
    status = f["status"]
    provider = f["provider"]
    pr_number = f["pr_number"] or "xx"
    playbook_path = os.path.join(f["intern_dir"], "playbook.md")

    if status == "Working" and task_id:
        if get_locale() == "en":
            content = _build_working_playbook_en(name, repo, shared_repo, task_id, pr_number, provider)
        else:
            content = _build_working_playbook(name, repo, shared_repo, task_id, pr_number, provider)
    else:
        if get_locale() == "en":
            content = _build_idle_playbook_en(name, repo, shared_repo, provider)
        else:
            content = _build_idle_playbook(name, repo, shared_repo, provider)

    os.makedirs(os.path.dirname(playbook_path), exist_ok=True)
    with open(playbook_path, "w") as fp:
        fp.write(content)


def _build_working_playbook_en(name, repo, shared_repo, task_id, pr_number, provider):
    """Working playbook (EN): PR Merge + create new task."""
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. PR Merge completion procedure

Execute these steps only when the supervisor explicitly says "allow merge", "you can merge", "approved to merge", or equivalent.
If the supervisor only says "continue", continue implementation, verification, tests, or progress reporting; do not treat "continue" as merge approval, do not mark the task Completed, and do not run `codeup_pr merge`.

### Steps 1-4: pre-merge state updates

On **your own branch**, complete the updates and push:

1. Update `workspace/interns/{name}/status.md`:
   - METADATA line (line 3): `<!-- METADATA:STATUS=Idle,TASK=,ROLE=<preserve existing ROLE, default independent> -->`
   - Status in the table: Working -> Idle
   - Current task in the table: clear

2. Update task README METADATA (line 3): `<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - Worker tasks assigned by a team_lead also complete through this step; after PR merge, keep the task Completed and report the merge result to the team_lead through mailbox.

3. Distill valuable content from task_knowledge.md into your personal knowledge base:
   `workspace/interns/{name}/knowledge.md`

4. Commit all updates:
```bash
git add workspace/
git commit -m "Complete task {task_id}"
git push
```

### Step 5: Merge PR

5. You merge the PR yourself: `codeup_pr merge <pr_number>` (default: squash — multiple commits on the branch are squashed into one; pass `--merge-type no-fast-forward` explicitly if you need to keep the branch commit history). If merge is rejected (e.g. 405 not allowed), there is usually a conflict with master: run `git fetch origin && git merge origin/master`, resolve and push, then retry merge; if it still fails, report to the supervisor.

### Steps 6-8: post-merge cleanup

6. Confirm PR is merged:
{pr_view_cmd}
- If `state` is not `MERGED`, **stop cleanup** and confirm with supervisor
- Continue only after `mergedAt` has a value

7. Clean up local branch:
```bash
cd {repo}
git checkout master && git pull origin master
git branch -d {name}/{task_id}
```

8. Clean up temporary files:
```bash
rm -rf {repo}/../debug/*
rm -rf {repo}/../outputs/*
```

---

## 2. Create new task procedure

Used when a complex task needs to be split into subtasks.

> **`<new_task_id>` convention**: full directory name (format `taskNNN_description` or `task_description`); use the full directory name in files and commit messages.

[WARN] **Must operate in shared repo `{shared_repo}`**. Do not modify `workspace/tasks/` from the current task branch (`{repo}`) and push master — it will pollute the task branch or cause conflicts. The shared repo always stays on master; do not checkout.

```bash
cd {shared_repo}
git pull --ff-only origin master
mkdir -p workspace/tasks/<new_task_id>
```

Create the following files (must include the METADATA header):

**README.md**:
```markdown
# <new_task_id> - Task title

<!-- METADATA:STATUS=Open,ASSIGNEE= -->

## Background
...
## Goals
...
## Acceptance criteria
- [ ] ...
```

**history_log.md**:
```markdown
# <new_task_id> - History log

<!-- METADATA:SESSION=0 -->

---

## Session 0 - YYYY-MM-DD - Init

**Executor**: {name}

Task created

---
```

**task_knowledge.md**:
```markdown
# <new_task_id> - Task knowledge

<!-- METADATA:SESSION=0 -->

> **Writing rule**: one line each, format `N. category: content`
>
> Categories: supervisor request, technical fact, file change, research conclusion

---

## Knowledge entries

(Task not started; no knowledge yet)

---
```

Commit:
```bash
cd {shared_repo}
git add workspace/tasks/<new_task_id>/
git commit -m "[{name}] Create subtask <new_task_id>"
git push origin master
```
"""


def _build_idle_playbook_en(name, repo, shared_repo, provider):
    """Idle playbook (EN): create task + accept task + respond."""
    pr_create_cmd = _build_pr_create_cmd(provider, name)

    return f"""# Idle Playbook

> **`<task_id>` convention**: every `<task_id>` below means the **full directory name** under `workspace/tasks/` (e.g. `task152_cleanup_question_resolve_dead_code`). The supervisor may say a short alias (e.g. `task152`) verbally; first `ls {shared_repo}/workspace/tasks/` to find the full directory name. status.md METADATA and tables must use the full directory name.

## 1. Create task procedure

Execute when the supervisor asks to create a new task. Status remains Idle afterward.
Unless the supervisor separately says "assign <task_id> to you" or "accept <task_id>", stop after creating and pushing the task; do not create a task branch, create a PR/MR, edit code files, or change status to Working.

[WARN] **Must operate in shared repo `{shared_repo}`**. Do not modify `workspace/tasks/` from your worktree (`{repo}`) and push master — it will pollute the current task branch or cause conflicts. The shared repo always stays on master; do not checkout.

```bash
cd {shared_repo}
git pull --ff-only origin master
mkdir -p workspace/tasks/<task_id>
```

Create README.md / history_log.md / task_knowledge.md (METADATA on line 3):

- README.md: `<!-- METADATA:STATUS=Open,ASSIGNEE= -->` + background/goals/acceptance
- history_log.md: `<!-- METADATA:SESSION=0 -->` + Session 0 init entry
- task_knowledge.md: `<!-- METADATA:SESSION=0 -->` + writing rule + empty entries

> Reference templates: same-named files of existing tasks under {shared_repo}/workspace/tasks/

```bash
cd {shared_repo}
git add workspace/tasks/<task_id>/
git commit -m "[{name}] Create task <task_id>"
git push origin master
```

Stop here and reply with the Checklist. Use scenario `Idle - create task`.

---

## 2. Accept task procedure

```bash
cd {repo}
git checkout master && git pull origin master
cat workspace/tasks/<task_id>/README.md
cat workspace/tasks/<task_id>/history_log.md
cat workspace/tasks/<task_id>/task_knowledge.md
```

Create branch:
```bash
git checkout -b {name}/<task_id>
```
If the supervisor specified a base branch: `git checkout -b {name}/<task_id> origin/<base_branch>`

Placeholder commit and push:
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "[<task_id>] init"
git push -u origin {name}/<task_id>
```

Create PR:
{pr_create_cmd}
If the supervisor specified a target branch, substitute `<target_branch>`; otherwise default `master`.

Update state:
- `workspace/interns/{name}/status.md` METADATA: `<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<preserve existing ROLE, default independent> -->`
- Table: status -> Working, current task -> <task_id>, PR -> <pr_url>
- Task README METADATA: `<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
git add workspace/ && git commit -m "Accept task <task_id>" && git push
```

---

## 3. Other situations

Respond normally to the supervisor's questions or instructions.
"""


def _build_local_only_working_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    default_branch = f.get("default_branch") or "master"
    return f"""# Working Playbook

## 1. local_only Merge Closeout

Follow these steps only when the supervisor explicitly says "allow merge", "you can merge", "approved to merge", or equivalent. local_only task metadata stays on this machine; do not commit or push metadata-only changes.
If the supervisor only says "continue", continue implementation, verification, tests, or progress reporting; do not treat "continue" as merge approval, do not mark the task Completed, and do not merge the task branch.

1. Update `{f['status_path']}`:
   - METADATA becomes `<!-- METADATA:STATUS=Idle,TASK=,ROLE=<preserve existing ROLE, default independent> -->`
   - Status becomes Idle, current task is cleared, PR may be N/A

2. Update `{f['task_readme_path']}`:
   - METADATA becomes `<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - The status value must be the literal `Completed`; do not write `Done`, `Closed`, or translated status words

3. Append history and refine task knowledge:
   - `{f['history_log_path']}`
   - `{f['task_knowledge_path']}`
   - `{f['knowledge_path']}`

4. Merge the local task branch only after approval:

```bash
cd {repo}
git status
git checkout {default_branch}
git merge --ff-only {name}/{task_id} || git merge --no-ff {name}/{task_id} -m "{task_id}: merge approved local work"
```

If this repo has a writable `origin`, you may run `git push origin {default_branch}`. If origin is not writable, keep the local master ahead and say so in your reply.
"""


def _build_local_only_idle_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    default_branch = f.get("default_branch") or "master"
    metadata_root = f["metadata_root"]
    status_path = f["status_path"]
    return f"""# Idle Playbook

> Every `<task_id>` below means the full directory name under `{metadata_root}/tasks/`.

## 1. Create Task
If the supervisor did not separately say "assign <task_id> to you" or "accept <task_id>", stop after creating the task metadata; do not create a branch, edit code files, or change status to Working.
Task creation and task assignment are separate actions. During task creation, do not create/switch a task branch, write the requested code file, commit code, push a code branch, mark the README InProgress, or mark `{status_path}` Working.

```bash
mkdir -p {metadata_root}/tasks/<task_id>
```

Create README.md / history_log.md / task_knowledge.md with these METADATA lines:
- README.md: `<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md: `<!-- METADATA:SESSION=0 -->`
- task_knowledge.md: `<!-- METADATA:SESSION=0 -->`

local_only metadata stays on this machine; do not commit or push metadata-only changes.
Stop here and reply with the Checklist. Use scenario `Idle - create task`. Even if the task asks for code changes, wait for a later assignment/accept instruction before implementing.

## 2. Accept Task

Run this section only when the supervisor explicitly says "assign <task_id> to you" or "accept <task_id>". Do not automatically enter this section just because you created the task.

```bash
cat {metadata_root}/tasks/<task_id>/README.md
cat {metadata_root}/tasks/<task_id>/history_log.md
cat {metadata_root}/tasks/<task_id>/task_knowledge.md
cd {repo}
git checkout {default_branch}
git checkout -b {name}/<task_id>
```

Update local metadata:
- `{status_path}` METADATA: `<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<preserve existing ROLE, default independent> -->`
- `{status_path}` table: status Working, current task `<task_id>`
- `{metadata_root}/tasks/<task_id>/README.md` METADATA: `<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

Commit code changes on `{name}/<task_id>`. After implementation, stay Working and wait for explicit supervisor merge approval; do not merge early, do not mark Completed, and do not switch yourself back to Idle.
"""


# ================================================================
# Playbook 写盘 — 按 locale 选 ZH/EN builder
# ================================================================

def _metadata_checkout_path(f):
    metadata = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
    return metadata.get("metadata_checkout_path") or f.get("metadata_root") or f.get("repo_dir")


def _metadata_branch_name(f):
    if f.get("metadata_mode") == "metadata_branch":
        return f.get("metadata_branch") or "intern_workspace"
    return f.get("default_branch") or "master"


def _metadata_rel_path(f, path):
    base = _metadata_checkout_path(f)
    if not path:
        return ""
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _build_resolver_working_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    provider = f["provider"]
    pr_number = f["pr_number"] or "xx"
    default_branch = f.get("default_branch") or "master"
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    task_readme_rel = _metadata_rel_path(f, f.get("task_readme_path", ""))
    task_knowledge_rel = _metadata_rel_path(f, f.get("task_knowledge_path", ""))
    knowledge_rel = _metadata_rel_path(f, f.get("knowledge_path", ""))
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. PR Merge 完结流程

只有主管明确说“允许 merge / 可以 merge / 批准 merge”等语义时，才按以下步骤执行。
如果主管只说“继续”，你必须继续实现、复核、补测试或汇报当前状态；禁止把“继续”解释为 merge 批准，禁止把任务标记 Completed，禁止执行 merge。

### 步骤 1-4：更新 resolver metadata

metadata 必须写入 resolver 指定路径 `{metadata_root}`。不要使用代码 checkout 中残留的 `workspace/` 或 `.intern_workspace/`，除非它正是上面的 metadata root。

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
```

1. 更新 `{status_rel}`：
   - METADATA 行：`<!-- METADATA:STATUS=Idle,TASK=,ROLE=<保持原 ROLE，缺省 independent> -->`
   - 表格中的状态改为 Idle，当前任务清空，PR 可写 N/A

2. 更新 `{task_readme_rel}`：
   - METADATA 行：`<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - 状态值必须是字面值 `Completed`，不能写 `Done`、`Closed` 或中文状态词

3. 追加历史日志、整理任务知识到个人知识库：
   - `{_metadata_rel_path(f, f.get("history_log_path", ""))}`
   - `{task_knowledge_rel}`
   - `{knowledge_rel}`

4. 提交并推送 metadata：
```bash
git add {status_rel} {task_readme_rel} {_metadata_rel_path(f, f.get("history_log_path", ""))} {task_knowledge_rel} {knowledge_rel}
git commit -m "完成任务 {task_id}"
git push origin {metadata_branch}
```

### 步骤 5：Merge PR

```bash
cd {repo}
```

由你自行执行 merge PR：`codeup_pr merge <pr_number>`（默认走 squash；如需保留分支 commit 历史，显式加 `--merge-type no-fast-forward`）。如果 merge 被拒绝，通常是分支与 `{default_branch}` 有 conflict：先 `git fetch origin && git merge origin/{default_branch}` 解决冲突并 push，再重试 merge；仍失败则汇报主管。

### 步骤 6-8：Merge 后清理

6. 确认 PR 已 merge：
{pr_view_cmd}
- 如果 `state` 不是 `MERGED`，停止清理并向主管确认
- 确认 `mergedAt` 有值后再继续

7. 清理本地分支：
```bash
cd {repo}
git checkout {default_branch} && git pull origin {default_branch}
git branch -d {name}/{task_id}
```

8. 清理临时文件：
```bash
rm -rf {repo}/../debug {repo}/../outputs && mkdir -p {repo}/../debug {repo}/../outputs
```

---

## 2. 创建新任务流程

复杂任务需要拆分子任务时使用。新任务 metadata 写入 `{tasks_rel}/<new_task_id>`，并推送 metadata 分支 `{metadata_branch}`。

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
mkdir -p {tasks_rel}/<new_task_id>
```

创建 README.md / history_log.md / task_knowledge.md，METADATA 行分别使用：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md：`<!-- METADATA:SESSION=0 -->`
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->`

```bash
git add {tasks_rel}/<new_task_id>/
git commit -m "[{name}] 创建子任务 <new_task_id>"
git push origin {metadata_branch}
```
"""


def _build_repo_dotdir_working_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    provider = f["provider"]
    pr_number = f["pr_number"] or "xx"
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    task_readme_rel = _metadata_rel_path(f, f.get("task_readme_path", ""))
    history_rel = _metadata_rel_path(f, f.get("history_log_path", ""))
    task_knowledge_rel = _metadata_rel_path(f, f.get("task_knowledge_path", ""))
    knowledge_rel = _metadata_rel_path(f, f.get("knowledge_path", ""))
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. repo_dotdir PR Merge 完结流程

只有主管明确说“允许 merge / 可以 merge / 批准 merge”等语义时，才按以下步骤执行。
如果主管只说“继续”，你必须继续实现、复核、补测试或汇报当前状态；禁止把“继续”解释为 merge 批准，禁止把任务标记 Completed，禁止执行 merge。

repo_dotdir metadata 与代码同仓同分支，当前任务的 metadata 必须在自己的任务分支上更新并随 PR 合并。metadata root 是 `{metadata_root}`。

```bash
cd {repo}
git status
```

1. 更新 `{status_rel}`：
   - METADATA 行：`<!-- METADATA:STATUS=Idle,TASK=,ROLE=<保持原 ROLE，缺省 independent> -->`
   - 表格中的状态改为 Idle，当前任务清空，PR 可写 N/A

2. 更新 `{task_readme_rel}`：
   - METADATA 行：`<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - 状态值必须是字面值 `Completed`

3. 追加历史日志、整理任务知识到个人知识库：
   - `{history_rel}`
   - `{task_knowledge_rel}`
   - `{knowledge_rel}`

4. 提交并推送任务分支：
```bash
git add {status_rel} {task_readme_rel} {history_rel} {task_knowledge_rel} {knowledge_rel}
git commit -m "完成任务 {task_id}"
git push
```

5. Merge PR：`codeup_pr merge <pr_number>`。如果 merge 被拒绝，先 `git fetch origin && git merge origin/{default_branch}` 解决冲突并 push，再重试。

6. 确认 PR 已 merge：
{pr_view_cmd}

7. 清理本地分支：
```bash
git checkout {default_branch} && git pull origin {default_branch}
git branch -d {name}/{task_id}
```

8. 清理临时文件：
```bash
rm -rf {repo}/../debug {repo}/../outputs && mkdir -p {repo}/../debug {repo}/../outputs
```

## 2. 创建新任务流程

复杂任务需要拆分子任务时使用。为避免污染当前任务分支，新任务 metadata 通过临时 worktree 写到 `{default_branch}`。

```bash
cd {repo}
git fetch origin {default_branch}
tmp_dir=$(mktemp -d {repo}/../metadata-task.XXXXXX)
git worktree add "$tmp_dir" origin/{default_branch}
cd "$tmp_dir"
mkdir -p {tasks_rel}/<new_task_id>
```

创建 README.md / history_log.md / task_knowledge.md，METADATA 行分别使用：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md：`<!-- METADATA:SESSION=0 -->`
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->`

```bash
git add {tasks_rel}/<new_task_id>/
git commit -m "[{name}] 创建子任务 <new_task_id>"
git push origin HEAD:{default_branch}
cd {repo}
git worktree remove "$tmp_dir"
```
"""


def _build_repo_dotdir_idle_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f["provider"]
    pr_create_cmd = _build_pr_create_cmd(provider, name)
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))

    return f"""# Idle Playbook

> **`<task_id>` 约定**：下文所有 `<task_id>` 均指 `{metadata_root}/tasks/` 下的完整目录名。repo_dotdir 的 metadata 在代码仓 `.intern_workspace` 下；不要使用旧版 `workspace/tasks`。

## 1. 创建任务流程

主管要求创建新任务时执行。创建后仍为 Idle。
只要主管没有单独说“分配 <task_id> 给你”或“接受 <task_id>”，到创建任务并 push 后就停止；不要切任务分支，不要创建 PR/MR，不要修改代码文件，不要把 status 改成 Working。

```bash
cd {repo}
git checkout {default_branch}
git pull --ff-only origin {default_branch}
mkdir -p {tasks_rel}/<task_id>
```

创建 README.md / history_log.md / task_knowledge.md（METADATA 在第三行）：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->` + 背景/目标/验收标准
- history_log.md：`<!-- METADATA:SESSION=0 -->` + Session 0 初始化记录
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->` + 空知识条目

```bash
git add {tasks_rel}/<task_id>/
git commit -m "[{name}] 创建任务 <task_id>"
git push origin {default_branch}
```

到这里必须停止并回复 Checklist，场景写 `Idle - 创建任务`。

---

## 2. 接受任务流程

仅当用户明确说“分配 <task_id> 给你”或“接受 <task_id>”时执行本节。不要因为自己刚创建了任务就自动进入本节。

```bash
cd {repo}
git checkout {default_branch}
git pull --ff-only origin {default_branch}
cat {tasks_rel}/<task_id>/README.md
cat {tasks_rel}/<task_id>/history_log.md
cat {tasks_rel}/<task_id>/task_knowledge.md
git checkout -b {name}/<task_id>
```

占位 commit 并推送代码任务分支：
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "【<task_id>】初始化"
git push -u origin {name}/<task_id>
```

创建 PR：
{pr_create_cmd}
如果主管指定了目标分支，替换 `<target_branch>`，否则默认 `{default_branch}`。

更新任务分支上的 repo_dotdir metadata：
- `{status_rel}` METADATA：`<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<保持原 ROLE，缺省 independent> -->`
- `{status_rel}` 表格：状态改为 Working，当前任务改为 `<task_id>`，PR 写入 `<pr_url>`
- `{tasks_rel}/<task_id>/README.md` METADATA：`<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
git add {status_rel} {tasks_rel}/<task_id>/README.md
git commit -m "接受任务 <task_id>"
git push
```

---

## 3. 其他情况

正常响应主管的问题或指示。
"""


def _build_resolver_idle_playbook(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f["provider"]
    pr_create_cmd = _build_pr_create_cmd(provider, name)
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    default_branch = f.get("default_branch") or "master"

    return f"""# Idle Playbook

> **`<task_id>` 约定**：下文所有 `<task_id>` 均指 `{metadata_root}/tasks/` 下的完整目录名。禁止使用代码 checkout 中残留的 `workspace/tasks` 或 `.intern_workspace/tasks`，除非它正是 resolver 指定的 metadata root。

## 1. 创建任务流程

主管要求创建新任务时执行。创建后仍为 Idle。
只要主管没有单独说“分配 <task_id> 给你”或“接受 <task_id>”，到创建任务并 push 后就停止；不要切任务分支，不要创建 PR/MR，不要修改代码文件，不要把 status 改成 Working。

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
mkdir -p {tasks_rel}/<task_id>
```

创建 README.md / history_log.md / task_knowledge.md（METADATA 在第三行）：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->` + 背景/目标/验收标准
- history_log.md：`<!-- METADATA:SESSION=0 -->` + Session 0 初始化记录
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->` + 空知识条目

```bash
git add {tasks_rel}/<task_id>/
git commit -m "[{name}] 创建任务 <task_id>"
git push origin {metadata_branch}
```

到这里必须停止并回复 Checklist，场景写 `Idle - 创建任务`。

---

## 2. 接受任务流程

仅当用户明确说“分配 <task_id> 给你”或“接受 <task_id>”时执行本节。不要因为自己刚创建了任务就自动进入本节。

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
cat {tasks_rel}/<task_id>/README.md
cat {tasks_rel}/<task_id>/history_log.md
cat {tasks_rel}/<task_id>/task_knowledge.md

cd {repo}
git checkout {default_branch} && git pull origin {default_branch}
git checkout -b {name}/<task_id>
```

占位 commit 并推送代码任务分支：
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "【<task_id>】初始化"
git push -u origin {name}/<task_id>
```

创建 PR：
{pr_create_cmd}
如果主管指定了目标分支，替换 `<target_branch>`，否则默认 `{default_branch}`。

更新 resolver metadata：
- `{status_rel}` METADATA：`<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<保持原 ROLE，缺省 independent> -->`
- `{status_rel}` 表格：状态改为 Working，当前任务改为 `<task_id>`，PR 写入 `<pr_url>`
- `{tasks_rel}/<task_id>/README.md` METADATA：`<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git add {status_rel} {tasks_rel}/<task_id>/README.md
git commit -m "接受任务 <task_id>"
git push origin {metadata_branch}
```

---

## 3. 其他情况

正常响应主管的问题或指示。
"""


def _build_repo_dotdir_working_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    provider = f["provider"]
    pr_number = f["pr_number"] or "xx"
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    task_readme_rel = _metadata_rel_path(f, f.get("task_readme_path", ""))
    history_rel = _metadata_rel_path(f, f.get("history_log_path", ""))
    task_knowledge_rel = _metadata_rel_path(f, f.get("task_knowledge_path", ""))
    knowledge_rel = _metadata_rel_path(f, f.get("knowledge_path", ""))
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. repo_dotdir PR Merge Closeout

Run only when the supervisor explicitly approves merge. repo_dotdir metadata lives in the code repo and must be updated on your task branch so it is merged through the PR. Metadata root: `{metadata_root}`.

```bash
cd {repo}
git status
```

1. Update `{status_rel}` to Idle.
2. Update `{task_readme_rel}` to `<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`.
3. Append history and refine knowledge:
   - `{history_rel}`
   - `{task_knowledge_rel}`
   - `{knowledge_rel}`

```bash
git add {status_rel} {task_readme_rel} {history_rel} {task_knowledge_rel} {knowledge_rel}
git commit -m "Complete task {task_id}"
git push
codeup_pr merge <pr_number>
```

Confirm PR is merged:
{pr_view_cmd}

```bash
git checkout {default_branch} && git pull origin {default_branch}
git branch -d {name}/{task_id}
rm -rf {repo}/../debug {repo}/../outputs && mkdir -p {repo}/../debug {repo}/../outputs
```

## 2. Create new task

Use a temporary worktree so the new metadata lands on `{default_branch}`, not on the current task branch.

```bash
cd {repo}
git fetch origin {default_branch}
tmp_dir=$(mktemp -d {repo}/../metadata-task.XXXXXX)
git worktree add "$tmp_dir" origin/{default_branch}
cd "$tmp_dir"
mkdir -p {tasks_rel}/<new_task_id>
git add {tasks_rel}/<new_task_id>/
git commit -m "[{name}] Create subtask <new_task_id>"
git push origin HEAD:{default_branch}
cd {repo}
git worktree remove "$tmp_dir"
```
"""


def _build_repo_dotdir_idle_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f["provider"]
    pr_create_cmd = _build_pr_create_cmd(provider, name)
    default_branch = f.get("default_branch") or "master"
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))

    return f"""# Idle Playbook

> Every `<task_id>` below means the full directory name under `{metadata_root}/tasks/`. repo_dotdir metadata lives under `.intern_workspace` in the code repo; do not use legacy `workspace/tasks`.

## 1. Create task

Unless the supervisor separately says "assign <task_id> to you" or "accept <task_id>", stop after creating and pushing the task; do not create a task branch, create a PR/MR, edit code files, or change status to Working.

```bash
cd {repo}
git checkout {default_branch}
git pull --ff-only origin {default_branch}
mkdir -p {tasks_rel}/<task_id>
git add {tasks_rel}/<task_id>/
git commit -m "[{name}] Create task <task_id>"
git push origin {default_branch}
```

Stop here and reply with the Checklist. Use scenario `Idle - create task`.

## 2. Accept task

Run this section only when the supervisor explicitly says "assign <task_id> to you" or "accept <task_id>".

```bash
cd {repo}
git checkout {default_branch}
git pull --ff-only origin {default_branch}
cat {tasks_rel}/<task_id>/README.md
cat {tasks_rel}/<task_id>/history_log.md
cat {tasks_rel}/<task_id>/task_knowledge.md
git checkout -b {name}/<task_id>
```

Create and push the code branch, then create PR:
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "[<task_id>] init"
git push -u origin {name}/<task_id>
```

{pr_create_cmd}

Update repo_dotdir metadata on the task branch:
- `{status_rel}` METADATA: `<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<preserve existing ROLE, default independent> -->`
- `{tasks_rel}/<task_id>/README.md` METADATA: `<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
git add {status_rel} {tasks_rel}/<task_id>/README.md
git commit -m "Accept task <task_id>"
git push
```
"""


def _build_resolver_working_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    task_id = f["task_id"]
    provider = f["provider"]
    pr_number = f["pr_number"] or "xx"
    default_branch = f.get("default_branch") or "master"
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    task_readme_rel = _metadata_rel_path(f, f.get("task_readme_path", ""))
    history_rel = _metadata_rel_path(f, f.get("history_log_path", ""))
    task_knowledge_rel = _metadata_rel_path(f, f.get("task_knowledge_path", ""))
    knowledge_rel = _metadata_rel_path(f, f.get("knowledge_path", ""))
    pr_view_cmd = _build_pr_view_cmd(provider, repo, pr_number)

    return f"""# Working Playbook

## 1. PR Merge Closeout

Execute only when the supervisor explicitly says "allow merge", "you can merge", "approved to merge", or equivalent.
If the supervisor only says "continue", continue implementation, verification, tests, or progress reporting; do not treat "continue" as merge approval, do not mark the task Completed, and do not merge.

### Steps 1-4: update resolver metadata

Metadata must be written under resolver path `{metadata_root}`. Do not use stale `workspace/` or `.intern_workspace/` directories in the code checkout unless they are exactly this metadata root.

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
```

1. Update `{status_rel}`:
   - METADATA becomes `<!-- METADATA:STATUS=Idle,TASK=,ROLE=<preserve existing ROLE, default independent> -->`
   - Status becomes Idle, current task is cleared, PR may be N/A

2. Update `{task_readme_rel}`:
   - METADATA becomes `<!-- METADATA:STATUS=Completed,ASSIGNEE={name} -->`
   - The status value must be the literal `Completed`

3. Append history and refine task knowledge:
   - `{history_rel}`
   - `{task_knowledge_rel}`
   - `{knowledge_rel}`

4. Commit and push metadata:
```bash
git add {status_rel} {task_readme_rel} {history_rel} {task_knowledge_rel} {knowledge_rel}
git commit -m "Complete task {task_id}"
git push origin {metadata_branch}
```

### Step 5: merge PR

```bash
cd {repo}
```

Merge the PR yourself: `codeup_pr merge <pr_number>`. If merge is rejected, run `git fetch origin && git merge origin/{default_branch}`, resolve conflicts, push, then retry.

### Steps 6-8: post-merge cleanup

6. Confirm PR is merged:
{pr_view_cmd}

7. Clean local branch:
```bash
cd {repo}
git checkout {default_branch} && git pull origin {default_branch}
git branch -d {name}/{task_id}
```

8. Clean temporary files:
```bash
rm -rf {repo}/../debug {repo}/../outputs && mkdir -p {repo}/../debug {repo}/../outputs
```

## 2. Create new task

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
mkdir -p {tasks_rel}/<new_task_id>
```

Create README.md / history_log.md / task_knowledge.md with METADATA headers, then:

```bash
git add {tasks_rel}/<new_task_id>/
git commit -m "[{name}] Create subtask <new_task_id>"
git push origin {metadata_branch}
```
"""


def _build_resolver_idle_playbook_en(f):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f["provider"]
    pr_create_cmd = _build_pr_create_cmd(provider, name)
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    metadata_root = f.get("metadata_root", "")
    tasks_rel = _metadata_rel_path(f, f.get("tasks_dir", ""))
    status_rel = _metadata_rel_path(f, f.get("status_path", ""))
    default_branch = f.get("default_branch") or "master"

    return f"""# Idle Playbook

> Every `<task_id>` below means the full directory name under `{metadata_root}/tasks/`. Do not use stale `workspace/tasks` or `.intern_workspace/tasks` directories in the code checkout unless they are exactly the resolver metadata root.

## 1. Create task

Execute when the supervisor asks to create a new task. Status remains Idle afterward.
Unless the supervisor separately says "assign <task_id> to you" or "accept <task_id>", stop after creating and pushing the task; do not create a task branch, create a PR/MR, edit code files, or change status to Working.

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
mkdir -p {tasks_rel}/<task_id>
```

Create README.md / history_log.md / task_knowledge.md with METADATA headers, then:

```bash
git add {tasks_rel}/<task_id>/
git commit -m "[{name}] Create task <task_id>"
git push origin {metadata_branch}
```

Stop here and reply with the Checklist. Use scenario `Idle - create task`.

## 2. Accept task

Run this section only when the supervisor explicitly says "assign <task_id> to you" or "accept <task_id>".

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git pull --ff-only origin {metadata_branch}
cat {tasks_rel}/<task_id>/README.md
cat {tasks_rel}/<task_id>/history_log.md
cat {tasks_rel}/<task_id>/task_knowledge.md

cd {repo}
git checkout {default_branch} && git pull origin {default_branch}
git checkout -b {name}/<task_id>
```

Create and push the code branch, then create PR:
```bash
echo "# WIP" >> WIP.md && git add WIP.md
git commit -m "[<task_id>] init"
git push -u origin {name}/<task_id>
```

{pr_create_cmd}

Update resolver metadata:
- `{status_rel}` METADATA: `<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<preserve existing ROLE, default independent> -->`
- `{tasks_rel}/<task_id>/README.md` METADATA: `<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

```bash
cd {metadata_checkout}
git checkout {metadata_branch}
git add {status_rel} {tasks_rel}/<task_id>/README.md
git commit -m "Accept task <task_id>"
git push origin {metadata_branch}
```
"""


def _playbook_profile_zh(f):
    return f"""## 当前 Workspace Profile

- workspace_id：{f.get("workspace_id") or f.get("workspace_key") or "未设置"}
- mode：{f.get("metadata_mode", "repo_dotdir")}
- repo provider：{f.get("provider", "github")}
- runtime provider：{f.get("runtime_provider", "")}
- role：{f.get("role", "independent")}
- metadata root：{f.get("metadata_root", "")}
- status：{f.get("status_path", "")}
- tasks：{f.get("tasks_dir", "")}

### 写入规则
{_write_rule_zh(f)}
"""


def _playbook_profile_en(f):
    return f"""## Current Workspace Profile

- workspace_id: {f.get("workspace_id") or f.get("workspace_key") or "unset"}
- mode: {f.get("metadata_mode", "repo_dotdir")}
- repo provider: {f.get("provider", "github")}
- runtime provider: {f.get("runtime_provider", "")}
- role: {f.get("role", "independent")}
- metadata root: {f.get("metadata_root", "")}
- status: {f.get("status_path", "")}
- tasks: {f.get("tasks_dir", "")}

### Write Rules
{_write_rule_en(f)}
"""


# ================================================================
# State v1 playbook builder
# ================================================================

def _workspace_source_path(f):
    metadata = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
    return f.get("workspace_source_path") or metadata.get("workspace_source_path") or f.get("repo_dir")


def _rel_to_checkout(f, path):
    checkout = _metadata_checkout_path(f)
    if not path:
        return ""
    try:
        return os.path.relpath(path, checkout)
    except ValueError:
        return path


def _repo_rel(repo, path):
    if not path:
        return ""
    try:
        return os.path.relpath(path, repo)
    except ValueError:
        return path


def _default_branch_refresh(repo, default_branch):
    return (
        f"cd {repo}\n"
        "git fetch origin\n"
        f"git checkout {default_branch}\n"
        f"git pull --ff-only origin {default_branch}"
    )


def _pr_create_assignment_cmd(provider, name, default_branch, locale):
    if provider == "codeup":
        if locale == "en":
            return f'''PR_URL=$(codeup_pr create --title "[$TASK_FULL_NAME][{name}] <description>" --base {default_branch} --body "## Task
$TASK_FULL_NAME

## Owner
{name}

## Status
In progress")'''
        return f'''PR_URL=$(codeup_pr create --title "【$TASK_FULL_NAME】【{name}】<描述>" --base {default_branch} --body "## 任务
$TASK_FULL_NAME

## Owner
{name}

## 状态
进行中")'''
    if locale == "en":
        return f'''PR_URL=$(gh pr create --title "[$TASK_FULL_NAME][{name}] <description>" --base {default_branch} --head {name}/$TASK_FULL_NAME --body "## Task
$TASK_FULL_NAME

## Owner
{name}

## Status
In progress")'''
    return f'''PR_URL=$(gh pr create --title "【$TASK_FULL_NAME】【{name}】<描述>" --base {default_branch} --head {name}/$TASK_FULL_NAME --body "## 任务
$TASK_FULL_NAME

## Owner
{name}

## 状态
进行中")'''


def _pr_merge_cmd(provider, repo, pr_number, default_branch):
    number = pr_number or "PR_NUMBER"
    if provider == "codeup":
        view = _build_pr_view_cmd(provider, repo, number)
        return f"""```bash
cd {repo}
PR_NUMBER="{number}"
codeup_pr merge "$PR_NUMBER"
{view.strip()}
git checkout {default_branch}
git pull --ff-only origin {default_branch}
```"""
    return f"""```bash
cd {repo}
PR_NUMBER="{number}"
gh pr merge "$PR_NUMBER" --squash
gh pr view "$PR_NUMBER" --json state,mergedAt --jq '{{{{ state, mergedAt }}}}'
git checkout {default_branch}
git pull --ff-only origin {default_branch}
```"""


def _state_v1_common_paths(f, is_working, locale):
    repo = f["repo_dir"]
    metadata_root = f.get("metadata_root", "")
    metadata_checkout = _metadata_checkout_path(f) if f.get("metadata_mode") == "metadata_branch" else ""
    source = _workspace_source_path(f)
    default_branch = f.get("default_branch") or "master"
    tasks_root = f.get("tasks_dir") or os.path.join(metadata_root, "tasks")
    status_path = f.get("status_path", "")
    knowledge_path = f.get("knowledge_path", "")
    task_dir = f.get("task_dir", "")
    history_log_path = f.get("history_log_path", "")
    task_knowledge_path = f.get("task_knowledge_path", "")
    if locale == "en":
        text = f"""## File Paths

- Code checkout: `{repo}`
- Workspace source checkout: `{source}`
- Metadata root: `{metadata_root}`
- Status file: `{status_path}`
- Personal knowledge: `{knowledge_path}`
- Tasks root: `{tasks_root}`
- Default code branch: `{default_branch}`
"""
        if is_working:
            text += f"""- Current task directory: `{task_dir}`
- Current history log: `{history_log_path}`
- Current task knowledge: `{task_knowledge_path}`
"""
        if metadata_checkout:
            text += f"- Status checkout: `{metadata_checkout}`\n"
        return text
    text = f"""## 文件路径

- 代码 checkout：`{repo}`
- Workspace source checkout：`{source}`
- Metadata root：`{metadata_root}`
- 状态文件：`{status_path}`
- 个人知识库：`{knowledge_path}`
- 任务根目录：`{tasks_root}`
- 代码默认分支：`{default_branch}`
"""
    if is_working:
        text += f"""- 当前任务目录：`{task_dir}`
- 当前历史日志：`{history_log_path}`
- 当前任务知识：`{task_knowledge_path}`
"""
    if metadata_checkout:
        text += f"- Status checkout：`{metadata_checkout}`\n"
    return text


def _state_v1_task_name_rule(tasks_root, locale):
    if locale == "en":
        return f"""## Complete Task Directory Name Rule

Always use the complete directory name under `{tasks_root}`. If the supervisor says a shorthand like `task377`, list `{tasks_root}` first and resolve the complete name before editing status, README, history, knowledge, branch names, commits, or PR text.
"""
    return f"""## 完整任务目录名约定

下文的任务名都必须使用 `{tasks_root}` 下的完整目录名。如果主管口头说简称（例如 `task377`），先查看 `{tasks_root}` 并解析成完整目录名，再写入 status、README、history、knowledge、分支名、提交信息和 PR 文本。
"""


def _task_template(tasks_root, task_var, locale):
    if locale == "en":
        var_line = f'{task_var}="<complete task directory name, for example task377_update_readme>"'
        return f"""```bash
{var_line}
mkdir -p {tasks_root}/"${task_var}"
$EDITOR {tasks_root}/"${task_var}"/README.md
$EDITOR {tasks_root}/"${task_var}"/history_log.md
$EDITOR {tasks_root}/"${task_var}"/task_knowledge.md
```

Required metadata lines:
- README.md: `<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md: `<!-- METADATA:SESSION=0 -->`
- task_knowledge.md: `<!-- METADATA:SESSION=0 -->`"""
    var_line = f'{task_var}="<完整任务目录名，例如 task377_update_readme>"'
    return f"""```bash
{var_line}
mkdir -p {tasks_root}/"${task_var}"
$EDITOR {tasks_root}/"${task_var}"/README.md
$EDITOR {tasks_root}/"${task_var}"/history_log.md
$EDITOR {tasks_root}/"${task_var}"/task_knowledge.md
```

必须写入这些 METADATA 行：
- README.md：`<!-- METADATA:STATUS=Open,ASSIGNEE= -->`
- history_log.md：`<!-- METADATA:SESSION=0 -->`
- task_knowledge.md：`<!-- METADATA:SESSION=0 -->`"""


def _state_v1_create_task_steps(mode, f, locale, *, new_task=False):
    repo = f["repo_dir"]
    metadata_root = f.get("metadata_root", "")
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    default_branch = f.get("default_branch") or "master"
    task_var = "NEW_TASK_FULL_NAME" if new_task else "TASK_FULL_NAME"
    if mode == "repo_dotdir":
        creation_root = _workspace_source_path(f) or repo
        tasks_root = os.path.join(creation_root, ".intern_workspace", "tasks")
        tasks_rel = _repo_rel(creation_root, tasks_root)
        template = _task_template(tasks_root, task_var, locale)
        if locale == "en":
            intro = "For repo_dotdir mode, creating a task or refining an unassigned Open task may push the default branch directly because only `.intern_workspace/tasks/` metadata is changed."
        else:
            intro = "repo_dotdir 模式下，创建任务或 refine 未分配的 Open 任务内容，允许直接推送默认分支，因为只修改 `.intern_workspace/tasks/` metadata。"
        return f"""{template}

{intro}

```bash
{_default_branch_refresh(creation_root, default_branch)}
git add {tasks_rel}/"${task_var}"/
git commit -m "${task_var}: create task metadata"
git push origin {default_branch}
```"""
    if mode == "metadata_branch":
        tasks_root = os.path.join(metadata_root, "tasks")
        tasks_rel = _rel_to_checkout(f, tasks_root)
        return f"""{_task_template(tasks_root, task_var, locale)}

```bash
cd {metadata_checkout}
git fetch origin
git checkout -B {metadata_branch} origin/{metadata_branch}
git add {tasks_rel}/"${task_var}"/
git commit -m "${task_var}: create task metadata"
git push origin HEAD:{metadata_branch}
```"""
    tasks_root = os.path.join(metadata_root, "tasks")
    if locale == "en":
        return f"""{_task_template(tasks_root, task_var, locale)}

Files under `{metadata_root}` are local metadata. Do not commit or push metadata-only task creation changes."""
    return f"""{_task_template(tasks_root, task_var, locale)}

`{metadata_root}` 下的文件是本机 metadata。创建任务只保存在本机，不要 commit 或 push 纯 metadata 变化。"""


def _state_v1_assign_task_steps(mode, f, locale):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f.get("provider", "github")
    metadata_root = f.get("metadata_root", "")
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    default_branch = f.get("default_branch") or "master"
    status_path = f.get("status_path", "")
    tasks_root = f.get("tasks_dir") or os.path.join(metadata_root, "tasks")
    if locale == "en":
        stop_after_work = (
            "\nAfter assignment, if the supervisor also asks you to continue implementation in the same turn, "
            "finish code changes on the task branch and push/update the PR when a writable remote exists, then stop with status still Working. "
            "Do not mark the task Completed, do not switch yourself to Idle, and do not merge. Only merge after explicit supervisor approval.\n"
        )
    else:
        stop_after_work = (
            "\n任务完成分配后，如果主管同时要求你继续实现，就在任务分支完成代码修改，有可写远端时 push/更新 PR，然后停在 Working 状态等待主管验收。"
            "不要把任务标记 Completed，不要把自己切回 Idle，不要 merge。只有主管明确批准 merge 后，才允许执行 merge。\n"
        )

    if mode == "metadata_branch":
        task_readme_rel = os.path.join(_rel_to_checkout(f, tasks_root), '"$TASK_FULL_NAME"', "README.md")
        status_rel = _rel_to_checkout(f, status_path)
        return f"""```bash
TASK_FULL_NAME="<完整任务目录名，例如 task377_update_readme>"
cd {repo}
git fetch origin
git checkout {default_branch}
git pull --ff-only origin {default_branch}
git checkout -b {name}/$TASK_FULL_NAME
git commit --allow-empty -m "$TASK_FULL_NAME: initialize"
git push -u origin {name}/$TASK_FULL_NAME
{_pr_create_assignment_cmd(provider, name, default_branch, locale)}

cd {metadata_checkout}
git fetch origin
git checkout -B {metadata_branch} origin/{metadata_branch}
$EDITOR {task_readme_rel}
$EDITOR {status_rel}
git add {task_readme_rel} {status_rel}
git commit -m "$TASK_FULL_NAME: assign metadata"
git push origin HEAD:{metadata_branch}
```

把 README metadata 改成 `METADATA:STATUS=InProgress,ASSIGNEE={name}`。把 status metadata 改成 `METADATA:STATUS=Working,TASK=$TASK_FULL_NAME`，并把 `PR_URL` 写入状态表格。
{stop_after_work}"""
    if mode == "local_only":
        return f"""```bash
TASK_FULL_NAME="<完整任务目录名，例如 task377_update_readme>"
cat {tasks_root}/"$TASK_FULL_NAME"/README.md
cat {tasks_root}/"$TASK_FULL_NAME"/history_log.md
cat {tasks_root}/"$TASK_FULL_NAME"/task_knowledge.md
cd {repo}
git checkout {default_branch}
git checkout -b {name}/$TASK_FULL_NAME
$EDITOR {tasks_root}/"$TASK_FULL_NAME"/README.md
$EDITOR {status_path}
```

local_only 的分配 metadata 只保存在本机。把 README 改为 InProgress/ASSIGNEE={name}；把 status 改为 Working/TASK=$TASK_FULL_NAME。如果这个 repo 没有可写 origin，代码提交保留在本地任务分支并跳过 PR 命令。
{stop_after_work}"""

    task_readme_rel = os.path.join(".intern_workspace", "tasks", '"$TASK_FULL_NAME"', "README.md")
    status_rel = _repo_rel(repo, status_path)
    return f"""```bash
TASK_FULL_NAME="<完整任务目录名，例如 task377_update_readme>"
cd {repo}
git fetch origin
git checkout {default_branch}
git pull --ff-only origin {default_branch}
cat .intern_workspace/tasks/"$TASK_FULL_NAME"/README.md
cat .intern_workspace/tasks/"$TASK_FULL_NAME"/history_log.md
cat .intern_workspace/tasks/"$TASK_FULL_NAME"/task_knowledge.md
git checkout -b {name}/$TASK_FULL_NAME
git commit --allow-empty -m "$TASK_FULL_NAME: initialize"
git push -u origin {name}/$TASK_FULL_NAME
{_pr_create_assignment_cmd(provider, name, default_branch, locale)}
$EDITOR {task_readme_rel}
$EDITOR {status_rel}
git add {task_readme_rel} {status_rel}
git commit -m "$TASK_FULL_NAME: accept task"
git push
```

把 README metadata 改成 `METADATA:STATUS=InProgress,ASSIGNEE={name}`。把 status metadata 改成 `METADATA:STATUS=Working,TASK=$TASK_FULL_NAME`，并把 `PR_URL` 写入状态表格。
{stop_after_work}"""


def _state_v1_merge_closeout_steps(mode, f, locale):
    name = f["intern_name"]
    repo = f["repo_dir"]
    provider = f.get("provider", "github")
    task_id = f.get("task_id", "")
    pr_number = f.get("pr_number") or "PR_NUMBER"
    metadata_root = f.get("metadata_root", "")
    metadata_checkout = _metadata_checkout_path(f)
    metadata_branch = _metadata_branch_name(f)
    default_branch = f.get("default_branch") or "master"
    status_path = f.get("status_path", "")
    task_readme_path = f.get("task_readme_path") or os.path.join(metadata_root, "tasks", task_id, "README.md")
    history_log_path = f.get("history_log_path", "")
    task_knowledge_path = f.get("task_knowledge_path", "")
    knowledge_path = f.get("knowledge_path", "")
    if mode == "metadata_branch":
        paths = [
            _rel_to_checkout(f, status_path),
            _rel_to_checkout(f, task_readme_path),
            _rel_to_checkout(f, history_log_path),
            _rel_to_checkout(f, task_knowledge_path),
            _rel_to_checkout(f, knowledge_path),
        ]
        publish = f"""```bash
cd {metadata_checkout}
git fetch origin
git checkout -B {metadata_branch} origin/{metadata_branch}
$EDITOR {paths[0]}
$EDITOR {paths[1]}
$EDITOR {paths[2]}
$EDITOR {paths[3]}
$EDITOR {paths[4]}
git add {' '.join(paths)}
git commit -m "{task_id}: close metadata"
git push origin HEAD:{metadata_branch}
```"""
    elif mode == "local_only":
        publish = f"""```bash
$EDITOR {status_path}
$EDITOR {task_readme_path}
$EDITOR {history_log_path}
$EDITOR {task_knowledge_path}
$EDITOR {knowledge_path}
```

local_only closeout metadata stays on this machine. Do not commit or push metadata-only changes."""
    else:
        paths = [
            _repo_rel(repo, status_path),
            _repo_rel(repo, task_readme_path),
            _repo_rel(repo, history_log_path),
            _repo_rel(repo, task_knowledge_path),
            _repo_rel(repo, knowledge_path),
        ]
        publish = f"""```bash
cd {repo}
$EDITOR {paths[0]}
$EDITOR {paths[1]}
$EDITOR {paths[2]}
$EDITOR {paths[3]}
$EDITOR {paths[4]}
git add {' '.join(paths)}
git commit -m "{task_id}: close metadata"
git push
```"""

    if mode == "local_only":
        merge = f"""```bash
cd {repo}
git checkout {default_branch}
git merge --ff-only {name}/{task_id} || git merge --no-ff {name}/{task_id} -m "{task_id}: merge approved local work"
```

如果这个 repo 有可写 `origin`，可以在合并后执行 `git push origin {default_branch}`；如果没有可写远端，保留本地分支状态并说明。"""
    else:
        merge = _pr_merge_cmd(provider, repo, pr_number, default_branch)

    continue_guard_en = (
        'If the supervisor only says "continue", continue implementation, verification, tests, or progress reporting; '
        "do not treat continue as merge approval, do not mark the task Completed, and do not merge."
    )
    continue_guard_zh = (
        "如果主管只说“继续”，你必须继续实现、复核、补测试或汇报当前状态；"
        "禁止把“继续”解释为 merge 批准，禁止把任务标记 Completed，禁止执行 merge。"
    )
    if locale == "en":
        return f"""## 1. PR Merge Closeout

When the supervisor explicitly allows merge, first update metadata: status becomes Idle with empty task and PR=N/A; task README becomes Completed/ASSIGNEE={name}; append final history; refine useful task knowledge into the personal knowledge file.
{continue_guard_en}

{publish}

Then merge only after that explicit approval:

{merge}

Clean temporary debug/output files only after the merge is confirmed."""
    return f"""## 1. PR Merge 完结流程

主管明确允许 merge 时，先更新 metadata：status 改回 Idle、任务清空、PR=N/A；任务 README 改为 Completed/ASSIGNEE={name}；追加最终 history；把有价值的 task knowledge 精炼到个人知识库。
{continue_guard_zh}

{publish}

然后只在主管明确批准后执行 merge：

{merge}

确认 merge 完成后再清理 debug/output 临时文件。"""


def _build_state_v1_playbook(f, locale):
    name = f["intern_name"]
    repo = f["repo_dir"]
    status = f.get("status", "Idle")
    is_working = status == "Working" and bool(f.get("task_id"))
    mode = f.get("metadata_mode", "repo_dotdir")
    metadata_root = f.get("metadata_root", "")
    tasks_root = f.get("tasks_dir") or os.path.join(metadata_root, "tasks")

    if locale == "en":
        if not is_working:
            return f"""# Idle Playbook

{_state_v1_common_paths(f, False, locale)}
{_state_v1_task_name_rule(tasks_root, locale)}
## 1. Create A Task

Use this only when the supervisor asks for a new task or asks you to refine an unassigned Open task. Creating task files does not assign the task and does not change your Idle status.

{_state_v1_create_task_steps(mode, f, locale)}

---

## 2. Accept Or Assign Work

Use this when the supervisor assigns a task to you. Creation and assignment are separate actions.

{_state_v1_assign_task_steps(mode, f, locale)}

Forbidden: do not use deprecated task helper CLI flows; do not treat task creation and assignment as one atomic action; do not merge, close the task, or switch to Idle after implementation until the supervisor explicitly approves merge.
"""
        return f"""# Working Playbook

{_state_v1_common_paths(f, True, locale)}
{_state_v1_task_name_rule(tasks_root, locale)}
{_state_v1_merge_closeout_steps(mode, f, locale)}

---

## 2. Create A New Task

Use this only when the supervisor asks you to split out or create another task while you are already Working. Creating a new task does not assign that task and does not close your current task.

{_state_v1_create_task_steps(mode, f, locale, new_task=True)}

Forbidden: do not use deprecated task helper CLI flows; do not merge without supervisor approval; do not mix the new task metadata with your current task closeout.
"""

    if not is_working:
        return f"""# Idle Playbook

{_state_v1_common_paths(f, False, locale)}
{_state_v1_task_name_rule(tasks_root, locale)}
## 1. 创建任务

只在主管要求创建新任务，或要求 refine 未分配的 Open 任务时执行。创建任务文件不等于分配任务，也不会改变自己的 Idle 状态。
如果主管只要求创建任务，到创建任务并 push 后就停止；不要切任务分支，不要写目标代码文件，不要把 `{f.get("status_path", "status.md")}` 改成 Working。

{_state_v1_create_task_steps(mode, f, locale)}

---

## 2. 接受或分配任务

主管把任务分配给你时使用。创建任务和分配任务是两个独立动作。

{_state_v1_assign_task_steps(mode, f, locale)}

禁止：不要使用已废弃的任务 helper CLI 流程；不要把创建任务和分配任务当成一个原子动作；实现完成后不要 merge、不要关闭任务、不要切回 Idle，必须等主管明确批准 merge。
"""
    return f"""# Working Playbook

{_state_v1_common_paths(f, True, locale)}
{_state_v1_task_name_rule(tasks_root, locale)}
{_state_v1_merge_closeout_steps(mode, f, locale)}

---

## 2. 创建新任务流程

只在主管要求你在 Working 状态下拆分或创建另一个任务时使用。创建新任务不会分配该任务，也不会关闭当前任务。

{_state_v1_create_task_steps(mode, f, locale, new_task=True)}

禁止：不要使用已废弃的任务 helper CLI 流程；不要在未获主管批准时 merge；不要把新任务 metadata 混入当前任务 closeout。
"""


def _write_playbook(f):
    playbook_path = os.path.join(f["intern_dir"], "playbook.md")
    locale = get_locale()
    metadata_mode = f.get("metadata_mode", "repo_dotdir")
    if metadata_mode == "legacy":
        name = f["intern_name"]
        repo = f["repo_dir"]
        shared_repo = f.get("shared_repo") or os.path.join(WORK_AGENTS_ROOT, os.path.basename(repo))
        provider = f.get("provider", "github")
        task_id = f.get("task_id", "")
        pr_number = f.get("pr_number") or "xx"
        if f.get("status") == "Working" and task_id:
            content = (
                _build_working_playbook_en(name, repo, shared_repo, task_id, pr_number, provider)
                if locale == "en"
                else _build_working_playbook(name, repo, shared_repo, task_id, pr_number, provider)
            )
        else:
            content = (
                _build_idle_playbook_en(name, repo, shared_repo, provider)
                if locale == "en"
                else _build_idle_playbook(name, repo, shared_repo, provider)
            )
        os.makedirs(os.path.dirname(playbook_path), exist_ok=True)
        with open(playbook_path, "w") as fp:
            fp.write(content)
        return
    if metadata_mode not in {"repo_dotdir", "metadata_branch", "local_only"}:
        raise ValueError(f"unsupported metadata mode: {metadata_mode}")

    content = _build_state_v1_playbook(f, locale)
    profile = _playbook_profile_en(f) if locale == "en" else _playbook_profile_zh(f)
    content = content.replace("\n\n", "\n\n" + profile + "\n", 1)
    os.makedirs(os.path.dirname(playbook_path), exist_ok=True)
    with open(playbook_path, "w") as fp:
        fp.write(content)
