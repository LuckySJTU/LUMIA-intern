"""
LogModule — 对话日志记录。

日志路径：
  - Idle:    llm_intern_logs/<intern>/<timestamp>.log
  - Working: llm_intern_logs/<task_id>/<date>_session_<N>_<intern>_turn_<T>.log

日志内容：
  [meta info] → header
  [system prompt] → SessionStart 注入的 additionalContext
  [history messages] → 从飞书 buffer 复用（之前 turn 的内容）
  [user prompt] → 当前用户消息
  [assistant text + tool calls] → 实时追加

Session/Turn 概念：
  - "我们的 session" = 任务 session 号（history_log.md 的 METADATA:SESSION + 1）
  - Turn: 正常结束 → turn=1；异常结束（打断等） → session 不递增，turn+1
  - Stop APPROVE 标记正常完成
"""
import os
import json
from datetime import datetime
from common.utils import (
    get_intern_name, read_file_safe,
    parse_status_metadata, parse_session_metadata, log_debug,
    get_intern_type, get_metadata_context, get_task_metadata_paths,
)

WORK_AGENTS_ROOT = os.environ.get("WORK_AGENTS_ROOT") or os.getcwd()


class LogModule:

    # ── SessionStart ──────────────────────────────────────────
    def on_session_start(self, state, hook_input):
        """保存 system prompt 到 state，供后续 log 使用。不创建 log 文件。"""
        intern_dir = state["_intern_dir"]
        name = get_intern_name(intern_dir)
        metadata = get_metadata_context(intern_dir)

        # 解析状态
        status_raw = read_file_safe(metadata["status_path"])
        meta = parse_status_metadata(status_raw)
        task_id = meta.get("task", "")
        status = meta.get("status", "Idle")

        # 计算 session number
        session_num = 0
        if task_id:
            task_paths = get_task_metadata_paths(intern_dir, task_id)
            history_raw = read_file_safe(
                task_paths["history_log_path"], max_chars=16000)
            session_num = parse_session_metadata(history_raw) + 1

        # 计算 turn: 检查上次 session 是否正常完成
        turn = 1
        prev = state.get("log", {})
        prev_session = prev.get("session_num", 0)
        prev_completed = prev.get("session_completed", False)
        if task_id and prev_session == session_num and not prev_completed:
            # 同一个 session 号，上次未正常完成 → turn 递增
            turn = prev.get("turn", 1) + 1

        # 保存到 state（不创建文件，等 UserPromptSubmit）
        state.setdefault("log", {})
        state["log"]["session_num"] = session_num
        state["log"]["task_id"] = task_id
        state["log"]["status"] = status
        state["log"]["intern_name"] = name
        state["log"]["turn"] = turn
        state["log"]["turn_count"] = 0
        state["log"]["session_completed"] = False
        # 保存 system prompt
        system_prompt = state.get("_context_text", "")
        if system_prompt:
            state["log"]["system_prompt"] = system_prompt
        # 重置 transcript
        state["log"]["transcript_offset"] = 0
        state["log"]["assistant_texts"] = []
        state["log"]["transcript_path"] = ""
        state["log"]["session_start_utc"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        state["log"]["log_path"] = ""

        log_debug(intern_dir, "LogModule.session_start",
                  f"session={session_num} turn={turn} status={status} task={task_id}")

    # ── UserPromptSubmit ──────────────────────────────────────
    def on_user_prompt(self, state, hook_input):
        """创建 log 文件，写 header + system prompt + history + user prompt。"""
        log = state.get("log", {})
        intern_dir = state["_intern_dir"]
        name = get_intern_name(intern_dir)
        metadata = get_metadata_context(intern_dir)

        # 重新读取状态（可能在 session 中变化）
        status_raw = read_file_safe(metadata["status_path"])
        meta = parse_status_metadata(status_raw)
        # 严格以 status.md 为准：不 fallback 到 stale log.task_id（规则 #6）。
        # 同一 session 内 status.md 不会被清空，需保留仅为跨 session intern 切 Idle 后 Task 仍存留的场景：该场景应以 Idle 处理，Task 为空。
        task_id = meta.get("task", "")
        status = meta.get("status", "Idle")
        turn = log.get("turn", 1)

        # 重新计算 session_num（history_log.md 可能在 session 中被更新）
        session_num = 0
        if task_id:
            task_paths = get_task_metadata_paths(intern_dir, task_id)
            history_raw = read_file_safe(
                task_paths["history_log_path"], max_chars=16000)
            session_num = parse_session_metadata(history_raw) + 1
        log["session_num"] = session_num

        # 计算 log 路径
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if task_id and status == "Working":
            # Working: llm_intern_logs/<task_id>/<date>_session_<N>_<intern>_turn_<T>.log
            logs_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", task_id)
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_filename = f"{date_str}_session_{session_num}_{name}_turn_{turn}.log"
        else:
            # Idle: llm_intern_logs/<intern>/<timestamp>.log
            logs_dir = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs", name)
            log_filename = f"{ts}.log"

        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, log_filename)

        log["log_path"] = log_path
        log["turn_count"] = log.get("turn_count", 0) + 1

        # === 写 meta info ===
        header = f"""=== Session Log ===
Intern: {name}
Task: {task_id or 'none'}
Status: {status}
Session: {session_num}
Turn: {turn}
Started: {datetime.now().isoformat()}
===

"""
        self._append(log_path, header)

        # === 写 [Enabled Skills]（task186）===
        skills_block = self._render_enabled_skills(intern_dir)
        if skills_block:
            self._append(log_path, skills_block)

        # === 写 system prompt ===
        system_prompt = log.get("system_prompt", "")
        if system_prompt:
            self._append(log_path, f"=== System Prompt ({len(system_prompt)} chars) ===\n{system_prompt}\n=== End System Prompt ===\n\n")

        # === 写 history messages（从飞书 buffer 复用） ===
        feishu = state.get("feishu", {})
        prev_buffer = feishu.get("_prev_buffer_lines", [])
        if prev_buffer:
            self._append(log_path, "=== History Messages ===\n")
            for line in prev_buffer:
                self._append(log_path, f"{line}\n")
            self._append(log_path, "=== End History ===\n\n")

        # === 写 user prompt ===
        prompt = hook_input.get("prompt", "")
        ts_short = datetime.now().strftime("%H:%M:%S")
        self._append(log_path, f"[{ts_short}] [USER] ({len(prompt)} chars)\n{prompt}\n\n")

        log_debug(intern_dir, "LogModule.user_prompt", f"log created: {log_path}")

    # ── PostToolUse ───────────────────────────────────────────
    def on_post_tool(self, state, hook_input):
        """追加 assistant 文本 + 工具调用记录。从 transcript 提取 assistant 文本。"""
        log = state.get("log", {})
        log_path = log.get("log_path", "")

        # 从 transcript 增量提取 assistant 文本
        transcript_path = hook_input.get("transcript_path", "")
        if transcript_path and os.path.exists(transcript_path):
            self._extract_assistant_texts(state, transcript_path)

        if not log_path:
            return

        tool_name = hook_input.get("tool_name", "unknown")
        tool_input = hook_input.get("tool_input", {})
        tool_output = hook_input.get("tool_response", "")
        ts = datetime.now().strftime("%H:%M:%S")

        # 先写新的 assistant 文本（工具调用前的思考）
        new_texts = log.get("assistant_texts", [])
        text_logged = log.get("text_logged_count", 0)
        for t in new_texts[text_logged:]:
            self._append(log_path, f"[{ts}] [ASSISTANT]\n{t}\n\n")
        log["text_logged_count"] = len(new_texts)

        # 写工具调用
        if isinstance(tool_input, dict):
            input_preview = json.dumps(tool_input, ensure_ascii=False)[:300]
        else:
            input_preview = str(tool_input)[:300]
        result_preview = str(tool_output)[:300] if tool_output else ""

        entry = f"[{ts}] [TOOL] {tool_name}\n  input: {input_preview}\n"
        if result_preview:
            entry += f"  result: {result_preview}\n"
        entry += "\n"
        self._append(log_path, entry)

    # ── Stop ──────────────────────────────────────────────────
    def on_stop(self, state, hook_input):
        """写结束标记。BLOCK 路径记录 issues，APPROVE 路径写 SESSION_END。"""
        # 最终提取 transcript
        transcript_path = hook_input.get("transcript_path", "")
        if transcript_path and os.path.exists(transcript_path):
            self._extract_assistant_texts(state, transcript_path)

        log = state.get("log", {})
        log_path = log.get("log_path", "")
        if not log_path:
            return

        intern_dir = state["_intern_dir"]
        ts = datetime.now().strftime("%H:%M:%S")

        # 写剩余的 assistant 文本
        new_texts = log.get("assistant_texts", [])
        text_logged = log.get("text_logged_count", 0)
        for t in new_texts[text_logged:]:
            self._append(log_path, f"[{ts}] [ASSISTANT]\n{t}\n\n")
        log["text_logged_count"] = len(new_texts)

        issues = state.get("validation", {}).get("issues", [])
        if issues:
            entry = f"[{ts}] [VALIDATION_BLOCK] Issues: {', '.join(issues)}\n"
            self._append(log_path, entry)
            log_debug(intern_dir, "LogModule.stop", f"BLOCK logged: {issues}")
            return

        # APPROVE：标记正常完成
        last_msg = state.get("_stop_last_message", "") or hook_input.get("last_assistant_message", "")
        entry = f"\n{'='*60}\n[{ts}] [SESSION_END] Turns: {log.get('turn_count', 0)}\n"
        if last_msg:
            entry += f"\n--- Final Response ({len(last_msg)} chars) ---\n{last_msg[:5000]}\n--- End ---\n"
        entry += f"{'='*60}\n"
        self._append(log_path, entry)

        log["session_completed"] = True
        log_debug(intern_dir, "LogModule.stop", f"session end written to {log_path}")

    # ── SubAgent boundaries ───────────────────────────────────
    def on_subagent_start(self, state, hook_input):
        """写 SubAgent 开始边界标记，含 prompt。"""
        log = state.get("log", {})
        log_path = log.get("log_path", "")
        if not log_path:
            return

        agent_id = hook_input.get("agent_id", "")
        agent_type = hook_input.get("agent_type", "")
        ts = datetime.now().strftime("%H:%M:%S")
        label = agent_type or agent_id or "unknown"

        # Get prompt from state (saved by pre_tool_hook) or hook_input
        prompt = state.pop("_pending_subagent_prompt", "")
        if not prompt:
            ti = hook_input.get("tool_input", {})
            if isinstance(ti, dict):
                prompt = ti.get("prompt", ti.get("description", ""))

        entry = f"\n[{ts}] === SubAgent Start ({label}) ==="
        if prompt:
            preview = prompt[:500] + ("..." if len(prompt) > 500 else "")
            entry += f"\n  prompt: {preview}"
        entry += "\n\n"
        self._append(log_path, entry)

    def on_subagent_stop(self, state, hook_input):
        """写 SubAgent 结束边界标记，含最终 transcript 提取。"""
        # 提取 SubAgent 最后的 assistant 文本
        transcript_path = hook_input.get("transcript_path", "")
        if transcript_path and os.path.exists(transcript_path):
            self._extract_assistant_texts(state, transcript_path)

        log = state.get("log", {})
        log_path = log.get("log_path", "")
        if not log_path:
            return

        ts = datetime.now().strftime("%H:%M:%S")

        # 写剩余的 assistant 文本
        new_texts = log.get("assistant_texts", [])
        text_logged = log.get("text_logged_count", 0)
        for t in new_texts[text_logged:]:
            self._append(log_path, f"[{ts}] [ASSISTANT]\n{t}\n\n")
        log["text_logged_count"] = len(new_texts)

        agent_id = hook_input.get("agent_id", "")
        self._append(log_path, f"[{ts}] === SubAgent End ({agent_id or 'unknown'}) ===\n\n")

    # ── Helper ────────────────────────────────────────────────
    @staticmethod
    def _append(log_path, content):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    @staticmethod
    def _render_enabled_skills(intern_dir):
        """task186/task220: 读当前 intern 类型对应的 skill 农场生成 [Enabled Skills] 段。

        farm 内每个条目是一个 symlink 指向源 skill 目录。读 SKILL.md frontmatter
        提取 description（首句）。失败/空时返回 ''。
        """
        import re
        intern_name = get_intern_name(intern_dir)
        intern_type = get_intern_type(intern_name)
        farm_rel = (".agents", "skills") if intern_type == "codex" else (".claude", "skills")
        farm = os.path.join(intern_dir, *farm_rel)
        if not os.path.isdir(farm):
            return ""
        try:
            entries = sorted(os.listdir(farm))
        except OSError:
            return ""
        if not entries:
            return ""
        repo_lines = []
        personal_lines = []
        for name in entries:
            link = os.path.join(farm, name)
            try:
                target = os.readlink(link) if os.path.islink(link) else link
            except OSError:
                target = link
            skill_md = os.path.join(link, "SKILL.md")
            desc = ""
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read(2048)
                m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                if m:
                    dm = re.search(r"^description:\s*(.+?)$", m.group(1), re.MULTILINE)
                    if dm:
                        desc = dm.group(1).strip().strip("'\"").split(".")[0].strip()
            except OSError:
                pass
            scope = "personal" if "/workspace/.skill_sources/personal/" in target or "/workspace/interns/" in target else "repo"
            line = f"    - {name:20s} \u2014 {desc}"
            (personal_lines if scope == "personal" else repo_lines).append(line)
        block = "=== Enabled Skills ===\n"
        if repo_lines:
            block += f"  Repo ({len(repo_lines)}):\n" + "\n".join(repo_lines) + "\n"
        if personal_lines:
            block += f"  Personal ({len(personal_lines)}):\n" + "\n".join(personal_lines) + "\n"
        block += "=== End Enabled Skills ===\n\n"
        return block

    @staticmethod
    def _extract_assistant_texts(state, transcript_path):
        """从 transcript JSONL 增量提取 assistant 文本。

        使用 common.transcript 共享解析器，LogModule 维护自己的 offset。
        """
        from common.utils import log_debug
        from common.transcript import extract_assistant_texts
        intern_dir = state.get("_intern_dir", "/tmp")
        log = state.setdefault("log", {})
        last_offset = log.get("transcript_offset", 0)
        last_transcript_path = log.get("transcript_path", "")

        if last_transcript_path and last_transcript_path != transcript_path:
            log_debug(intern_dir, "LogModule._extract",
                      f"transcript path changed, resetting offset")
            last_offset = 0
            log["assistant_texts"] = []
        log["transcript_path"] = transcript_path

        filter_before = log.get("session_start_utc") if last_offset == 0 else None
        if filter_before:
            log_debug(intern_dir, "LogModule._extract",
                      f"offset=0, filtering entries before {filter_before}")

        file_size = os.path.getsize(transcript_path) if os.path.exists(transcript_path) else 0
        log_debug(intern_dir, "LogModule._extract",
                  f"path={transcript_path} size={file_size} offset={last_offset}")

        if file_size < last_offset:
            log_debug(intern_dir, "LogModule._extract", f"file shrunk, resetting")
            last_offset = 0
            log["assistant_texts"] = []
            filter_before = log.get("session_start_utc")

        new_texts, new_offset, preview = extract_assistant_texts(
            transcript_path, last_offset, filter_before=filter_before)

        if preview:
            log_debug(intern_dir, "LogModule._extract",
                      f"new_content_len={new_offset - last_offset} first200={preview!r}")
        log_debug(intern_dir, "LogModule._extract",
                  f"new_texts_count={len(new_texts)}")
        log["transcript_offset"] = new_offset
        if new_texts:
            log.setdefault("assistant_texts", []).extend(new_texts)
