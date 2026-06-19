"""internctl team — workspace team metadata commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from commands import create as create_cmd
from commands import delete as delete_cmd
from lib.git_ops import add_commit_push, get_default_branch, run_git
from lib.intern_registry import get_intern, name_exists_in_repo
from lib.team_registry import (
    WORK_AGENTS_ROOT,
    MAX_TEAM_WORKERS,
    build_team_metadata,
    coordinator_json_path,
    coordinators_dir,
    default_team_lead_name,
    default_worker_names,
    interns_dir,
    list_teams,
    project_metadata_rel_path,
    project_repo_path,
    read_coordinator,
    read_team,
    tasks_dir,
    team_json_path,
    validate_member_role,
    validate_team_name,
    write_coordinator,
    write_team,
)


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("team", help="管理 workspace team metadata")
    team_sub = parser.add_subparsers(dest="team_command", help="team commands")

    create = team_sub.add_parser("create", help="创建 workspace team metadata")
    create.add_argument("--project", default="axis_intern_agents", help="项目名称")
    create.add_argument("team_name", nargs="?", help="team name，在当前 workspace 内唯一；默认成员名使用 intern_<team_name>_*")
    create.add_argument("--worker-count", type=int, default=0, help=f"同时创建 worker 数量，0-{MAX_TEAM_WORKERS}")
    create.add_argument("--repo-url", default=create_cmd.DEFAULT_REPO_URL, help="Git repo URL，传给 intern 创建流程")
    create.add_argument("--type", choices=["copilot", "claude", "codex"], default="copilot", help="创建出的 intern 类型")
    create.add_argument("--coordinator-id", default="", help="创建后默认绑定到该 coordinator")
    create.set_defaults(func=run_create)

    list_cmd = team_sub.add_parser("list", help="列出 workspace teams")
    list_cmd.add_argument("--project", default="axis_intern_agents", help="项目名称")
    list_cmd.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    list_cmd.set_defaults(func=run_list)

    status = team_sub.add_parser("status", help="显示 team metadata")
    status.add_argument("team_id", help="team name")
    status.add_argument("--project", default="axis_intern_agents", help="项目名称")
    status.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    status.set_defaults(func=run_status)

    delete = team_sub.add_parser("delete", help="删除 workspace team 及成员")
    delete.add_argument("team_id", help="team name")
    delete.add_argument("--project", default="axis_intern_agents", help="项目名称")
    delete.add_argument("--confirm", action="store_true", help="跳过交互确认")
    delete.add_argument("--force", action="store_true", help="强制删除非 Idle member；用于 create team 回滚")
    delete.set_defaults(func=run_delete)

    assign = team_sub.add_parser("assign-worker-task", help="team_lead 创建 task 文档后通知 worker 接受")
    assign.add_argument("team_id", help="team name")
    assign.add_argument("worker_name", help="目标 worker intern 名")
    assign.add_argument("--project", default="axis_intern_agents", help="项目名称")
    assign.add_argument("--lead-name", default="", help="发送通知的 team_lead intern 名；默认读取 team metadata")
    assign.add_argument("--task-id", required=True, help="要创建的 metadata tasks/<task_id> 目录名")
    assign.add_argument("--title", required=True, help="task 标题")
    assign.add_argument("--background", required=True, help="背景说明")
    assign.add_argument("--goal", required=True, help="任务目标")
    assign.add_argument("--acceptance", action="append", required=True, help="验收标准，可重复传入")
    assign.add_argument("--details", default="", help="补充实现细节")
    assign.add_argument("--no-notify", action="store_true", help="只创建 task，不通过 peer send 通知 worker")
    assign.set_defaults(func=run_assign_worker_task)


def run_create(args: argparse.Namespace) -> int:
    project = args.project
    try:
        team_name = _resolve_team_name(args)
        worker_count = int(args.worker_count)
        workers = default_worker_names(team_name, worker_count)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    team_lead = default_team_lead_name(team_name)
    if os.path.exists(team_json_path(project, team_name)):
        if _team_exists_in_head(project, team_name):
            print(f"❌ team '{team_name}' 已存在", file=sys.stderr)
            return 1
        return _resume_pending_team_create(args, team_name, workers, team_lead)

    try:
        coordinator_id = args.coordinator_id.strip()
        _pull_project_repo(project)
        _preflight_team_members(project, [team_lead, *workers])
        coordinator_project = _resolve_coordinator_project(project, coordinator_id) if coordinator_id else ""
        _create_member(project, args.repo_url, args.type, team_lead, "team_lead", team_name)
        for worker in workers:
            _create_member(project, args.repo_url, args.type, worker, "worker", team_name)

        _pull_project_repo(project)
        validate_member_role(project, team_lead, "team_lead", team_name)
        for worker in workers:
            validate_member_role(project, worker, "worker", team_name)

        data = build_team_metadata(
            project=project,
            team_id=team_name,
            team_lead=team_lead,
            workers=workers,
            coordinator=None,
        )
        write_team(project, team_name, data)
        commit_paths = [project_metadata_rel_path(project, "teams", team_name, "team.json")]
        if coordinator_id:
            coordinator_project = _bind_coordinator_to_team(project, coordinator_id, team_name, coordinator_project)
            if coordinator_project == project:
                commit_paths.append(project_metadata_rel_path(project, "coordinators", coordinator_id, "coordinator.json"))
        _commit_project_metadata(project, commit_paths, f"[team] create {team_name}")
        if coordinator_id and coordinator_project != project:
            _commit_project_metadata(
                coordinator_project,
                [project_metadata_rel_path(coordinator_project, "coordinators", coordinator_id, "coordinator.json")],
                f"[team] bind {coordinator_id} to {team_name}",
            )
    except Exception as exc:
        print(f"❌ 创建 team 失败: {exc}", file=sys.stderr)
        return 1

    print(f"✅ team '{team_name}' 创建成功（lead: {team_lead}, workers: {len(workers)}）")
    return 0


def _resume_pending_team_create(args: argparse.Namespace, team_name: str, workers: list[str], team_lead: str) -> int:
    project = args.project
    coordinator_id = args.coordinator_id.strip()
    try:
        team_data = read_team(project, team_name)
        _validate_pending_team_metadata(team_data, team_name, team_lead, workers)
        validate_member_role(project, team_lead, "team_lead", team_name)
        for worker in workers:
            validate_member_role(project, worker, "worker", team_name)

        commit_paths = [project_metadata_rel_path(project, "teams", team_name, "team.json")]
        if coordinator_id:
            coordinator = team_data.get("coordinator") if isinstance(team_data.get("coordinator"), dict) else {}
            if coordinator.get("coordinator_id") != coordinator_id:
                coordinator_project = _bind_coordinator_to_team(project, coordinator_id, team_name)
            else:
                coordinator_project = _resolve_coordinator_project(project, coordinator_id)
            commit_paths.append(project_metadata_rel_path(project, "coordinators", coordinator_id, "coordinator.json"))

        _commit_project_metadata(
            project,
            [path for path in commit_paths if "coordinators/" not in path or coordinator_project == project],
            f"[team] create {team_name}",
        )
        if coordinator_id and coordinator_project != project:
            _commit_project_metadata(
                coordinator_project,
                [project_metadata_rel_path(coordinator_project, "coordinators", coordinator_id, "coordinator.json")],
                f"[team] bind {coordinator_id} to {team_name}",
            )
    except Exception as exc:
        print(f"❌ 恢复未完成 team 创建失败: {exc}", file=sys.stderr)
        return 1

    print(f"✅ team '{team_name}' 创建成功（lead: {team_lead}, workers: {len(workers)}）")
    return 0


def _validate_pending_team_metadata(data: dict[str, object], team_name: str, team_lead: str, workers: list[str]) -> None:
    if str(data.get("team_id") or data.get("team_name") or "") != team_name:
        raise ValueError(f"existing team metadata does not match team '{team_name}'")

    lead = data.get("team_lead") if isinstance(data.get("team_lead"), dict) else {}
    if lead.get("intern_name") != team_lead:
        raise ValueError(f"existing team metadata lead does not match '{team_lead}'")

    existing_workers = data.get("workers") if isinstance(data.get("workers"), list) else []
    existing_worker_names = [
        str(worker.get("intern_name") or "")
        for worker in existing_workers
        if isinstance(worker, dict)
    ]
    if existing_worker_names != workers:
        raise ValueError(f"existing team metadata workers do not match {workers}")


def _team_exists_in_head(project: str, team_name: str) -> bool:
    repo_path = project_repo_path(project)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return os.path.exists(team_json_path(project, team_name))
    rel_path = project_metadata_rel_path(project, "teams", team_name, "team.json")
    return run_git(["cat-file", "-e", f"HEAD:{rel_path}"], cwd=repo_path, check=False).returncode == 0


def run_bind(args: argparse.Namespace) -> int:
    project = args.project
    coordinator_id = args.coordinator_id
    team_id = args.team_id
    try:
        coordinator_project = _bind_coordinator_to_team(project, coordinator_id, team_id)
        team_paths = [project_metadata_rel_path(project, "teams", team_id, "team.json")]
        if coordinator_project == project:
            team_paths.append(project_metadata_rel_path(project, "coordinators", coordinator_id, "coordinator.json"))
        _commit_project_metadata(project, team_paths, f"[team] bind {coordinator_id} to {team_id}")
        if coordinator_project != project:
            _commit_project_metadata(
                coordinator_project,
                [project_metadata_rel_path(coordinator_project, "coordinators", coordinator_id, "coordinator.json")],
                f"[team] bind {coordinator_id} to {team_id}",
            )
    except Exception as exc:
        print(f"❌ 绑定 coordinator 失败: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "as_json", False):
        print(json.dumps(read_coordinator(project, coordinator_id), ensure_ascii=False, indent=2))
        return 0

    print(f"✅ coordinator '{coordinator_id}' 已绑定 team '{team_id}'")
    return 0


def _bind_coordinator_to_team(project: str, coordinator_id: str, team_id: str, coordinator_project: str | None = None) -> str:
    if not validate_team_name(coordinator_id):
        raise ValueError(f"coordinator_id 无效: {coordinator_id}（必须匹配 [a-z][a-z0-9_]*）")
    if not validate_team_name(team_id):
        raise ValueError(f"team_id 无效: {team_id}（必须匹配 [a-z][a-z0-9_]*）")

    coordinator_project = coordinator_project or _resolve_coordinator_project(project, coordinator_id)
    coordinator = read_coordinator(coordinator_project, coordinator_id)
    team_data = read_team(project, team_id)
    now = build_updated_timestamp()

    anchor = coordinator.get("anchor") if isinstance(coordinator.get("anchor"), dict) else {}
    binding = {
        "coordinator_id": str(coordinator.get("coordinator_id") or coordinator_id),
        "intern_name": str(coordinator.get("intern_name") or ""),
        "anchor_project": str(anchor.get("project") or project),
        "anchor_workspace_root": str(anchor.get("repo_path") or os.path.join(WORK_AGENTS_ROOT, str(coordinator.get("intern_name") or ""), project)),
        "bound_at": now,
    }
    if not binding["intern_name"]:
        raise ValueError("coordinator metadata 缺少 intern_name")

    team_data["coordinator"] = binding
    team_data["updated_at"] = now

    managed = coordinator.setdefault("managed_workspaces", [])
    if not isinstance(managed, list):
        raise ValueError("coordinator managed_workspaces must be a list")
    managed[:] = [entry for entry in managed if not (isinstance(entry, dict) and entry.get("project") == project and entry.get("team_id") == team_id)]
    managed.append({
        "project": project,
        "workspace_root": os.path.join(WORK_AGENTS_ROOT, project),
        "team_id": team_id,
        "team_metadata_path": os.path.join("workspace", "teams", team_id, "team.json"),
        "status": str(team_data.get("status") or "active"),
        "bound_at": now,
        "updated_at": now,
    })

    team_leads = coordinator.setdefault("team_leads", [])
    if not isinstance(team_leads, list):
        raise ValueError("coordinator team_leads must be a list")
    lead = team_data.get("team_lead") if isinstance(team_data.get("team_lead"), dict) else {}
    lead_name = str(lead.get("intern_name") or "")
    if not lead_name:
        raise ValueError("team metadata 缺少 team_lead.intern_name")
    team_leads[:] = [entry for entry in team_leads if not (isinstance(entry, dict) and entry.get("project") == project and entry.get("team_id") == team_id)]
    team_leads.append({
        "intern_name": lead_name,
        "project": project,
        "team_id": team_id,
        "status": str(lead.get("status") or "active"),
        "bound_at": now,
        "updated_at": now,
    })

    coordinator["updated_at"] = now
    write_team(project, team_id, team_data)
    write_coordinator(coordinator_project, coordinator_id, coordinator)
    return coordinator_project


def _resolve_coordinator_project(project: str, coordinator_id: str) -> str:
    if not validate_team_name(coordinator_id):
        raise ValueError(f"coordinator_id 无效: {coordinator_id}（必须匹配 [a-z][a-z0-9_]*）")
    if os.path.isfile(coordinator_json_path(project, coordinator_id)):
        return project

    matches: list[str] = []
    for entry in sorted(os.listdir(WORK_AGENTS_ROOT)):
        if entry.startswith("intern_"):
            continue
        candidate = os.path.join(WORK_AGENTS_ROOT, entry)
        if not os.path.isdir(candidate):
            continue
        if os.path.isfile(coordinator_json_path(entry, coordinator_id)):
            matches.append(entry)
    if not matches:
        raise FileNotFoundError(f"coordinator '{coordinator_id}' metadata not found under workspace/coordinators")
    return matches[0]


def build_updated_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_team_name(args: argparse.Namespace) -> str:
    team_name = args.team_name
    if not team_name:
        raise ValueError("必须传 team_name")
    if not validate_team_name(team_name):
        raise ValueError(f"team_name 无效: {team_name}（必须匹配 [a-z][a-z0-9_]*）")
    return team_name


def _create_member(project: str, repo_url: str, intern_type: str, name: str, role: str, team_id: str) -> None:
    member_args = argparse.Namespace(
        name=name,
        project=project,
        repo_url=repo_url,
        type=intern_type,
        role=role,
        team_id=team_id,
    )
    result = create_cmd.run(member_args)
    if result != 0:
        raise RuntimeError(f"create intern {name} failed")


def _pull_project_repo(project: str) -> None:
    repo_path = project_repo_path(project)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return
    branch = run_git(["branch", "--show-current"], cwd=repo_path, check=False).stdout.strip()
    if not branch:
        branch = get_default_branch(repo_path)
    run_git(["pull", "--rebase", "--autostash", "origin", branch], cwd=repo_path)


def _project_push_branch(project: str) -> str | None:
    repo_path = project_repo_path(project)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return None
    branch = run_git(["branch", "--show-current"], cwd=repo_path, check=False).stdout.strip()
    if branch:
        return branch
    try:
        return get_default_branch(repo_path)
    except RuntimeError:
        return None


def _commit_project_metadata(project: str, paths: list[str], message: str) -> None:
    if not paths:
        return
    repo_path = project_repo_path(project)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return
    add_commit_push(
        repo_path=repo_path,
        paths=paths,
        message=message,
        branch=_project_push_branch(project),
    )


def _preflight_team_members(project: str, names: list[str]) -> None:
    metadata_interns_dir = interns_dir(project)
    for name in names:
        intern_root = os.path.join(WORK_AGENTS_ROOT, name)
        if os.path.exists(intern_root):
            raise ValueError(f"team member intern '{name}' 本地路径已存在: {intern_root}")
        if name_exists_in_repo(name, interns_dir=metadata_interns_dir):
            raise ValueError(f"team member intern '{name}' 已存在于 {project} repo")


def run_list(args: argparse.Namespace) -> int:
    try:
        teams = list_teams(args.project)
    except Exception as exc:
        print(f"❌ 读取 team 列表失败: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(teams, ensure_ascii=False, indent=2))
        return 0

    if not teams:
        print("（无 workspace team）")
        return 0

    for team in teams:
        lead = team.get("team_lead", {}).get("intern_name", "-")
        workers = team.get("workers", [])
        coordinator = team.get("coordinator", {}).get("intern_name", "-")
        print(f"{team.get('team_name') or team.get('team_id')}  status={team.get('status')}  lead={lead}  workers={len(workers)}  coordinator={coordinator}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    try:
        team = read_team(args.project, args.team_id)
    except Exception as exc:
        print(f"❌ 读取 team 失败: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(team, ensure_ascii=False, indent=2))
        return 0

    lead = team.get("team_lead", {}).get("intern_name", "-")
    workers = [worker.get("intern_name", "-") for worker in team.get("workers", [])]
    coordinator = team.get("coordinator", {}).get("intern_name", "-")
    print(f"Team:        {team.get('team_name') or team.get('team_id')}")
    print(f"Project:     {team.get('project')}")
    print(f"Status:      {team.get('status')}")
    print(f"Coordinator: {coordinator}")
    print(f"Team Lead:   {lead}")
    print(f"Workers:     {', '.join(workers) if workers else '-'}")
    return 0


TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def run_assign_worker_task(args: argparse.Namespace) -> int:
    project = args.project
    team_id = args.team_id
    worker_name = args.worker_name
    task_id = args.task_id
    try:
        _pull_project_repo(project)
        team_data = read_team(project, team_id)
        lead_name = _resolve_team_lead_for_assignment(team_data, args.lead_name)
        _validate_worker_for_assignment(team_data, worker_name, project, team_id)
        _validate_worker_task_id(task_id)

        task_dir = Path(tasks_dir(project)) / task_id
        if task_dir.exists():
            raise ValueError(f"task '{task_id}' already exists")
        _write_worker_task_docs(
            task_dir=task_dir,
            task_id=task_id,
            title=args.title,
            background=args.background,
            goal=args.goal,
            acceptance=list(args.acceptance or []),
            details=args.details,
            lead_name=lead_name,
            worker_name=worker_name,
            team_id=team_id,
        )
        _commit_project_metadata(
            project,
            [project_metadata_rel_path(project, "tasks", task_id)],
            f"[team] create worker task {task_id}",
        )
        if not getattr(args, "no_notify", False):
            _notify_worker_to_accept_task(
                project=project,
                lead_name=lead_name,
                worker_name=worker_name,
                task_id=task_id,
            )
    except Exception as exc:
        print(f"❌ 分配 worker task 失败: {exc}", file=sys.stderr)
        return 1

    suffix = f"并通知 worker '{worker_name}' 接受" if not getattr(args, "no_notify", False) else "未发送通知"
    print(f"✅ 已创建 task '{task_id}'，{suffix}")
    return 0


def _resolve_team_lead_for_assignment(team_data: dict[str, object], lead_name: str) -> str:
    lead = team_data.get("team_lead") if isinstance(team_data.get("team_lead"), dict) else {}
    expected = str(lead.get("intern_name") or "")
    if not expected:
        raise ValueError("team metadata 缺少 team_lead.intern_name")
    if lead_name and lead_name != expected:
        raise ValueError(f"lead_name '{lead_name}' is not team lead '{expected}'")
    return expected


def _validate_worker_for_assignment(team_data: dict[str, object], worker_name: str, project: str, team_id: str) -> None:
    workers = team_data.get("workers") if isinstance(team_data.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        if worker.get("status", "active") == "deleted":
            continue
        if worker.get("intern_name") != worker_name:
            continue
        validate_member_role(project, worker_name, "worker", team_id)
        return
    raise ValueError(f"worker '{worker_name}' is not an active worker of team '{team_id}'")


def _validate_worker_task_id(task_id: str) -> None:
    if not TASK_ID_PATTERN.fullmatch(task_id) or os.path.basename(task_id) != task_id or task_id in (".", ".."):
        raise ValueError(f"task_id 无效: {task_id}（必须匹配 [a-z][a-z0-9_]*）")


def _write_worker_task_docs(
    *,
    task_dir: Path,
    task_id: str,
    title: str,
    background: str,
    goal: str,
    acceptance: list[str],
    details: str,
    lead_name: str,
    worker_name: str,
    team_id: str,
) -> None:
    task_dir.mkdir(parents=True, exist_ok=False)
    acceptance_lines = "\n".join(f"- {item}" for item in acceptance)
    details_section = f"\n## 实现说明\n\n{details.strip()}\n" if details.strip() else ""
    (task_dir / "README.md").write_text(
        f"""# {task_id} - {title}

