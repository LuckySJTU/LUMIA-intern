"""
ValidationModule — Stop 时检查回复质量。

移植自旧版 Chat Participant 插件的 outputValidator.ts + validation.ts + historyLogValidator.ts。
"""
import os
import re
from collections import Counter
from common.utils import (
    get_intern_name, read_file_safe, read_file_head_tail,
    parse_status_metadata, parse_session_metadata, file_hash, log_debug,
    get_metadata_context, get_task_metadata_paths,
)
from common.i18n import t, get_locale

# ============================================================
# 双语词表与正则 — 按 locale 分别选用，不混用
# 设计：每个 locale 自己的 LLM 输出按自己的 schema 校验，避免互相干扰。
# ============================================================

# ---------- ZH ----------
ESCAPE_PATTERNS_ZH = [
    r"待更新",
    r"稍后",
    r"下次(?:session|会话)?(?:再)?(?:更新|处理|补充)",
    r"后续(?:再)?(?:更新|处理|补充)",
    r"后面(?:再)?(?:更新|处理|补充)",
    r"统一(?:更新|处理)",
    r"一起(?:更新|处理)",
]
IDENTITY_RE_ZH = re.compile(r'我是\s*[\w\u4e00-\u9fa5_]+')
CURRENT_SUMMARY_TOKENS_ZH = ('本次：', '本次:')
NEXT_STEP_TOKENS_ZH = ('下步：', '下步:')
KNOWLEDGE_NA_RE_ZH = re.compile(r'知识.*无需更新', re.IGNORECASE)

# ---------- EN ----------
ESCAPE_PATTERNS_EN = [
    r"\bTBD\b",
    r"\bTODO later\b",
    r"\bto be updated\b",
    r"\bwill update later\b",
    r"\bnext (?:session|turn) (?:will )?(?:update|handle|fix|add)",
    r"\bupdate (?:in )?(?:the )?next (?:session|turn)",
    r"\b(?:handle|update|fix) (?:later|afterwards|subsequently)",
]
IDENTITY_RE_EN = re.compile(r'\bI am\s+[\w_\-]+')
CURRENT_SUMMARY_RE_EN = re.compile(r'^\s*This turn\s*[:：]', re.MULTILINE | re.IGNORECASE)
NEXT_STEP_RE_EN = re.compile(r'^\s*Next\s*[:：]', re.MULTILINE | re.IGNORECASE)
KNOWLEDGE_NA_RE_EN = re.compile(r'knowledge.*no update needed', re.IGNORECASE)

# ---------- 共用 ----------
KNOWLEDGE_NA_GENERIC_PATTERNS = [
    re.compile(r'task_knowledge\.md.*N/A', re.IGNORECASE),
    re.compile(r'N/A.*task_knowledge', re.IGNORECASE),
]

# Messages shorter than this are likely truncated/API-error/empty — skip validation.
# Real API errors in VS Code produce empty or very short messages (VS Code chat UI
# handles error display; errors don't appear as assistant.message in transcript).
# The stop_hook_active mechanism prevents infinite retries: first Stop → BLOCK →
# agent retries → second Stop (stop_hook_active=True) → unconditional APPROVE.
_SHORT_MSG_THRESHOLD = 50


# ============================================================
# 纯函数：格式检查（按 locale 分流）
# ============================================================

def _has_checklist(text):
    return bool(re.search(r'(?:📋\s*)?(?:\*\*)?Checklist(?:\*\*)?[：:]', text, re.IGNORECASE))

def _has_identity(text):
    if get_locale() == 'en':
        return bool(IDENTITY_RE_EN.search(text))
    return bool(IDENTITY_RE_ZH.search(text))

def _has_current_summary(text):
    if get_locale() == 'en':
        return bool(CURRENT_SUMMARY_RE_EN.search(text))
    return any(tok in text for tok in CURRENT_SUMMARY_TOKENS_ZH)

def _has_next_step(text):
    if get_locale() == 'en':
        return bool(NEXT_STEP_RE_EN.search(text))
    return any(tok in text for tok in NEXT_STEP_TOKENS_ZH)

def _extract_session_from_checklist(text):
    m = re.search(r'(?:📋\s*)?(?:\*\*)?Checklist(?:\*\*)?[：:][\s\S]*$', text, re.IGNORECASE)
    if m:
        sm = re.search(r'Session\b(?:\s*[：:]\s*|\s+)(\d+)', m.group(0), re.IGNORECASE)
        if sm:
            return int(sm.group(1))
    return None

def _check_escape_words(text):
    patterns = ESCAPE_PATTERNS_EN if get_locale() == 'en' else ESCAPE_PATTERNS_ZH
    found = []
    for line in text.splitlines():
        if _is_escape_word_rule_line(line):
            continue
        for pattern in patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                found.append(m.group(0))
    return found


