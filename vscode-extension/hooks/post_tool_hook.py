#!/usr/bin/env python3
"""VS Code PostToolUse hook.
Loads state → runs modules → saves state.

SubAgent depth tracking (hybrid):
- runSubagent: native SubagentStart/SubagentStop events
- execution_subagent: manual decrement here + offset advance (no native events)
When depth > 0, only LogModule runs (FeishuModule skipped — SubAgent internals
should be logged for debug but not sent to feishu).
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import get_intern_dir, load_state, save_state, log_debug, state_lock
from log_module.module import LogModule
from feishu_module.module import FeishuModule


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        sys.exit(0)

    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir

        # Debug: log raw stdin keys and transcript_path
        tp = hook_input.get("transcript_path", "")
        tn = hook_input.get("tool_name", "")
        log_debug(intern_dir, "post_tool_hook", 
                  f"stdin_keys={sorted(hook_input.keys())} transcript_path={tp!r} exists={os.path.exists(tp) if tp else 'N/A'}")

        # SubAgent depth managed by SubagentStart/SubagentStop hooks.
        # depth > 0: inside SubAgent — run LogModule only (for debug), skip FeishuModule
        # Exception: execution_subagent's own PostToolUse fires at depth>0 (before
        # decrement below), but FeishuModule must run to capture pre-SubAgent text
        # and advance offset past SubAgent internals.
        #
        # Non-user-triggered turns (task-notification / terminal-notification) are
        # NOT filtered here: users care how the LLM reacts to the notification,
        # so tool progress must reach feishu just like a main turn. Visibility is
        # decided by event semantics, not by "who triggered this turn".
        depth = state.get("subagent_depth", 0)
        if depth > 0 and tn != "execution_subagent":
            log_debug(intern_dir, "post_tool_hook",
                      f"SubAgent depth={depth}: LogModule only (tool={tn})")
            modules = [LogModule()]
        else:
            modules = [LogModule(), FeishuModule()]

        for m in modules:
            m.on_post_tool(state, hook_input)

        # execution_subagent doesn't trigger SubagentStop — manually decrement
        # FeishuModule handles offset advance in on_post_tool (SUBAGENT_TOOLS path)
        if tn == "execution_subagent":
            old_depth = state.get("subagent_depth", 0)
            state["subagent_depth"] = max(0, old_depth - 1)
            log_debug(intern_dir, "post_tool_hook",
                      f"execution_subagent done: depth {old_depth}->{state['subagent_depth']}")
            # Write SubAgent end boundary marker to log
            log_mod = LogModule()
            log_mod.on_subagent_stop(state, hook_input)

        state.pop("_intern_dir", None)
        save_state(intern_dir, state)

    log_debug(intern_dir, "post_tool_hook", "done")


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