<!-- METADATA:STATUS=Open,ASSIGNEE= -->

## 背景

{background.strip()}

## 任务目标

{goal.strip()}
{details_section}
## 验收标准

{acceptance_lines}

## 分配信息

- Team：{team_id}
- Team lead：{lead_name}
- Worker：{worker_name}
- 分配方式：team_lead 创建本 task 文档后，通知 worker 接受该 task。
""",
        encoding="utf-8",
    )
    (task_dir / "history_log.md").write_text(
        f"""# {task_id} - History Log

<!-- METADATA:SESSION=0 -->

## Session 0 - {datetime.now(timezone.utc).date().isoformat()} UTC - Task created by team lead

- Team lead `{lead_name}` 为 worker `{worker_name}` 创建本 task。
- Worker 应接受本 task，按普通 task/PR 流程开发、测试、提交，并在 PR merge 后完成 task。
""",
        encoding="utf-8",
    )
    (task_dir / "task_knowledge.md").write_text(
        f"""# {task_id} - Task Knowledge

<!-- METADATA:SESSION=0 -->

## 记录规则

- 只记录本任务相关的事实、决策、踩坑和验证结果。
- 每条尽量一句话，避免重复 README 的完整内容。

## Knowledge Entries

1. 本 task 由 team_lead `{lead_name}` 创建并分配给 worker `{worker_name}`。
""",
        encoding="utf-8",
    )


def _notify_worker_to_accept_task(*, project: str, lead_name: str, worker_name: str, task_id: str) -> dict[str, object]:
    addr_path = os.environ.get("FEISHU_DAEMON_ADDR_FILE") or "/tmp/feishu_daemon.json"
    with open(addr_path, "r", encoding="utf-8") as fp:
        port = int(json.load(fp)["http_port"])
    content = (
        f"请接受 task `{task_id}`。任务细节已写入当前 workspace metadata 的 tasks/{task_id}/README.md；"
        "请按普通 task/PR 流程执行。PR merge 后按 worker merge 流程把该 task 标记为 Completed，并通过 mailbox 向 team_lead 汇报。"
    )
    body = json.dumps({
        "from_intern_name": lead_name,
        "to_intern_name": worker_name,
        "to_project": project,
        "mode": "default",
        "content": content,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/intern/peer/send",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8") or "{}")
    if result.get("status") != "delivered":
        raise RuntimeError(f"peer send failed: {result}")
    return result


def run_delete(args: argparse.Namespace) -> int:
    project = args.project
    team_id = args.team_id
    force = getattr(args, "force", False) is True
    if not validate_team_name(team_id):
        print(f"❌ team_id 无效: {team_id}（必须匹配 [a-z][a-z0-9_]*）", file=sys.stderr)
        return 1

    try:
        team_data = read_team(project, team_id)
        members = _team_member_names(team_data)
        if not force:
            _assert_members_idle(project, members)
    except Exception as exc:
        print(f"❌ 删除 team 前检查失败: {exc}", file=sys.stderr)
        return 1

    if not getattr(args, "confirm", False):
        print(f"⚠️  即将删除 team '{team_id}'（project={project}）及成员 intern：")
        for member in members:
            print(f"   - {member}")
        answer = input("确认删除？(y/N) ").strip().lower()
        if answer != "y":
            print("已取消")
            return 0

    errors: list[str] = []
    for member in members:
        _kill_tmux_session(member)
        _delete_feishu_group(member)
        result = delete_cmd.run(argparse.Namespace(
            name=member,
            project=project,
            confirm=True,
            force=force,
        ))
        if result != 0:
            errors.append(f"delete member {member} failed")

    try:
        project_paths, external_paths_by_project = _remove_team_metadata(project, team_id, team_data)
        if project_paths:
            _commit_project_metadata(project, project_paths, f"[team] delete {team_id}")
        for coordinator_project, paths in external_paths_by_project.items():
            _commit_project_metadata(coordinator_project, paths, f"[team] unbind from {team_id}")
    except Exception as exc:
        errors.append(f"delete team metadata failed: {exc}")

    if errors:
        print("❌ 删除 team 未完全成功:", file=sys.stderr)
        for error in errors:
            print(f"   - {error}", file=sys.stderr)
        return 1

    print(f"✅ team '{team_id}' 已删除")
    return 0


def _team_member_names(team_data: dict[str, object]) -> list[str]:
    lead = team_data.get("team_lead") if isinstance(team_data.get("team_lead"), dict) else {}
    names = [str(lead.get("intern_name") or "")]
    workers = team_data.get("workers") if isinstance(team_data.get("workers"), list) else []
    names.extend(
        str(worker.get("intern_name") or "")
        for worker in workers
        if isinstance(worker, dict)
    )
    return [name for name in dict.fromkeys(names) if name]


def _member_exists(project: str, intern_name: str) -> bool:
    member_interns_dir = interns_dir(project)
    return get_intern(intern_name, interns_dir=member_interns_dir) is not None


def _assert_members_idle(project: str, members: list[str]) -> None:
    member_interns_dir = interns_dir(project)
    busy: list[str] = []
    for member in members:
        info = get_intern(member, interns_dir=member_interns_dir)
        if info is None:
            continue
        if info.status not in ("Idle", "Unknown", ""):
            busy.append(f"{member}({info.status})")
    if busy:
        raise ValueError(f"team members are not Idle: {', '.join(busy)}")


def _remove_team_metadata(
    project: str,
    team_id: str,
    team_data: dict[str, object],
) -> tuple[list[str], dict[str, list[str]]]:
    changed_paths: list[str] = []
    external_paths_by_project: dict[str, list[str]] = {}

    team_rel = project_metadata_rel_path(project, "teams", team_id)
    team_dir = os.path.join(project_repo_path(project), team_rel)
    if os.path.isdir(team_dir):
        shutil.rmtree(team_dir)
        changed_paths.append(team_rel)

    for coordinator_project, coordinator_id in _candidate_coordinators_for_team(project, team_data):
        data = read_coordinator(coordinator_project, coordinator_id)
        before = json.dumps(data, sort_keys=True, ensure_ascii=False)
        _remove_team_from_coordinator(data, project, team_id)
        after = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if after == before:
            continue
        write_coordinator(coordinator_project, coordinator_id, data)
        rel_path = project_metadata_rel_path(coordinator_project, "coordinators", coordinator_id, "coordinator.json")
        if coordinator_project == project:
            changed_paths.append(rel_path)
        else:
            external_paths_by_project.setdefault(coordinator_project, []).append(rel_path)

    return changed_paths, external_paths_by_project


def _candidate_coordinators_for_team(project: str, team_data: dict[str, object]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    coordinator = team_data.get("coordinator") if isinstance(team_data.get("coordinator"), dict) else {}
    coordinator_id = str(coordinator.get("coordinator_id") or "")
    if coordinator_id:
        try:
            candidates.append((_resolve_coordinator_project(project, coordinator_id), coordinator_id))
        except Exception:
            candidates.append((project, coordinator_id))

    base = coordinators_dir(project)
    if os.path.isdir(base):
        for local_id in sorted(os.listdir(base)):
            path = os.path.join(base, local_id, "coordinator.json")
            if os.path.isfile(path):
                candidates.append((project, local_id))

    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _remove_team_from_coordinator(coordinator: dict[str, object], project: str, team_id: str) -> None:
    managed = coordinator.get("managed_workspaces")
    if isinstance(managed, list):
        coordinator["managed_workspaces"] = [
            entry for entry in managed
            if not (isinstance(entry, dict) and entry.get("project") == project and entry.get("team_id") == team_id)
        ]
    team_leads = coordinator.get("team_leads")
    if isinstance(team_leads, list):
        coordinator["team_leads"] = [
            entry for entry in team_leads
            if not (isinstance(entry, dict) and entry.get("project") == project and entry.get("team_id") == team_id)
        ]
    coordinator["updated_at"] = build_updated_timestamp()


def _kill_tmux_session(intern_name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", f"={intern_name}"], capture_output=True, text=True)


def _delete_feishu_group(intern_name: str) -> None:
    addr_path = os.environ.get("FEISHU_DAEMON_ADDR_FILE") or "/tmp/feishu_daemon.json"
    try:
        with open(addr_path, "r", encoding="utf-8") as fp:
            port = int(json.load(fp)["http_port"])
        body = json.dumps({"intern_name": intern_name}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/group/delete",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
