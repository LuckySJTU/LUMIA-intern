#!/usr/bin/env python3
"""VS Code SubagentStop hook.
Fires when a SubAgent completes. Decrements depth counter.
Runs LogModule so SubAgent activity is recorded in session log.

Also advances FeishuModule's transcript_offset to skip SubAgent internal content.
This ensures SubAgent text (including <final_answer>) never reaches Feishu,
without needing regex filters or cross-module coupling.
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    get_intern_dir, load_state, save_state, log_debug, state_lock,
    get_chat_id, load_feishu_credentials, dump_hook_stdin,
)
from log_module.module import LogModule
from feishu_module.feishu_api import get_tenant_token, send_message


def _send_btw_answer(intern_name, answer, log_fn):
    """直接调 Feishu API 发 /btw 答案到该 intern 的群。

    /btw 触发 SubagentStop（不触发 Stop），FeishuModule 默认 Stop 路径
    走不到；这里直接复用 send_message，避免绕 daemon HTTP 一跳。
    """
    chat_id = get_chat_id(intern_name)
    if not chat_id:
        log_fn(f"/btw answer: no chat_id registered for {intern_name}, skip")
        return
    try:
        app_id, app_secret = load_feishu_credentials()
        token = get_tenant_token(app_id, app_secret)
    except Exception as e:
        log_fn(f"/btw answer: failed to get token: {e}")
        return
    text = f"💡 /btw 答案：\n{answer}"
    msg_id, err = send_message(token, chat_id, text)
    if err:
        log_fn(f"/btw answer: send_message err: {err}")
    else:
        log_fn(f"/btw answer sent for {intern_name} ({len(answer)} chars), msg_id={msg_id}")


def _send_plan_card(intern_name, plan_text, log_fn):
    """把 Plan subagent 的 plan 发到飞书，附 5 选项提示。

    Plan subagent 的工具清单排除 ExitPlanMode（系统描述明文），plan 文本只在
    last_assistant_message。这里复用 handle_exit_plan_mode 的 5 选项（auto /
    acceptEdits / reviewEdits / keepPlanning / ultraplan）但不走 question_handler
    的交互式轮询——SubagentStop 不能阻塞等几小时。走纯文本消息，主管看到后
    在群里回复选项名，主 agent 下一轮会按此继续。
    """
    chat_id = get_chat_id(intern_name)
    if not chat_id:
        log_fn(f"plan card: no chat_id for {intern_name}, skip")
        return
    try:
        app_id, app_secret = load_feishu_credentials()
        token = get_tenant_token(app_id, app_secret)
    except Exception as e:
        log_fn(f"plan card: failed to get token: {e}")
        return
    # Truncate overly long plan to keep feishu message readable
    max_len = 2000
    plan_summary = plan_text
    if len(plan_summary) > max_len:
        plan_summary = plan_summary[:max_len] + f"\n\n... (截断，共 {len(plan_text)} 字符)"
    text = (
        f"📋 Plan subagent 完成，请选择执行方式：\n\n"
        f"{plan_summary}\n\n"
        f"---\n"
        f"选项：`auto` / `acceptEdits` / `reviewEdits` / `keepPlanning` / `ultraplan`\n"
        f"在群里回复以上词之一，主 agent 下一轮按此继续。"
    )
    msg_id, err = send_message(token, chat_id, text)
    if err:
        log_fn(f"plan card: send_message err: {err}")
    else:
        log_fn(f"plan card sent for {intern_name} ({len(plan_text)} chars), msg_id={msg_id}")


def _is_compact_summary(hook_input) -> bool:
    """Detect /compact's internal summarize subagent finishing — task270.

    BACKGROUND
    ----------
    When the user runs `/compact` on a sufficiently large transcript, Claude
    Code internally spawns an LLM "summarize" subagent. When that subagent
    completes, SubagentStop fires with:
      - agent_type == ""
      - agent_transcript_path = "/.../subagents/agent-<id>.jsonl" (non-empty
        string, but the file does NOT exist on disk)

    These fields are **identical** to the /btw and prompt_suggestion cases
    (task200 finding, re-confirmed in task270 against archived dumps), so
    classify() cannot tell them apart by field shape and falls back to "btw".
    That fallback path calls _send_btw_answer and ships a 17-18 KB compact
    summary into Feishu as a fake `💡 /btw 答案:` message.

    The only deterministic signal is the **content** of last_assistant_message:
    Anthropic's internal system prompt forces the summarize subagent to emit
    `<analysis>...</analysis>\\n<summary>...</summary>` envelope on every
    compact run (verified against the real dump captured in
    intern_rule_alice 2026-05-16 09:00:59; cross-checked against archived
    task200 dumps for /btw real-answer + prompt_suggestion ×2, none of which
    carry both tags).

    ROLLBACK NOTE
    -------------
    If a future Claude Code release changes the compact summary prompt and
    this detector starts missing, the symptom is the original task270 bug
    (17 KB `💡 /btw 答案:` ship to Feishu on /compact). To temporarily
    disable this gate while investigating, change the body to `return False`
    — that restores pre-task270 behavior without affecting real /btw, Plan,
    Task, or team_agent paths. The proper fix is then to re-capture the new
    compact summary shape via `debug/stdin_dump_enabled` marker and update
    the content check.

    See workspace/tasks/task270_compact_misclassified_as_btw/task_knowledge.md
    items 13-14 for the captured stdin samples and field-level comparison.
    """
    if hook_input.get("agent_type"):
        return False
    msg = (hook_input.get("last_assistant_message") or "").strip()
    return msg.startswith("<analysis>") and msg.endswith("</summary>")


def classify(hook_input) -> str:
    """Classify SubagentStop event into one of:
      - "plan"       : Agent(subagent_type="Plan") — plan text in last_assistant_message
      - "normal"     : regular Task tool subagent (agent_type non-empty)
      - "team_agent" : team-custom agent with empty agent_type but real transcript
                       (ref: claude-code issue #33384)
      - "btw"        : /btw side question — fork with skipTranscript, no transcript file

    Prompt suggestion 也走 agent_type=="" 分支且 stdin 与 /btw 同构 (Session 26
    实测)，无法在 hook 层 deterministic 区分。靠 .claude/settings.json env
    字段 CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false 源头禁用，hook 层 fallback
    到 btw 不再误伤。
    """
    at = hook_input.get("agent_type", "") or ""
    tp = hook_input.get("agent_transcript_path", "") or ""
    if at == "Plan":
        return "plan"
    if at:
        return "normal"
    if tp and os.path.exists(tp):
        return "team_agent"
    return "btw"


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        return

    # task200 调研：在执行任何判别前 dump 完整 stdin（gated by marker file）
    dump_hook_stdin(intern_dir, "subagent_stop", hook_input)

    # task270: /compact 内部 summarize subagent 完成时触发的 SubagentStop 在 stdin
    # 字段层与 /btw / prompt_suggestion 同构（agent_type='' + agent_transcript_path
    # 非空但文件不存在），classify() 走 fallback 命中 "btw" 会把 17 KB compact
    # summary 当 /btw 答案误推飞书。这里在 dump 之后、classify 之前用内容包络
    # 短路 — 命中 compact 直接 return，不发飞书也不更新 subagent_depth（因为
    # 没有对应 SubagentStart 配对）。详见 _is_compact_summary docstring。
    if _is_compact_summary(hook_input):
        log_debug(intern_dir, "subagent_stop_hook",
                  f"classify: compact (skip /btw send, msg_len={len(hook_input.get('last_assistant_message') or '')})")
        return

    agent_id = hook_input.get("agent_id", "")
    agent_type = hook_input.get("agent_type", "")
    event = classify(hook_input)
    log_fn = lambda msg: log_debug(intern_dir, "subagent_stop_hook", msg)
    log_fn(f"classify: {event} agent_id={agent_id} agent_type={agent_type!r}")

    # /btw: no SubagentStart paired, no transcript file. Direct-send answer.
    if event == "btw":
        answer = (hook_input.get("last_assistant_message") or "").strip()
        intern_name = os.path.basename(intern_dir.rstrip("/"))
        log_fn(f"/btw detected: agent_id={agent_id} answer_len={len(answer)}")
        if answer:
            _send_btw_answer(intern_name, answer, log_fn)
        return

    # normal / team_agent / plan: paired with SubagentStart — decrement depth.
    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir

        old_depth = state.get("subagent_depth", 0)
        state["subagent_depth"] = max(0, old_depth - 1)
        log_debug(intern_dir, "subagent_stop_hook",
                  f"{event} STOP: agent_id={agent_id} depth={old_depth}->{state['subagent_depth']}")

        # Plan subagent: surface last_assistant_message as the plan card
        # (Plan agent tool list excludes ExitPlanMode, so plan text lives here).
        if event == "plan":
            plan_text = (hook_input.get("last_assistant_message") or "").strip()
            intern_name = os.path.basename(intern_dir.rstrip("/"))
            if plan_text:
                _send_plan_card(intern_name, plan_text, log_fn)
            else:
                log_fn("plan subagent stop but last_assistant_message empty, skip card")

        # Run LogModule to record SubAgent end boundary in session log
        log_mod = LogModule()
        log_mod.on_subagent_stop(state, hook_input)

        # NOTE: FeishuModule offset advance is NOT done here.
        # It's handled by FeishuModule.on_post_tool when the SubAgent tool's
        # PostToolUse fires (using subagent_transcript_start saved at SubagentStart).
        # This ensures pre-SubAgent assistant text is captured before skipping internals.

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
