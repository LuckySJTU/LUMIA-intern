# goal_send - intern 之间设置或取消 pressing goal

A intern 向本机 daemon 的 `/api/intern/goal/set` 或 `/api/intern/goal/cancel` 发目标指令给 B intern；daemon 同步返 transport 层送达回执。这个接口专用于 goal，不再复用 `peer_send` 的普通消息通道。

## 请求体

```json
{
  "from_intern_name": "<your_name>",
  "to_intern_name":   "<peer_name>",
  "to_project":       "<peer_proj>",
  "content":          "把这个内容设置为 pressing goal",
  "client_goal_id":   "optional-stable-id"
}
```

## API

- `POST /api/intern/goal/set`: 清掉目标 CLI 里已有 goal，再设置 `content` 为新的 pressing goal。Codex 目标会在短确认窗口内等待 transcript ack，并在 ack 缺失时检查 tmux panel 中的 goal 可见状态；两者都确认失败时返回 `unconfirmed`。调用方不得把 HTTP timeout 当成成功或失败；新 daemon 会尽量在确认窗口内返回明确状态。
- `POST /api/intern/goal/cancel`: 清掉目标 CLI 当前 goal；`content` 可省略或为空。

## 边界

- 支持同 daemon 本地投递，也支持通过 relay 跨 daemon 投递。
- `to_project` 可省略；省略时由 relay 按 `to_intern_name` 解析，多个候选返回 `ambiguous_target`。
- `coordinator -> team_lead` 允许 goal set/cancel。
- `independent -> independent` 只允许同项目、同 daemon 的 goal set/cancel。
- `independent` 与 team 三角色之间的 goal set/cancel 会被拒绝，返回 `team_only_accepts_supervisor_tasks_via_coordinator`，message 为 `team只允许coordinator从主管接受任务`。
- 其他 role path 的 goal set/cancel 会被拒绝。
- Copilot 和非 tmux intern 返回 `unsupported_target`。
- 目标 daemon 太旧、不支持 goal API 时返回 `target_outdated`。
- `delivered` 表示目标 tmux 已收到 `/goal ...` 或 `/goal clear` 控制命令，且 goal set 已通过 transcript ack 或 panel 二次检查确认；不表示目标 LLM 已完成 goal。
- 调用方必须同时检查 HTTP status 和 JSON body。HTTP 2xx 只用于 `status=delivered`；`status=undeliverable` 会使用非 2xx 状态码并在 body 中给出稳定 `reason`。

## 响应

- `{"status": "delivered", "kind": "goal", "goal_id": "<id>"}`
- `{"status": "delivered", "kind": "goal_cancel", "goal_id": "<id>"}`
- `{"status": "undeliverable", "reason": "<X>"}`，X ∈ `offline` / `tmux_session_missing` / `session_not_running` / `tmux_send_failed` / `unknown_target` / `ambiguous_target` / `unsupported_target` / `unsupported_action` / `relay_unreachable` / `target_outdated` / `source_outdated` / `unconfirmed` / `team_only_accepts_supervisor_tasks_via_coordinator` / `goal_independent_same_daemon_required` / `unsupported_goal_target` / `coordinator_to_worker_use_team_lead` / `worker_to_team_lead_use_mailbox` / `same_role_team_channel_not_supported`
- 对 `offline` / `tmux_session_missing` / `session_not_running` / `tmux_send_failed`，响应会附加 `message` 和 `remediation`。若目标与发送方同机，`remediation.action=restart_session_via_daemon`，发送方可调用本机 daemon/session restart 能力尝试重启目标 session 后重试；若不同机，`remediation.action=notify_supervisor`，发送方应通知主管协助在目标机器修复。
- HTTP 400 - `invalid_from` / `content_empty` / `content_too_long` / `self_send` / `missing_field` / `invalid_action`
- HTTP 404 - `unknown_target`
- HTTP 409 - goal 未设置成功或被 team/role/scope 规则拒绝，例如 `unconfirmed`、`ambiguous_target`、`session_not_running`、`target_outdated`
- HTTP 503 - `relay_unreachable`
