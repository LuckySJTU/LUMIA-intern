"""Codex ChatGPT quota helpers.

The transcript is the primary source. When it lacks a valid rate-limit snapshot,
Feishu falls back to the official local `codex app-server` JSON-RPC surface.
"""

import json
import os
import select
import subprocess
import time


CACHE_TTL_SECONDS = 30


def _expect_number(value, field_name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Codex app-server rateLimits.{field_name} must be a number")
    return value


def _optional_string(value, field_name):
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Codex app-server rateLimits.{field_name} must be a string or null")
    return value


def _get_any(raw, *names):
    for name in names:
        if isinstance(raw, dict) and name in raw:
            return raw[name]
    return None


def _parse_window(raw, field_name):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Codex app-server rateLimits.{field_name} must be an object or null")
    used_percent = _expect_number(_get_any(raw, "usedPercent", "used_percent"), f"{field_name}.usedPercent")
    window_minutes = _expect_number(
        _get_any(raw, "windowDurationMins", "window_minutes"),
        f"{field_name}.windowDurationMins",
    )
    resets_at = _expect_number(_get_any(raw, "resetsAt", "resets_at"), f"{field_name}.resetsAt")
    return {
        "used_percent": float(used_percent),
        "window_minutes": int(window_minutes),
        "resets_at": int(resets_at),
    }


def _parse_credits(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Codex app-server rateLimits.credits must be an object or null")
    parsed = {}
    has_credits = _get_any(raw, "hasCredits", "has_credits")
    if has_credits is not None:
        if not isinstance(has_credits, bool):
            raise ValueError("Codex app-server rateLimits.credits.hasCredits must be a boolean")
        parsed["has_credits"] = has_credits
    unlimited = raw.get("unlimited")
    if unlimited is not None:
        if not isinstance(unlimited, bool):
            raise ValueError("Codex app-server rateLimits.credits.unlimited must be a boolean")
        parsed["unlimited"] = unlimited
    balance = raw.get("balance")
    if balance is not None:
        if isinstance(balance, bool) or not isinstance(balance, (int, float, str)):
            raise ValueError("Codex app-server rateLimits.credits.balance must be a number")
        balance = float(balance)
    if "balance" in raw:
        parsed["balance"] = balance
    return parsed


def _select_rate_limits(result):
    raw = _get_any(result, "rateLimits", "rate_limits")
    if raw is not None:
        return raw
    by_limit = _get_any(result, "rateLimitsByLimitId", "rate_limits_by_limit_id")
    if isinstance(by_limit, dict):
        return by_limit.get("codex")
    return None


def parse_app_server_rate_limits(result):
    """Normalize app-server account/rateLimits/read output to footer quota shape."""
    if not isinstance(result, dict):
        raise ValueError("Codex app-server rateLimits result must be an object")
    raw = _select_rate_limits(result)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Codex app-server rateLimits must be an object or null")

    plan_type = _optional_string(
        _get_any(raw, "planType", "plan_type") or _get_any(result, "planType", "plan_type"),
        "planType",
    )
    quota = {
        "limit_id": _optional_string(_get_any(raw, "limitId", "limit_id") or "codex", "limitId"),
        "limit_name": _optional_string(_get_any(raw, "limitName", "limit_name"), "limitName"),
        "primary": _parse_window(raw.get("primary"), "primary"),
        "secondary": _parse_window(raw.get("secondary"), "secondary"),
        "credits": _parse_credits(raw.get("credits")),
        "plan_type": plan_type,
        "rate_limit_reached_type": _optional_string(
            _get_any(raw, "rateLimitReachedType", "rate_limit_reached_type"),
            "rateLimitReachedType",
        ),
    }
    if not any((
            quota["primary"],
            quota["secondary"],
            quota["credits"],
            quota["plan_type"],
            quota["rate_limit_reached_type"],
    )):
        return None
    return quota


def _send(proc, payload):
    if proc.stdin is None:
        raise RuntimeError("codex app-server stdin is closed")
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _read_response(proc, request_id, timeout_sec):
    if proc.stdout is None:
        raise RuntimeError("codex app-server stdout is closed")
    deadline = time.monotonic() + timeout_sec
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for codex app-server response id={request_id}")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if line == "":
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            raise RuntimeError(f"codex app-server exited before response id={request_id}: {stderr.strip()}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"codex app-server error for id={request_id}: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"codex app-server returned non-object result for id={request_id}")
        return result


def read_app_server_quota(intern_dir=None, timeout_sec=5, codex_bin=None):
    codex_bin = codex_bin or os.environ.get("CODEX_BIN", "codex")
    cwd = intern_dir or os.getcwd()
    proc = subprocess.Popen(
        [codex_bin, "app-server", "--listen", "stdio://"],
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _send(proc, {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "axis_intern_agents",
                    "title": "Axis Intern Agents",
                    "version": "0.0.0",
                }
            },
        })
        _read_response(proc, 0, timeout_sec)
        _send(proc, {"method": "initialized", "params": {}})
        _send(proc, {"method": "account/rateLimits/read", "id": 1})
        result = _read_response(proc, 1, timeout_sec)
        return parse_app_server_rate_limits(result)
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def get_quota_with_message_cache(message_state, now=None, reader=None):
    """Return (quota, error, cache_hit) using a per-Feishu-message 30s cache."""
    if now is None:
        now = time.time()
    cache = message_state.get("codex_quota_cache")
    if isinstance(cache, dict) and cache.get("expires_at", 0) > now:
        return cache.get("quota"), cache.get("error", ""), True

    try:
        quota = reader() if reader is not None else read_app_server_quota()
        error = ""
    except Exception as exc:
        quota = None
        error = str(exc)
    message_state["codex_quota_cache"] = {
        "expires_at": now + CACHE_TTL_SECONDS,
        "quota": quota,
        "error": error,
    }
    return quota, error, False
