"""internctl skill — Skill framework CLI.

子命令：
  list <intern>                                                列出 intern 已启用 skills（JSON）
  list-available [--scope repo|personal] [--intern <i>]        递归扫源列出可启用 SKILL.md
  add-skill --scope repo|personal [--intern <i>]
           --source-type git|local <key> <source>              统一添加入口；source 是 git URL 或本地目录
  update-source <key>                                          更新 git 源（submodule）到 upstream 默认分支最新
  remove-source [--scope repo|personal] [--intern <i>] <key>   级联清理 enabled + 物理删除 + 本地 commit
  enable <intern> <scope:repo|personal> <key>                  写 .intern_skill.json + 本地 commit
  disable <intern> <scope> <key>                               同上
  sync <intern>                                                重建农场 ${INTERN_DIR}/.claude/skills/ 或 .agents/skills/，stdout JSON

state v1 metadata mode decides where repo/personal skill data is stored.
repo_dotdir uses the workspace checkout .intern_workspace, metadata_branch uses
the metadata branch checkout, and local_only uses the state local metadata root.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from lib.intern_registry import WORK_AGENTS_ROOT, validate_name
from lib.git_ops import git_lock, run_git, get_default_branch, ensure_local_write_branch
from lib.state_v1 import (
    METADATA_BRANCH,
    StateStore,
    ensure_git_identity,
    ensure_metadata_base_branch,
    ensure_metadata_checkout,
    intern_runtime_dir,
    list_state_interns,
    safe_segment,
    workspace_metadata_checkout_path,
)


# ── 常量 ────────────────────────────────────────────────────────────────
SKILL_SOURCES_META_REL = ".skill_sources"
PERSONAL_SOURCES_META_REL = ".skill_sources/personal/{intern}"
REPO_CFG_META_REL = ".intern_skill.json"
PERSONAL_CFG_META_REL = "interns/{intern}/.intern_skill.json"

CLAUDE_FARM_REL = ".claude/skills"                    # 相对 ${INTERN_DIR}
CODEX_FARM_REL = ".agents/skills"                     # 相对 ${INTERN_DIR}
SESSION_MAP_PATH = ".intern_sessions.json"            # 相对 ${WORK_AGENTS_ROOT}


# ── 项目枚举 ───────────────────────────────────────────────────
def list_intern_projects(intern: str) -> list[str]:
    out: list[str] = []
    try:
        store = StateStore(WORK_AGENTS_ROOT)
        for item in list_state_interns(store):
            if item.get("name") != intern:
                continue
            workspace = store.load_workspace(item["workspace_key"]).data
            project = workspace.get("display_name") or workspace.get("workspace_key") or ""
            if project and project not in out:
                out.append(project)
    except Exception:
        pass
    if out:
        return out
    idir = os.path.join(WORK_AGENTS_ROOT, intern)
    if not os.path.isdir(idir):
        return []
    for entry in sorted(os.listdir(idir)):
        sub = os.path.join(idir, entry)
        if os.path.isdir(sub) and (os.path.isdir(os.path.join(sub, ".git")) or os.path.isfile(os.path.join(sub, ".git"))):
            out.append(entry)
    return out


# ── 路径工具 ─────────────────────────────────────────────────────────────
def shared_repo(project: str) -> str:
    return os.path.join(WORK_AGENTS_ROOT, project)


def intern_dir(intern: str) -> str:
    try:
        store = StateStore(WORK_AGENTS_ROOT)
        for item in list_state_interns(store):
            if item.get("name") == intern:
                code_path = item.get("code_worktree_path") or ""
                if code_path:
                    return str(Path(code_path).parent)
                return str(intern_runtime_dir(store.work_root, item["workspace_key"], intern))
    except Exception:
        pass
    legacy = os.path.join(WORK_AGENTS_ROOT, intern)
    return legacy if os.path.isdir(legacy) else os.path.join(WORK_AGENTS_ROOT, "state", "v1", "runtime", "missing", intern)


def intern_repo(intern: str, project: str) -> str:
    try:
        store = StateStore(WORK_AGENTS_ROOT)
        for item in list_state_interns(store):
            if item.get("name") != intern:
                continue
            workspace = store.load_workspace(item["workspace_key"]).data
            if _workspace_matches_project(workspace, project):
                return item.get("code_worktree_path") or os.path.join(intern_dir(intern), "repo")
    except Exception:
        pass
    return os.path.join(intern_dir(intern), "repo")


def _sync_target_intern_if_present(intern: str | None, project: str) -> dict | None:
    if not intern or not os.path.isdir(intern_dir(intern)):
        return None
    return skill_sync(intern, [project])


def _state_skill_capable_interns_for_project(project: str) -> list[str]:
    store, ref = _state_workspace_for_project(project)
    if ref is None:
        return []
    names: list[str] = []
    for item in list_state_interns(store, ref.workspace_key):
        name = item.get("name")
        if not name or name in names or not os.path.isdir(intern_dir(name)):
            continue
        intern_type, _warnings = get_intern_type(name)
        if farm_rel_for_type(intern_type):
            names.append(name)
    return names


def _sync_skill_scope_if_present(scope: str, intern: str | None, project: str) -> dict | None:
    if scope == "personal":
        return _sync_target_intern_if_present(intern, project)
    targets = _state_skill_capable_interns_for_project(project)
    if intern and os.path.isdir(intern_dir(intern)) and intern not in targets:
        intern_type, _warnings = get_intern_type(intern)
        if farm_rel_for_type(intern_type):
            targets.append(intern)
    if not targets:
        return None
    results = {name: skill_sync(name, [project]) for name in targets}
    return {
        "ok": all(result.get("ok") for result in results.values()),
        "targets": targets,
        "results": results,
    }


def _workspace_matches_project(workspace: dict, project: str) -> bool:
    local_path = workspace.get("local_path") or ""
    return project in {
        workspace.get("display_name") or "",
        workspace.get("workspace_key") or "",
        os.path.basename(local_path.rstrip("/")) if local_path else "",
    }


def _state_workspace_for_project(project: str):
    try:
        store = StateStore(WORK_AGENTS_ROOT)
        for ref in store.list_workspaces():
            if _workspace_matches_project(ref.data, project):
                return store, ref
    except Exception:
        return None, None
    return None, None


def _first_state_intern_for_workspace(workspace_dir: Path) -> str:
    interns_dir = workspace_dir / "interns"
    if not interns_dir.is_dir():
        return ""
    for record_path in sorted(interns_dir.glob("*/intern.json")):
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("name"):
            return data["name"]
    return ""


def _first_state_intern_name(store: StateStore, workspace_key: str) -> str:
    for item in list_state_interns(store, workspace_key):
        if item.get("name"):
            return item["name"]
    return ""


def _metadata_context(project: str, intern: str | None = None, *, for_write: bool = False) -> dict:
    """Resolve skill storage under the state v1 metadata root."""
    store, ref = _state_workspace_for_project(project)
    if ref is None:
        legacy_root = os.path.join(WORK_AGENTS_ROOT, project, "workspace")
        if os.path.isdir(legacy_root):
            return {
                "mode": "legacy",
                "metadata_root": legacy_root,
                "commit_repo": os.path.join(WORK_AGENTS_ROOT, project),
                "repo_rel_prefix": "workspace",
                "push_allowed": False,
            }
        raise RuntimeError(f"state workspace not found for project: {project}")

    workspace = ref.data
    metadata = workspace.get("metadata", {})
    mode = metadata.get("mode") or "repo_dotdir"
    workspace_dir = ref.path.parent
    local_path = workspace.get("local_path") or shared_repo(project)

    if mode == "metadata_branch":
        metadata_branch = metadata.get("branch") or METADATA_BRANCH
        resolved_intern = intern or (_first_state_intern_name(store, ref.workspace_key) if for_write else "")
        if for_write and not resolved_intern:
            raise RuntimeError("metadata_branch skill writes require --intern or an existing state intern")
        if resolved_intern:
            if for_write:
                report = ensure_metadata_checkout(store, ref.workspace_key, resolved_intern)
                checkout = report["metadata_checkout_path"]
            else:
                checkout = ensure_metadata_checkout(store, ref.workspace_key, resolved_intern)["metadata_checkout_path"]
            return {
                "mode": mode,
                "metadata_root": os.path.join(checkout, ".intern_workspace"),
                "commit_repo": checkout,
                "repo_rel_prefix": ".intern_workspace",
                "push_allowed": True,
                "push_branch": metadata_branch,
            }
        if not for_write:
            checkout = _ensure_metadata_read_checkout(store, ref)
            return {
                "mode": mode,
                "metadata_root": os.path.join(checkout, ".intern_workspace"),
                "commit_repo": checkout,
                "repo_rel_prefix": ".intern_workspace",
                "push_allowed": False,
            }
        return {
            "mode": mode,
            "metadata_root": str(intern_runtime_dir(store.work_root, ref.workspace_key, "__missing__") / "metadata" / ".intern_workspace"),
            "commit_repo": "",
            "repo_rel_prefix": ".intern_workspace",
            "push_allowed": False,
        }
    if mode == "local_only":
        return {
            "mode": mode,
            "metadata_root": metadata.get("local_path") or str(workspace_dir / "metadata" / "local" / ".intern_workspace"),
            "commit_repo": "",
            "repo_rel_prefix": "",
            "push_allowed": False,
        }
    if mode == "repo_dotdir":
        repo = local_path
        return {
            "mode": mode,
            "metadata_root": os.path.join(repo, metadata.get("repo_relative_path") or ".intern_workspace"),
            "commit_repo": repo,
            "repo_rel_prefix": metadata.get("repo_relative_path") or ".intern_workspace",
            "push_allowed": True,
        }
    raise RuntimeError(f"unsupported metadata mode for project {project}: {mode}")


def _ensure_metadata_read_checkout(store: StateStore, ref) -> str:
    workspace = ref.data
    workspace_dir = ref.path.parent
    repo_url = workspace.get("repo_url") or workspace.get("local_path")
    if not repo_url:
        raise RuntimeError("repo_url is missing")
    branch = workspace.get("metadata", {}).get("branch") or METADATA_BRANCH
    ensure_metadata_base_branch(repo_url, branch)
    checkout = workspace_metadata_checkout_path(store.work_root, ref.workspace_key)
    if not (checkout / ".git").exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--sparse", repo_url, str(checkout)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "sparse-checkout", "set", ".intern_workspace"], cwd=checkout, check=True, capture_output=True, text=True)
    ensure_git_identity(checkout)
    subprocess.run(["git", "fetch", "origin", branch], cwd=checkout, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=checkout, check=True, capture_output=True, text=True)
    return str(checkout)


def _meta_abs(project: str, rel: str, intern: str | None = None) -> str:
    return os.path.join(_metadata_context(project, intern).get("metadata_root"), rel)


def _meta_repo_rel(ctx: dict, rel: str) -> str:
    prefix = ctx.get("repo_rel_prefix") or ""
    return f"{prefix}/{rel}" if prefix else rel


def _ctx_abs(ctx: dict, rel: str) -> str:
    return os.path.join(ctx["metadata_root"], rel)


def _with_metadata_commit(ctx: dict, message: str, paths_to_add: list[str],
                          mutator, extra_rollback=None, push: bool = False) -> None:
    if ctx.get("mode") == "local_only":
        if push:
            raise RuntimeError("local_only metadata mode cannot push skill changes")
        try:
            mutator()
        except Exception:
            if extra_rollback is not None:
                extra_rollback(ctx.get("metadata_root") or "", "")
            raise
        return
    kwargs = {"push": push}
    if ctx.get("push_branch"):
        kwargs["push_branch"] = ctx["push_branch"]
    _with_repo_commit(
        ctx["commit_repo"],
        message,
        paths_to_add,
        mutator,
        extra_rollback,
        **kwargs,
    )


def session_map_file() -> str:
    return os.path.join(WORK_AGENTS_ROOT, SESSION_MAP_PATH)


def get_intern_type(intern: str) -> tuple[str, list[str]]:
    """Return claude/codex/copilot from session map or state intern record."""
    def state_record_type() -> str | None:
        try:
            store = StateStore(WORK_AGENTS_ROOT)
            for item in list_state_interns(store):
                if item.get("name") == intern and item.get("type") in ("claude", "codex", "copilot"):
                    return item["type"]
        except Exception:
            return None
        return None

    path = session_map_file()
    warnings: list[str] = []
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        fallback = state_record_type()
        if fallback:
            warnings.append(f"{SESSION_MAP_PATH} is invalid JSON ({exc}); using state intern type {fallback}")
            return fallback, warnings
        warnings.append(f"{SESSION_MAP_PATH} is invalid JSON ({exc}); defaulting skill farm type to claude")
        return "claude", warnings
    if data is None:
        fallback = state_record_type()
        if fallback:
            return fallback, warnings
        warnings.append(f"{SESSION_MAP_PATH} not found; defaulting skill farm type to claude")
        return "claude", warnings
    entry = data.get(intern, {})
    if not isinstance(entry, dict):
        fallback = state_record_type()
        if fallback:
            warnings.append(f"{SESSION_MAP_PATH} entry for {intern} is invalid; using state intern type {fallback}")
            return fallback, warnings
        warnings.append(f"{SESSION_MAP_PATH} entry for {intern} is invalid; defaulting skill farm type to claude")
        return "claude", warnings
    intern_type = entry.get("type")
    if intern_type in ("claude", "codex", "copilot"):
        return intern_type, warnings
    fallback = state_record_type()
    if fallback:
        return fallback, warnings
    warnings.append(f"{SESSION_MAP_PATH} missing type for {intern}; defaulting skill farm type to claude")
    return "claude", warnings


def farm_rel_for_type(intern_type: str) -> str | None:
    if intern_type == "codex":
        return CODEX_FARM_REL
    if intern_type == "claude":
        return CLAUDE_FARM_REL
    return None


def repo_cfg_file(project: str) -> str:
    return _meta_abs(project, REPO_CFG_META_REL)


def personal_cfg_file(intern: str, project: str) -> str:
    """task222 起 personal cfg 在 shared repo 下，便于走 git_lock+本地 commit 同步。"""
    return _meta_abs(project, PERSONAL_CFG_META_REL.format(intern=intern), intern)


def skill_sources_root(project: str) -> str:
    return _meta_abs(project, SKILL_SOURCES_META_REL)


def personal_skills_root(intern: str, project: str) -> str:
    """task222 起 personal 数据在 shared repo 的 .skill_sources/personal/<intern>/ 下。"""
    return _meta_abs(project, PERSONAL_SOURCES_META_REL.format(intern=intern), intern)


# ── JSON I/O ────────────────────────────────────────────────────────────
def load_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.rename(tmp, path)


def load_or_init_cfg(path: str) -> dict:
    cfg = load_json(path)
    if cfg is None:
        return {"enabled": []}
    if "enabled" not in cfg or not isinstance(cfg["enabled"], list):
        raise RuntimeError(f"invalid skill config at {path}: missing 'enabled' list")
    return cfg


def _load_personal_cfg(intern: str, project: str) -> dict:
    """读 personal cfg（shared repo 下）。"""
    return load_or_init_cfg(personal_cfg_file(intern, project))


def _resolve_personal_skill_src(intern: str, project: str, key: str) -> str | None:
    """Resolve personal enabled key.

    task223 made personal enable point at the shared repo source pool. Keep a
    fallback for the task222/add-skill personal pool so existing keys and the
    still-exposed CLI personal add path remain syncable.
    """
    candidates = [
        os.path.join(_meta_abs(project, SKILL_SOURCES_META_REL), key),
        os.path.join(personal_skills_root(intern, project), key),
    ]
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, "SKILL.md")):
            return candidate
    return None


# \u2500\u2500 git \u64cd\u4f5c\u7edf\u4e00 helper\uff1ahead snapshot + try/rollback + commit + optional push \u2500\u2500
def _format_git_error(exc: subprocess.CalledProcessError) -> str:
    stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    return (stderr or stdout or str(exc)).strip()


def _ensure_push_target(repo: str, branch: str) -> None:
    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    if current != branch:
        raise RuntimeError(
            f"Refusing to push {branch!r} while current branch is {current!r}. "
            "Run without --push for local-only use."
        )
    remote = run_git(["remote", "get-url", "origin"], cwd=repo, check=False)
    if remote.returncode != 0 or not remote.stdout.strip():
        raise RuntimeError(
            "Refusing to push because remote 'origin' is not configured. "
            "Run without --push for local-only use or configure a writable origin."
        )
    upstream = run_git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"], cwd=repo, check=False)
    if upstream.returncode != 0:
        raise RuntimeError(
            f"Refusing to push because origin/{branch} is missing. "
            "Fetch the remote/default branch first, or run without --push for local-only use."
        )
    try:
        run_git(["push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Remote push preflight failed for origin/{branch}. "
            "The branch may be protected or the remote may be read-only. "
            f"Git output: {_format_git_error(exc)}"
        ) from exc


def _with_repo_commit(repo: str, message: str, paths_to_add: list[str],
                      mutator, extra_rollback=None, push: bool = False,
                      push_branch: str | None = None) -> None:
    """包裹 git_lock + checkout default + pull --rebase + mutator() + add/commit；fail → reset --hard + extra_rollback。

    mutator() 是你在 head_before 后、commit 前要做的写操作（如创建文件、反向修 cfg）。
    paths_to_add 是相对 repo 的路径列表，最后一起 git add。
    extra_rollback(repo, head_before) 只在失败路径调用。
    push=True 时先做 writable remote preflight，再在 commit 后显式 push；push 失败保留本地 commit。
    """
    with git_lock(repo):
        if push:
            branch = push_branch or get_default_branch(repo)
            run_git(["fetch", "origin"], cwd=repo, check=False)
            run_git(["checkout", branch], cwd=repo)
            run_git(["pull", "--rebase", "origin", branch], cwd=repo, check=False)
            _ensure_push_target(repo, branch)
        else:
            ensure_local_write_branch(repo)
        head_before = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        try:
            mutator()
            for p in paths_to_add:
                if not os.path.exists(os.path.join(repo, p)):
                    tracked = run_git(["ls-files", "--error-unmatch", "--", p], cwd=repo, check=False)
                    if tracked.returncode != 0:
                        continue
                run_git(["add", "--sparse", p], cwd=repo)
            # Only staged changes belong to this operation. Unrelated untracked
            # files in long-lived metadata checkouts must not trigger a commit.
            staged = run_git(["diff", "--cached", "--quiet"], cwd=repo, check=False)
            if staged.returncode == 0:
                return  # noop, no staged metadata changes
            run_git(["commit", "-m", message], cwd=repo)
        except Exception as e:
            print(f"\u274c repo op failed: {e}; rolling back to {head_before[:8]}...", file=sys.stderr)
            run_git(["reset", "--hard", head_before], cwd=repo, check=False)
            if extra_rollback is not None:
                try:
                    extra_rollback(repo, head_before)
                except Exception as inner:
                    print(f"\u26a0\ufe0f extra_rollback also failed: {inner}", file=sys.stderr)
            raise
        if push:
            try:
                run_git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Push failed for origin/{branch}; local commit {message!r} was preserved. "
                    "The branch may be protected or the remote may be read-only. "
                    f"Git output: {_format_git_error(exc)}"
                ) from exc


# ── SKILL.md frontmatter 解析 ──────────────────────────────────────────
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+?)$", re.MULTILINE)
_NAME_RE = re.compile(r"^name:\s*(.+?)$", re.MULTILINE)


def parse_skill_md(skill_md_path: str) -> dict:
    """提取 SKILL.md frontmatter 关键字段（name, description）。"""
    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    fm = m.group(1)
    out = {}
    nm = _NAME_RE.search(fm)
    if nm:
        out["name"] = nm.group(1).strip().strip("'\"")
    dm = _DESC_RE.search(fm)
    if dm:
        out["description"] = dm.group(1).strip().strip("'\"")
    return out


# ── 递归扫描 SKILL.md ──────────────────────────────────────────────────
def scan_skills(root: str) -> list[dict]:
    """递归扫 root 下所有"目录中含 SKILL.md"的目录。

    返回 [{"rel_path": "ltp-jobs", "abs_path": "...", "name": "...", "description": "..."}]
    rel_path 相对 root。
    """
    results: list[dict] = []
    if not os.path.isdir(root):
        return results
    for dirpath, dirnames, filenames in os.walk(root):
        # 忽略 .git 等隐藏目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "SKILL.md" not in filenames:
            continue
        skill_md = os.path.join(dirpath, "SKILL.md")
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            # 包根本身就是单 skill：rel_path 用空串（避免 <key>/<key> 嵌套，task222 修）
            rel = ""
        meta = parse_skill_md(skill_md)
        results.append({
            "rel_path": rel,
            "abs_path": dirpath,
            "name": meta.get("name") or os.path.basename(dirpath),
            "description": meta.get("description", ""),
        })
        # 已是 skill 目录，停止深入子目录（避免嵌套 SKILL.md 误识别）
        dirnames[:] = []
    return results


COPILOT_SHARED_ROOT_REL = ".github/skills"
COPILOT_SHARED_MANIFEST = ".axis-intern-copilot-skills.json"


def _copilot_root() -> str:
    return os.path.join(WORK_AGENTS_ROOT, COPILOT_SHARED_ROOT_REL)


def _copilot_manifest_path() -> str:
    return os.path.join(_copilot_root(), COPILOT_SHARED_MANIFEST)


def _load_copilot_manifest() -> dict:
    path = _copilot_manifest_path()
    if not os.path.exists(path):
        return {"version": 1, "skills": {}}
    data = load_json(path) or {}
    return {"version": 1, "skills": data.get("skills") or {}}


def _save_copilot_manifest(manifest: dict) -> None:
    path = _copilot_manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "skills": manifest.get("skills") or {}}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.rename(tmp, path)


def _validate_copilot_skill_name(name: str, source: str) -> None:
    if not re.match(r"^[a-z0-9-]{1,64}$", name):
        raise RuntimeError(
            f"Invalid Copilot skill name '{name}' in {source}; "
            "expected lowercase letters, numbers, hyphen only"
        )


def _hash_directory(directory: str) -> str:
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".git"))
        for filename in sorted(filenames):
            if filename.startswith(".git"):
                continue
            path = os.path.join(dirpath, filename)
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, directory).replace(os.sep, "/")
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as f:
                digest.update(f.read())
            digest.update(b"\0")
    return digest.hexdigest()


def _copilot_shared_info(name: str, skill_dir: str, manifest: dict) -> dict | None:
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(skill_md):
        return None
    meta = parse_skill_md(skill_md)
    entry = (manifest.get("skills") or {}).get(name)
    return {
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "skill_dir": skill_dir,
        "skill_md_path": skill_md,
        "managed": bool(entry),
        "source_project": entry.get("sourceProject") if entry else None,
        "source_key": entry.get("sourceKey") if entry else None,
        "source_path": entry.get("sourcePath") if entry else None,
        "checksum": entry.get("checksum") if entry else None,
        "updated_at": entry.get("updatedAt") if entry else None,
    }


def _resolve_repo_skill_for_copilot(project: str, key: str) -> dict:
    source_path = _meta_abs(project, f"{SKILL_SOURCES_META_REL}/{key}")
    skill_md = os.path.join(source_path, "SKILL.md")
    if not os.path.exists(skill_md):
        raise RuntimeError(f"Skill not found: {project}/{key} ({skill_md})")
    meta = parse_skill_md(skill_md)
    name = meta.get("name") or os.path.basename(source_path)
    _validate_copilot_skill_name(name, skill_md)
    return {
        "project": project,
        "key": key,
        "name": name,
        "description": meta.get("description", ""),
        "source_path": source_path,
        "skill_md_path": skill_md,
    }


def cmd_copilot_list(args: argparse.Namespace) -> int:
    root = _copilot_root()
    manifest = _load_copilot_manifest()
    skills: list[dict] = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name.startswith("."):
                continue
            skill_dir = os.path.join(root, name)
            if not os.path.isdir(skill_dir):
                continue
            info = _copilot_shared_info(name, skill_dir, manifest)
            if info:
                skills.append(info)
    print(json.dumps({"root": root, "skills": skills}, ensure_ascii=False, indent=2))
    return 0


def cmd_copilot_enable(args: argparse.Namespace) -> int:
    source = _resolve_repo_skill_for_copilot(args.project, args.key)
    root = _copilot_root()
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, source["name"])
    manifest = _load_copilot_manifest()
    existing = os.path.exists(dest)
    existing_entry = (manifest.get("skills") or {}).get(source["name"])
    if existing and not args.overwrite:
        same_source = (
            existing_entry
            and existing_entry.get("sourceProject") == source["project"]
            and existing_entry.get("sourceKey") == source["key"]
        )
        if not same_source:
            print(f"❌ Copilot shared skill '{source['name']}' already exists from another source", file=sys.stderr)
            return 1

    tmp = os.path.join(root, f".{source['name']}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(
        source["source_path"],
        tmp,
        symlinks=False,
        ignore=shutil.ignore_patterns(".git"),
    )
    backup = f"{dest}.backup-{os.getpid()}" if existing else ""
    if backup:
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        os.rename(dest, backup)
    try:
        os.rename(tmp, dest)
    except Exception:
        if backup and os.path.exists(backup):
            os.rename(backup, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    checksum = _hash_directory(dest)
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest.setdefault("skills", {})[source["name"]] = {
        "name": source["name"],
        "sourceProject": source["project"],
        "sourceKey": source["key"],
        "sourcePath": source["source_path"],
        "checksum": checksum,
        "updatedAt": updated_at,
    }
    _save_copilot_manifest(manifest)
    if backup and os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    print(json.dumps(_copilot_shared_info(source["name"], dest, manifest), ensure_ascii=False, indent=2))
    return 0


def cmd_copilot_update(args: argparse.Namespace) -> int:
    manifest = _load_copilot_manifest()
    entry = manifest.get("skills", {}).get(args.name)
    if not entry:
        print(f"❌ Copilot shared skill '{args.name}' is not managed by Intern Agent Helper", file=sys.stderr)
        return 1
    args.project = entry.get("sourceProject")
    args.key = entry.get("sourceKey")
    args.overwrite = True
    return cmd_copilot_enable(args)


def cmd_copilot_disable(args: argparse.Namespace) -> int:
    _validate_copilot_skill_name(args.name, "copilot shared skill name")
    root = _copilot_root()
    dest = os.path.join(root, args.name)
    manifest = _load_copilot_manifest()
    changed = False
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
        changed = True
    if args.name in manifest.get("skills", {}):
        del manifest["skills"][args.name]
        _save_copilot_manifest(manifest)
        changed = True
    print(json.dumps({"name": args.name, "changed": changed}, ensure_ascii=False, indent=2))
    return 0


# ── 子命令：list ────────────────────────────────────────────────────────
def cmd_list(args: argparse.Namespace) -> int:
    intern = args.intern
    project = args.project
    if project:
        projects = [project]
    else:
        projects = list_intern_projects(intern)
    out: dict = {"intern": intern, "projects": {}}
    for p in projects:
        repo_cfg = load_or_init_cfg(_meta_abs(p, REPO_CFG_META_REL, intern))
        personal_cfg = _load_personal_cfg(intern, p)
        out["projects"][p] = {
            "repo_enabled": repo_cfg["enabled"],
            "personal_enabled": personal_cfg["enabled"],
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ── 子命令：list-available ─────────────────────────────────────────────
def cmd_list_available(args: argparse.Namespace) -> int:
    project = args.project
    source_root = skill_sources_root(project)
    if args.source:
        source_root = os.path.join(source_root, args.source)
    skills = scan_skills(source_root)
    print(json.dumps({"root": source_root, "skills": skills},
                     ensure_ascii=False, indent=2))
    return 0



# ── 子命令：add-skill（task222 统一入口） ─────────────────────────────
def cmd_add_skill(args: argparse.Namespace) -> int:
    return _add_skill(args)


def _add_skill(args: argparse.Namespace) -> int:
    """统一加 skill：scope=repo|personal × source_type=git|local。

    repo+git    → submodule 到 .skill_sources/<key>
    repo+local  → cp 到 .skill_sources/<key>，普通 git tracked 目录
    personal+*  → 同上，目标改为 .skill_sources/personal/<intern>/<key>
    全部走 git_lock + 本地 commit；显式 --push 时才同步远端。失败 reset --hard 回滚。
    """
    project = args.project
    scope = args.scope
    source_type = args.source_type
    key = args.key
    source = args.source
    intern = getattr(args, "intern", None)
    if scope not in ("repo", "personal"):
        print(f"❌ invalid scope '{scope}'", file=sys.stderr); return 2
    if source_type not in ("git", "local"):
        print(f"❌ invalid source_type '{source_type}'", file=sys.stderr); return 2
    if scope == "personal" and not intern:
        print("❌ scope=personal requires --intern", file=sys.stderr); return 2

    ctx = _metadata_context(project, intern, for_write=True)
    repo = ctx.get("commit_repo") or ctx["metadata_root"]
    if scope == "repo":
        meta_rel_target = f"{SKILL_SOURCES_META_REL}/{key}"
    else:
        meta_rel_target = f"{SKILL_SOURCES_META_REL}/personal/{intern}/{key}"
    rel_target = _meta_repo_rel(ctx, meta_rel_target)
    abs_target = os.path.join(ctx["metadata_root"], meta_rel_target)
    if os.path.exists(abs_target):
        print(f"❌ skill '{key}' already exists at {meta_rel_target}", file=sys.stderr)
        return 1

    if source_type == "local":
        if not os.path.isdir(source):
            print(f"❌ source dir not found: {source}", file=sys.stderr); return 1
        # health check：source 内必须有至少一个 SKILL.md（顶层或子目录）
        if not scan_skills(source):
            print(f"❌ source '{source}' contains no SKILL.md", file=sys.stderr); return 1

    def _mutate() -> None:
        if source_type == "git":
            os.makedirs(os.path.dirname(abs_target), exist_ok=True)
            if ctx.get("mode") == "local_only":
                run_git(["clone", source, abs_target], cwd=ctx["metadata_root"])
            else:
                run_git(["-c", "protocol.file.allow=always", "submodule", "add",
                         source, rel_target], cwd=repo)
            # health check
            if not scan_skills(abs_target):
                raise RuntimeError(f"skill source cloned but no SKILL.md found in {meta_rel_target}")
        else:
            # local copy
            os.makedirs(os.path.dirname(abs_target), exist_ok=True)
            shutil.copytree(source, abs_target, symlinks=False,
                            ignore=shutil.ignore_patterns(".git"))

    def _extra_rollback(_repo: str, _head: str) -> None:
        # submodule 残留
        if source_type == "git":
            if ctx.get("mode") != "local_only":
                run_git(["submodule", "deinit", "-f", rel_target], cwd=repo, check=False)
            shutil.rmtree(abs_target, ignore_errors=True)
            if ctx.get("mode") != "local_only":
                shutil.rmtree(os.path.join(repo, ".git", "modules", rel_target),
                              ignore_errors=True)
                run_git(["checkout", "--", ".gitmodules"], cwd=repo, check=False)
        else:
            shutil.rmtree(abs_target, ignore_errors=True)

    paths = [".gitmodules", rel_target] if source_type == "git" and ctx.get("mode") != "local_only" else [rel_target]
    msg = f"[skill] add {scope} skill {key} ({source_type})"
    if scope == "personal":
        msg += f" for {intern}"
    _with_metadata_commit(ctx, msg, paths, _mutate, _extra_rollback, push=args.push)

    print(f"✅ added {scope} skill: {meta_rel_target}", file=sys.stderr)
    skills = scan_skills(abs_target)
    print(json.dumps({"key": key, "scope": scope, "source_type": source_type,
                      "skills": skills}, ensure_ascii=False, indent=2))
    return 0


# ── 子命令：update-source ──────────────────────────────────────────────
def cmd_update_source(args: argparse.Namespace) -> int:
    project = args.project
    key = args.key
    ctx = _metadata_context(project, None, for_write=True)
    repo = ctx.get("commit_repo") or ctx["metadata_root"]
    meta_rel_target = f"{SKILL_SOURCES_META_REL}/{key}"
    rel_target = _meta_repo_rel(ctx, meta_rel_target)
    sub = os.path.join(ctx["metadata_root"], meta_rel_target)
    if not os.path.isdir(os.path.join(sub, ".git")) and not os.path.isfile(os.path.join(sub, ".git")):
        print(f"❌ '{key}' is not a git source (no .git in {meta_rel_target}); local sources can't be updated remotely",
              file=sys.stderr)
        return 1
    sub_branch = get_default_branch(sub)

    def _mutate() -> None:
        run_git(["fetch", "origin"], cwd=sub, check=False)
        run_git(["checkout", sub_branch], cwd=sub)
        run_git(["pull", "origin", sub_branch], cwd=sub)

    _with_metadata_commit(ctx, f"[skill] update source {key}", [rel_target], _mutate, push=args.push)
    print(f"✅ updated: {meta_rel_target}", file=sys.stderr)
    return 0


# ── 子命令：remove-source（级联清理） ──────────────────────────────────
def cmd_remove_source(args: argparse.Namespace) -> int:
    """删除 skill 源 + 级联清理 enabled 配置（repo cfg 与所有 personal cfg）。

    --scope personal 时需 --intern；目标在 .skill_sources/personal/<intern>/<key>。
    repo scope 默认。
    """
    project = args.project
    scope = getattr(args, "scope", "repo")
    intern = getattr(args, "intern", None)
    key = args.key
    ctx = _metadata_context(project, intern, for_write=True)
    repo = ctx.get("commit_repo") or ctx["metadata_root"]
    if scope == "personal":
        if not intern:
            print("❌ scope=personal requires --intern", file=sys.stderr); return 2
        meta_rel_target = f"{SKILL_SOURCES_META_REL}/personal/{intern}/{key}"
    else:
        meta_rel_target = f"{SKILL_SOURCES_META_REL}/{key}"
    rel_target = _meta_repo_rel(ctx, meta_rel_target)
    abs_target = os.path.join(ctx["metadata_root"], meta_rel_target)
    if not os.path.exists(abs_target):
        print(f"❌ source '{key}' not found at {meta_rel_target}", file=sys.stderr)
        return 1
    is_submodule = (os.path.isdir(os.path.join(abs_target, ".git"))
                    or os.path.isfile(os.path.join(abs_target, ".git")))

    # 先在 working tree 上把 cfg 改好（在 mutator 里）
    cleaned: list[str] = []

    def _mutate() -> None:
        # 1. 共享 repo cfg
        if scope == "repo":
            cfg_path = _ctx_abs(ctx, REPO_CFG_META_REL)
            if os.path.exists(cfg_path):
                cfg = load_or_init_cfg(cfg_path)
                new_enabled = [k for k in cfg["enabled"]
                               if not (k == key or k.startswith(f"{key}/"))]
                if new_enabled != cfg["enabled"]:
                    cfg["enabled"] = new_enabled
                    write_json_atomic(cfg_path, cfg)
                    cleaned.append(_meta_repo_rel(ctx, REPO_CFG_META_REL))
            # 2. 所有 personal cfg（在 shared repo 下，task222 修复路径）
            interns_root = os.path.join(ctx["metadata_root"], "interns")
            if os.path.isdir(interns_root):
                for entry in sorted(os.listdir(interns_root)):
                    pcfg = os.path.join(interns_root, entry, ".intern_skill.json")
                    if not os.path.exists(pcfg):
                        continue
                    try:
                        pc = load_or_init_cfg(pcfg)
                    except Exception:
                        continue
                    new = [k for k in pc["enabled"]
                           if not (k == key or k.startswith(f"{key}/"))]
                    if new != pc["enabled"]:
                        pc["enabled"] = new
                        write_json_atomic(pcfg, pc)
                        cleaned.append(_meta_repo_rel(ctx, os.path.relpath(pcfg, ctx["metadata_root"])))
        else:  # personal scope
            pcfg = _ctx_abs(ctx, PERSONAL_CFG_META_REL.format(intern=intern))
            if os.path.exists(pcfg):
                pc = load_or_init_cfg(pcfg)
                new = [k for k in pc["enabled"]
                       if not (k == key or k.startswith(f"{key}/"))]
                if new != pc["enabled"]:
                    pc["enabled"] = new
                    write_json_atomic(pcfg, pc)
                    cleaned.append(_meta_repo_rel(ctx, os.path.relpath(pcfg, ctx["metadata_root"])))

        # 3. 物理删 skill 源
        if is_submodule:
            if ctx.get("mode") == "local_only":
                shutil.rmtree(abs_target, ignore_errors=True)
            else:
                run_git(["submodule", "deinit", "-f", rel_target], cwd=repo, check=False)
                run_git(["rm", "-f", "--sparse", rel_target], cwd=repo)
                gitmod_dir = os.path.join(repo, ".git", "modules", rel_target)
                if os.path.isdir(gitmod_dir):
                    shutil.rmtree(gitmod_dir, ignore_errors=True)
        else:
            # local source：直接 git rm -rf
            if ctx.get("mode") == "local_only":
                shutil.rmtree(abs_target, ignore_errors=True)
            else:
                run_git(["rm", "-rf", "--sparse", rel_target], cwd=repo)

    paths_to_add = cleaned + [".gitmodules"] if is_submodule else cleaned

    def _extra_rollback(_repo: str, _head: str) -> None:
        if is_submodule and ctx.get("mode") != "local_only":
            run_git(["-c", "protocol.file.allow=always", "submodule", "update",
                     "--init", rel_target], cwd=repo, check=False)

    msg = f"[skill] remove {scope} source {key} (cascade-clean: pending)"
    # 注意：cleaned 在 mutator 内填充，所以传入的是按 reference 引用的列表；
    # 但 _with_repo_commit 接收 paths_to_add 参数已固化——改成在 mutator 内 add：
    def _mutate_with_add() -> None:
        _mutate()
        if ctx.get("mode") == "local_only":
            return
        for p in cleaned:
            run_git(["add", "--sparse", p], cwd=repo)
        if is_submodule and ctx.get("mode") != "local_only":
            run_git(["add", "--sparse", ".gitmodules"], cwd=repo)

    _with_metadata_commit(ctx, msg.replace("pending", str(len(cleaned)) + " cfg"),
                          [], _mutate_with_add, _extra_rollback, push=args.push)
    sync_report = _sync_skill_scope_if_present(scope, intern, project)
    print(f"✅ removed: {meta_rel_target} (cleaned {len(cleaned)} cfg)", file=sys.stderr)
    if sync_report and not sync_report.get("ok"):
        print(json.dumps({"removed": True, "sync": sync_report}, ensure_ascii=False, indent=2))
        return 1
    return 0


# ── 子命令：enable / disable ──────────────────────────────────────────
def _modify_cfg_inplace(cfg_path: str, key: str, enable: bool) -> tuple[bool, list[str]]:
    cfg = load_or_init_cfg(cfg_path)
    enabled = cfg["enabled"]
    changed = False
    if enable:
        if key not in enabled:
            enabled.append(key)
            changed = True
    else:
        if key in enabled:
            enabled.remove(key)
            changed = True
    if changed:
        cfg["enabled"] = enabled
        write_json_atomic(cfg_path, cfg)
    return changed, enabled


def cmd_enable(args: argparse.Namespace) -> int:
    return _enable_disable(args, enable=True)


def cmd_disable(args: argparse.Namespace) -> int:
    return _enable_disable(args, enable=False)


def _enable_disable(args: argparse.Namespace, enable: bool) -> int:
    project = args.project
    intern = args.intern
    scope = args.scope
    key = args.key
    ctx = _metadata_context(project, intern, for_write=True)
    if scope == "repo":
        cfg_path = _ctx_abs(ctx, REPO_CFG_META_REL)
        rel_cfg = _meta_repo_rel(ctx, REPO_CFG_META_REL)
    elif scope == "personal":
        cfg_path = _ctx_abs(ctx, PERSONAL_CFG_META_REL.format(intern=intern))
        rel_cfg = _meta_repo_rel(ctx, PERSONAL_CFG_META_REL.format(intern=intern))
    else:
        print(f"❌ invalid scope '{scope}' (expected repo|personal)", file=sys.stderr)
        return 2

    # 预读旧 enabled 用于差异判断
    state: dict = {"changed": False, "enabled": []}

    def _mutate() -> None:
        changed, enabled = _modify_cfg_inplace(cfg_path, key, enable)
        state["changed"] = changed
        state["enabled"] = enabled

    verb = "enable" if enable else "disable"
    _with_metadata_commit(
        ctx, f"[skill] {verb} {scope}:{key}" + (f" ({intern})" if scope == "personal" else ""),
        [rel_cfg], _mutate, push=args.push,
    )

    sync_report = _sync_skill_scope_if_present(scope, intern, project) if state["changed"] else None

    if state["changed"]:
        print(f"✅ {verb}d {scope}:{key}", file=sys.stderr)
    else:
        print(f"ℹ️ no change: {scope}:{key} already {'in' if enable else 'absent from'} list",
              file=sys.stderr)
    print(json.dumps({"changed": state["changed"], "enabled": state["enabled"], "sync": sync_report},
                     ensure_ascii=False, indent=2))
    return 0 if not sync_report or sync_report.get("ok") else 1


# ── 子命令：sync（核心） ──────────────────────────────────────────────
def cmd_sync(args: argparse.Namespace) -> int:
    project = args.project
    intern = args.intern
    if not validate_name(intern):
        print(f"❌ invalid intern name: {intern}", file=sys.stderr)
        return 2
    if project:
        result = skill_sync(intern, [project])
    else:
        projects = list_intern_projects(intern)
        if not projects:
            result = {"ok": False, "errors": [f"intern '{intern}' has no project subdirs"]}
        else:
            result = skill_sync(intern, projects)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def skill_sync(intern: str, projects: list[str]) -> dict:
    """重建当前 intern 类型对应的 skill 农场，聊合多项目 enabled。返回 {ok, enabled, conflicts, errors}。

    错误时不动农场（保留上一次成功状态）。
    """
    idir = intern_dir(intern)
    if not os.path.isdir(idir):
        return {"ok": False, "errors": [f"intern dir not found: {idir}"]}
    intern_type, warnings = get_intern_type(intern)
    farm_rel = farm_rel_for_type(intern_type)
    if farm_rel is None:
        return {
            "ok": False,
            "enabled": [],
            "conflicts": [],
            "errors": [f"skill sync is only supported for claude/codex interns; {intern} is type={intern_type}"],
            "warnings": warnings,
            "intern_type": intern_type,
        }

    items: list[dict] = []
    errors: list[str] = []

    for project in projects:
        repo_cfg = load_or_init_cfg(_meta_abs(project, REPO_CFG_META_REL))
        personal_cfg = _load_personal_cfg(intern, project)

        sources_root = _meta_abs(project, SKILL_SOURCES_META_REL)

        for key in repo_cfg["enabled"]:
            src = os.path.join(sources_root, key)
            if not os.path.exists(os.path.join(src, "SKILL.md")):
                errors.append(f"[{project}] repo skill '{key}' missing SKILL.md at {src}")
                continue
            items.append({"key": key, "scope": "repo", "project": project, "src": src,
                          "name": os.path.basename(key)})

        for key in personal_cfg["enabled"]:
            src = _resolve_personal_skill_src(intern, project, key)
            if src is None:
                errors.append(f"[{project}] personal skill '{key}' missing SKILL.md at {src}")
                continue
            items.append({"key": key, "scope": "personal", "project": project, "src": src,
                          "name": os.path.basename(key)})

    if errors:
        return {
            "ok": False,
            "enabled": [],
            "conflicts": [],
            "errors": errors,
            "warnings": warnings,
            "intern_type": intern_type,
            "farm": os.path.join(idir, farm_rel),
        }

    # 同名冲突：personal 后于 repo，覆盖之
    seen: dict[str, dict] = {}
    conflicts: list[dict] = []
    for it in items:
        prev = seen.get(it["name"])
        if prev:
            conflicts.append({
                "name": it["name"],
                "loser": prev["key"],
                "winner": it["key"],
                "guidance": (
                    f"WARN: skill '{it['name']}' from '{it['key']}' overrides "
                    f"previously enabled '{prev['key']}'. To keep both, rename one "
                    f"(e.g. mv the directory under workspace/.skill_sources/ or personal "
                    f"skills/, then update enabled keys). To revert, run "
                    f"`internctl skill disable {intern} {it['scope']} {it['key']}`."
                ),
            })
        seen[it["name"]] = it

    farm = os.path.join(idir, farm_rel)
    # 幂等清空 + 重建
    if os.path.lexists(farm):
        shutil.rmtree(farm, ignore_errors=True)
    os.makedirs(farm, exist_ok=True)

    enabled_out: list[dict] = []
    for name, it in seen.items():
        link_path = os.path.join(farm, name)
        os.symlink(it["src"], link_path)
        meta = parse_skill_md(os.path.join(it["src"], "SKILL.md"))
        enabled_out.append({
            "name": name,
            "key": it["key"],
            "scope": it["scope"],
            "project": it.get("project"),
            "src": it["src"],
            "description": meta.get("description", ""),
        })

    return {
        "ok": True,
        "enabled": enabled_out,
        "conflicts": conflicts,
        "errors": [],
        "warnings": warnings,
        "intern_type": intern_type,
        "farm": farm,
    }


# ── argparse 接线 ──────────────────────────────────────────────────────
def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("skill", help="Manage intern skills")
    sp = p.add_subparsers(dest="skill_cmd")

    def _add_proj(x: argparse.ArgumentParser) -> None:
        x.add_argument("--project", required=True,
                       help="Project name for this skill operation")

    def _add_proj_optional(x: argparse.ArgumentParser) -> None:
        x.add_argument("--project", default=None,
                       help="Optional project filter; omitted means all projects for the intern")

    def _add_push(x: argparse.ArgumentParser) -> None:
        x.add_argument("--push", action="store_true",
                       help="Push after the local commit; use only when you intend to update a writable remote")

    pl = sp.add_parser("list"); _add_proj_optional(pl); pl.add_argument("intern"); pl.set_defaults(func=cmd_list)
    pa = sp.add_parser("list-available"); _add_proj(pa); pa.add_argument("source", nargs="?", default=""); pa.set_defaults(func=cmd_list_available)
    # task222 统一入口
    pas = sp.add_parser("add-skill", help="Add a repo or personal skill from Git or a local folder")
    _add_proj(pas)
    pas.add_argument("--scope", choices=["repo", "personal"], required=True)
    pas.add_argument("--source-type", choices=["git", "local"], required=True, dest="source_type")
    pas.add_argument("--intern", default=None, help="Required when adding a personal skill")
    _add_push(pas)
    pas.add_argument("key")
    pas.add_argument("source", help="Git URL or local folder")
    pas.set_defaults(func=cmd_add_skill)
    pu = sp.add_parser("update-source"); _add_proj(pu); _add_push(pu); pu.add_argument("key"); pu.set_defaults(func=cmd_update_source)
    pr = sp.add_parser("remove-source")
    _add_proj(pr)
    pr.add_argument("--scope", choices=["repo", "personal"], default="repo")
    pr.add_argument("--intern", default=None, help="Required for personal skill sources")
    _add_push(pr)
    pr.add_argument("key")
    pr.set_defaults(func=cmd_remove_source)
    pe = sp.add_parser("enable"); _add_proj(pe); _add_push(pe); pe.add_argument("intern"); pe.add_argument("scope", choices=["repo", "personal"]); pe.add_argument("key"); pe.set_defaults(func=cmd_enable)
    pd = sp.add_parser("disable"); _add_proj(pd); _add_push(pd); pd.add_argument("intern"); pd.add_argument("scope", choices=["repo", "personal"]); pd.add_argument("key"); pd.set_defaults(func=cmd_disable)
    ps = sp.add_parser("sync"); _add_proj_optional(ps); ps.add_argument("intern"); ps.set_defaults(func=cmd_sync)

    pc = sp.add_parser("copilot", help="Manage workspace-root Copilot shared skills")
    cps = pc.add_subparsers(dest="copilot_cmd")
    pcl = cps.add_parser("list"); pcl.set_defaults(func=cmd_copilot_list)
    pce = cps.add_parser("enable")
    _add_proj(pce)
    pce.add_argument("--overwrite", action="store_true")
    pce.add_argument("key")
    pce.set_defaults(func=cmd_copilot_enable)
    pcu = cps.add_parser("update"); pcu.add_argument("name"); pcu.set_defaults(func=cmd_copilot_update)
    pcd = cps.add_parser("disable"); pcd.add_argument("name"); pcd.set_defaults(func=cmd_copilot_disable)

    def _dispatch(args: argparse.Namespace) -> int:
        if not getattr(args, "skill_cmd", None):
            p.print_help()
            return 1
        if args.skill_cmd == "copilot" and not getattr(args, "copilot_cmd", None):
            pc.print_help()
            return 1
        return args.func(args)

    p.set_defaults(func=_dispatch)