def _is_escape_word_rule_line(line):
    lowered = line.lower()
    return "禁止" in line or "forbidden" in lowered or "avoid" in lowered


def _has_knowledge_na(text):
    if any(p.search(text) for p in KNOWLEDGE_NA_GENERIC_PATTERNS):
        return True
    locale_re = KNOWLEDGE_NA_RE_EN if get_locale() == 'en' else KNOWLEDGE_NA_RE_ZH
    return bool(locale_re.search(text))


# ============================================================
# 纯函数：history_log.md 结构检查（对应旧版 historyLogValidator.ts）
# ============================================================

def _quick_check_history_log(content):
    issues = []
    metadata_lines = re.findall(r'<!--\s*METADATA:SESSION=(\d+)\s*-->', content)
    if len(metadata_lines) == 0:
        issues.append(t('validation.issue.historyNoMetadata'))
    elif len(metadata_lines) > 1:
        issues.append(t('validation.issue.historyMultipleMetadata', len(metadata_lines)))
    session_headers = re.findall(r'^## Session (\d+)', content, re.MULTILINE)
    counts = Counter(session_headers)
    duplicates = [num for num, count in counts.items() if count > 1]
    if duplicates:
        issues.append(t('validation.issue.historyDuplicateSession', ', '.join(sorted(duplicates))))
    return issues


def _quick_check_task_readme_status(content):
    issues = []
    match = re.search(r'<!--\s*METADATA:STATUS=([^,\s>]+)', content)
    if not match:
        return issues
    status = match.group(1)
    valid_task_statuses = {"Open", "InProgress", "Completed"}
    if status not in valid_task_statuses:
        issues.append(t('validation.issue.invalidTaskStatus', status, ', '.join(sorted(valid_task_statuses))))
    return issues


# ============================================================
# ValidationModule
# ============================================================

