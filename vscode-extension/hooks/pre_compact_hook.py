#!/usr/bin/env python3
"""VS Code PreCompact hook.
Fires before conversation compaction.

Compact is transparent to hooks — it doesn't trigger Stop, and hook state
(.hook_state.json) persists across compact. FeishuModule continues updating
the same message after compact via PostToolUse.

Sends a brief note to feishu so the user knows the intern is compacting
(not offline).
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import get_intern_dir, load_state, save_state, log_debug, state_lock


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        return

    trigger = hook_input.get("trigger", "auto")
    log_debug(intern_dir, "pre_compact_hook",
              f"PreCompact: trigger={trigger}")

    # Send compact note to feishu
    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir
        fs = state.get("feishu", {})
        msg_id = fs.get("message_id")
        if msg_id:
            buffer_lines = fs.get("buffer_lines", [])
            buffer_lines.append("🔄 context compacting...")
            fs["buffer_lines"] = buffer_lines

            from feishu_module.module import _get_token
            from feishu_module.feishu_api import update_message
            from feishu_module.timeline_composer import compose

            token = _get_token(state)
            if token:
                text = compose(buffer_lines, spinner=True)
                ok, err = update_message(token, msg_id, text)
                count = fs.get("update_count", 0) + 1
                fs["update_count"] = count
                log_debug(intern_dir, "pre_compact_hook",
                          f"feishu compact note: ok={ok} update#{count}" + (f" ERR={err}" if err else ""))

            state["feishu"] = fs
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
