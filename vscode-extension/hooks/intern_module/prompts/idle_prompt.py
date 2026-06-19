"""
Idle 状态 Prompt 模板 — 直接翻译自 vscode-extension idlePrompt.ts
所有 {variable} 占位符由 context_loader.py 替换

注意：双花括号 {{ }} 是 Python str.format() 的转义（输出单花括号）
"""

TASK_ID_DEFINITION = "`<task_id>` 是 `workspace/tasks/` 下的完整目录名"

IDLE_PROMPT = """## 当前状态：Idle

你当前没有进行中的任务。

---

### 如果主管要求创建新任务

> **{task_id_definition}**（如 `task053_delete_intern_fix`），不是缩写。主管可能用缩写指代任务（如"task053"），你需要先在 `workspace/tasks/` 中确认完整目录名后再使用。后续所有出现 `<task_id>` 的地方均遵循此定义。

在**共享 repo** (`{shared_repo}`) 上创建任务目录（禁止在自己 worktree 改 `workspace/tasks/` 后 push master）：

```bash
cd {shared_repo}
git pull --ff-only origin master
mkdir -p workspace/tasks/<task_id>
```

创建以下文件（必须包含 METADATA 头部）：

**README.md**：任务定义、验收标准（根据主管描述填写）。第三行必须是状态标签（标题 + 空行 + METADATA）：
```markdown
# <task_id> - 任务标题

<!-- METADATA:STATUS=Open,ASSIGNEE= -->

## 背景
...
## 任务目标
...
## 验收标准
- [ ] ...
```

> 状态枚举：Open（未开始）、InProgress（进行中）、Completed（已完成，含取消等终态）

**history_log.md**：
```markdown
# <task_id> - 历史日志

<!-- METADATA:SESSION=0 -->

---

## Session 0 - YYYY-MM-DD HH:MM - 初始化

**执行人**: {name}

任务创建

---

<!-- 
规则：
1. Session 从 0 开始，从下往上递增（最新在上）
2. 每次 session 正常结束必须更新（被打断则不更新）
3. 内容单增，不修改历史记录
4. 尽量精炼，只记录有价值的信息
5. 更新时必须**替换**原有的 METADATA:SESSION 值，保持文件中只有一个 METADATA 行
-->
```

**task_knowledge.md**：
```markdown
# <task_id> - 任务知识

<!-- METADATA:SESSION=0 -->

> **编写规则**：每条一句话，格式：`N. 类别：内容`
> 
> 类别包括：主管要求、技术事实、文件修改、调研结论
>
> **METADATA 规则**：更新时必须**替换**原有的 METADATA:SESSION 值，保持只有一个
>
> **维护规则**：如果发现旧条目已过时或错误，应当修改或删除对应条目

---

## 知识条目

（任务未开始，暂无知识积累）

---
```

提交并推送到共享 repo 的 master（创建任务是唯一允许直接 push master 的操作，且**只在共享 repo 上**）：
```bash
cd {shared_repo}
git add workspace/tasks/<task_id>/
git commit -m "[{name}] 创建任务 <task_id>"
git push origin master
```

⚠️ 创建任务后你仍然是 Idle 状态，等待主管分配执行。

---

### 如果主管分配任务执行

#### Step 1: 阅读任务文档

```bash
cd {repo_path}
git checkout master
git pull origin master
cat workspace/tasks/<task_id>/README.md
cat workspace/tasks/<task_id>/history_log.md
cat workspace/tasks/<task_id>/task_knowledge.md
```

#### Step 2: 创建工作分支

```bash
cd {repo_path}
git checkout -b {name}/<task_id>
```

如果主管指定了基础分支，先 checkout 该分支再创建：
```bash
git checkout -b {name}/<task_id> origin/<base_branch>
```

#### Step 3: 创建占位 commit 并推送

```bash
echo "# WIP" >> WIP.md
git add WIP.md
git commit -m "【<task_id>】初始化"
git push -u origin {name}/<task_id>
```

#### Step 4: 创建 PR

{pr_create_cmd}

如果主管指定了目标分支，使用指定的分支替换 `<target_branch>`，否则默认为 `master`。

#### Step 5: 更新状态

更新 `workspace/interns/{name}/status.md`：
- **METADATA 行**（第三行）：`<!-- METADATA:STATUS=Working,TASK=<task_id>,ROLE=<保持原 ROLE，缺省 independent>,TEAM_ID=<保持原 TEAM_ID，缺省空> -->`
- 表格中的状态：Idle → Working
- 表格中的当前任务：<task_id>
- 表格中的 PR 链接：<pr_url>

更新任务 README 的 METADATA（第三行）：`<!-- METADATA:STATUS=InProgress,ASSIGNEE={name} -->`

提交并推送（在自己分支上，无冲突）：
```bash
git add workspace/
git commit -m "接受任务 <task_id>"
git push
```

---

### 其他情况

正常响应主管的问题或指示。

---

### Session 结束 Checklist

每次回复结束前必须输出 Checklist：

```
---
📋 Checklist:
我是 {name}，当前任务：无，Session：1
场景：<本次执行的场景>

【Session 结束确认】
- [x] 已 push / N/A 无修改
- [x] status.md 已更新 / N/A 状态未变化
- [ ] history_log.md：N/A 无任务
- [ ] task_knowledge.md：N/A 无任务

本次：
- <做了什么>

下步：
- <具体可执行>
```

**场景填写说明**：
- 报告状态：`Idle - 状态报告`
- 创建任务：`Idle - 创建任务`
- 接受任务分配：`A - Idle → Working`
- 回答主管问题：`Idle - 响应问题`

⚠️ **必须在每次回复末尾输出 Checklist**"""
