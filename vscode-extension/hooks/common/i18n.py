"""i18n runtime for Python hooks.

Reads the locale from ~/.config/intern_agents/locale (written atomically by
the VS Code extension whenever `intern.locale` changes). Falls back to env
LANG / LC_ALL detection when the file is absent. Returns 'zh-cn' or 'en'.

Usage:
    from common.i18n import t
    print(t('hook.idle.greeting', name))

Project rule 6: NO cross-language fallback. If a key is missing in the
current locale's table, return the key string itself (and warn to stderr).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict


_LOCALE_FILE = Path.home() / ".config" / "intern_agents" / "locale"
_VALID_LOCALES = ("zh-cn", "en")


def _detect_from_env() -> str:
    # 默认 zh-cn；用户需要英文界面时显式把 intern.locale 设为 'en'（会写入 locale 文件）。
    return "zh-cn"


def _read_locale() -> str:
    try:
        v = _LOCALE_FILE.read_text(encoding="utf-8").strip().lower()
        if v in _VALID_LOCALES:
            return v
    except Exception:
        pass
    return _detect_from_env()


# Lazy-loaded message tables
_tables: Dict[str, Dict[str, str]] = {}
_current_locale: str | None = None


def _load_tables() -> None:
    global _tables
    if _tables:
        return
    here = Path(__file__).parent
    msgs_zh = here / "messages_zh_cn.py"
    msgs_en = here / "messages_en.py"
    ns_zh: Dict[str, Any] = {}
    ns_en: Dict[str, Any] = {}
    if msgs_zh.exists():
        exec(compile(msgs_zh.read_text(encoding="utf-8"), str(msgs_zh), "exec"), ns_zh)
    if msgs_en.exists():
        exec(compile(msgs_en.read_text(encoding="utf-8"), str(msgs_en), "exec"), ns_en)
    _tables["zh-cn"] = ns_zh.get("MESSAGES", {})
    _tables["en"] = ns_en.get("MESSAGES", {})


def get_locale() -> str:
    global _current_locale
    if _current_locale is None:
        _current_locale = _read_locale()
    return _current_locale


def reload_locale() -> str:
    """Force re-read locale file (call after long-running scripts)."""
    global _current_locale
    _current_locale = _read_locale()
    return _current_locale


def t(key: str, *args: Any) -> str:
    _load_tables()
    locale = get_locale()
    table = _tables.get(locale, {})
    msg = table.get(key)
    if msg is None:
        sys.stderr.write(f"[i18n] missing key '{key}' in locale '{locale}'\n")
        return key
    # {0}/{1} ordinal placeholder substitution (matches TS implementation)
    out = msg
    for i, a in enumerate(args):
        out = out.replace("{" + str(i) + "}", str(a))
    return out
