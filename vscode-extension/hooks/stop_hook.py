#!/usr/bin/env python3
"""VS Code Stop hook.
Loads state → runs modules → saves state.
Exit 2 + stderr = block (validation issues), exit 0 = approve.

Due to _Hr mapping, this hook also fires for SubagentStop events.
Detect via agent_id field and skip — SubagentStop is handled by subagent_stop_hook.py.

VS Code 版差异：从 transcript_path 提取 last_assistant_message（VS Code Stop stdin 没有此字段）。
"""
import sys
import traceback
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    get_intern_dir, load_state, save_state, log_debug, state_lock,
    extract_last_assistant_message, get_intern_name, read_file_safe,
    get_intern_type, parse_status_metadata, WORK_AGENTS_ROOT,
    get_metadata_context, get_task_metadata_paths, MetadataResolverError,
)
from common.i18n import t
from validation_module.module import ValidationModule
from log_module.module import LogModule
from feishu_module.module import FeishuModule
from _daemon_addr import get_daemon_http_url


def _is_machine_helper_state(state):
    return state.get("role") in {"machine_helper", "machine_debugger"} or state.get("projectless") is True


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", os.getcwd())
    session_id = hook_input.get("session_id", "") or hook_input.get("sessionId", "")

    try:
        intern_dir = get_intern_dir(cwd, session_id=session_id)
    except ValueError:
        sys.exit(0)

    # SubagentStop events include agent_id; regular Stop does not.
    # Skip — SubagentStop is handled by subagent_stop_hook.py.
    if hook_input.get("agent_id"):
        log_debug(intern_dir, "stop_hook",
                  f"SKIP: SubagentStop event (agent_id={hook_input['agent_id']})")
        sys.exit(0)

    # execution_subagent doesn't trigger native SubagentStop, but its internal
    # sub-agent fires Stop. Check subagent_depth to skip.
    # Also extract last_assistant_message outside the main lock — but use a single
    # depth check + main processing lock to avoid race conditions where another
    # hook (e.g., user_prompt_hook) modifies state between the two operations.
    with state_lock(intern_dir):
        state = load_state(intern_dir)
        depth = state.get("subagent_depth", 0)
        if depth > 0:
            log_debug(intern_dir, "stop_hook",
                      f"SKIP: inside SubAgent (depth={depth})")
            sys.exit(0)

    # Debug: 记录 Stop hook 输入的关键字段
    sha = hook_input.get("stop_hook_active", "NOT_PRESENT")
    log_debug(intern_dir, "stop_hook",
              f"stdin: stop_hook_active={sha}, keys={sorted(hook_input.keys())}")

    # Resolve intern name and type before extracting transcript
    intern_name = get_intern_name(intern_dir)
    intern_type = get_intern_type(intern_name)

    # P3: VS Code Stop stdin 没有 last_assistant_message，从 transcript 提取
    last_msg = hook_input.get("last_assistant_message", "")
    if not last_msg:
        transcript_path = hook_input.get("transcript_path", "")
        last_msg = extract_last_assistant_message(transcript_path, intern_type)

    # Snapshot status.md before modules run (for change detection)
    try:
        status_path = _find_status_path(intern_dir, intern_name)
    except MetadataResolverError as e:
        reason = f"Metadata resolver failed; enterprise hooks cannot fall back to legacy workspace paths: {e}"
        log_debug(intern_dir, "stop_hook", f"BLOCK: {reason}")
        if hook_input.get("stop_hook_active", False):
            sys.exit(0)
        print(reason, file=sys.stderr)
        sys.exit(2)
    status_before = read_file_safe(status_path) if status_path else ""

    with state_lock(intern_dir):
        state = load_state(intern_dir)
        state["_intern_dir"] = intern_dir
        state["_stop_last_message"] = last_msg

        # Re-check depth: SubagentStart may have fired between the first
        # depth check and now (slow transcript extraction in between).
        depth = state.get("subagent_depth", 0)
        if depth > 0:
            log_debug(intern_dir, "stop_hook",
                      f"SKIP (re-check): inside SubAgent (depth={depth})")
            state.pop("_intern_dir", None)
            save_state(intern_dir, state)
            sys.exit(0)

        # Pop background flags (set by user_prompt_hook for this turn's header
        # styling / additionalContext decision). They are NOT used here to gate
        # visibility — task-notification turns run full FINAL so spinner is
        # properly closed and any LLM tool activity surfaces in feishu.
        # Validation IS skipped because the LLM didn't receive intern rules via
        # additionalContext (see user_prompt_hook.py) so can't be expected to
        # comply with Checklist format.
        is_background = state.pop("is_background_turn", False)
        state.pop("background_type", None)
        if _is_machine_helper_state(state):
            log_debug(intern_dir, "stop_hook",
                      "machine helper turn: FINAL + log, skip validation")
            modules = [LogModule(), FeishuModule()]
        elif is_background:
            log_debug(intern_dir, "stop_hook",
                      "non-user-triggered turn: FINAL + log, skip validation")
            modules = [LogModule(), FeishuModule()]
        else:
            modules = [ValidationModule(), LogModule(), FeishuModule()]
        for m in modules:
            m.on_stop(state, hook_input)

        issues = [] if (is_background or _is_machine_helper_state(state)) else state.get("validation", {}).get("issues", [])

        # 如果格式检查被禁用（.format_check_disabled 文件存在），跳过 validation issues
        format_check_disabled = os.path.exists(os.path.join(WORK_AGENTS_ROOT, ".format_check_disabled"))
        if format_check_disabled:
            issues = []

        state.pop("_intern_dir", None)
        save_state(intern_dir, state)

    # Check if status.md changed → notify plugin
    if status_path:
        status_after = read_file_safe(status_path)
        if status_after != status_before:
            _notify_status_changed(intern_name, intern_dir)

    if issues:
            # 构建增强 BLOCK 提示：缺 Checklist 或子字段时追加完整模板
            task_id = ""
            expected_session = state.get("validation", {}).get("expected_session", 0)
            if status_before:
                _meta = parse_status_metadata(status_before)
                task_id = _meta.get("task", "")

            reason = t('stop.block.header') + "、".join(issues) + t('stop.block.headerSep')
            # 缺 Checklist 或其字段 → 追加完整模板（按 t() 出来的当前 locale 文本对照，不靠关键词嗅探）
            _missing_keys = {
                t('validation.issue.missingChecklist'),
                t('validation.issue.missingIdentity'),
                t('validation.issue.missingCurrentSummary'),
                t('validation.issue.missingNextStep'),
            }
            if any(i in _missing_keys for i in issues):
                reason += t('stop.block.fullFormat')
                reason += _build_checklist_example(intern_name, task_id, expected_session, intern_dir)
            reason += t('stop.block.footer')
            log_debug(intern_dir, "stop_hook", f"BLOCK: {reason[:80]}")
            # Exit code 2 = blocking error: stderr 展示给模型，agent 继续工作
            print(reason, file=sys.stderr)
            sys.exit(2)

    log_debug(intern_dir, "stop_hook", "APPROVE")
    sys.exit(0)


