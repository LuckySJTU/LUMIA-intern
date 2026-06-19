"""
FeishuModule — 飞书消息生命周期：CREATE → UPDATE → FINAL。

核心规则：
- 每个 UserPromptSubmit 创建一条新消息（CREATE）
- PostToolUse 累积内容到 buffer → UPDATE 同一条消息
- Stop APPROVE 时做 FINAL update（去掉 ⏳ + 追加 last_assistant_message + ✅ 完成）
- Stop BLOCK 时不做 FINAL（保留 ⏳，Claude 继续工作后 PostToolUse 继续 UPDATE）
- Overflow (>MAX_UPDATES) 时把旧消息 finalize，创建续消息

独立性：FeishuModule 直接读 transcript JSONL（通过 common.transcript），
不依赖 LogModule 的 assistant_texts。两个系统各自维护独立的 transcript_offset。
"""
import os
import json
import time
from common.utils import (
    get_intern_name, get_intern_type, load_feishu_credentials, get_chat_id, log_debug,
    is_machine_helper_state, machine_helper_chat_id_from_state,
)
from common.transcript import (
    extract_assistant_texts, extract_usage_stats, extract_session_usage,
    extract_codex_session_usage, extract_codex_goal_events,
    extract_latest_codex_goal_state,
)
from common.codex_quota import get_quota_with_message_cache, read_app_server_quota
from feishu_module.feishu_api import get_tenant_token, send_message, update_message, estimate_post_body_size
from feishu_module.timeline_composer import compose, SPINNER
from feishu_module.chat_config_reader import get_detail_mode

FEISHU_EDIT_LIMIT_CODE = 230072  # The message has reached the number of times it can be edited
FEISHU_BOT_NOT_IN_CHAT_CODE = 230002  # Bot/User can NOT be out of the chat
FEISHU_CONTENT_TOO_LONG_CODE = 230025  # The length of the message content reaches its limit

MAX_UPDATES_PER_MESSAGE = 17  # 飞书消息最多编辑 20 次，留 3 次给 overflow/FINAL/BLOCK
MAX_POST_BODY_BYTES = 28000  # 飞书 post 消息请求体上限 30KB，留 2KB 安全余量

# task258: when detail_mode=="summary" the in-progress message keeps only
# "stage signal" tools (work that represents a phase, not a low-level step).
# Anything not listed here is suppressed — buffer_lines stays compact, the
# supervisor only sees user prompt → assistant prose → stage events → final.
#
# Includes both Claude CLI tool names and VS Code/Copilot equivalents so the
# filter works for every intern type.
_SUMMARY_VISIBLE_TOOL_NAMES = frozenset({
    # Sub-agent dispatch — large chunk of work
    "execution_subagent", "runSubagent", "Agent",
    # AskUserQuestion — supervisor-facing interaction
    "AskUserQuestion",
    # Todo / task management — phase signals
    "manage_todo_list",
    "TodoRead", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskOutput", "TaskStop",
})


def _is_tool_visible_in_summary(tool_name):
    """task258: True if this tool should still appear in summary mode."""
    return tool_name in _SUMMARY_VISIBLE_TOOL_NAMES

# Claude model pricing: $/1M tokens. Cache: write = input×1.25, read = input×0.1
# Source of truth: Claude Code binary internal pricing table
# (/root/.local/share/claude/versions/<ver>: opus47/opus46/opus45 share _p={in:5,out:25,cw:6.25,cr:0.5})
_PRICING = {
    "claude-opus-4-7":           {"input": 5,  "output": 25, "cw": 6.25, "cr": 0.50, "max_ctx": 1_000_000},
    "claude-opus-4-6":           {"input": 5,  "output": 25, "cw": 6.25, "cr": 0.50, "max_ctx": 1_000_000},
    "claude-opus-4-5":           {"input": 5,  "output": 25, "cw": 6.25, "cr": 0.50, "max_ctx": 200_000},
    "claude-opus-4-1":           {"input": 15, "output": 75, "cw": 18.75,"cr": 1.50, "max_ctx": 200_000},
    "claude-opus-4-0":           {"input": 15, "output": 75, "cw": 18.75,"cr": 1.50, "max_ctx": 200_000},
    "claude-sonnet-4-6":         {"input": 3,  "output": 15, "cw": 3.75, "cr": 0.30, "max_ctx": 1_000_000},
    "claude-sonnet-4-5":         {"input": 3,  "output": 15, "cw": 3.75, "cr": 0.30, "max_ctx": 200_000},
    "claude-sonnet-4-0":         {"input": 3,  "output": 15, "cw": 3.75, "cr": 0.30, "max_ctx": 200_000},
    "claude-haiku-4-5":          {"input": 1,  "output": 5,  "cw": 1.25, "cr": 0.10, "max_ctx": 200_000},
}

# Codex pricing source: https://developers.openai.com/codex/pricing
# Units are credits per 1M tokens for token-based Codex credit accounting.
_CODEX_CREDIT_PRICING = {
    "gpt-5.5": {"input": 125, "output": 750, "cr": 12.50},
    "gpt-5.4": {"input": 62.50, "output": 375, "cr": 6.250},
    "gpt-5.4-mini": {"input": 18.75, "output": 113, "cr": 1.875},
    "gpt-5.3-codex": {"input": 43.75, "output": 350, "cr": 4.375},
    "gpt-5.2": {"input": 43.75, "output": 350, "cr": 4.375},
}


def _get_pricing(model_name):
    """Match model name to pricing entry. Tries exact match then prefix match."""
    if model_name in _PRICING:
        return _PRICING[model_name]
    # Strip snapshot suffix: "claude-haiku-4-5-20251001" → try "claude-haiku-4-5"
    for prefix, price in _PRICING.items():
        if model_name.startswith(prefix):
            return price
    return None


def _get_codex_credit_pricing(model_name):
    if model_name in _CODEX_CREDIT_PRICING:
        return _CODEX_CREDIT_PRICING[model_name]
    for prefix, price in sorted(_CODEX_CREDIT_PRICING.items(), key=lambda item: len(item[0]), reverse=True):
        if model_name.startswith(prefix):
            return price
    return None


def _calc_cost(by_model):
    """Calculate total cost from per-model token breakdown."""
    total = 0.0
    for model, tokens in by_model.items():
        price = _get_pricing(model)
        if not price:
            continue
        total += (tokens["input"] * price["input"] +
                  tokens["output"] * price["output"] +
                  tokens["cache_write"] * price["cw"] +
                  tokens["cache_read"] * price["cr"]) / 1_000_000
    return total


def _calc_codex_credits(by_model):
    total = 0.0
    for model, tokens in by_model.items():
        price = _get_codex_credit_pricing(model)
        if not price:
            continue
        total += (
            tokens["input"] * price["input"] +
            tokens["output"] * price["output"] +
            tokens["cache_read"] * price["cr"]
        ) / 1_000_000
    return total


