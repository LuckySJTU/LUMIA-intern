#!/usr/bin/env python3
"""UserPromptSubmit hook for Copilot, Claude CLI, and Codex CLI.

映射由插件在 startInternSession 时预写入。
此 hook 仅在映射存在时执行 module 逻辑（log/feishu）。
无映射时静默放行。

关键设计：每次 UserPromptSubmit 通过 additionalContext 注入完整 system prompt。
Claude/Copilot 将这段 context 作为 per-turn hook context 处理；Codex CLI
0.125+ 支持同一个 hookSpecificOutput.additionalContext schema，因此这里
不得因 intern_type == "codex" 绕过注入。
"""
import sys
import traceback
import os
import json
import re
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    get_intern_dir, load_state, save_state, log_debug, state_lock,
    MetadataResolverError, read_file_safe,
)
from _daemon_addr import get_daemon_http_url

# Claude CLI <task-notification> pattern: background task completion
_TASK_NOTIFICATION_RE = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)

# <output-file> path embeds the session_id of the session that launched the bg task.
# Example path: /tmp/claude-0/-work-agents-probe/<session-uuid>/tasks/<task-id>.output
_OUTPUT_FILE_SESSION_RE = re.compile(
    r"<output-file>[^<]*?/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/tasks/"
)

# Copilot terminal notification: async terminal completion/input-waiting injected as user message
# Example: "[Terminal 8cb50388-... notification: command completed with exit code 128. ...]"
_TERMINAL_NOTIFICATION_RE = re.compile(r"^\[Terminal [0-9a-f-]+ notification:")


def _extract_output_file_session(prompt):
    """Return the session_id embedded in <output-file> of a task-notification,
    or empty string if the prompt is not a task-notification or lacks the tag.
    Used to detect notifications from background tasks launched before /clear.
    """
    m = _OUTPUT_FILE_SESSION_RE.search(prompt or "")
    return m.group(1) if m else ""


def build_user_prompt_additional_context_output(prompt_text):
    """Build the shared UserPromptSubmit additionalContext output shape.

    Codex CLI, Claude CLI, and VS Code Copilot hooks all consume this
    hookSpecificOutput schema for UserPromptSubmit. Keep this helper free of
    intern-type branching so Codex stays aligned with the Claude path.
    """
    if not prompt_text:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": prompt_text,
        }
    }


def build_metadata_resolver_failure_context(error: str) -> str:
    return (
        "Metadata resolver failed. Enterprise hooks cannot fall back to legacy "
        f"workspace paths. Resolve task342 metadata resolver contract first: {error}"
    )


def _is_machine_helper_state(state):
    return state.get("role") in {"machine_helper", "machine_debugger"} or state.get("projectless") is True