class ValidationModule:

    def on_user_prompt(self, state, hook_input):
        """每个 turn 开始时记录关键文件的 hash 基线。"""
        intern_dir = state["_intern_dir"]
        metadata = get_metadata_context(intern_dir)
        hashes = {}
        status_path = metadata["status_path"]
        h = file_hash(status_path)
        if h:
            hashes["status.md"] = h
        status_raw = read_file_safe(status_path)
        meta = parse_status_metadata(status_raw)
        task_id = meta.get("task")
        if task_id:
            task_paths = get_task_metadata_paths(intern_dir, task_id)
            for fname in ("history_log.md", "task_knowledge.md"):
                fpath = task_paths["history_log_path"] if fname == "history_log.md" else task_paths["task_knowledge_path"]
                fh = file_hash(fpath)
                if fh:
                    hashes[fname] = fh
        # 记录 expected_session（在文件未被 agent 修改前计算，避免 Stop 时 TOCTOU）
        expected_session = 0
        if task_id:
            task_paths = get_task_metadata_paths(intern_dir, task_id)
            history_raw = read_file_safe(
                task_paths["history_log_path"], max_chars=16000)
            expected_session = parse_session_metadata(history_raw) + 1

        state.setdefault("validation", {})
        state["validation"]["file_hashes"] = hashes
        state["validation"]["expected_session"] = expected_session
        state["validation"]["task_id"] = task_id
        # intern_start 命令标记：该轮只是回顾历史，不需要格式检查
        prompt = hook_input.get("prompt", "").strip()
        state["validation"]["is_start_command"] = prompt.startswith("intern_start")
        # task-notification 标记：背景任务完成通知，不需要格式检查
        state["validation"]["is_task_notification"] = bool(state.get("_is_task_notification"))

    def on_stop(self, state, hook_input):
        """检查 last_assistant_message 质量。Working 状态严格检查，其他状态不检查。"""
        intern_dir = state["_intern_dir"]
        issues = []

        # stop_hook_active=True 时直接放行（防死循环）
        if hook_input.get("stop_hook_active", False):
            log_debug(intern_dir, "ValidationModule.stop", "skip: stop_hook_active=True")
            state.setdefault("validation", {})
            state["validation"]["issues"] = issues
            return

        name = get_intern_name(intern_dir)
        metadata = get_metadata_context(intern_dir)
        status_raw = read_file_safe(metadata["status_path"])
        meta = parse_status_metadata(status_raw)
        status = meta.get("status", "")
        task_id = meta.get("task", "")
        validation_task_id = task_id or state.get("validation", {}).get("task_id", "")

        if status == "Idle":
            if validation_task_id:
                for issue in _task_readme_status_issues(intern_dir, validation_task_id):
                    issues.append(issue)
                if issues:
                    state.setdefault("validation", {})
                    state["validation"]["issues"] = issues
                    log_debug(intern_dir, "ValidationModule.stop", f"idle issues={len(issues)}: {issues}")
                    return
            log_debug(intern_dir, "ValidationModule.stop", f"skip: status={status}")
            state.setdefault("validation", {})
            state["validation"]["issues"] = issues
            return

        # 状态枚举校验：intern status 只允许 Idle / Working
        valid_intern_statuses = {"Idle", "Working"}
        if status not in valid_intern_statuses:
            issues.append(t('validation.issue.invalidStatus', status, ', '.join(sorted(valid_intern_statuses))))

        # intern_start 命令：只是回顾历史，不需要格式检查
        if state.get("validation", {}).get("is_start_command", False):
            log_debug(intern_dir, "ValidationModule.stop", "skip: intern_start command")
            state.setdefault("validation", {})
            state["validation"]["issues"] = issues
            return

        # task-notification：背景任务完成通知，不需要格式检查
        if state.get("validation", {}).get("is_task_notification", False):
            log_debug(intern_dir, "ValidationModule.stop", "skip: task-notification")
            state.setdefault("validation", {})
            state["validation"]["issues"] = issues
            return

        message = state.get("_stop_last_message", "") or hook_input.get("last_assistant_message", "")
        if len(message) < _SHORT_MSG_THRESHOLD:
            log_debug(intern_dir, "ValidationModule.stop", f"skip: len={len(message)} < {_SHORT_MSG_THRESHOLD}")
            state.setdefault("validation", {})
            state["validation"]["issues"] = issues
            return

        # 1. 格式检查
        if not _has_checklist(message):
            issues.append(t('validation.issue.missingChecklist'))
        else:
            if not _has_identity(message):
                issues.append(t('validation.issue.missingIdentity'))
            if not _has_current_summary(message):
                issues.append(t('validation.issue.missingCurrentSummary'))
            if not _has_next_step(message):
                issues.append(t('validation.issue.missingNextStep'))

        # 2. 逃逸词
        for word in _check_escape_words(message):
            issues.append(t('validation.issue.escapeWord', word))

        # 3. Session 号（从 UserPromptSubmit 时记录的 expected_session 读取，避免 TOCTOU）
        expected_session = state.get("validation", {}).get("expected_session", 0)
        if task_id and expected_session > 1:
            actual_session = _extract_session_from_checklist(message)
            if actual_session is not None:
                if actual_session != expected_session:
                    issues.append(t('validation.issue.sessionMismatch', actual_session, expected_session))

        # 4. 文件修改检查
        prev_hashes = state.get("validation", {}).get("file_hashes", {})
        if prev_hashes:
            status_path = metadata["status_path"]
            old_status = prev_hashes.get("status.md", "")
            if old_status and file_hash(status_path) == old_status:
                issues.append(t('validation.issue.statusUnchanged', status_path))

            if task_id:
                task_paths = get_task_metadata_paths(intern_dir, task_id)
                # history_log.md
                hl_path = task_paths["history_log_path"]
                old_hl = prev_hashes.get("history_log.md", "")
                if old_hl and file_hash(hl_path) == old_hl:
                    issues.append(t('validation.issue.historyUnchanged', hl_path))
                elif os.path.exists(hl_path) and expected_session > 0:
                    # 头 8k（含 METADATA）+ 尾 8k（最新 Session 必在尾部）
                    hl_content = read_file_head_tail(hl_path, 8000, 8000)
                    if f"Session {expected_session}" not in hl_content:
                        issues.append(t('validation.issue.historyMissingSession', expected_session, hl_path))
                # task_knowledge.md（允许 N/A）
                tk_path = task_paths["task_knowledge_path"]
                old_tk = prev_hashes.get("task_knowledge.md", "")
                if old_tk and file_hash(tk_path) == old_tk:
                    if not _has_knowledge_na(message):
                        issues.append(t('validation.issue.knowledgeUnchanged', tk_path))

        # 5. history_log.md 结构检查（头 8k + 尾 8k 足以覆盖 METADATA 与最新 Session）
        if task_id:
            hl_path = get_task_metadata_paths(intern_dir, task_id)["history_log_path"]
            if os.path.exists(hl_path):
                for issue in _quick_check_history_log(read_file_head_tail(hl_path, 8000, 8000)):
                    issues.append(issue)
            for issue in _task_readme_status_issues(intern_dir, task_id):
                issues.append(issue)

        state.setdefault("validation", {})
        state["validation"]["issues"] = issues
        log_debug(intern_dir, "ValidationModule.stop", f"issues={len(issues)}: {issues}")

    def on_pre_tool(self, state, hook_input):
        """预留接口，当前无拦截规则。"""
        pass


def _task_readme_status_issues(intern_dir, task_id):
    if not task_id:
        return []
    try:
        task_paths = get_task_metadata_paths(intern_dir, task_id)
    except Exception:
        return []
    task_readme = task_paths.get("task_readme_path")
    if not task_readme or not os.path.exists(task_readme):
        return []
    return _quick_check_task_readme_status(read_file_head_tail(task_readme, 8000, 8000))