def _build_checklist_example(name, task_id, expected_session, intern_dir):
    """构建完整 Checklist 模板示例，用于 BLOCK 提示。"""
    # 推导 3 个目标文件绝对路径，让 LLM 看到精确路径不会改错地方
    status_path = _find_status_path(intern_dir, name) or "<status.md>"
    if task_id and status_path != "<status.md>":
        task_paths = get_task_metadata_paths(intern_dir, task_id)
        history_path = task_paths["history_log_path"]
        knowledge_path = task_paths["task_knowledge_path"]
    else:
        history_path = "<history_log.md>"
        knowledge_path = "<task_knowledge.md>"
    return t(
        'stop.block.checklistTemplate',
        name,
        task_id or '<task_id>',
        expected_session or '<N>',
        status_path,
        history_path,
        knowledge_path,
    )


def _find_status_path(intern_dir: str, intern_name: str) -> str:
    """Return the resolver-provided status.md path."""
    return get_metadata_context(intern_dir)["status_path"]


def _notify_status_changed(intern_name: str, intern_dir: str) -> None:
    """POST to daemon to notify plugin that intern status changed."""
    import urllib.request
    import urllib.error
    url = f"{get_daemon_http_url()}/api/intern/status_changed"
    data = json.dumps({"intern_name": intern_name}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3)
        log_debug(intern_dir, "stop_hook", f"Notified status_changed for {intern_name}")
    except Exception as e:
        log_debug(intern_dir, "stop_hook", f"Failed to notify status_changed: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 严重：任何未捕获异常都 exit(0)，避免平台无限重试（死循环防护）
        try:
            from common.utils import log_debug
            log_debug("/tmp", os.path.basename(__file__),
                      f"FATAL (graceful exit 0): {e}\n{traceback.format_exc()}")
        except Exception:
            pass
        sys.exit(0)
