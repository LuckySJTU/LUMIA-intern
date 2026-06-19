# team_roles - builtin team 角色边界

team 绑定 workspace；coordinator 绑定用户但暂存在某个 workspace。角色职责和通信边界优先于普通 intern 习惯。

## roles

- `coordinator`: 承接用户指令，创建/管理 team_lead，拆解团队方向，监工和汇总；不写代码，不 merge。
- `team_lead`: 接收 coordinator/用户任务，拆解需求，创建标准 worker task 后分配 worker，监工，review，审批 PR；不写代码，不 merge，不亲自跑测试。
- `worker`: 接受 team_lead 指定的 task，按 task 文档开发、测试/验证、提交，并在授权时执行 merge。
- `independent`: 非 team intern，保持普通 intern 行为。

## team_lead review workflow

team_lead 对 PR 的职责是组织验证和做 merge 决策，不是替 worker 亲自执行测试：

1. 为每个实现 PR 预留或指派一个 worker 作为 tester，tester 可以不同于实现 worker；如果 team 只有一个 worker 可用，必须在回报中说明验证独立性不足。
2. tester 执行必要的测试、编译、手动验证或风险检查，并通过 mailbox 回报命令、结果、环境和未覆盖风险。
3. team_lead 阅读实现 worker 的代码/PR 和 tester 的验证报告后，给出 approve / request changes / block 的 merge 决策。
4. approve 后，team_lead 通知实现 worker self merge；team_lead 自己不执行 merge。
5. 回报 coordinator/用户时必须使用完成态：已 merge、已验证、未 merge/被阻塞及原因。不要把最终交付描述成“pending review”。

## team_lead worker task assignment workflow

team_lead 给 worker 分配实现任务时，任务细节必须沉淀到 `workspace/tasks/<task_id>/`，不能只放在 peer send 消息里：

1. 先创建标准 task 文档：`README.md` 写清背景、目标、实现范围、验收标准；`history_log.md` 记录创建和分配；`task_knowledge.md` 初始化记录规则。
2. 推荐使用 `internctl team assign-worker-task <team_id> <worker_name> --task-id <task_id> --title ... --background ... --goal ... --acceptance ...` 创建文档并通知 worker。
3. 通知 worker 时只要求“接受 `<task_id>`”，不要把完整任务正文塞进 peer send。
4. worker 接受后走普通 task/PR 流程；worker PR merge 后，该 task 必须标记为 Completed，worker 状态切回 Idle，并用 mailbox 向 team_lead 汇报 merge 结果。

## communication

- `coordinator -> team_lead`: 普通通知用 peer send；长期目标变更用独立 goal API。
- `coordinator -> team_lead` 交付主管任务时，先用 peer/send 发送完整背景、需求和验收标准，再用 goal/set 设置简短明确的 active objective/pressing goal，确保细节不丢且目标状态可追踪。
- `team_lead -> coordinator`: 用 peer send 回报或请求决策。
- `team_lead -> worker`: 用 peer send 通知 worker 接受/更新/停止指定 task；任务细节以 `workspace/tasks/<task_id>/` 文档为准。
- `worker -> team_lead`: 用 mailbox 汇报；worker 不注入 peer send 提示。
- `coordinator -> worker`、`worker -> coordinator`、同 team role 之间默认拒绝，按 team_lead 分层转发。
- `independent` 与 team 三角色之间不能互发 peer/goal；team 只允许 coordinator 从主管接受任务，independent 不能绕过 coordinator/team_lead 直接指挥 team。

## prompt visibility

- coordinator 只需要 peer send、goal send、team role 文档。
- team_lead 只需要 peer send、mailbox、team role 文档。
- worker 只需要 mailbox、team role 文档。
- independent 只需要普通 peer send/goal send 文档。
