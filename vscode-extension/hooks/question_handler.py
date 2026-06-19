#!/usr/bin/env python3
"""
交互式问答处理器 — 拦截 AskUserQuestion / ExitPlanMode，
通过飞书 daemon 转发给主管，轮询等待回复后返回 updatedInput。
"""
import json
import time
import traceback
import urllib.request
import urllib.error
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _daemon_addr import get_daemon_http_url

# DAEMON_BASE 是动态的（取决于 daemon 当前的 ephemeral 端口）——
# 每次调用时重新读 /tmp/feishu_daemon.json。
# 足够长的超时（6 小时），让主管有充足时间回复
POLL_TIMEOUT = 6 * 3600
POLL_INTERVAL = 2  # 秒


def _daemon_post(path, body, log_fn=None):
    """POST to daemon, return (parsed_json, error_str). error_str=None on success."""
    url = f"{get_daemon_http_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()), None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if log_fn:
            log_fn(f"_daemon_post {path} failed: {err}\n{traceback.format_exc()}")
        return None, err


def _daemon_get(path, log_fn=None):
    """GET from daemon, return (parsed_json, error_str). error_str=None on success."""
    url = f"{get_daemon_http_url()}{path}"
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        return json.loads(resp.read()), None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if log_fn:
            log_fn(f"_daemon_get {path} failed: {err}\n{traceback.format_exc()}")
        return None, err


def handle_ask_user_question(intern_dir, hook_input, log_fn):
    """处理 AskUserQuestion / request_user_input 工具调用。

    1. 发题到飞书（tool_name 透传，daemon 端识别 Codex/Claude 标识）
    2. 轮询等回答
    3. 返回 allow + updatedInput 或 deny
       - Claude AskUserQuestion：updatedInput.answers 是 dict {question_text: label}
       - Codex request_user_input：updatedInput.answers 是 list [label_for_q1, ...]
         （按 questions 数组顺序对应）

    Args:
        intern_dir: intern 根目录
        hook_input: PreToolUse 的完整 stdin JSON
        log_fn: log_debug 的偏函数

    Returns:
        dict: hookSpecificOutput JSON，或 None（允许原生行为）
    """
    intern_name = os.path.basename(intern_dir.rstrip("/"))
    tool_name = hook_input.get("tool_name", "AskUserQuestion")
    is_codex = tool_name == "request_user_input"
    tool_input = hook_input.get("tool_input", {})
    questions = tool_input.get("questions", [])

    if not questions:
        log_fn(f"{tool_name}: no questions, allow native")
        return None

    log_fn(f"{tool_name}: {len(questions)} questions, forwarding to feishu")

    # 1. 发送到飞书 daemon（透传 tool_name 让 daemon 区分 prefix）
    resp, err = _daemon_post("/api/question/ask", {
        "intern_name": intern_name,
        "tool_name": tool_name,
        "questions": questions,
    }, log_fn=log_fn)
    if err or not resp or resp.get("error"):
        # 项目规则 #6：不 fallback 到 native，直接 deny 并把错误告诉主管
        reason = err or (resp.get("error") if resp else "daemon 返回空")
        log_fn(f"{tool_name}: daemon ask failed, denying: {reason}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"飞书 daemon 不可达，问题未转发主管：{reason}。请人工处理（检查 daemon 状态 / 或尝试 kill -USR1 dump 栈）。",
            }
        }

    # 2. 轮询等待飞书回答
    answered = _poll_for_answer(intern_name, log_fn)

    if answered is None:
        # 超时 — 更新飞书卡片为"⏰ 已超时" + pop
        _daemon_post("/api/question/timeout",
                     {"intern_name": intern_name, "hours": POLL_TIMEOUT // 3600},
                     log_fn=log_fn)
        log_fn(f"{tool_name}: timeout, denied")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"主管 {POLL_TIMEOUT // 3600} 小时内未回复，已超时",
            }
        }

    # 3. 构造 updatedInput（双 schema 适配）
    answers = answered.get("answers", {})
    log_fn(f"{tool_name}: got answers: {answers}")

    if is_codex:
        # Codex `RequestUserInputResponse`：{ answers: Array<string> }，按 questions 顺序
        # daemon 端 answers 仍是 dict（key=question 文本），这里 flatten 成 ordered list
        ordered_answers = [answers.get(q.get("question", ""), "") for q in questions]
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {
                    "questions": questions,  # 透传含 id 字段
                    "answers": ordered_answers,
                },
            }
        }

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "questions": questions,
                "answers": answers,
            },
        }
    }