def _format_credit_amount(value):
    if value == 0:
        return "0cr"
    if abs(value) < 0.01:
        return f"{value:.4f}cr"
    if abs(value) < 10:
        return f"{value:.2f}cr"
    if abs(value) < 100:
        return f"{value:.1f}cr"
    return f"{value:.0f}cr"


def _format_codex_credit_cost(usage_stats, turn_start_cost=0):
    total_cost = _calc_codex_credits(usage_stats.get("by_model", {}))
    if total_cost <= 0 and turn_start_cost <= 0:
        return ""
    turn_cost = max(0.0, total_cost - (turn_start_cost or 0))
    return f"cost +{_format_credit_amount(turn_cost)} / {_format_credit_amount(total_cost)}"


def _format_k_tokens(tokens):
    ctx_k = tokens / 1000
    if ctx_k >= 100:
        return f"{ctx_k:.0f}k"
    if ctx_k >= 10:
        return f"{ctx_k:.0f}k"
    return f"{ctx_k:.1f}k"


def _format_context_part(usage_stats, force=False):
    """Format the shared context segment for Claude billing and Codex quota footers."""
    ctx = usage_stats.get("context", 0)
    model = usage_stats.get("model", "")
    by_model = usage_stats.get("by_model", {})
    max_context_override = usage_stats.get("max_context", 0) or 0
    if not force and not ctx and not by_model and not max_context_override:
        return ""

    price = _get_pricing(model)
    ctx_str = _format_k_tokens(ctx)
    if max_context_override:
        max_str = f"{max_context_override/1000:.0f}k"
    elif price:
        max_str = f"{price['max_ctx']/1000:.0f}k"
    else:
        max_str = "?"
    return f"📊 ctx {ctx_str}/{max_str}"


def _format_billing_footer(usage_stats, turn_start_cost=0):
    """Format Claude context + USD cost into a one-line footer string."""
    context = _format_context_part(usage_stats)
    if not context:
        return ""

    by_model = usage_stats.get("by_model", {})
    total_cost = _calc_cost(by_model)
    turn_cost = total_cost - turn_start_cost
    footer = f"{context} · +${turn_cost:.2f} / ${total_cost:.2f}"
    unknown = sorted({m for m in by_model if m and not _get_pricing(m)})
    if unknown:
        footer += f" ⚠️ unknown model: {','.join(unknown)}"
    return footer


def _format_reset_delta(resets_at, now=None):
    if now is None:
        now = time.time()
    seconds = max(0, int(resets_at - now))
    if seconds >= 86400:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d{hours}h" if hours else f"{days}d"
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_window_label(window_minutes):
    if window_minutes % 10080 == 0:
        return f"{window_minutes // 10080 * 7}d"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def _format_percent(value):
    if abs(value - round(value)) < 0.05:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def _format_quota_window(window, now=None):
    if not window:
        return ""
    remaining = max(0.0, min(100.0, 100.0 - float(window["used_percent"])))
    return (
        f"{_format_window_label(int(window['window_minutes']))}余"
        f"{_format_percent(remaining)} reset {_format_reset_delta(window['resets_at'], now)}"
    )


def _format_credits(credits):
    if not credits:
        return ""
    if credits.get("unlimited"):
        return "credits ∞"
    if credits.get("balance") is not None:
        balance = credits["balance"]
        if isinstance(balance, float) and not balance.is_integer():
            return f"credits {balance:.2f}"
        return f"credits {balance}"
    if credits.get("has_credits"):
        return "credits available"
    return ""


def _format_codex_quota_footer(usage_stats, turn_start_cost=0, now=None):
    """Format Codex context + estimated credits + ChatGPT subscription quota."""
    quota = usage_stats.get("quota")
    context = _format_context_part(usage_stats)
    cost = _format_codex_credit_cost(usage_stats, turn_start_cost)
    if not context and not cost and quota is None:
        return ""

    parts = []
    has_prefix = bool(context)
    if context:
        parts.append(context)
    if cost:
        parts.append(cost if has_prefix else f"📊 {cost}")
        has_prefix = True
    if quota is None:
        return " · ".join(part for part in parts if part)

    quota_part = f"quota {(quota.get('plan_type') or '?').capitalize()}"
    parts.append(quota_part if has_prefix else f"📊 {quota_part}")
    primary = _format_quota_window(quota.get("primary"), now)
    secondary = _format_quota_window(quota.get("secondary"), now)
    if primary:
        parts.append(primary)
    if secondary:
        parts.append(secondary)
    credits = _format_credits(quota.get("credits"))
    if credits:
        parts.append(credits)
    reached = quota.get("rate_limit_reached_type")
    if reached:
        parts.append(f"⚠️ limit {reached}")
    return " · ".join(part for part in parts if part)


def _format_footer_for_intern_type(intern_type, usage_stats, turn_start_cost=0, now=None):
    if intern_type == "codex" or "quota" in usage_stats:
        return _format_codex_quota_footer(usage_stats, turn_start_cost=turn_start_cost, now=now)
    return _format_billing_footer(usage_stats, turn_start_cost)


def _format_stats_footer(usage_stats, turn_start_cost=0):
    """Backward-compatible Claude billing formatter."""
    return _format_billing_footer(usage_stats, turn_start_cost)


def _log_codex_quota_unavailable(intern_dir, hook_name, usage_stats):
    parse_error = usage_stats.get("quota_parse_error")
    if parse_error:
        log_debug(intern_dir, hook_name, f"codex transcript rate_limits invalid: {parse_error}")
    if usage_stats.get("quota") is None:
        if parse_error:
            log_debug(intern_dir, hook_name, f"codex quota unavailable: invalid rate_limits ({parse_error})")
        else:
            log_debug(intern_dir, hook_name, "codex quota unavailable: rate_limits missing/null")


def _attach_codex_quota_fallback(intern_dir, hook_name, usage_stats, fs):
    if usage_stats.get("quota") is not None:
        return usage_stats
    reader = lambda: read_app_server_quota(intern_dir=intern_dir)
    quota, error, cache_hit = get_quota_with_message_cache(fs, reader=reader)
    if quota is not None:
        usage_stats["quota"] = quota
        log_debug(intern_dir, hook_name, f"codex quota loaded from app-server cache_hit={cache_hit}")
    elif error:
        log_debug(intern_dir, hook_name, f"codex app-server quota unavailable cache_hit={cache_hit}: {error}")
    else:
        log_debug(intern_dir, hook_name, f"codex app-server quota unavailable cache_hit={cache_hit}: empty rateLimits")
    return usage_stats


def _get_token(state=None):
    try:
        app_id, app_secret = load_feishu_credentials()
        return get_tenant_token(app_id, app_secret, state=state)
    except Exception:
        return None


