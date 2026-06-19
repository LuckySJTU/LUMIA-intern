"""Intern-bound mailbox storage for worker -> team_lead mail."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lib import team_registry
from lib.intern_registry import validate_name


def _member_name(member: dict[str, Any]) -> str:
    return str(member.get("intern_name") or "")


def _member_project(member: dict[str, Any], default_project: str) -> str:
    return str(member.get("project") or default_project)


def _active_workers(team: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        worker for worker in team.get("workers", [])
        if isinstance(worker, dict) and worker.get("status", "active") != "deleted"
    ]


def _team_lead(team: dict[str, Any]) -> dict[str, Any]:
    lead = team.get("team_lead") or {}
    return lead if isinstance(lead, dict) else {}


def _find_candidate_teams(
    *,
    project: str,
    worker_name: str,
    worker_project: str,
    team_lead_name: str,
    team_id: str = "",
) -> list[dict[str, Any]]:
    teams = [team_registry.read_team(project, team_id)] if team_id else team_registry.list_teams(project)
    result: list[dict[str, Any]] = []
    for team in teams:
        if team.get("status", "active") == "deleted":
            continue
        team_project = str(team.get("project") or project)
        lead = _team_lead(team)
        if _member_name(lead) != team_lead_name:
            continue
        if _member_project(lead, team_project) != project:
            continue
        for worker in _active_workers(team):
            if _member_name(worker) != worker_name:
                continue
            if _member_project(worker, team_project) != worker_project:
                continue
            result.append(team)
            break
    return result


def resolve_worker_team(
    *,
    project: str,
    worker_name: str,
    worker_project: str,
    team_lead_name: str,
    team_id: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    if not validate_name(worker_name) or not validate_name(team_lead_name):
        return None, "invalid_intern_name"
    if team_id and not team_registry.validate_team_id(team_id):
        return None, "invalid_team_id"
    try:
        candidates = _find_candidate_teams(
            project=project,
            worker_name=worker_name,
            worker_project=worker_project,
            team_lead_name=team_lead_name,
            team_id=team_id,
        )
    except FileNotFoundError:
        return None, "unknown_team"
    if not candidates:
        return None, "not_managed_worker"
    if len(candidates) > 1:
        return None, "ambiguous_team"
    return candidates[0], None


def mailbox_path(project: str, intern_name: str) -> str:
    if not validate_name(intern_name):
        raise ValueError("invalid_intern_name")
    return os.path.join(team_registry.interns_dir(project), intern_name, "mailbox.json")


def lock_path(project: str, intern_name: str) -> str:
    return mailbox_path(project, intern_name) + ".lock"


def _empty_store(project: str, intern_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": project,
        "intern_name": intern_name,
        "messages": [],
    }


@contextmanager
def _mailbox_lock(project: str, intern_name: str, exclusive: bool):
    path = mailbox_path(project, intern_name)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path(project, intern_name)
    with open(lock_file, "a+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _read_mailbox_unlocked(project: str, intern_name: str) -> dict[str, Any]:
    path = mailbox_path(project, intern_name)
    if not os.path.isfile(path):
        return _empty_store(project, intern_name)
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"mailbox must be an object: {path}")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"mailbox messages must be a list: {path}")
    return data


def read_mailbox(project: str, intern_name: str) -> dict[str, Any]:
    with _mailbox_lock(project, intern_name, exclusive=False):
        return _read_mailbox_unlocked(project, intern_name)


def _write_mailbox_unlocked(project: str, intern_name: str, data: dict[str, Any]) -> None:
    path = mailbox_path(project, intern_name)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp, path)


def write_mailbox(project: str, intern_name: str, data: dict[str, Any]) -> None:
    with _mailbox_lock(project, intern_name, exclusive=True):
        _write_mailbox_unlocked(project, intern_name, data)


def append_message(
    *,
    target_project: str,
    from_intern_name: str,
    from_project: str,
    to_intern_name: str,
    content: str,
    team_id: str = "",
    kind: str = "progress",
    related_task: str = "",
    related_pr: str = "",
    client_message_id: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content:
        raise ValueError("content_empty")
    if len(content.encode("utf-8")) > 4096:
        raise ValueError("content_too_long")
    team, reason = resolve_worker_team(
        project=target_project,
        worker_name=from_intern_name,
        worker_project=from_project,
        team_lead_name=to_intern_name,
        team_id=team_id,
    )
    if reason:
        raise PermissionError(reason)
    resolved_team_id = str(team.get("team_id") or team.get("team_name") or team_id)
    now = created_at or team_registry.utc_now()
    message_id = client_message_id or uuid.uuid4().hex
    message = {
        "message_id": message_id,
        "client_message_id": client_message_id,
        "kind": kind or "progress",
        "from_project": from_project,
        "from_intern_name": from_intern_name,
        "to_project": target_project,
        "to_intern_name": to_intern_name,
        "team_id": resolved_team_id,
        "content": content,
        "related_task": related_task,
        "related_pr": related_pr,
        "created_at": now,
        "read": False,
        "read_at": "",
    }
    with _mailbox_lock(target_project, to_intern_name, exclusive=True):
        store = _read_mailbox_unlocked(target_project, to_intern_name)
        store["messages"].append(message)
        _write_mailbox_unlocked(target_project, to_intern_name, store)
    return message


def list_messages(
    *,
    project: str,
    intern_name: str,
    include_read: bool = False,
) -> list[dict[str, Any]]:
    with _mailbox_lock(project, intern_name, exclusive=False):
        store = _read_mailbox_unlocked(project, intern_name)
        messages = [dict(message) for message in store.get("messages", [])]
    if not include_read:
        messages = [message for message in messages if not message.get("read")]
    return sorted(messages, key=lambda message: message.get("created_at", ""))


def mark_read(
    *,
    project: str,
    intern_name: str,
    message_ids: list[str],
    read_at: str | None = None,
) -> list[str]:
    if not message_ids:
        raise ValueError("message_ids_required")
    wanted = set(message_ids)
    now = read_at or team_registry.utc_now()
    with _mailbox_lock(project, intern_name, exclusive=True):
        store = _read_mailbox_unlocked(project, intern_name)
        marked: list[str] = []
        for message in store.get("messages", []):
            if message.get("message_id") not in wanted:
                continue
            message["read"] = True
            message["read_at"] = now
            marked.append(str(message.get("message_id")))
        _write_mailbox_unlocked(project, intern_name, store)
    return marked