def register_codex_request_user_input_watcher(intern_dir, hook_input):
    """Register Codex transcript tailing for request_user_input in 0.130+.

    Codex 0.130 handles request_user_input in the TUI and no longer emits a
    PreToolUse hook for it, so the daemon watches the active transcript instead.
    """
    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        return

    intern_name = os.path.basename(intern_dir.rstrip("/"))
    try:
        offset = os.path.getsize(transcript_path)
    except OSError:
        offset = 0

    body = {
        "intern_name": intern_name,
        "session_id": hook_input.get("session_id", "") or hook_input.get("sessionId", ""),
        "transcript_path": transcript_path,
        "offset": offset,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{get_daemon_http_url()}/api/codex/request_user_input/register",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=1).read()
        log_debug(intern_dir, "user_prompt_hook", f"registered codex request_user_input watcher offset={offset}")
    except Exception as e:
        log_debug(intern_dir, "user_prompt_hook", f"codex request_user_input watcher register failed: {e}")


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        # 未绑定 intern，静默放行
        sys.exit(0)

    from feishu_module.module import FeishuModule
    from log_module.module import LogModule
    from validation_module.module import ValidationModule
    from intern_module.context_loader import build_system_prompt

    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir

        # Skip if inside SubAgent — flag set by SubagentStart, consumed here.
        # Only skip when depth > 0 (truly inside SubAgent). At depth=0 the
        # SubAgent has already ended and this is a real Human UPS — the flag
        # is stale (e.g. Claude CLI sub-agents don't fire UPS to consume it).
        depth = state.get("subagent_depth", 0)
        if state.pop("skip_next_ups", False) and depth > 0:
            # Safety net: clear finalized to prevent blocking post-SubAgent
            # PostToolUse if Stop APPROVE fired before this SubAgent started.
            fs = state.get("feishu", {})
            fs.pop("finalized", None)
            state["feishu"] = fs
            log_debug(intern_dir, "user_prompt_hook", f"SKIP: inside SubAgent (depth={depth})")
            state.pop("_intern_dir", None)
            save_state(intern_dir, state)
            sys.exit(0)

        # Defensive: depth > 0 but skip_next_ups missing (edge case — e.g.
        # SubagentStart fired but flag was consumed by a prior UPS).
        # Skip injection this once and reset depth to avoid getting stuck.
        if depth > 0:
            log_debug(intern_dir, "user_prompt_hook",
                      f"depth={depth} without skip_next_ups — clearing stale depth, skipping injection")
            state["subagent_depth"] = 0
            state.pop("_intern_dir", None)
            save_state(intern_dir, state)
            sys.exit(0)

        # Human UPS reached here (depth=0) — reset depth (clears stale depth from
        # missing SubagentStop events)
        state["subagent_depth"] = 0

        # intern_start 命令拦截：block LLM 调用，只输出 systemMessage
        # Copilot intern 用预填 intern_start 创建 chat panel，不需要 LLM 处理
        user_prompt = hook_input.get("prompt", "").strip()
        if user_prompt.startswith("intern_start"):
            log_debug(intern_dir, "user_prompt_hook", f"BLOCK: intern_start command")
            state.pop("_intern_dir", None)
            save_state(intern_dir, state)
            output = {
                "decision": "block",
                "reason": "intern session initialized",
            }
            print(json.dumps(output, ensure_ascii=False))
            sys.exit(0)

        # Detect background/auto-injected prompts that are NOT real user input:
        # 1. Claude CLI <task-notification> (background task completion)
        # 2. Copilot terminal notification (async terminal completion/waiting)
        is_task_notification = bool(_TASK_NOTIFICATION_RE.search(user_prompt))
        is_terminal_notification = bool(_TERMINAL_NOTIFICATION_RE.match(user_prompt))
        is_background = is_task_notification or is_terminal_notification

        if is_task_notification:
            state["_is_task_notification"] = True
            # Bug-15: detect notifications from background tasks launched by a
            # session that was subsequently /clear-ed. hook layer can't kill the
            # old bash process (CLI-internal), but we can label it so supervisor
            # knows "this doesn't belong to the current turn's context".
            of_sid = _extract_output_file_session(user_prompt)
            current_sid = hook_input.get("session_id", "") or hook_input.get("sessionId", "")
            if of_sid and current_sid and of_sid != current_sid:
                state["background_stale"] = True
                log_debug(intern_dir, "user_prompt_hook",
                          f"stale bg task-notification (of_session={of_sid} current={current_sid})")
            else:
                state["background_stale"] = False
        if is_background:
            bg_type = "task-notification" if is_task_notification else "terminal-notification"
            state["is_background_turn"] = True
            state["background_type"] = bg_type
            log_debug(intern_dir, "user_prompt_hook",
                      f"background prompt detected ({bg_type}), skipping system prompt")
        else:
            # Clear background flags from previous turn
            state.pop("is_background_turn", None)
            state.pop("background_type", None)
            state.pop("background_stale", None)

        # 每次从本地文件重新生成 system prompt（不依赖 SessionStart）
        # background prompts 不注入 system prompt（节省 token）
        prompt_text = ""
        resolver_error = ""
        if not is_background:
            if _is_machine_helper_state(state):
                prompt_text = read_file_safe(os.path.join(intern_dir, "prompt.md"), max_chars=12000)
                state.setdefault("log", {})["system_prompt"] = prompt_text
                state.setdefault("intern", {})["status"] = "Idle"
            else:
                try:
                    prompt_text, status = build_system_prompt(intern_dir)
                    state.setdefault("log", {})["system_prompt"] = prompt_text
                    state.setdefault("intern", {})["status"] = status
                    state.pop("metadata_resolver_error", None)
                except MetadataResolverError as e:
                    resolver_error = str(e)
                    prompt_text = build_metadata_resolver_failure_context(resolver_error)
                    state["metadata_resolver_error"] = resolver_error
                    log_debug(intern_dir, "user_prompt_hook", f"metadata resolver failed: {resolver_error}")
                except Exception as e:
                    prompt_text = ""
                    log_debug(intern_dir, "user_prompt_hook", f"build_system_prompt failed: {e}")

        # task228: 本轮附件注入——feishu_daemon 收到主管发的 image/file 后已把
        # 附件落盘到 $WORK_AGENTS_ROOT/<intern>/.feishu_inbox/<mid>/ 并把元信息
        # 追加到 state.pending_attachments。此处把累积的附件 bullet 拼进 prompt_text
        # 末尾，AI 看到 additionalContext 里「本轮附件」section 就能 Read 打开；
        # 然后 pop 掉 pending_attachments，相当于"本轮已消费"。background prompts
        # 不消费（保留给下一条真 text）。
        if not is_background:
            pending = state.pop("pending_attachments", None)
            if isinstance(pending, list) and pending:
                bullets = []
                for item in pending:
                    if not isinstance(item, dict):
                        continue
                    kind = item.get("kind") or "?"
                    path = item.get("path") or ""
                    if path:
                        bullets.append(f"- {kind}: {path}")
                if bullets:
                    prompt_text = (prompt_text or "") + "\n\n## 本轮附件\n" + "\n".join(bullets)
                    log_debug(intern_dir, "user_prompt_hook",
                              f"injected {len(bullets)} pending attachments")

        # Terminal notifications: run FeishuModule (create brief feishu message
        # for visibility) but skip ValidationModule and system prompt injection.
        # Task notifications: handled by FeishuModule (append to existing message).
        if _is_machine_helper_state(state):
            modules = [FeishuModule(), LogModule()]
        elif is_terminal_notification:
            modules = [FeishuModule(), LogModule()]
        elif resolver_error:
            modules = [FeishuModule()]
        else:
            modules = [ValidationModule(), FeishuModule(), LogModule()]
        for m in modules:
            m.on_user_prompt(state, hook_input)

        register_codex_request_user_input_watcher(intern_dir, hook_input)

        state.pop("_intern_dir", None)
        state.pop("_is_task_notification", None)
        save_state(intern_dir, state)

    # 通过 hookSpecificOutput.additionalContext 注入完整 system prompt。
    # Codex CLI 已支持同一 UserPromptSubmit 输出 schema，不能特殊跳过。
    output = build_user_prompt_additional_context_output(prompt_text)

    print(json.dumps(output, ensure_ascii=False))
    log_debug(intern_dir, "user_prompt_hook", f"additionalContext: {len(prompt_text)} chars")
    log_debug(intern_dir, "user_prompt_hook", "done")



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