def _make_tool_summary(tool_name, tool_input):
    """Generate concise tool summary for feishu display."""
    # VS Code tool names
    if tool_name in ("run_in_terminal", "Bash"):
        cmd = tool_input.get("command", "")
        return f"Bash: `{cmd[:80]}`"
    if tool_name in ("read_file", "Read", "ReadFile", "copilot_readFile"):
        p = tool_input.get("filePath", tool_input.get("file_path", tool_input.get("path", "?")))
        return f"Read: `{p}`"
    if tool_name in ("create_file", "Write", "WriteFile"):
        p = tool_input.get("filePath", tool_input.get("file_path", tool_input.get("path", "?")))
        return f"Write: `{p}`"
    if tool_name in ("replace_string_in_file", "multi_replace_string_in_file", "Edit", "EditFile", "MultiEdit", "copilot_insertEdit"):
        p = tool_input.get("filePath", tool_input.get("file_path", tool_input.get("path", "")))
        if not p and tool_name == "multi_replace_string_in_file":
            # filePath is nested inside replacements[0].filePath
            repls = tool_input.get("replacements", [])
            if repls and isinstance(repls, list) and isinstance(repls[0], dict):
                p = repls[0].get("filePath", "")
        if not p:
            p = tool_input.get("explanation", "?")[:80]
        return f"Edit: `{p}`"
    if tool_name in ("grep_search", "file_search", "semantic_search", "Grep", "Glob"):
        q = tool_input.get("query", tool_input.get("pattern", "?"))
        return f"{tool_name}: `{q[:60]}`"
    if tool_name in ("list_dir",):
        p = tool_input.get("path", "?")
        return f"ListDir: `{p}`"
    if tool_name in ("execution_subagent", "runSubagent", "Agent"):
        desc = tool_input.get("description", tool_input.get("query", tool_input.get("prompt", "?")))
        if isinstance(desc, str) and len(desc) > 80:
            desc = desc[:80] + "…"
        return f"SubAgent: `{desc}`"
    if tool_name == "manage_todo_list":
        items = tool_input.get("todoList", [])
        total = len(items)
        for i, it in enumerate(items):
            if isinstance(it, dict) and it.get("status") == "in-progress":
                title = it.get("title", "")
                return f"📋 Todo[{i+1}/{total}] {title}"
        return f"📋 Todo[{total}]"
    # Claude CLI tools
    if tool_name == "TodoRead":
        return "📋 TodoRead"
    if tool_name == "TodoWrite":
        items = tool_input.get("todos", tool_input.get("todoList", []))
        if isinstance(items, list):
            total = len(items)
            for it in items:
                if isinstance(it, dict) and it.get("status") in ("in_progress", "in-progress"):
                    return f"📋 TodoWrite[{total}] {it.get('content', it.get('title', ''))[:60]}"
            return f"📋 TodoWrite[{total}]"
        return "📋 TodoWrite"
    if tool_name == "WebFetch":
        url = tool_input.get("url", "?")
        return f"WebFetch: `{url[:80]}`"
    if tool_name == "WebSearch":
        q = tool_input.get("query", "?")
        return f"WebSearch: `{q[:60]}`"
    if tool_name == "AskUserQuestion":
        qs = tool_input.get("questions", [])
        if qs and isinstance(qs, list) and isinstance(qs[0], dict):
            return f"AskUser: {qs[0].get('question', qs[0].get('header', '?'))[:60]}"
        return "AskUser"
    if tool_name == "MultiEdit":
        p = tool_input.get("file_path", "?")
        return f"MultiEdit: `{p}`"
    # Claude CLI Task tools
    if tool_name == "TaskCreate":
        subject = tool_input.get("subject", "")
        return f"📋 创建任务: {subject[:60]}" if subject else "📋 创建任务"
    if tool_name == "TaskUpdate":
        status = tool_input.get("status", "")
        task_id = tool_input.get("taskId", "")
        label = {"in_progress": "▶️ 开始", "completed": "✅ 完成"}.get(status, status)
        return f"📋 任务#{task_id} {label}" if task_id else f"📋 {label}"
    if tool_name == "TaskOutput":
        return "📄 任务输出"
    if tool_name == "TaskStop":
        return "⏹ 停止任务"
    return tool_name


def _goal_identity(goal):
    if not isinstance(goal, dict):
        return None
    return (
        str(goal.get("status") or ""),
        str(goal.get("objective") or ""),
        int(goal.get("created_at") or 0),
    )


def _shorten_goal_objective(objective, max_len=180):
    objective = (objective or "").replace("\n", " ").strip()
    if len(objective) <= max_len:
        return objective
    return objective[: max_len - 1] + "…"


def _format_goal_event(goal, previous_goal=None):
    status = str(goal.get("status") or "").lower()
    objective = _shorten_goal_objective(goal.get("objective", ""))
    if status == "cleared":
        return "🎯 Goal cleared"
    label = "active"
    if isinstance(previous_goal, dict):
        previous_objective = str(previous_goal.get("objective") or "")
        previous_created = int(previous_goal.get("created_at") or 0)
        if (
            previous_goal.get("status") == "active"
            and status == "active"
            and (previous_objective != goal.get("objective", "") or previous_created != int(goal.get("created_at") or 0))
        ):
            label = "replaced"
    if objective:
        return f"🎯 Goal {label}: {objective}"
    return f"🎯 Goal {label}"


def _append_codex_goal_events(buffer_lines, fs, transcript_path):
    events, new_offset = extract_codex_goal_events(
        transcript_path, fs.get("goal_offset", 0))
    if new_offset:
        fs["goal_offset"] = new_offset
    previous_goal = fs.get("goal_state")
    for event in events:
        if _goal_identity(event) == _goal_identity(previous_goal):
            previous_goal = event
            fs["goal_state"] = event
            continue
        line = _format_goal_event(event, previous_goal)
        if line:
            buffer_lines.append(line)
        previous_goal = event
        fs["goal_state"] = event
    return buffer_lines


