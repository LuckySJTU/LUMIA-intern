#!/usr/bin/env python3
"""VS Code PreToolUse hook.
Loads state → runs modules → outputs block decision if dangerous command.

SubAgent depth tracking (hybrid):
- runSubagent: native SubagentStart/SubagentStop events
- execution_subagent: manual increment here (no native events)
When depth > 0, modules are skipped (tools inside SubAgent don't need validation).
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import get_intern_dir, load_state, save_state, log_debug, state_lock
from validation_module.module import ValidationModule
from log_module.module import LogModule
from question_handler import handle_ask_user_question, handle_exit_plan_mode


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        return  # no output = allow

    tool_name = hook_input.get("tool_name", "")

    # ── 交互式工具拦截（AskUserQuestion / ExitPlanMode） ──
    # 这些工具会长时间阻塞等待飞书回复，故意跳过 state_lock / ValidationModule / subagent_depth check：
    #   - state_lock 不需要（不读写 state）
    #   - ValidationModule.on_pre_tool 当前是 no-op（validation_module/module.py:285）
    #   - AskUserQuestion/ExitPlanMode 不会在 SubAgent 内触发（CLI 限制）
    # 调用边界由 PostToolUse 阶段的 LogModule/FeishuModule.on_post_tool 记录（hook
    # 返回 allow+updatedInput → CLI emit ToolUse → PostToolUse 正常触发）。
    # Codex request_user_input 走原生 TUI：UserPromptSubmit hook 已注册 daemon transcript
    # watcher，飞书选择返回后由 daemon 向 tmux 注入答案。不要再在 PreToolUse 同步等待，
    # 否则会和 watcher 双重注册 pending question，并可能用“6 小时未回复”误判 deny。
    #
    # 注：pre-tool assistant commentary 无法补发到飞书 — PreToolUse stdin 不含 message
    # content，transcript JSONL 在 tool_use resolve 之后才 flush（Claude CLI 原子写整条
    # assistant message）。替代方案：daemon 卡片把 options[].description 渲染成独立可见
    # text block（task198 §4 pivot）。
    if tool_name == "request_user_input" and os.environ.get("INTERN_CODEX_RUI_PRETOOL_BRIDGE") != "1":
        log_debug(
            intern_dir,
            "pre_tool_hook",
            "request_user_input: allow native Codex TUI; Feishu bridge is handled by transcript watcher",
        )
        return

    if tool_name in ("AskUserQuestion", "request_user_input", "ExitPlanMode"):
        _log = lambda msg: log_debug(intern_dir, "pre_tool_hook", msg)
        if tool_name == "ExitPlanMode":
            result = handle_exit_plan_mode(intern_dir, hook_input, _log)
        else:
            result = handle_ask_user_question(intern_dir, hook_input, _log)
        if result:
            print(json.dumps(result, ensure_ascii=False))
        return

    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir

        # Skip modules for tools running inside a SubAgent
        # (depth managed by SubagentStart/SubagentStop hooks for runSubagent,
        #  and manually for execution_subagent which doesn't trigger native events)

        # execution_subagent doesn't trigger SubagentStart — manually increment
        if tool_name == "execution_subagent":
            state["subagent_depth"] = state.get("subagent_depth", 0) + 1
            log_debug(intern_dir, "pre_tool_hook",
                      f"execution_subagent detected: depth -> {state['subagent_depth']}")
            # Save transcript position so FeishuModule can read pre-SubAgent text.
            # Key by tool_use_id so concurrent subagents don't overwrite each other.
            transcript_path = hook_input.get("transcript_path", "")
            if transcript_path and os.path.exists(transcript_path):
                fs = state.get("feishu", {})
                if fs:
                    tuid = hook_input.get("tool_use_id", "")
                    sts = fs.setdefault("subagent_transcripts", {})
                    key = tuid or f"_legacy_{id(hook_input)}"
                    sts[key] = os.path.getsize(transcript_path)
                    state["feishu"] = fs
            # Write SubAgent start boundary marker to log
            log_mod = LogModule()
            log_mod.on_subagent_start(state, hook_input)
            state.pop("_intern_dir", None)
            save_state(intern_dir, state)
            return  # skip modules — this tool IS the SubAgent

        depth = state.get("subagent_depth", 0)
        if depth > 0:
            log_debug(intern_dir, "pre_tool_hook",
                      f"SKIP modules: inside SubAgent (depth={depth}, tool={tool_name})")
            save_state(intern_dir, state)
            return

        # Save SubAgent prompt for log boundary marker
        # (SubagentStart hook will read this from state)
        if tool_name in ("runSubagent", "Agent"):
            ti = hook_input.get("tool_input", {})
            if isinstance(ti, dict):
                prompt = ti.get("prompt", ti.get("description", ""))
                if prompt:
                    state["_pending_subagent_prompt"] = prompt
            # Save tool_use_id so SubagentStart can key subagent_transcripts by it.
            # CLI serializes SubagentStart launches (≥500ms gap), so a single
            # pending slot is safe even for concurrent Agent spawns.
            tuid = hook_input.get("tool_use_id", "")
            if tuid:
                state["_pending_agent_tool_use_id"] = tuid

        modules = [ValidationModule()]
        for m in modules:
            m.on_pre_tool(state, hook_input)

        decision = state.get("validation", {}).get("block_decision")

        state.pop("_intern_dir", None)
        if "validation" in state and "block_decision" in state.get("validation", {}):
            del state["validation"]["block_decision"]
        save_state(intern_dir, state)

    if decision:
        # VS Code PreToolUse 支持 hookSpecificOutput.permissionDecision
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.get("reason", "Blocked by policy"),
            }
        }
        print(json.dumps(output, ensure_ascii=False))
        log_debug(intern_dir, "pre_tool_hook", f"BLOCK: {decision.get('reason', '')[:80]}")


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
