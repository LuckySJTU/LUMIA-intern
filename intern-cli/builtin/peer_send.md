# peer_send — intern 之间点对点对话

A intern 向本机 daemon 的 `/api/intern/peer/send` 发文本给 B intern；daemon 同步返 transport 层送达回执。

## 请求体

```json
{
  "from_intern_name": "<your_name>",
  "to_intern_name":   "<peer_name>",
  "to_project":       "<peer_proj>",     // 可选；省略时由 relay 解析，多候选返 ambiguous_target+candidates 让你选
  "mode":             "default",         // 必填；default|next|goal|stop
  "content":          "...",             // ≤4KB；"/esc" 是特殊命令打断 B 当前 turn
  "attachments": [                          // 可选；仅 cli intern 收件支持
    {"kind": "image|file", "filename": "...", "bytes_b64": "..."}
  ]
}
```

## mode

- `default`: 直接送入目标 Claude/Codex/Copilot 入口；Claude/Codex 忙时由 CLI pending input 接管，不返回 busy。
- `next`: Claude/Codex 当前 turn 未完成时先进入目标 daemon 队列，空闲后再发起下一条消息；不打断当前 turn。
- `goal`: 兼容旧调用的 legacy goal 通道。新调用请使用 `/api/intern/goal/set` 或 `/api/intern/goal/cancel`，它们独立于 peer 文本通道。
- `stop`: 立刻向 Claude/Codex tmux pane 发送 Escape；`content` 可为空。Copilot 暂不支持。

## role contract

- `independent` 只能和 `independent` 互发 peer send；`independent` 与 team 三角色互发会被拒绝，返回 `team_only_accepts_supervisor_tasks_via_coordinator`，message 为 `team只允许coordinator从主管接受任务`。
- `coordinator -> team_lead` 允许 `default`、`next`、`stop`。
- `team_lead -> coordinator` 只允许 `default`。
- `team_lead -> worker` 允许 `default`、`next`、`stop`；分配新实现任务时，team_lead 必须先创建 `workspace/tasks/<task_id>/` 标准 task 文档，再用 peer send 通知 worker 接受该 task。
- `worker -> team_lead` 不走 peer send，返回 `worker_to_team_lead_use_mailbox`。
- `coordinator -> worker`、`worker -> coordinator`、team role 同角色之间会被拒绝。

## 响应

- `{"status": "delivered"}` / `{"status": "delivered", "kind": "queued|goal|stop|esc"}`
- `{"status": "undeliverable", "reason": "<X>"}`，X ∈ `offline` / `tmux_session_missing` / `session_not_running` / `tmux_send_failed` / `unknown_target` / `ambiguous_target`（附 candidates）/ `unsupported_target` / `unsupported_mode` / `unsupported_attachment_target` / `goal_same_daemon_project_required`（legacy `goal` 只能同 daemon、同 project 投递）/ `relay_unreachable` / `source_outdated`（发送方 daemon 太旧，跨机请求没有 mode 或 role contract 字段，需要升级）/ `target_outdated`（接收方机器插件太旧，不支持 peer、peer mode 或 role contract；daemon 会给主管发飞书提示升级）/ `team_only_accepts_supervisor_tasks_via_coordinator` / `worker_to_team_lead_use_mailbox` / `coordinator_to_worker_use_team_lead` / `worker_to_coordinator_use_team_lead` / `same_role_team_channel_not_supported` / `unsupported_mode_for_team` / `not_same_team` / `role_not_allowed` / `coordinator_goal_requires_goal_api`
- 对 `offline` / `tmux_session_missing` / `session_not_running` / `tmux_send_failed`，响应会附加 `message` 和 `remediation`。若目标与发送方同机，`remediation.action=restart_session_via_daemon`，发送方可调用本机 daemon/session restart 能力尝试重启目标 session 后重试；若不同机，`remediation.action=notify_supervisor`，发送方应通知主管协助在目标机器修复。
- HTTP 400 — `invalid_from` / `content_empty` / `content_too_long` / `self_send` / `missing_field` / `invalid_mode`

## 注意

- `delivered` 只表示消息到达目标 intern 的 transport 入口，不表示目标 LLM 已经读完、开始处理或会回复。
- 对 Claude/Codex tmux intern，`delivered` 表示 daemon 已成功把文本写入目标 tmux pane、排入 next 队列或发送控制按键；它不等待 Codex transcript 写入，也不检查目标 LLM 是否已生成回复。
- 对 Copilot intern，`delivered` 表示消息已推给当前 active 的 VS Code window。
- B 处理完**可能**反向调同接口给你发回复。