class FeishuModule:

    def _resolve_chat_id(self, state, intern_dir: str, intern_name: str) -> str:
        """Resolve the Feishu chat for normal interns or projectless helpers."""
        helper_chat_id = ""
        if is_machine_helper_state(state, intern_name):
            helper_chat_id = machine_helper_chat_id_from_state(state)
            if not helper_chat_id:
                log_debug(intern_dir, "FeishuModule.resolve_chat",
                          f"machine helper {intern_name} missing dedicated chat_id; skip normal group creation")
                return ""
        if helper_chat_id:
            return helper_chat_id
        return self._ensure_groupchat(intern_dir, intern_name)

    def _ensure_groupchat(self, intern_dir: str, intern_name: str) -> str:
        """确保 intern 有飞书群聊，没有则通过 daemon 创建。返回 chat_id 或空串。

        快速路径：先读本地 registry 文件，有 chatId 直接返回（不走 HTTP）。
        """
        # 快速路径：本地 registry 已有 chatId
        local_id = get_chat_id(intern_name)
        if local_id:
            return local_id

        import urllib.request
        try:
            from _daemon_addr import get_daemon_http_url
            data = json.dumps({"intern_name": intern_name}).encode()
            req = urllib.request.Request(
                f"{get_daemon_http_url()}/api/group/create",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())
            chat_id = result.get("chat_id", "")
            if chat_id:
                log_debug(intern_dir, "FeishuModule.ensure_groupchat",
                          f"ensured group for {intern_name}: {chat_id}")
            return chat_id
        except Exception as e:
            log_debug(intern_dir, "FeishuModule.ensure_groupchat",
                      f"failed: {e}")
            return ""

    def _invalidate_chat(self, intern_name: str):
        """删除 intern 的 registry 文件，使下次 ensure_groupchat 重新创建群。"""
        import os
        registry_dir = os.path.join(os.environ.get("WORK_AGENTS_ROOT", "/work-agents"),
                                     ".feishu_registry")
        registry_file = os.path.join(registry_dir, f"{intern_name}.json")
        try:
            if os.path.exists(registry_file):
                os.remove(registry_file)
        except Exception:
            pass

    def _start_codex_goal_continuation(self, state, hook_input, fs):
        """Create a new Feishu message for Codex /goal continuations.

        Codex goal mode can keep running tools after a Stop event without a new
        UserPromptSubmit. The previous Feishu turn is already finalized then, so
        PostToolUse needs to open an implicit system-triggered message.
        """
        intern_dir = state["_intern_dir"]
        intern_name = get_intern_name(intern_dir)
        if get_intern_type(intern_name) != "codex":
            return fs

        chat_id = fs.get("chat_id") or self._resolve_chat_id(state, intern_dir, intern_name)
        if not chat_id:
            log_debug(intern_dir, "FeishuModule.goal",
                      f"no chat_id for {intern_name} (ensure failed)")
            return fs

        token = _get_token(state)
        if not token:
            log_debug(intern_dir, "FeishuModule.goal", "no token")
            return fs

        header = "🤖 Codex goal 自动继续\n---"
        transcript_path = hook_input.get("transcript_path", "")
        initial_lines = [header]
        latest_goal = extract_latest_codex_goal_state(transcript_path)
        if latest_goal:
            initial_lines.append(_format_goal_event(latest_goal))
        initial_text = compose(initial_lines, spinner=True)
        msg_id, err = send_message(token, chat_id, initial_text)
        if err and str(FEISHU_BOT_NOT_IN_CHAT_CODE) in str(err):
            log_debug(intern_dir, "FeishuModule.goal",
                      f"bot not in chat {chat_id}, re-creating group")
            self._invalidate_chat(intern_name)
            chat_id = self._resolve_chat_id(state, intern_dir, intern_name)
            if chat_id:
                msg_id, err = send_message(token, chat_id, initial_text)

        if not msg_id:
            log_debug(intern_dir, "FeishuModule.goal",
                      f"CREATE failed: {err}")
            return fs

        transcript_offset = fs.get("transcript_offset", 0)
        if not transcript_offset and transcript_path and os.path.exists(transcript_path):
            try:
                transcript_offset = os.path.getsize(transcript_path)
            except OSError:
                transcript_offset = 0

        new_fs = {
            "message_id": msg_id,
            "chat_id": chat_id,
            "buffer_lines": initial_lines,
            "update_count": 0,
            "transcript_offset": transcript_offset,
            "goal_offset": transcript_offset,
            "usage_offset": 0,
        }
        if latest_goal:
            new_fs["goal_state"] = latest_goal
        if fs.get("_token_cache"):
            new_fs["_token_cache"] = fs["_token_cache"]
        if transcript_path:
            usage = extract_codex_session_usage(transcript_path)
            usage = _attach_codex_quota_fallback(intern_dir, "FeishuModule.goal", usage, new_fs)
            new_fs["usage_stats"] = usage
            new_fs["turn_start_cost"] = _calc_codex_credits(usage.get("by_model", {}))
            _log_codex_quota_unavailable(intern_dir, "FeishuModule.goal", usage)

        state["feishu"] = new_fs
        log_debug(intern_dir, "FeishuModule.goal",
                  f"CREATED {msg_id} transcript_offset={transcript_offset}")
        return new_fs

    # ── UserPromptSubmit ──────────────────────────────────────
    def on_user_prompt(self, state, hook_input):
        """CREATE 飞书消息。

        如果上一轮有未关闭的消息（Stop BLOCK 后 Claude 继续 → 下一轮 UserPrompt），
        先 FINAL 上一条消息再 CREATE 新消息。

        特殊处理：<task-notification>（Claude CLI 背景任务完成通知）
        不创建新消息，而是 append 到当前消息中，避免消息混乱。
        """
        intern_dir = state["_intern_dir"]
        intern_name = get_intern_name(intern_dir)

        # always ensure 群聊存在（幂等：已有群则直接返回 chat_id）
        chat_id = self._resolve_chat_id(state, intern_dir, intern_name)
        if not chat_id:
            log_debug(intern_dir, "FeishuModule.user_prompt",
                      f"no chat_id for {intern_name} (ensure failed)")
            return

        prompt = hook_input.get("prompt", "").strip()

        # task-notification: append to existing message instead of creating new one
        if state.get("_is_task_notification"):
            fs = state.get("feishu", {})
            msg_id = fs.get("message_id")
            if msg_id and not fs.get("finalized"):
                # Extract summary from <task-notification> XML
                import re
                summary_m = re.search(r"<summary>(.*?)</summary>", prompt, re.DOTALL)
                summary = summary_m.group(1).strip() if summary_m else "task completed"
                note = f"📌 {summary}"
                buffer_lines = fs.get("buffer_lines", [])
                buffer_lines.append(note)
                fs["buffer_lines"] = buffer_lines
                token = _get_token(state)
                if token:
                    text = compose(buffer_lines, spinner=True)
                    ok, err = update_message(token, msg_id, text)
                    count = fs.get("update_count", 0)
                    fs["update_count"] = count + 1
                    log_debug(intern_dir, "FeishuModule.user_prompt",
                              f"task-notification appended to {msg_id} ok={ok}" + (f" ERR={err}" if err else ""))
                state["feishu"] = fs
                return
            # No existing message or finalized → fall through to create new one
            log_debug(intern_dir, "FeishuModule.user_prompt",
                      "task-notification but no active message, creating new one")

        # Truncate long prompts only for background turns (e.g. terminal notifications with verbose output)
        display_prompt = prompt
        is_background = state.get("is_background_turn", False)
        if is_background and len(display_prompt) > 100:
            display_prompt = display_prompt[:100] + "…"
        # Header: 🤖 for non-user-triggered turns so users can distinguish
        # system-initiated conversations from their own prompts at a glance.
        # ⚠️ prefix for stale background notifications (bg task from a
        # /clear-ed session — see user_prompt_hook Bug-15).
        if is_background:
            bg_type = state.get("background_type", "")
            if bg_type == "task-notification":
                label = "🤖 系统通知"
            elif bg_type == "terminal-notification":
                label = "🤖 终端通知"
            else:
                label = "🤖"
            if state.get("background_stale"):
                label = f"⚠️ {label}（来自已清除的 session）"
            header = f"{label}: {display_prompt}\n---"
        else:
            header = f"🧑 用户: {display_prompt}\n---"

        token = _get_token(state)
        if not token:
            log_debug(intern_dir, "FeishuModule.user_prompt", "no token")
            return

        # Finalize previous message if exists (from previous turn)
        old_msg = state.get("feishu", {}).get("message_id")
        if old_msg:
            old_lines = state.get("feishu", {}).get("buffer_lines", [])
            if old_lines:
                if not any(l.strip().startswith("✅") for l in old_lines[-2:]):
                    old_lines.append("\n✅ 完成")
                old_usage = state.get("feishu", {}).get("usage_stats")
                old_footer = ""
                if old_usage:
                    old_footer = _format_footer_for_intern_type(
                        get_intern_type(intern_name),
                        old_usage,
                        state.get("feishu", {}).get("turn_start_cost", 0),
                    )
                final_text = compose(old_lines, spinner=False, footer=old_footer)
                ok, err = update_message(token, old_msg, final_text)
                log_debug(intern_dir, "FeishuModule.user_prompt",
                          f"finalized previous msg {old_msg} ok={ok}" + (f" ERR={err}" if err else ""))

        # Reset feishu state for new turn: clear stale finalized/message_id/buffer.
        # Preserve _token_cache to avoid redundant token fetches.
        _token_cache = state.get("feishu", {}).get("_token_cache")
        state["feishu"] = {}
        if _token_cache:
            state["feishu"]["_token_cache"] = _token_cache

        initial_text = compose([header], spinner=True)
        msg_id, err = send_message(token, chat_id, initial_text)

        # 230002: bot 不在群中 → 清除旧 chat_id，重新建群
        if err and str(FEISHU_BOT_NOT_IN_CHAT_CODE) in str(err):
            log_debug(intern_dir, "FeishuModule.user_prompt",
                      f"bot not in chat {chat_id}, re-creating group")
            # 删除旧 registry 文件让 ensure_groupchat 重新创建
            self._invalidate_chat(intern_name)
            chat_id = self._resolve_chat_id(state, intern_dir, intern_name)
            if chat_id:
                msg_id, err = send_message(token, chat_id, initial_text)

        if err:
            log_debug(intern_dir, "FeishuModule.user_prompt", f"CREATE failed: {err}")
            # state["feishu"] is clean — no message_id, no finalized.
            # This turn will be Feishu-silent: on_post_tool and on_stop skip.
        else:
            # Initialize feishu state for this turn.
            # transcript_offset: start from current file position so only new entries
            # are read (entries before this point belong to previous turns).
            transcript_path = hook_input.get("transcript_path", "")
            t_offset = 0
            if transcript_path and os.path.exists(transcript_path):
                try:
                    t_offset = os.path.getsize(transcript_path)
                except OSError:
                    pass
            state["feishu"] = {
                "message_id": msg_id,
                "chat_id": chat_id,
                "buffer_lines": [header],
                "update_count": 0,
                "transcript_offset": t_offset,
                "goal_offset": t_offset,
            }
            if _token_cache:
                state["feishu"]["_token_cache"] = _token_cache

            # Initialize usage stats (context + cost/quota display)
            intern_type_now = get_intern_type(intern_name)
            if intern_type_now == "claude" and transcript_path:
                session_usage = extract_session_usage(transcript_path)
                state["feishu"]["usage_stats"] = session_usage
                state["feishu"]["usage_offset"] = t_offset
                # Record session-level cost at turn start for per-turn delta
                state["feishu"]["turn_start_cost"] = _calc_cost(session_usage.get("by_model", {}))
                footer = _format_footer_for_intern_type(
                    intern_type_now,
                    session_usage,
                    state["feishu"]["turn_start_cost"],
                )
                if footer:
                    log_debug(intern_dir, "FeishuModule.user_prompt",
                              f"usage init: {footer}")
            elif intern_type_now == "codex" and transcript_path:
                # Codex emits cumulative token_count events — re-read whole file each time.
                session_usage = extract_codex_session_usage(transcript_path)
                session_usage = _attach_codex_quota_fallback(
                    intern_dir, "FeishuModule.user_prompt", session_usage, state["feishu"])
                state["feishu"]["usage_stats"] = session_usage
                state["feishu"]["usage_offset"] = 0  # Codex re-reads full file every time
                state["feishu"]["turn_start_cost"] = _calc_codex_credits(session_usage.get("by_model", {}))
                footer = _format_footer_for_intern_type(
                    intern_type_now,
                    session_usage,
                    state["feishu"]["turn_start_cost"],
                )
                if footer:
                    log_debug(intern_dir, "FeishuModule.user_prompt",
                              f"codex usage init: {footer}")
                _log_codex_quota_unavailable(intern_dir, "FeishuModule.user_prompt", session_usage)

            log_debug(intern_dir, "FeishuModule.user_prompt",
                      f"CREATED {msg_id} transcript_offset={t_offset}")

    # ── PostToolUse ───────────────────────────────────────────
    def on_post_tool(self, state, hook_input):
        """UPDATE 飞书消息: 累积 assistant 文本 + 工具摘要。

        FeishuModule 独立读 transcript（不依赖 LogModule）。
        SubAgent 隔离由 post_tool_hook 控制（depth > 0 时不调用本模块）。
        """
        intern_dir = state["_intern_dir"]
        fs = state.get("feishu", {})

        # finalized flag: Stop APPROVE 已完成本轮，忽略后续 PostToolUse
        if fs.get("finalized"):
            fs = self._start_codex_goal_continuation(state, hook_input, fs)
            if fs.get("finalized"):
                log_debug(intern_dir, "FeishuModule.post_tool",
                          "SKIP: turn already finalized")
                return

        transcript_path = hook_input.get("transcript_path", "")
        msg_id = fs.get("message_id")
        if not msg_id:
            fs = self._start_codex_goal_continuation(state, hook_input, fs)
            msg_id = fs.get("message_id")
        if not msg_id:
            log_debug(intern_dir, "FeishuModule.post_tool",
                      "SKIP: no message_id")
            return

        buffer_lines = fs.get("buffer_lines", [])
        prev_len = len(buffer_lines)  # 新增内容前的分割点，用于 content overflow

        # 独立读 transcript JSONL，提取新 assistant 文本
        transcript_path = hook_input.get("transcript_path", "")
        feishu_offset = fs.get("transcript_offset", 0)
        tool_name = hook_input.get("tool_name", "")

        # SubAgent tool completion: read pre-SubAgent text only, skip internals
        SUBAGENT_TOOLS = {"execution_subagent", "runSubagent", "Agent"}
        subagent_start_off = None
        if tool_name in SUBAGENT_TOOLS:
            # Bug-11: key by tool_use_id so concurrent Agent spawns each get
            # their own offset. Agent-tool's tool_use_id matches the tuid stored
            # by pre_tool_hook and subagent_start_hook. Fall back to legacy
            # single-slot for states saved before the dict migration.
            tuid = hook_input.get("tool_use_id", "")
            sts = fs.get("subagent_transcripts", {})
            if tuid and tuid in sts:
                subagent_start_off = sts.pop(tuid)
            else:
                subagent_start_off = fs.pop("subagent_transcript_start", None)
            fs["subagent_transcripts"] = sts

        if tool_name in SUBAGENT_TOOLS and subagent_start_off is not None:
            # Read only the portion BEFORE SubAgent started (pre-SubAgent assistant text)
            pre_texts, _, preview = extract_assistant_texts(
                transcript_path, feishu_offset, end_offset=subagent_start_off)
            for t in pre_texts:
                if t.strip():
                    buffer_lines.append(t)
            # Advance offset past all SubAgent internal content
            if transcript_path and os.path.exists(transcript_path):
                try:
                    fs["transcript_offset"] = os.path.getsize(transcript_path)
                except OSError:
                    fs["transcript_offset"] = subagent_start_off
            if preview:
                log_debug(intern_dir, "FeishuModule.post_tool",
                          f"SubAgent pre-texts={len(pre_texts)} offset->{fs.get('transcript_offset')}")
        else:
            new_texts, new_offset, preview = extract_assistant_texts(
                transcript_path, feishu_offset)
            if preview:
                log_debug(intern_dir, "FeishuModule.post_tool",
                          f"transcript new_len={new_offset - feishu_offset} texts={len(new_texts)}")
            fs["transcript_offset"] = new_offset
            for t in new_texts:
                if t.strip():
                    buffer_lines.append(t)

        # 工具摘要（✅ = 完成的工具，自含状态标记）
        # task258: in summary mode, suppress noisy tool calls (Bash / Read /
        # Write / Edit / Grep / list_dir / Web*). Stage-signal tools
        # (SubAgent / AskUser / Todo* / Task*) and assistant prose stay.
        # detail_mode is read per-PostToolUse so a /config flip mid-turn
        # takes effect on the next event without restarting the hook.
        chat_id = fs.get("chat_id")
        detail_mode = get_detail_mode(chat_id)
        # task283: log on transitions so supervisors can grep `detail_mode=`
        # in llm_intern_logs/<intern>/hooks.log to confirm the daemon-local
        # value is what they set. fs persists across hook invocations so we
        # only log when the value actually changes (first call after restart
        # or a /detail_mode flip mid-session), not on every PostToolUse.
        last_logged = fs.get("last_logged_detail_mode")
        if last_logged != detail_mode:
            log_debug(intern_dir, "FeishuModule.post_tool",
                      f"detail_mode={detail_mode} (was {last_logged or '<initial>'})")
            fs["last_logged_detail_mode"] = detail_mode
        tool_input_data = hook_input.get("tool_input", {})
        if detail_mode == "summary" and not _is_tool_visible_in_summary(tool_name):
            log_debug(intern_dir, "FeishuModule.post_tool",
                      f"summary: suppress tool={tool_name}")
        else:
            summary = _make_tool_summary(tool_name, tool_input_data)
            buffer_lines.append(f"✅ {summary}")

        intern_name_for_goal = state.get("intern_name") or get_intern_name(intern_dir)
        if get_intern_type(intern_name_for_goal) == "codex":
            buffer_lines = _append_codex_goal_events(buffer_lines, fs, transcript_path)

        fs["buffer_lines"] = buffer_lines
        count = fs.get("update_count", 0)

        # Update usage stats. Claude: incremental delta from offset.
        # Codex: token_count events are cumulative — overwrite from latest scan.
        usage_stats = fs.get("usage_stats")
        footer = ""
        if usage_stats is not None:
            intern_name_for_usage = state.get("intern_name") or get_intern_name(intern_dir)
            intern_type_for_usage = get_intern_type(intern_name_for_usage)
            if intern_type_for_usage == "codex":
                fresh = extract_codex_session_usage(transcript_path)
                fresh = _attach_codex_quota_fallback(
                    intern_dir, "FeishuModule.post_tool", fresh, fs)
                if fresh.get("token_count_seen") or fresh.get("model") or fresh.get("context") or fresh.get("quota") is not None:
                    fs["usage_stats"] = fresh
                    usage_stats = fresh
                _log_codex_quota_unavailable(intern_dir, "FeishuModule.post_tool", usage_stats)
            else:
                usage_offset = fs.get("usage_offset", 0)
                new_stats, new_usage_offset = extract_usage_stats(transcript_path, usage_offset)
                fs["usage_offset"] = new_usage_offset
                for k in ("input", "output", "cache_write", "cache_read"):
                    usage_stats[k] += new_stats[k]
                if new_stats["context"]:
                    usage_stats["context"] = new_stats["context"]
                if new_stats["model"]:
                    usage_stats["model"] = new_stats["model"]
                    m = new_stats["model"]
                    if m not in usage_stats.get("by_model", {}):
                        usage_stats["by_model"][m] = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
                    for k in ("input", "output", "cache_write", "cache_read"):
                        usage_stats["by_model"][m][k] += new_stats[k]
                fs["usage_stats"] = usage_stats
            footer = _format_footer_for_intern_type(
                intern_type_for_usage,
                usage_stats,
                fs.get("turn_start_cost", 0),
            )

        token = _get_token(state)
        if not token:
            return

        if count >= MAX_UPDATES_PER_MESSAGE:
            # 编辑次数溢出：finalize 旧消息，创建续消息
            overflow_text = compose(buffer_lines, spinner=False) + "\n\n(续下条...)"
            ok, err = update_message(token, msg_id, overflow_text)
            log_debug(intern_dir, "FeishuModule.post_tool",
                      f"OVERFLOW finalize msg={msg_id} ok={ok}" + (f" ERR={err}" if err else ""))

            # 续消息只放单行占位，不带历史 buffer，避免长段被反复 carry（task181）
            continuation_lines = ["（接上条消息）"]
            continuation_text = compose(continuation_lines, spinner=True, footer=footer)
            new_msg_id, err = send_message(token, chat_id, continuation_text)
            if new_msg_id:
                fs["message_id"] = new_msg_id
                fs.pop("codex_quota_cache", None)
                fs["buffer_lines"] = continuation_lines
                fs["update_count"] = 0
                log_debug(intern_dir, "FeishuModule.post_tool",
                          f"OVERFLOW new msg={new_msg_id}")
            else:
                log_debug(intern_dir, "FeishuModule.post_tool",
                          f"OVERFLOW CREATE failed: {err}")
        else:
            text = compose(buffer_lines, spinner=True, footer=footer)
            body_size = estimate_post_body_size(text)

            if body_size > MAX_POST_BODY_BYTES:
                # 内容长度溢出（主动检测）：用旧内容 finalize，新内容放续消息
                log_debug(intern_dir, "FeishuModule.post_tool",
                          f"CONTENT OVERFLOW body={body_size}B > {MAX_POST_BODY_BYTES}B")
                old_lines = buffer_lines[:prev_len]
                if old_lines:
                    overflow_text = compose(old_lines, spinner=False) + "\n\n(续下条...)"
                    ok, err = update_message(token, msg_id, overflow_text)
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"CONTENT OVERFLOW finalize ok={ok}" + (f" ERR={err}" if err else ""))

                # 续消息只放单行占位 + 本次新增内容，不带 old_lines（task181）
                new_lines = buffer_lines[prev_len:]
                continuation_lines = ["（接上条消息）"] + new_lines
                continuation_text = compose(continuation_lines, spinner=True, footer=footer)
                new_msg_id, err2 = send_message(token, chat_id, continuation_text)
                if new_msg_id:
                    fs["message_id"] = new_msg_id
                    fs.pop("codex_quota_cache", None)
                    fs["buffer_lines"] = continuation_lines
                    fs["update_count"] = 0
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"CONTENT OVERFLOW new msg={new_msg_id}")
                else:
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"CONTENT OVERFLOW CREATE failed: {err2}")
            else:
                ok, err = update_message(token, msg_id, text)
                fs["update_count"] = count + 1

                # 飞书编辑上限：立即触发 overflow
                if not ok and err and str(FEISHU_EDIT_LIMIT_CODE) in str(err):
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"EDIT LIMIT reached at #{count+1}, triggering overflow")
                    # 续消息只放单行占位，不带历史 buffer（task181）
                    continuation_lines = ["（接上条消息）"]
                    continuation_text = compose(continuation_lines, spinner=True, footer=footer)
                    new_msg_id, err2 = send_message(token, chat_id, continuation_text)
                    if new_msg_id:
                        fs["message_id"] = new_msg_id
                        fs.pop("codex_quota_cache", None)
                        fs["buffer_lines"] = continuation_lines
                        fs["update_count"] = 0
                        log_debug(intern_dir, "FeishuModule.post_tool",
                                  f"EDIT LIMIT → new msg={new_msg_id}")
                    else:
                        log_debug(intern_dir, "FeishuModule.post_tool",
                                  f"EDIT LIMIT → CREATE failed: {err2}")
                # 飞书内容过长（主动检测的 fallback）
                elif not ok and err and str(FEISHU_CONTENT_TOO_LONG_CODE) in str(err):
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"CONTENT TOO LONG (reactive) at #{count+1}")
                    # 续消息只放单行占位 + 本次新增内容（task181）
                    new_lines = buffer_lines[prev_len:]
                    continuation_lines = ["（接上条消息）"] + new_lines
                    continuation_text = compose(continuation_lines, spinner=True, footer=footer)
                    new_msg_id, err2 = send_message(token, chat_id, continuation_text)
                    if new_msg_id:
                        fs["message_id"] = new_msg_id
                        fs.pop("codex_quota_cache", None)
                        fs["buffer_lines"] = continuation_lines
                        fs["update_count"] = 0
                        log_debug(intern_dir, "FeishuModule.post_tool",
                                  f"CONTENT TOO LONG → new msg={new_msg_id}")
                    else:
                        log_debug(intern_dir, "FeishuModule.post_tool",
                                  f"CONTENT TOO LONG → CREATE failed: {err2}")
                else:
                    log_debug(intern_dir, "FeishuModule.post_tool",
                              f"UPDATE #{count+1} buf={len(buffer_lines)} body={body_size}B ok={ok}" + (f" ERR={err}" if err else ""))

        state["feishu"] = fs

    # ── Stop ──────────────────────────────────────────────────
    def on_stop(self, state, hook_input):
        """FINAL update 飞书消息。

        Stop 阶段不再从 transcript 增量补读 final assistant reply。
        当前 Stop 回复统一读 `state["_stop_last_message"]`（stop_hook.py 已写入的权威来源）：
          - Claude CLI: 来自 stdin 的原生 last_assistant_message 字段（stdin 早于 transcript 写入）
          - Copilot / Codex: 来自 transcript 回读（stdin 不带该字段）

        流程固定为："append 当前 Stop 回复 → 推进 transcript_offset 到 EOF
        → 按 issues 分支进 BLOCK 或 FINAL"，顺序天然正确，不依赖去重兜底。
        - APPROVE: buffer + stop_message + ✅ 完成 → 去 ⏳
        - BLOCK:   buffer + stop_message + ❗ 格式检查未通过 → 保留 ⏳，等 Claude 重试

        Compact 对飞书透明——compact 不触发 Stop，message_id 保持在 state 中，
        PostToolUse 继续 UPDATE 同一条消息。
        """
        intern_dir = state["_intern_dir"]
        fs = state.get("feishu", {})
        transcript_path = hook_input.get("transcript_path", "")
        intern_name_for_goal = state.get("intern_name") or get_intern_name(intern_dir)
        is_codex = get_intern_type(intern_name_for_goal) == "codex"

        # Already finalized by a previous Stop APPROVE in this turn
        # (stop hook retry can fire multiple Stop events for the same turn)
        if fs.get("finalized"):
            if is_codex:
                latest_goal = extract_latest_codex_goal_state(transcript_path)
                if latest_goal and _goal_identity(latest_goal) != _goal_identity(fs.get("goal_state")):
                    fs = self._start_codex_goal_continuation(state, hook_input, fs)
                else:
                    log_debug(intern_dir, "FeishuModule.stop",
                              "SKIP: turn already finalized")
                    return
            else:
                log_debug(intern_dir, "FeishuModule.stop",
                          "SKIP: turn already finalized")
                return

        if is_codex and not fs.get("message_id"):
            latest_goal = extract_latest_codex_goal_state(transcript_path)
            if latest_goal:
                fs = self._start_codex_goal_continuation(state, hook_input, fs)

        if fs.get("finalized"):
            log_debug(intern_dir, "FeishuModule.stop",
                      "SKIP: turn already finalized")
            return

        msg_id = fs.get("message_id")
        if not msg_id:
            return

        buffer_lines = fs.get("buffer_lines", [])

        # 分割点：在 append stop_message 之前记录，FINAL overflow 按此切分，
        # 使续消息保留 "stop reply + ✅ 完成" 的既有 UX 语义。
        stop_content_start = len(buffer_lines)

        # 权威 Stop 回复来源：stop_hook.py 已提取并写入 _stop_last_message。
        # 不在 Stop 阶段再读 transcript，避免 Claude CLI 的 stdin-ahead-of-transcript race。
        stop_message = (state.get("_stop_last_message", "")
                        or hook_input.get("last_assistant_message", ""))
        stop_message = stop_message.strip() if stop_message else ""
        if stop_message and (not buffer_lines or buffer_lines[-1] != stop_message):
            buffer_lines.append(stop_message)

        if is_codex:
            buffer_lines = _append_codex_goal_events(buffer_lines, fs, transcript_path)

        # 把 transcript_offset 推到 EOF：Stop 回复已由 _stop_last_message 消费，
        # 避免下一次 PostToolUse / Stop 再把同一段内容从 transcript 补读进来。
        if transcript_path and os.path.exists(transcript_path):
            try:
                fs["transcript_offset"] = os.path.getsize(transcript_path)
            except OSError:
                pass
        fs["buffer_lines"] = buffer_lines

        # Update usage stats. Claude: incremental delta. Codex: full re-scan (cumulative).
        usage_stats = fs.get("usage_stats")
        footer = ""
        if usage_stats is not None:
            intern_name_for_usage = state.get("intern_name") or get_intern_name(intern_dir)
            intern_type_for_usage = get_intern_type(intern_name_for_usage)
            if intern_type_for_usage == "codex":
                fresh = extract_codex_session_usage(transcript_path)
                fresh = _attach_codex_quota_fallback(
                    intern_dir, "FeishuModule.stop", fresh, fs)
                if fresh.get("token_count_seen") or fresh.get("model") or fresh.get("context") or fresh.get("quota") is not None:
                    fs["usage_stats"] = fresh
                    usage_stats = fresh
                _log_codex_quota_unavailable(intern_dir, "FeishuModule.stop", usage_stats)
            else:
                usage_offset = fs.get("usage_offset", 0)
                new_stats, new_usage_offset = extract_usage_stats(transcript_path, usage_offset)
                fs["usage_offset"] = new_usage_offset
                for k in ("input", "output", "cache_write", "cache_read"):
                    usage_stats[k] += new_stats[k]
                if new_stats["context"]:
                    usage_stats["context"] = new_stats["context"]
                if new_stats["model"]:
                    usage_stats["model"] = new_stats["model"]
                    m = new_stats["model"]
                    if m not in usage_stats.get("by_model", {}):
                        usage_stats["by_model"][m] = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
                    for k in ("input", "output", "cache_write", "cache_read"):
                        usage_stats["by_model"][m][k] += new_stats[k]
                fs["usage_stats"] = usage_stats
            footer = _format_footer_for_intern_type(
                intern_type_for_usage,
                usage_stats,
                fs.get("turn_start_cost", 0),
            )

        issues = state.get("validation", {}).get("issues", [])
        if issues:
            # BLOCK 路径 — stop_message 已 append，这里再 append BLOCK reason，保留 ⏳
            reason = "---\n❗ 格式检查未通过：" + "、".join(issues) + "\n---"
            buffer_lines.append(reason)
            fs["buffer_lines"] = buffer_lines
            token = _get_token(state)
            if token:
                text = compose(buffer_lines, spinner=True, footer=footer)
                ok, err = update_message(token, msg_id, text)
                log_debug(intern_dir, "FeishuModule.stop",
                          f"BLOCK update ok={ok} buf={len(buffer_lines)}" + (f" ERR={err}" if err else ""))
            state["feishu"] = fs
            return

        # FINAL 路径
        buffer_lines.append("\n✅ 完成")
        fs["buffer_lines"] = buffer_lines
        final_text = compose(buffer_lines, spinner=False, footer=footer)

        token = _get_token(state)
        if token:
            body_size = estimate_post_body_size(final_text)
            if body_size > MAX_POST_BODY_BYTES:
                # 内容长度溢出：用旧 buffer finalize，stop_message+✅ 放续消息
                log_debug(intern_dir, "FeishuModule.stop",
                          f"FINAL CONTENT OVERFLOW body={body_size}B > {MAX_POST_BODY_BYTES}B")
                old_lines = buffer_lines[:stop_content_start]
                if old_lines:
                    overflow_text = compose(old_lines, spinner=False) + "\n\n(续下条...)"
                    ok, err = update_message(token, msg_id, overflow_text)
                    log_debug(intern_dir, "FeishuModule.stop",
                              f"CONTENT OVERFLOW finalize ok={ok}" + (f" ERR={err}" if err else ""))

                chat_id = fs.get("chat_id")
                # 续消息只放单行占位 + stop_message + ✅，不带 old_lines（task181）
                new_lines = buffer_lines[stop_content_start:]
                completion_lines = ["（接上条消息）"] + new_lines
                completion_text = compose(completion_lines, spinner=False, footer=footer)
                new_msg_id, err2 = send_message(token, chat_id, completion_text)
                log_debug(intern_dir, "FeishuModule.stop",
                          f"CONTENT OVERFLOW completion msg={'ok' if new_msg_id else 'FAIL'}" + (f" ERR={err2}" if err2 else ""))
            else:
                ok, err = update_message(token, msg_id, final_text)
                if not ok and err and str(FEISHU_CONTENT_TOO_LONG_CODE) in str(err):
                    # Reactive: content too long — create continuation with completion
                    log_debug(intern_dir, "FeishuModule.stop",
                              f"FINAL CONTENT TOO LONG (reactive)")
                    chat_id = fs.get("chat_id")
                    # 续消息只放单行占位 + stop_message + ✅（task181）
                    new_lines = buffer_lines[stop_content_start:]
                    completion_lines = ["（接上条消息）"] + new_lines
                    completion_text = compose(completion_lines, spinner=False, footer=footer)
                    new_msg_id, err2 = send_message(token, chat_id, completion_text)
                    log_debug(intern_dir, "FeishuModule.stop",
                              f"CONTENT TOO LONG → completion msg={'ok' if new_msg_id else 'FAIL'}" + (f" ERR={err2}" if err2 else ""))
                else:
                    log_debug(intern_dir, "FeishuModule.stop",
                              f"FINAL ok={ok} len={len(final_text)} msg={msg_id}" + (f" ERR={err}" if err else ""))
        else:
            log_debug(intern_dir, "FeishuModule.stop", "FINAL: no token")

        # APPROVE: mark turn as finalized to reject any late PostToolUse events.
        # Keep full feishu state (message_id, chat_id, etc.) so UserPromptSubmit
        # can finalize the previous message if needed.
        fs["finalized"] = True
        state["feishu"] = fs
