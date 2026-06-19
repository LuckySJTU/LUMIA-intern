"""Chinese (Simplified) message table for hook i18n.

Keys must stay in sync with messages_en.py. Locked by tests/test_i18n_keys.py.
Placeholders use {0}/{1} ordinals. Project rule 6: no cross-language fallback.
"""

MESSAGES: dict[str, str] = {
    # validation_module/module.py — Stop 后反馈给 LLM 的 issue 文案
    "validation.issue.invalidStatus": "⚠️ status.md 中的 STATUS 值 '{0}' 不合法，合法值为：{1}。请立即修正 METADATA 行。",
    "validation.issue.invalidTaskStatus": "⚠️ 任务 README 中的 STATUS 值 '{0}' 不合法，合法值为：{1}。任务完成必须写 Completed，不能写 Done。",
    "validation.issue.missingChecklist": "缺少 📋 Checklist",
    "validation.issue.missingIdentity": "缺少身份声明（我是 xxx）",
    "validation.issue.missingCurrentSummary": "缺少'本次'总结",
    "validation.issue.missingNextStep": "缺少'下步'计划",
    "validation.issue.escapeWord": "包含逃逸词: {0}",
    "validation.issue.sessionMismatch": "Session 号不匹配: 回复={0}, 预期={1}",
    "validation.issue.statusUnchanged": "status.md 未被修改：{0}",
    "validation.issue.historyUnchanged": "history_log.md 未被修改：{0}",
    "validation.issue.historyMissingSession": "history_log.md 未包含 Session {0} 记录：{1}",
    "validation.issue.knowledgeUnchanged": "task_knowledge.md 未被修改（如无需更新请标注 N/A）：{0}",
    "validation.issue.historyNoMetadata": "history_log.md 缺失 METADATA 行",
    "validation.issue.historyMultipleMetadata": "history_log.md 有 {0} 个 METADATA 行（应只有 1 个）",
    "validation.issue.historyDuplicateSession": "history_log.md Session 重复：{0}",

    # stop_hook.py — BLOCK 反馈给 LLM 的 Checklist 模板
    "stop.block.header": "你的回复缺少规范要求的内容：",
    "stop.block.headerSep": "。\n",
    "stop.block.fullFormat": "\n完整 Checklist 格式：\n",
    "stop.block.footer": "\n请补充完整后重新输出。",
    "stop.block.checklistTemplate": (
        "📋 Checklist:\n"
        "我是 {0}，当前任务：{1}，Session：{2}\n"
        "场景：<C - 工作中 | D - Working→Idle PR 已 merge>\n"
        "\n"
        "【Session 结束确认】\n"
        "- [x] 已 push\n"
        "- [x] {3} 已更新：<一句话>\n"
        "- [x] {4} 已更新：<一句话>\n"
        "- [x] {5} 已更新：<描述> / N/A\n"
        "\n"
        "本次：\n"
        "- <做了什么>\n"
        "\n"
        "下步：\n"
        "- <具体可执行>\n"
    ),
}
