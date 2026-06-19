"""internctl reset-hook-env — 按已安装 VSIX 的 bundled 内容修复 hooks 和 intern .claude/settings.json。

脱机可用，不依赖 VS Code IPC / Reload。不触发 daemon / relay 重启。

修复范围：
1. /work-agents/.github/hooks/ — 从 VSIX bundled hooks/ 覆盖（保留 hooks.json / .version）
2. /work-agents/.github/claude_settings.json — 从 VSIX bundled 覆盖
3. /work-agents/.github/codex_settings.toml — 从 VSIX bundled 覆盖
4. 每个 intern_*/.claude/settings.json — 重建 symlink → /work-agents/.github/claude_settings.json
"""
import os
import re
import shutil


EXT_GLOB_DIR = "/root/.vscode-server-insiders/extensions"
EXT_PREFIX = "llm-intern-agents.intern-agent-helper-"
SKIP_COPY_DIRS = ("__pycache__", "tests", ".pytest_cache", "llm_intern_logs")
PRESERVE_IN_HOOKS_DIR = ("hooks.json", ".version")


def setup_parser(subparsers):
    p = subparsers.add_parser(
        "reset-hook-env",
        help="Restore /work-agents/.github and intern .claude/settings.json to bundled state",
    )
    p.add_argument(
        "--extensions-dir",
        default=EXT_GLOB_DIR,
        help=f"Override VS Code extensions dir (default: {EXT_GLOB_DIR})",
    )
    p.set_defaults(func=run)


def _version_tuple(name: str) -> tuple:
    suffix = name[len(EXT_PREFIX):]
    parts = re.split(r"[.\-]", suffix)
    return tuple(int(x) if x.isdigit() else -1 for x in parts)


def _find_latest_vsix_hooks(extensions_dir: str) -> str:
    candidates = [
        n for n in os.listdir(extensions_dir)
        if n.startswith(EXT_PREFIX) and os.path.isdir(os.path.join(extensions_dir, n))
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No installed VSIX found under {extensions_dir} (expected {EXT_PREFIX}*)"
        )
    latest = max(candidates, key=_version_tuple)
    hooks_dir = os.path.join(extensions_dir, latest, "hooks")
    if not os.path.isdir(hooks_dir):
        raise FileNotFoundError(f"hooks/ missing in {latest}: {hooks_dir}")
    return hooks_dir


def _copy_tree(src: str, dst: str) -> int:
    """Recursive copy mirroring extension.ts copyDirRecursive. Returns file count copied."""
    os.makedirs(dst, exist_ok=True)
    count = 0
    for entry in os.listdir(src):
        src_path = os.path.join(src, entry)
        dst_path = os.path.join(dst, entry)
        if os.path.isdir(src_path):
            if entry in SKIP_COPY_DIRS:
                continue
            count += _copy_tree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)
            count += 1
    return count


def _clear_hooks_dir(hooks_dir: str) -> None:
    for entry in os.listdir(hooks_dir):
        if entry in PRESERVE_IN_HOOKS_DIR:
            continue
        p = os.path.join(hooks_dir, entry)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def _sync_hooks(bundled_hooks: str, github_dir: str) -> int:
    hooks_dir = os.path.join(github_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    _clear_hooks_dir(hooks_dir)
    # Copy bundled/*.py + subdirs (excluding claude_settings.json / codex_settings.toml which live outside hooks/)
    count = 0
    for entry in os.listdir(bundled_hooks):
        src_path = os.path.join(bundled_hooks, entry)
        if entry in ("claude_settings.json", "codex_settings.toml"):
            continue
        dst_path = os.path.join(hooks_dir, entry)
        if os.path.isdir(src_path):
            if entry in SKIP_COPY_DIRS:
                continue
            count += _copy_tree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)
            count += 1
    return count


def _sync_settings(bundled_hooks: str, github_dir: str) -> list[str]:
    copied = []
    for name in ("claude_settings.json", "codex_settings.toml"):
        shutil.copy2(os.path.join(bundled_hooks, name), os.path.join(github_dir, name))
        copied.append(name)
    return copied


def _relink_intern_settings(work_agents_root: str, github_dir: str) -> int:
    """Ensure every <root>/intern_*/.claude/settings.json is symlink → github_dir/claude_settings.json."""
    target = os.path.join(github_dir, "claude_settings.json")
    relinked = 0
    for entry in sorted(os.listdir(work_agents_root)):
        if not entry.startswith("intern_"):
            continue
        claude_dir = os.path.join(work_agents_root, entry, ".claude")
        if not os.path.isdir(claude_dir):
            continue
        settings_path = os.path.join(claude_dir, "settings.json")
        if os.path.islink(settings_path):
            if os.readlink(settings_path) == target:
                continue
            os.unlink(settings_path)
        elif os.path.exists(settings_path):
            os.unlink(settings_path)
        os.symlink(target, settings_path)
        relinked += 1
    return relinked


def run(args) -> int:
    work_agents_root = os.environ["WORK_AGENTS_ROOT"]
    github_dir = os.path.join(work_agents_root, ".github")
    os.makedirs(github_dir, exist_ok=True)

    bundled_hooks = _find_latest_vsix_hooks(args.extensions_dir)
    print(f"📦 Source: {bundled_hooks}")
    print(f"🎯 Target: {github_dir}")

    hook_files = _sync_hooks(bundled_hooks, github_dir)
    settings_files = _sync_settings(bundled_hooks, github_dir)
    symlinks = _relink_intern_settings(work_agents_root, github_dir)

    print(f"✅ Hook files copied: {hook_files}")
    print(f"✅ Settings files copied: {', '.join(settings_files)}")
    print(f"✅ Intern .claude/settings.json symlinks fixed: {symlinks}")
    print("\nNote: daemon / relay were NOT restarted. If hooks.json structure changed, run VS Code Reload.")
    return 0
