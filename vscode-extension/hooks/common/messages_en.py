"""English message table for hook i18n.

Keys must stay in sync with messages_zh_cn.py. Locked by tests/test_i18n_keys.py.
Placeholders use {0}/{1} ordinals. Project rule 6: no cross-language fallback.
"""

MESSAGES: dict[str, str] = {
    # validation_module/module.py — Stop issue strings fed back to the LLM
    "validation.issue.invalidStatus": "⚠️ STATUS value '{0}' in status.md is invalid; allowed values: {1}. Please fix the METADATA line immediately.",
    "validation.issue.invalidTaskStatus": "⚠️ STATUS value '{0}' in task README is invalid; allowed values: {1}. Completed tasks must use Completed, not Done.",
    "validation.issue.missingChecklist": "Missing 📋 Checklist",
    "validation.issue.missingIdentity": "Missing identity declaration (I am xxx)",
    "validation.issue.missingCurrentSummary": "Missing 'This turn:' summary",
    "validation.issue.missingNextStep": "Missing 'Next:' plan",
    "validation.issue.escapeWord": "Contains escape phrase: {0}",
    "validation.issue.sessionMismatch": "Session number mismatch: reply={0}, expected={1}",
    "validation.issue.statusUnchanged": "status.md was not modified: {0}",
    "validation.issue.historyUnchanged": "history_log.md was not modified: {0}",
    "validation.issue.historyMissingSession": "history_log.md does not contain Session {0} entry: {1}",
    "validation.issue.knowledgeUnchanged": "task_knowledge.md was not modified (mark N/A if no update needed): {0}",
    "validation.issue.historyNoMetadata": "history_log.md is missing the METADATA line",
    "validation.issue.historyMultipleMetadata": "history_log.md has {0} METADATA lines (should be exactly 1)",
    "validation.issue.historyDuplicateSession": "history_log.md duplicate Session: {0}",

    # stop_hook.py — BLOCK feedback to LLM with Checklist template
    "stop.block.header": "Your reply is missing required content: ",
    "stop.block.headerSep": ".\n",
    "stop.block.fullFormat": "\nFull Checklist format:\n",
    "stop.block.footer": "\nPlease re-output with the missing content added.",
    "stop.block.checklistTemplate": (
        "📋 Checklist:\n"
        "I am {0}, current task: {1}, Session: {2}\n"
        "Scenario: <C - working | D - Working→Idle PR merged>\n"
        "\n"
        "[Session end confirmation]\n"
        "- [x] Pushed\n"
        "- [x] {3} updated: <one line>\n"
        "- [x] {4} updated: <one line>\n"
        "- [x] {5} updated: <description> / N/A\n"
        "\n"
        "This turn:\n"
        "- <what was done>\n"
        "\n"
        "Next:\n"
        "- <concrete actionable items>\n"
    ),
}
