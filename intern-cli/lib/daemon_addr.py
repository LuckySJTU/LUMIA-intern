"""Resolve the local Feishu daemon address-file path."""

from __future__ import annotations

import hashlib
import os


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


def daemon_addr_file(work_root: str | os.PathLike[str] | None = None) -> str:
    """Return the daemon address-file path for this user and work root.

    ``FEISHU_DAEMON_ADDR_FILE`` remains an explicit escape hatch. Without it,
    the path is scoped by uid and the absolute WORK_AGENTS_ROOT so two users or
    two work roots on the same machine do not accidentally reuse one daemon.
    """

    explicit = os.environ.get("FEISHU_DAEMON_ADDR_FILE")
    if explicit:
        return explicit
    root = os.path.abspath(os.fspath(work_root or os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()))
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]
    return f"/tmp/feishu_daemon_{_uid()}_{digest}.json"
