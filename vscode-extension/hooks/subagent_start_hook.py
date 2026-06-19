#!/usr/bin/env python3
"""VS Code SubagentStart hook.
Fires when a SubAgent is initialized. Increments depth counter.
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    get_intern_dir, load_state, save_state, log_debug, state_lock,
    dump_hook_stdin,
)
from log_module.module import LogModule


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        return

    # task200 调研：dump 完整 stdin（gated by marker file），验证 recap/btw 是否经过 SubagentStart
    dump_hook_stdin(intern_dir, "subagent_start", hook_input)

    agent_id = hook_input.get("agent_id", "")
    agent_type = hook_input.get("agent_type", "")

    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir
        state["subagent_depth"] = state.get("subagent_depth", 0) + 1
        state["skip_next_ups"] = True
        log_debug(intern_dir, "subagent_start_hook",
                  f"SubAgent START: agent_id={agent_id} type={agent_type} depth={state['subagent_depth']}")
        # Save transcript position so FeishuModule can read pre-SubAgent text.
        # Key by the Agent tool's tool_use_id (stashed by pre_tool_hook) so
        # concurrent Agent spawns don't overwrite each other's offsets.
        transcript_path = hook_input.get("transcript_path", "")
        if transcript_path and os.path.exists(transcript_path):
            fs = state.get("feishu", {})
            if fs:
                pending_tuid = state.pop("_pending_agent_tool_use_id", "")
                key = pending_tuid or agent_id  # agent_id as safety-net key
                sts = fs.setdefault("subagent_transcripts", {})
                sts[key] = os.path.getsize(transcript_path)
                state["feishu"] = fs

        # Write SubAgent start boundary marker to log
        log_mod = LogModule()
        log_mod.on_subagent_start(state, hook_input)

        state.pop("_intern_dir", None)
        save_state(intern_dir, state)


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
