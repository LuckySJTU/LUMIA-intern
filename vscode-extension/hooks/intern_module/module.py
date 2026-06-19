"""
InternModule (VS Code) — SessionStart 时记录映射，UserPromptSubmit 时注入身份信息。

system prompt 注入已移至 user_prompt_hook.py（通过 additionalContext，阅后即焚不累积）。
"""
import os
from common.utils import (
    get_intern_name, log_debug,
    get_session_intern, WORK_AGENTS_ROOT,
)
from intern_module.context_loader import load_intern_files


class InternModule:

    # ── SessionStart ──────────────────────────────────────────
    def on_session_start(self, state, hook_input):
        """SessionStart 时记录映射日志。

        system prompt 注入已移至 UserPromptSubmit（每次注入，阅后即焚不累积）。
        """
        intern_dir = state["_intern_dir"]
        session_id = hook_input.get("sessionId", "")

        if session_id:
            mapped_intern = get_session_intern(session_id)
            if mapped_intern:
                log_debug(intern_dir, "InternModule.session_start",
                          f"session mapped to {mapped_intern}")

        log_debug(intern_dir, "InternModule.session_start", "done")

    # ── UserPromptSubmit ──────────────────────────────────────
    def on_user_prompt(self, state, hook_input):
        """注入简短身份提醒（VS Code systemMessage）。"""
        intern_dir = state["_intern_dir"]
        prompt = hook_input.get("prompt", "").strip()
        session_id = hook_input.get("sessionId", "")

        state.setdefault("intern", {})
        state["intern"]["last_prompt"] = prompt

        if session_id:
            mapped = get_session_intern(session_id)
            if mapped:
                name = mapped
            else:
                name = get_intern_name(intern_dir)
        else:
            name = get_intern_name(intern_dir)

        f = load_intern_files(intern_dir)
        brief = (
            f"[系统通知] 你是 {name}，"
            f"状态={f['status']}，"
            f"任务={f['task_id'] or '无'}。"
        )
        state.setdefault("_output", {})
        # VS Code: 用 systemMessage
        state["_output"]["systemMessage"] = brief
