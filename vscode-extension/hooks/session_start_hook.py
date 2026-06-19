#!/usr/bin/env python3
"""VS Code SessionStart hook.
Loads state → runs modules → outputs additionalContext (不写 CLAUDE.md).

Due to _Hr mapping, this hook also fires for SubagentStart events.
Detect via agent_id field and skip — SubagentStart is handled by subagent_start_hook.py.
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    get_intern_dir, get_intern_name, get_intern_type, load_state, save_state,
    log_debug, state_lock, WORK_AGENTS_ROOT, read_file_safe,
)


def _is_machine_helper_state(state):
    return state.get("role") in {"machine_helper", "machine_debugger"} or state.get("projectless") is True


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    # SubagentStart events include agent_id; regular SessionStart does not.
    # Skip — SubagentStart is handled by subagent_start_hook.py.
    if hook_input.get("agent_id"):
        sys.exit(0)

    # 1. 尝试从映射查找 intern
    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        # 2. 映射不存在，检查 .pending_intern 文件（由插件预写入）
        pending_path = os.path.join(WORK_AGENTS_ROOT, ".pending_intern")
        if not os.path.exists(pending_path):
            # 无映射无 pending → 不是 intern session，静默放行
            sys.exit(0)

        try:
            pending = json.loads(open(pending_path).read())
            intern_name = pending.get("intern_name", "")
            if not intern_name:
                sys.exit(0)

            intern_dir = os.path.join(WORK_AGENTS_ROOT, intern_name)
            if not os.path.isdir(intern_dir):
                log_debug(WORK_AGENTS_ROOT, "session_start_hook",
                          f"intern work dir not found: {intern_dir} (plugin should have called internctl init)")
                sys.exit(0)

            # 建立 session_id → intern 映射
            if session_id:
                from common.utils import set_session_intern
                set_session_intern(session_id, intern_name)
                log_debug(intern_dir, "session_start_hook",
                          f"mapped session {session_id} → {intern_name} (from pending)")

            # 删除 pending 文件
            os.remove(pending_path)

        except Exception as e:
            log_debug(WORK_AGENTS_ROOT, "session_start_hook", f"pending read failed: {e}")
            sys.exit(0)
    except ValueError:
        # 未绑定 intern，静默放行。记录 sessionId 格式便于调试
        log_debug(WORK_AGENTS_ROOT, "session_start_hook",
                  f"NO MAPPING: sessionId={session_id!r}, cwd={cwd}")
        sys.exit(0)

    with state_lock(intern_dir):
        # Reset state for new session, preserving token cache
        state = load_state(intern_dir)

        # Finalize previous session's feishu message before clearing state
        old_feishu = state.get("feishu", {})
        old_msg_id = old_feishu.get("message_id")
        if old_msg_id:
            try:
                from feishu_module.feishu_api import get_tenant_token, update_message
                from feishu_module.timeline_composer import compose
                from common.utils import load_feishu_credentials
                app_id, app_secret = load_feishu_credentials()
                token = get_tenant_token(app_id, app_secret, state=state)
                if token:
                    old_lines = old_feishu.get("buffer_lines", [])
                    old_lines.append("\n⚠️ session 已结束")
                    old_usage = old_feishu.get("usage_stats")
                    old_footer = ""
                    if old_usage:
                        from feishu_module.module import _format_footer_for_intern_type
                        old_footer = _format_footer_for_intern_type(
                            get_intern_type(get_intern_name(intern_dir)),
                            old_usage,
                            old_feishu.get("turn_start_cost", 0),
                        )
                    final_text = compose(old_lines, spinner=False, footer=old_footer)
                    ok, err = update_message(token, old_msg_id, final_text)
                    log_debug(intern_dir, "session_start_hook",
                              f"finalized prev feishu msg {old_msg_id} ok={ok}" + (f" ERR={err}" if err else ""))
            except Exception as e:
                log_debug(intern_dir, "session_start_hook",
                          f"failed to finalize prev feishu msg: {e}")

        old_token = old_feishu.get("_token_cache")
        old_chat_id = old_feishu.get("chat_id") or state.get("helper", {}).get("chat_id", "")
        is_machine_helper = _is_machine_helper_state(state)
        state["feishu"] = {}
        if old_token:
            state["feishu"]["_token_cache"] = old_token
        if old_chat_id:
            state["feishu"]["chat_id"] = old_chat_id
        state["validation"] = {"issues": [], "file_hashes": {}}
        state["subagent_depth"] = 0
        state["_intern_dir"] = intern_dir

        # Run modules
        from log_module.module import LogModule

        if is_machine_helper:
            state["_context_text"] = read_file_safe(os.path.join(intern_dir, "prompt.md"), max_chars=12000)
            modules = [LogModule()]
        else:
            from intern_module.module import InternModule
            modules = [InternModule(), LogModule()]
        for m in modules:
            m.on_session_start(state, hook_input)

        # VS Code: SessionStart 不再注入 additionalContext（改由 UserPromptSubmit 注入）
        # 保留 module 调用用于 state 初始化（log、intern status 等）
        state.pop("_context_text", None)

        # Clean transient keys and save
        state.pop("_intern_dir", None)
        state.pop("_output", None)
        save_state(intern_dir, state)

    log_debug(intern_dir, "session_start_hook", "done")


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
