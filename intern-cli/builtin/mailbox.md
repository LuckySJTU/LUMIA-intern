# mail to - worker 向 team_lead 汇报

Mailbox 是绑定到 intern 的异步收件箱。worker 用 `mail to` 给自己的 team_lead 汇报时，daemon 会写入目标 intern 的 mailbox；跨 daemon 时通过 relay 转发到目标 daemon 后写入。mail 不会打断 team_lead 当前 turn，也不会直接注入对话。

## mail to

先读取本机 daemon HTTP 端口。不要使用 `ws_port`：

```bash
DAEMON_HTTP_PORT="$(python3 - <<'PY'
import json
import os
path = os.environ["FEISHU_DAEMON_ADDR_FILE"]
with open(path, "r", encoding="utf-8") as f:
    print(json.load(f)["http_port"])
PY
)"
```

```text
POST /api/intern/mail/to
```

示例：

```bash
curl -sS -X POST "http://127.0.0.1:${DAEMON_HTTP_PORT}/api/intern/mail/to" \
  -H 'Content-Type: application/json' \
  --data '{"from_intern_name":"intern_runtime_worker_1","to_intern_name":"intern_runtime_lead","to_project":"axis_intern_agents","team_id":"runtime","kind":"progress","content":"Implemented parser changes; focused tests pass locally.","related_task":"task_123","related_pr":"https://codeup.aliyun.com/...","client_message_id":"optional-stable-id"}'
```

```json
{
  "from_intern_name": "intern_runtime_worker_1",
  "to_intern_name": "intern_runtime_lead",
  "to_project": "axis_intern_agents",
  "team_id": "runtime",
  "kind": "progress",
  "content": "Implemented parser changes; focused tests pass locally.",
  "related_task": "task_123",
  "related_pr": "https://codeup.aliyun.com/...",
  "client_message_id": "optional-stable-id"
}
```

成功返回：

```json
{
  "status": "stored",
  "message_id": "optional-stable-id",
  "team_id": "runtime",
  "read_state": "unread"
}
```

## list

```text
POST /api/team/mailbox/list
```

也支持：

```text
POST /api/intern/mailbox/list
```

```json
{
  "intern_name": "intern_runtime_lead",
  "project": "axis_intern_agents",
  "include_read": false
}
```

默认只返回 unread messages；`include_read=true` 返回全部。

## mark-read

```text
POST /api/team/mailbox/mark-read
```

也支持：

```text
POST /api/intern/mailbox/mark-read
```

```json
{
  "intern_name": "intern_runtime_lead",
  "project": "axis_intern_agents",
  "message_ids": ["msg_1"]
}
```

也支持单条 `message_id`。

## 规则

- sender 必须是目标 team_lead 管理的 active worker；传入 `team_id` 时只在该 team 内校验，不传时从目标项目的 active teams 中解析。
- worker 不使用 peer send 主动联系 team_lead；被拒绝时 reason 为 `worker_to_team_lead_use_mailbox`。
- mailbox 持久化到目标项目的 `workspace/interns/<to_intern_name>/mailbox.json`。
- 每一次 team_lead 发 peer send 前，必须先阅读并处理自己的 unread mail，处理后调用 mark-read；`/api/intern/mailbox/*` 是本人调用语义，请使用 `intern_name` 指定自己的 mailbox。
- team_lead 指派 tester worker 后，tester 也用 mailbox 回报测试/验证命令、结果、环境和未覆盖风险；team_lead 基于实现 worker 的代码/PR 与 tester 报告做 merge 决策。
- mailbox JSON 的 append 和 mark-read 都通过同一路径的 `.lock` 文件串行化读改写，并用临时文件 `os.replace` 原子替换。
- 非管理关系返回 `not_managed_worker`；未知 team 返回 `unknown_team`。
