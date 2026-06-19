#!/usr/bin/env python3
"""Codex Subagent dispatch.

Codex hook 系统硬编码只支持 6 个 event（PreToolUse/PermissionRequest/PostToolUse/
SessionStart/UserPromptSubmit/Stop），无原生 SubagentStart/SubagentStop。本脚本
作为 codex_settings.toml 中:
  - PreToolUse matcher="spawn_agent" → start
  - PostToolUse matcher="wait"        → stop
两个钩子的 dispatcher，把 Codex 的 PreToolUse/PostToolUse 输入字段翻译成
subagent_start_hook.py / subagent_stop_hook.py 期望的 Claude 风格输入字段，再
in-process import + 替换 stdin 调用，原 hook 脚本无需感知 Codex。

Usage:
  codex_subagent_dispatch.py start   # PreToolUse(spawn_agent) → SubagentStart
  codex_subagent_dispatch.py stop    # PostToolUse(wait) → SubagentStop

字段映射（Codex → Claude）:
  cwd                     ← cwd
  session_id              ← session_id
  transcript_path         ← transcript_path
  agent_id                ← tool_use_id（Codex 的 call_id）
  agent_type (start)      ← tool_input.agent_type or "codex_subagent"
  agent_type (stop)       ← "codex_subagent"（必须非空，避开 subagent_stop_hook
                              的 Claude `/btw` 分支：见 subagent_stop_hook.py:65）
"""
import sys
import io
import os
import json
import importlib
import traceback


HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOK_DIR)


def _translate_input(hook_input: dict, action: str) -> dict:
    tool_input = hook_input.get("tool_input") or {}
    if action == "start":
        agent_type = tool_input.get("agent_type") or "codex_subagent"
    else:
        agent_type = "codex_subagent"

    return {
        "cwd": hook_input.get("cwd", ""),
        "session_id": hook_input.get("session_id", ""),
        "transcript_path": hook_input.get("transcript_path", ""),
        "agent_id": hook_input.get("tool_use_id", ""),
        "agent_type": agent_type,
    }


def main():
    action = sys.argv[1]
    target_module = "subagent_start_hook" if action == "start" else "subagent_stop_hook"

    hook_input = json.loads(sys.stdin.read())
    translated = _translate_input(hook_input, action)

    sys.stdin = io.StringIO(json.dumps(translated, ensure_ascii=False))
    importlib.import_module(target_module).main()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            from common.utils import log_debug
            log_debug("/tmp", os.path.basename(__file__),
                      f"FATAL (graceful exit 0): {e}\n{traceback.format_exc()}")
        except Exception:
            pass
        sys.exit(0)
