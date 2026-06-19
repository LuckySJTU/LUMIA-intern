#!/usr/bin/env python3
"""Sync Codex project hook trust state into the user config.toml.

Codex 0.129+ requires project hooks to be reviewed before they run. The TUI
stores review state in the user config under [hooks.state."<hook key>"].
This helper asks the Codex app-server for the canonical hook keys and hashes,
then upserts the matching trusted_hash entries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HookTrustEntry:
    key: str
    current_hash: str


def _toml_basic_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _trusted_hash_for(text: str, key: str) -> str | None:
    escaped = _toml_basic_string(key)
    pattern = re.compile(
        r'(?ms)^\[hooks\.state\."' + re.escape(escaped) + r'"\]\s*\n(?P<body>.*?)(?=^\[|\Z)'
    )
    match = pattern.search(text)
    if not match:
        return None
    hash_match = re.search(r'(?m)^\s*trusted_hash\s*=\s*"([^"]+)"\s*$', match.group("body"))
    if not hash_match:
        return None
    return hash_match.group(1)


def _remove_table(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skipping = True
            continue
        if skipping and stripped.startswith("[") and stripped.endswith("]"):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def upsert_hook_trust(text: str, entries: list[HookTrustEntry]) -> tuple[str, bool]:
    if all(_trusted_hash_for(text, entry.key) == entry.current_hash for entry in entries):
        return text, False

    new_text = text
    for entry in entries:
        header = f'[hooks.state."{_toml_basic_string(entry.key)}"]'
        new_text = _remove_table(new_text, header)

    new_text = new_text.rstrip()
    if new_text:
        new_text += "\n\n"
    for entry in entries:
        escaped_key = _toml_basic_string(entry.key)
        escaped_hash = _toml_basic_string(entry.current_hash)
        new_text += f'[hooks.state."{escaped_key}"]\ntrusted_hash = "{escaped_hash}"\n\n'
    return new_text.rstrip() + "\n", True


def _feature_known(codex_bin: str, feature_name: str) -> bool:
    proc = subprocess.run(
        [codex_bin, "features", "list"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codex features list failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        cols = line.split()
        if cols and cols[0] == feature_name and (len(cols) < 2 or cols[1] != "removed"):
            return True
    return False


def _send(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("codex app-server stdin is closed")
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen[str], request_id: int, timeout_sec: float) -> dict[str, Any]:
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


def list_project_hooks(codex_bin: str, intern_dir: str, work_root: str, timeout_sec: float) -> list[HookTrustEntry]:
    env = os.environ.copy()
    env["INTERN_DIR"] = intern_dir
    env["WORK_AGENTS_ROOT"] = work_root
    proc = subprocess.Popen(
        [codex_bin, "app-server", "--listen", "stdio://", "--enable", "hooks"],
        cwd=intern_dir,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _send(
            proc,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "axis_intern_agents",
                        "title": "Axis Intern Agents",
                        "version": "0.0.0",
                    }
                },
            },
        )
        _read_response(proc, 0, timeout_sec)
        _send(proc, {"method": "initialized", "params": {}})
        _send(proc, {"method": "hooks/list", "id": 1, "params": {"cwds": [intern_dir]}})
        result = _read_response(proc, 1, timeout_sec)
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

    data = result.get("data")
    if not isinstance(data, list):
        raise RuntimeError("hooks/list returned no data array")
    entries: list[HookTrustEntry] = []
    errors: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        for err in item.get("errors", []):
            if isinstance(err, dict):
                errors.append(f"{err.get('path')}: {err.get('message')}")
        for hook in item.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            key = hook.get("key")
            current_hash = hook.get("currentHash")
            if isinstance(key, str) and isinstance(current_hash, str):
                entries.append(HookTrustEntry(key=key, current_hash=current_hash))
    if errors:
        raise RuntimeError("hooks/list reported errors: " + "; ".join(errors))
    if not entries:
        raise RuntimeError(f"hooks/list found no hooks for {intern_dir}")
    return entries


def sync(config_path: Path, intern_dir: str, work_root: str, codex_bin: str, timeout_sec: float) -> tuple[bool, int]:
    if not _feature_known(codex_bin, "hooks"):
        return False, 0
    entries = list_project_hooks(codex_bin, intern_dir, work_root, timeout_sec)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_text, changed = upsert_hook_trust(text, entries)
    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, config_path)
    return changed, len(entries)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--intern-dir", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    args = parser.parse_args(argv)

    try:
        changed, count = sync(
            config_path=Path(args.config),
            intern_dir=args.intern_dir,
            work_root=args.work_root,
            codex_bin=args.codex_bin,
            timeout_sec=args.timeout_sec,
        )
    except Exception as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1

    print(f"changed={1 if changed else 0} trusted_hooks={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
