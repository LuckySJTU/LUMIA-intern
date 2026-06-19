"""
Shared transcript parsing utility.

Both LogModule and FeishuModule read the VS Code transcript JSONL independently,
each maintaining their own offset. This module provides the shared parsing logic.
"""
import os
import json


def _goal_from_payload(payload):
    goal = payload.get("goal")
    if not isinstance(goal, dict):
        return {
            "status": "cleared",
            "objective": "",
            "thread_id": payload.get("threadId", ""),
            "created_at": 0,
            "updated_at": 0,
            "tokens_used": 0,
            "time_used_seconds": 0,
        }
    return {
        "status": str(goal.get("status") or ""),
        "objective": str(goal.get("objective") or ""),
        "thread_id": str(goal.get("threadId") or payload.get("threadId") or ""),
        "created_at": int(goal.get("createdAt") or 0),
        "updated_at": int(goal.get("updatedAt") or 0),
        "tokens_used": int(goal.get("tokensUsed") or 0),
        "time_used_seconds": int(goal.get("timeUsedSeconds") or 0),
    }


def extract_codex_goal_events(transcript_path, offset=0):
    """Read Codex thread goal update events from a rollout JSONL."""
    if not transcript_path or not os.path.exists(transcript_path):
        return [], offset

    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return [], offset
    if file_size < offset:
        offset = 0
    if file_size <= offset:
        return [], offset

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read(file_size - offset)
    except OSError:
        return [], offset

    events = []
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload", {})
        if entry.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "thread_goal_updated":
            events.append(_goal_from_payload(payload))
    return events, file_size


def extract_latest_codex_goal_state(transcript_path):
    """Return the latest Codex goal state from a rollout JSONL, or None."""
    events, _ = extract_codex_goal_events(transcript_path, 0)
    return events[-1] if events else None


