"""Shared enterprise user environment loading."""

from __future__ import annotations

import os
import re
from pathlib import Path


_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def enterprise_user_env_paths(root: str | os.PathLike[str]) -> list[Path]:
    """Return env candidates in override order."""

    return [
        Path("~/.codeup_env").expanduser(),
        Path("~/.intern-agent-helper/enterprise/user.env").expanduser(),
        Path("~/.config/intern-agent-helper/enterprise/user.env").expanduser(),
        Path(root) / "enterprise" / "user.env",
    ]


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    with Path(path).open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not _ENV_KEY_RE.fullmatch(key):
                continue
            values[key] = value.strip().strip("'\"")
    return values


def load_enterprise_user_env(root: str | os.PathLike[str], env: dict[str, str] | None = None) -> dict[str, str]:
    """Load all enterprise user env files into env and return loaded values."""

    target = env if env is not None else os.environ
    loaded: dict[str, str] = {}
    for path in enterprise_user_env_paths(root):
        if not path.is_file():
            continue
        for key, value in parse_env_file(path).items():
            target[key] = value
            loaded[key] = value
    return loaded
