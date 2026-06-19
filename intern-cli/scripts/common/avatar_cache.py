"""Intern 群头像 image_key 缓存（task203）。

飞书 image_key 一经 upload 就对同一 app 永久有效。按 (app_id, intern_type) 缓存避免重复上传。
cache 文件默认 `<root>/.feishu_registry/_avatar_cache.json`，与 registry 同目录。

结构：
{
  "app_id": "cli_xxx",
  "entries": {
    "claude":  {"image_key": "v3_...", "sha256": "abc...", "uploaded_at": "2026-05-02T..."},
    "codex":   {...},
    "copilot": {...}
  }
}

换 app 会整体失效（app_id 字段不匹配）；换生成规则（sha256 变）对应 entry 失效。
"""
import datetime
import json
import os
import sys
import threading

# avatar_generator 在同级 common/ 下
sys.path.insert(0, os.path.dirname(__file__))
from avatar_generator import render_png_bytes, SUPPORTED_TYPES


DEFAULT_CACHE_PATH_TMPL = ".feishu_registry/_avatar_cache.json"
_lock = threading.Lock()


def cache_path(root):
    return os.path.join(root, DEFAULT_CACHE_PATH_TMPL)


def _load(path):
    if not os.path.exists(path):
        return {"app_id": None, "entries": {}}
    with open(path) as f:
        return json.load(f)


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_or_upload(api, app_id, intern_type, root, force=False):
    """Return image_key for the given intern_type. Upload + cache on miss.

    Args:
        api: FeishuAPI instance with `upload_avatar_image(data_bytes) -> (image_key, err)`
        app_id: current feishu app_id (used as cache discriminator)
        intern_type: one of SUPPORTED_TYPES
        root: WORK_AGENTS_ROOT (cache file under <root>/.feishu_registry/)
        force: ignore cache; always re-upload

    Returns:
        (image_key, err)
    """
    if intern_type not in SUPPORTED_TYPES:
        return None, f"unsupported intern_type: {intern_type!r}"
    path = cache_path(root)
    with _lock:
        cache = _load(path)
        # 换 app 整体失效
        if cache.get("app_id") != app_id:
            cache = {"app_id": app_id, "entries": {}}
        data, sha = render_png_bytes(intern_type)
        entry = cache["entries"].get(intern_type)
        if (not force) and entry and entry.get("sha256") == sha and entry.get("image_key"):
            return entry["image_key"], None
        image_key, err = api.upload_avatar_image(data)
        if err:
            return None, err
        cache["entries"][intern_type] = {
            "image_key": image_key,
            "sha256": sha,
            "uploaded_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        _save(path, cache)
        return image_key, None


def clear(root):
    path = cache_path(root)
    if os.path.exists(path):
        os.remove(path)


def read_cache(root):
    return _load(cache_path(root))
