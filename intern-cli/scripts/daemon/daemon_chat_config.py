"""Daemon-local per-chat config (task283).

`detail_mode` decides how `FeishuModule.on_post_tool` renders the in-progress
Feishu message on the daemon machine. Because the filtering happens inside the
hook process — which only ever runs on the daemon machine — the truth source
for `detail_mode` must live on the daemon machine too, not the relay host.

Path: `$WORK_AGENTS_ROOT/.feishu_registry/_chat_config.json`
Schema: `{chat_id: {"detail_mode": "full"|"summary"}}`

The `_` prefix keeps RegistryManager.reload() from mis-parsing this file as an
intern registry entry (it requires a `chatId` key at the top level).

`trigger_mode` stays on the relay (see relay/chat_config.py). The two configs
intentionally do not share a file because they are owned by different
processes on different machines.

This module is used by daemon-process code (HTTP handler, future relay RPC).
It is strict — IO / JSON errors propagate so misconfiguration surfaces loudly
(project rule 6: no silent fallback). The hook hot-path reader lives in
`vscode-extension/hooks/feishu_module/chat_config_reader.py`, points at the
same file, and is intentionally resilient (returns default on read error +
debug log) because PostToolUse must never raise.
"""

import json
import os
import threading

_REGISTRY_DIR = os.path.join(
    os.environ.get("WORK_AGENTS_ROOT") or os.getcwd(), ".feishu_registry")
_PATH = os.path.join(_REGISTRY_DIR, "_chat_config.json")
_LOCK = threading.RLock()

_DEFAULT_DETAIL_MODE = "full"
_VALID_DETAIL_MODES = ("full", "summary")


def _read():
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def get_detail_mode(chat_id):
    """Return "full" | "summary" for chat_id. Defaults to "full" when the file
    is missing, the chat is unknown, or the stored value is not in the valid
    set. Empty chat_id also returns default (caller may not always have one).

    Raises OSError / ValueError on filesystem or JSON errors so the daemon
    surfaces broken state (project rule 6). Hook hot-path callers must use the
    resilient `chat_config_reader` in the hooks package instead.
    """
    if not chat_id:
        return _DEFAULT_DETAIL_MODE
    with _LOCK:
        data = _read()
    entry = data.get(chat_id) or {}
    value = entry.get("detail_mode", _DEFAULT_DETAIL_MODE)
    if value not in _VALID_DETAIL_MODES:
        return _DEFAULT_DETAIL_MODE
    return value


def set_detail_mode(chat_id, mode):
    """Persist detail_mode for chat_id. Returns True iff the value changed.

    Raises ValueError on empty chat_id or unknown mode so callers can surface
    HTTP 400 rather than silently writing nothing.
    """
    if not chat_id:
        raise ValueError("chat_id is required")
    if mode not in _VALID_DETAIL_MODES:
        raise ValueError(
            f"invalid detail_mode: {mode!r}, must be one of {_VALID_DETAIL_MODES}")
    with _LOCK:
        data = _read()
        entry = data.setdefault(chat_id, {})
        old = entry.get("detail_mode", _DEFAULT_DETAIL_MODE)
        if old not in _VALID_DETAIL_MODES:
            old = _DEFAULT_DETAIL_MODE
        if old == mode:
            return False
        entry["detail_mode"] = mode
        _write(data)
        return True


def valid_detail_modes():
    return _VALID_DETAIL_MODES
