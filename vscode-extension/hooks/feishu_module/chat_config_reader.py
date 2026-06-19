"""Read-only view of the daemon-local chat_config for the hook hot path.

task283: `detail_mode` truth source moved from the relay-host
`~/.feishu_relay/chat_config.json` to a daemon-local file at
`$WORK_AGENTS_ROOT/.feishu_registry/_chat_config.json`. The hook process
(PostToolUse) runs on the daemon machine, so reading from the daemon-local
file means the value actually reflects what the supervisor set via
`/detail_mode` or `/config` — even in the relay-client deployment where the
relay host is a different machine.

Writes always go through the daemon (HTTP `/api/group/detail_mode` or, after
task283 Session 3, the relay→daemon RPC). This module is deliberately
read-only and ultra-resilient: PostToolUse must never raise, and a corrupt
file or transient IO error should default to "full" (no filtering) so the
supervisor sees raw tool output and notices the misconfiguration.

Keep schema in sync with `intern-cli/scripts/daemon/daemon_chat_config.py`.
"""

import json
import logging
import os

# task283: env-driven path matches daemon's `daemon_chat_config._PATH` and
# `common/utils.REGISTRY_DIR` so the daemon writer and the hook reader hit the
# same file without any cross-process coordination.
_REGISTRY_DIR = os.environ.get(
    "FEISHU_REGISTRY_DIR",
    os.path.join(os.environ.get("WORK_AGENTS_ROOT") or os.getcwd(),
                 ".feishu_registry"))
_PATH = os.path.join(_REGISTRY_DIR, "_chat_config.json")

_DEFAULT_DETAIL_MODE = "full"
_VALID_DETAIL_MODES = ("full", "summary")

_log = logging.getLogger(__name__)


def _read():
    if not os.path.exists(_PATH):
        return {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        # task283: don't raise in the hot path. Log so the misconfiguration is
        # at least visible in daemon logs — the supervisor will see "full"
        # behavior and check the log when their /detail_mode setting appears
        # to be ignored.
        _log.debug(
            "chat_config_reader: failed to read %s (%s); falling back to "
            "default detail_mode", _PATH, e)
        return {}


def get_detail_mode(chat_id):
    """Return "full" | "summary" for chat_id. Defaults to "full" when missing,
    unknown, or unreadable. Never raises — hooks are in the hot path."""
    if not chat_id:
        return _DEFAULT_DETAIL_MODE
    data = _read()
    entry = data.get(chat_id) or {}
    mode = entry.get("detail_mode", _DEFAULT_DETAIL_MODE)
    if mode not in _VALID_DETAIL_MODES:
        _log.debug(
            "chat_config_reader: chat=%s has invalid detail_mode=%r; "
            "falling back to default", chat_id, mode)
        return _DEFAULT_DETAIL_MODE
    return mode