def _expect_number(value, field_name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Codex rate_limits.{field_name} must be a number")
    return value


def _expect_number_like(value, field_name):
    if isinstance(value, bool):
        raise ValueError(f"Codex rate_limits.{field_name} must be a number")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    raise ValueError(f"Codex rate_limits.{field_name} must be a number")


def _expect_optional_string(value, field_name):
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Codex rate_limits.{field_name} must be a string or null")
    return value


def _parse_codex_rate_limit_window(raw, field_name):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Codex rate_limits.{field_name} must be an object or null")

    used_percent = _expect_number(raw.get("used_percent"), f"{field_name}.used_percent")
    window_minutes = _expect_number(raw.get("window_minutes"), f"{field_name}.window_minutes")
    resets_at = _expect_number(raw.get("resets_at"), f"{field_name}.resets_at")
    return {
        "used_percent": float(used_percent),
        "window_minutes": int(window_minutes),
        "resets_at": int(resets_at),
    }


def _parse_codex_credits(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Codex rate_limits.credits must be an object or null")

    parsed = {}
    for key in ("has_credits", "unlimited"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"Codex rate_limits.credits.{key} must be a boolean")
            parsed[key] = raw[key]
    if "balance" in raw:
        balance = raw["balance"]
        if balance is not None:
            balance = _expect_number_like(balance, "credits.balance")
        parsed["balance"] = balance
    return parsed


def _parse_codex_rate_limits(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Codex rate_limits must be an object or null")

    return {
        "limit_id": _expect_optional_string(raw.get("limit_id"), "limit_id"),
        "limit_name": _expect_optional_string(raw.get("limit_name"), "limit_name"),
        "primary": _parse_codex_rate_limit_window(raw.get("primary"), "primary"),
        "secondary": _parse_codex_rate_limit_window(raw.get("secondary"), "secondary"),
        "credits": _parse_codex_credits(raw.get("credits")),
        "plan_type": _expect_optional_string(raw.get("plan_type"), "plan_type"),
        "rate_limit_reached_type": _expect_optional_string(
            raw.get("rate_limit_reached_type"), "rate_limit_reached_type"),
    }


def extract_assistant_texts(transcript_path, offset, filter_before=None, end_offset=None):
    """Read new assistant texts from transcript JSONL since *offset*.

    Args:
        transcript_path: Path to the transcript JSONL file.
        offset: File byte offset to start reading from.
        filter_before: If set, skip entries with timestamp < this value.
            Used when offset=0 to filter historical entries from restored sessions.
        end_offset: If set, read only up to this byte position (exclusive).
            Used to read pre-SubAgent text without including SubAgent internals.

    Returns:
        (texts, new_offset, new_content_preview):
            texts: list of extracted assistant text strings
            new_offset: updated file byte offset (== end_offset or file size)
            new_content_preview: first 200 chars of new content (for debug logging)
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], offset, ""

    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return [], offset, ""

    # File shrunk (rare: VS Code recreated it) — reset
    if file_size < offset:
        offset = 0

    read_end = end_offset if end_offset is not None else file_size
    if read_end <= offset:
        return [], offset, ""

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new_content = f.read(read_end - offset)
    except OSError:
        return [], offset, ""

    preview = new_content[:200]
    texts = []
    skipped_old = 0

    for line in new_content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type", "")

        # Skip historical entries when reading from offset=0
        if filter_before:
            entry_ts = entry.get("timestamp", "")
            if entry_ts and entry_ts < filter_before:
                skipped_old += 1
                continue

        data = entry.get("data", {})

        # Claude Code format: assistant.message with content/reasoningText
        if entry_type == "assistant.message" and isinstance(data, dict):
            reasoning = data.get("reasoningText", "")
            if reasoning and isinstance(reasoning, str):
                text = reasoning.strip()
                if text:
                    texts.append(f"\U0001f4ad 思考：\n```\n{text}\n```")
            content = data.get("content", "")
            if content and isinstance(content, str):
                text = content.strip()
                if text:
                    texts.append(text)

        # VS Code Copilot format: assistant with message.content blocks
        elif entry_type == "assistant":
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        texts.append(text)

        # Codex CLI format: response_item with payload.type=message + payload.role=assistant.
        # Reasoning items are encrypted (no plaintext) — skipped.
        elif entry_type == "response_item":
            payload = entry.get("payload", {})
            if (isinstance(payload, dict)
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"):
                for block in payload.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = (block.get("text", "") or "").strip()
                        if text:
                            texts.append(text)

    return texts, read_end, preview


def extract_usage_stats(transcript_path, offset=0):
    """Read token usage stats from transcript JSONL since *offset*.

    Deduplicates streaming entries: consecutive assistant entries with the same
    (model, input_tokens, cache_creation, cache_read) are from the same API call;
    only the last entry (with the highest output_tokens) is counted.

    Args:
        transcript_path: Path to the transcript JSONL file.
        offset: File byte offset to start reading from.

    Returns:
        (stats, new_offset):
            stats: dict with keys {input, output, cache_write, cache_read, context, model}
                - input/output/cache_write/cache_read: incremental token counts
                - context: latest API call's input-side total (input+cw+cr)
                - model: model name from the latest API call
            new_offset: updated file byte offset
    """
    stats = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
             "context": 0, "model": ""}
    if not transcript_path or not os.path.exists(transcript_path):
        return stats, offset

    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return stats, offset

    if file_size < offset:
        offset = 0
    if file_size <= offset:
        return stats, offset

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read(file_size - offset)
    except OSError:
        return stats, offset

    # Collect raw entries, then dedup streaming
    raw = []
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        usage = msg.get("usage", {})
        raw.append({
            "model": msg.get("model", ""),
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cw": usage.get("cache_creation_input_tokens", 0),
            "cr": usage.get("cache_read_input_tokens", 0),
        })

    # Dedup: consecutive same (model, input, cw, cr) = one API call, take max output
    calls = []
    for e in raw:
        key = (e["model"], e["input"], e["cw"], e["cr"])
        if calls and calls[-1][0] == key:
            calls[-1] = (key, max(calls[-1][1], e["output"]))
        else:
            calls.append((key, e["output"]))

    for (model, inp, cw, cr), out in calls:
        stats["input"] += inp
        stats["output"] += out
        stats["cache_write"] += cw
        stats["cache_read"] += cr
        stats["model"] = model
        stats["context"] = inp + cw + cr  # latest call's context size

    return stats, file_size


def extract_codex_session_usage(transcript_path):
    """Read cumulative usage from a Codex CLI transcript JSONL.

    Codex emits `event_msg.token_count` events with two distinct fields:
      - `total_token_usage`  — cumulative billing tokens across the session
      - `last_token_usage`   — current turn's usage (= context occupancy now)
      - `model_context_window` — Codex CLI's reported model context size

    Cumulative token fields (input/output/cache_read) come from
    `total_token_usage`. ChatGPT-auth Codex uses these for diagnostics; the
    user-facing footer displays subscription quota from `rate_limits`.

    Context display (footer "ctx X/Y") comes from `last_token_usage.input_tokens`
    (current turn's input = current context occupancy); ceiling comes from the
    Codex CLI-reported `model_context_window`.

    Codex `input_tokens` already INCLUDES the cached portion (verified empirically:
    total_tokens == input_tokens + output_tokens). So:
      - non-cached input = input_tokens - cached_input_tokens
      - output = output_tokens (already includes reasoning_output_tokens)

    Model name comes from the latest `turn_context.payload.model`.

    Returns the same shape as extract_session_usage plus optional
    `max_context` (model_context_window), `quota` (rate_limits),
    `quota_parse_error`, and `token_count_seen` fields.

    Args:
        transcript_path: Path to the main session transcript JSONL.

    Returns:
        dict with keys {input, output, cache_write, cache_read, context, model,
        by_model, max_context, quota, quota_parse_error, token_count_seen}.
    """
    total = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
             "context": 0, "model": "", "by_model": {}, "max_context": 0,
             "quota": None, "quota_parse_error": "", "token_count_seen": False}
    if not transcript_path or not os.path.exists(transcript_path):
        return total

    latest_total = None
    latest_last = None
    latest_max_context = 0
    latest_model = ""
    latest_valid_quota = None
    latest_quota_parse_error = ""
    saw_token_count = False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type", "")
                payload = entry.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if etype == "event_msg" and payload.get("type") == "token_count":
                    saw_token_count = True
                    raw_rate_limits = payload.get("rate_limits")
                    latest_quota_parse_error = ""
                    if raw_rate_limits is not None:
                        try:
                            latest_valid_quota = _parse_codex_rate_limits(raw_rate_limits)
                        except ValueError as exc:
                            latest_quota_parse_error = str(exc)
                    info = payload.get("info")
                    if info is not None and not isinstance(info, dict):
                        raise ValueError("Codex token_count.info must be an object or null")
                    if isinstance(info, dict):
                        tk_total = info.get("total_token_usage")
                        if isinstance(tk_total, dict):
                            latest_total = tk_total
                        tk_last = info.get("last_token_usage")
                        if isinstance(tk_last, dict):
                            latest_last = tk_last
                        mcw = info.get("model_context_window")
                        if isinstance(mcw, int) and mcw > 0:
                            latest_max_context = mcw
                elif etype == "turn_context":
                    m = payload.get("model")
                    if m:
                        latest_model = m
    except OSError:
        return total

    total["quota"] = latest_valid_quota
    total["quota_parse_error"] = latest_quota_parse_error
    total["token_count_seen"] = saw_token_count
    if not latest_total:
        if latest_valid_quota is not None:
            total["model"] = latest_model
        return total

    # Cumulative usage comes from total_token_usage.
    input_tokens = latest_total.get("input_tokens", 0) or 0
    cached_input = latest_total.get("cached_input_tokens", 0) or 0
    output_tokens = latest_total.get("output_tokens", 0) or 0
    non_cached_input = max(0, input_tokens - cached_input)

    total["input"] = non_cached_input
    total["output"] = output_tokens
    total["cache_read"] = cached_input
    total["cache_write"] = 0

    # Current context occupancy = last_token_usage.input_tokens (current turn's input)
    # This is what the codex TUI shows as the "context window used" indicator.
    # Falling back to total input only if last_token_usage is missing (very early turn).
    if latest_last and isinstance(latest_last.get("input_tokens"), int):
        total["context"] = latest_last.get("input_tokens", 0)
    else:
        total["context"] = input_tokens

    total["max_context"] = latest_max_context
    total["model"] = latest_model
    if latest_model:
        total["by_model"][latest_model] = {
            "input": non_cached_input,
            "output": output_tokens,
            "cache_write": 0,
            "cache_read": cached_input,
        }
    return total


def extract_session_usage(transcript_path):
    """Read usage from main transcript + all subagent transcripts.

    Args:
        transcript_path: Path to the main session transcript JSONL.

    Returns:
        dict with same keys as extract_usage_stats stats (cumulative across all files),
        plus per-model breakdown in "by_model" key.
    """
    total = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
             "context": 0, "model": "", "by_model": {}}
    if not transcript_path or not os.path.exists(transcript_path):
        return total

    # Main transcript
    main_stats, _ = extract_usage_stats(transcript_path, 0)
    for k in ("input", "output", "cache_write", "cache_read"):
        total[k] += main_stats[k]
    total["context"] = main_stats["context"]
    total["model"] = main_stats["model"]

    # Subagent transcripts: located at {session_dir}/subagents/*.jsonl
    session_dir = transcript_path.rsplit(".", 1)[0]  # remove .jsonl extension
    subagents_dir = os.path.join(session_dir, "subagents")
    if os.path.isdir(subagents_dir):
        for fname in os.listdir(subagents_dir):
            if not fname.endswith(".jsonl"):
                continue
            sub_path = os.path.join(subagents_dir, fname)
            sub_stats, _ = extract_usage_stats(sub_path, 0)
            for k in ("input", "output", "cache_write", "cache_read"):
                total[k] += sub_stats[k]
            # Track per-model for cost calculation
            m = sub_stats["model"]
            if m:
                if m not in total["by_model"]:
                    total["by_model"][m] = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
                for k in ("input", "output", "cache_write", "cache_read"):
                    total["by_model"][m][k] += sub_stats[k]

    # Add main model to by_model
    m = main_stats["model"]
    if m:
        if m not in total["by_model"]:
            total["by_model"][m] = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        for k in ("input", "output", "cache_write", "cache_read"):
            total["by_model"][m][k] += main_stats[k]

    return total