def handle_exit_plan_mode(intern_dir, hook_input, log_fn):
    """处理 ExitPlanMode 工具调用（仅 Claude CLI intern）。

    ExitPlanMode tool_input 包含：
    - plan: 完整 plan 文本
    - planFilePath: plan 文件路径
    - allowedPrompts: 允许的后续操作

    将完整 plan 作为普通消息（prelude）+ 选项卡片发到飞书。主管自由文本或
    keepPlanning 都会 deny ExitPlanMode 并把原文作为 reason 回传 Claude，
    plan mode 保持，Claude 可据此改 plan。
    """
    intern_name = os.path.basename(intern_dir.rstrip("/"))

    log_fn("ExitPlanMode: forwarding to feishu")

    tool_input = hook_input.get("tool_input", {})
    plan_text = tool_input.get("plan", "")
    plan_file = tool_input.get("planFilePath", "")
    if plan_text:
        log_fn(f"ExitPlanMode: got plan from tool_input ({len(plan_text)} chars), file={plan_file}")
    else:
        log_fn("ExitPlanMode: no plan in tool_input")

    # ExitPlanMode 的固定选项（去掉 keepPlanning —— 留 plan 应该自由文本回复）
    plan_options = [
        {"label": "auto", "description": "批准并以 auto 模式执行"},
        {"label": "acceptEdits", "description": "批准并自动接受编辑"},
        {"label": "reviewEdits", "description": "批准并逐个审核编辑"},
        {"label": "ultraplan", "description": "使用 Ultraplan 精炼"},
    ]

    # 卡片正文：完整 plan 已通过 prelude_file_path 单独发文件，这里只保留简短提示
    if plan_text:
        question_text = (
            f"Claude 已完成方案规划（完整 plan 共 {len(plan_text)} 字符，已作为 md 文件发到群）。"
            f"\n\n请选择执行方式；若想改 plan，直接回复自由文本即可（plan mode 保持）。"
        )
    else:
        question_text = "Claude 已完成方案规划，请选择执行方式；若想改 plan，直接回复自由文本即可。"

    questions = [{
        "question": question_text,
        "header": "PlanMode",
        "options": plan_options,
        "multiSelect": False,
    }]

    ask_body = {
        "intern_name": intern_name,
        "tool_name": "ExitPlanMode",
        "questions": questions,
    }
    # 把完整 plan 落到 md 文件，让 daemon 上传 + 发文件到飞书群。利用飞书对 md
    # 的渲染，主管点开即可阅读完整 plan，不污染聊天窗口。
    # planFilePath 若存在直接复用（agent 自己写的 plan md，文件名更友好）；
    # 否则把 plan 文本落到 /tmp 自己写一份。
    if plan_text:
        plan_path = plan_file if (plan_file and os.path.isfile(plan_file)) else None
        if not plan_path:
            ts = int(time.time())
            plan_path = f"/tmp/claude_plan_{intern_name}_{ts}.md"
            try:
                with open(plan_path, "w", encoding="utf-8") as f:
                    f.write(f"# Claude Plan ({intern_name})\n\n{plan_text}\n")
            except Exception as e:
                log_fn(f"ExitPlanMode: failed to write plan tmp file {plan_path}: {e}")
                plan_path = None
        if plan_path:
            ask_body["prelude_file_path"] = plan_path

    resp, err = _daemon_post("/api/question/ask", ask_body, log_fn=log_fn)
    if err or not resp or resp.get("error"):
        reason = err or (resp.get("error") if resp else "daemon 返回空")
        log_fn(f"ExitPlanMode: daemon ask failed, denying: {reason}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"飞书 daemon 不可达，ExitPlanMode 未转发主管：{reason}",
            }
        }

    answered = _poll_for_answer(intern_name, log_fn)

    if answered is None:
        _daemon_post("/api/question/timeout",
                     {"intern_name": intern_name, "hours": POLL_TIMEOUT // 3600},
                     log_fn=log_fn)
        log_fn("ExitPlanMode: timeout, denied")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "主管未回复 ExitPlanMode，已超时",
            }
        }

    answers = answered.get("answers", {})
    question_key = questions[0]["question"]
    # 项目规则 #6：不再 fallback 到 acceptEdits 隐式批准。answer 缺失直接 deny。
    if question_key not in answers:
        log_fn(f"ExitPlanMode: answer missing for question key, denying. answers={answers}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"主管回复无法解析（answers={answers}），请重新回复合法 mode "
                    f"(auto/acceptEdits/reviewEdits/ultraplan) 或自由文本反馈。"
                ),
            }
        }

    chosen = answers[question_key]
    # 批准 mode：必须是 4 个显式 mode 之一
    approve_modes = {"auto", "acceptEdits", "reviewEdits", "ultraplan"}
    if chosen in approve_modes:
        log_fn(f"ExitPlanMode: approved with mode={chosen}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {
                    "mode": chosen,
                },
            }
        }

    # 其他一切（自由文本反馈 / 历史遗留 keepPlanning）→ deny，把主管原文作为
    # permissionDecisionReason 让 Claude 看到反馈，plan mode 保持。
    log_fn(f"ExitPlanMode: feedback (not an approve mode), denying with reason. chosen={chosen!r}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"主管未批准 plan，回复内容如下：\n{chosen}\n\n"
                f"请根据主管反馈调整 plan 后重新调用 ExitPlanMode，或继续在 plan mode 中工作。"
            ),
        }
    }


def _poll_for_answer(intern_name, log_fn):
    """轮询 daemon 等待飞书回答。

    返回 answer dict 或 None（超时）。
    """
    deadline = time.time() + POLL_TIMEOUT
    poll_count = 0

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        poll_count += 1

        data, err = _daemon_get(f"/api/question/poll?intern_name={intern_name}", log_fn=log_fn)
        if data:
            status = data.get("status", "none")
            if status == "answered":
                log_fn("poll: answered via feishu")
                return data
            elif status == "none":
                log_fn("poll: question disappeared, aborting")
                return None

        if poll_count % 30 == 0:  # 每 60 秒 log 一次
            log_fn(f"poll: still waiting ({poll_count * POLL_INTERVAL}s elapsed)")

    return None
