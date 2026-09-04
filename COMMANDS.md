# 命令参考

本页说明当前 `nanobot` 的应用启动参数，以及用户可在 QQ 等聊天渠道发送的斜杠命令。

## 启动应用

```powershell
python -m nanobot [--config <path>] [--workspace <path>]
```

| 参数 | 作用 |
| --- | --- |
| `--config <path>` | 指定非敏感 JSON 配置文件。默认使用项目配置加载器定义的默认路径（通常为 `.nanobot/nanobot.json`）。 |
| `--workspace <path>` | 仅本次运行覆盖配置中的 workspace。Session、记忆、Cron 任务和 Skills 等运行时数据会使用该目录。 |

敏感凭据（例如 Provider API key、QQ app secret）应保留在本地 `.env`，不要作为命令行参数传入，也不要提交到仓库。

## 聊天命令

命令会在 Agent 调用模型之前由 `CommandRouter` 处理。命令名称不区分大小写，并会忽略首尾空格；未知或格式错误的 slash command 不会交给 LLM。

除 `/stop` 外，命令会与同一 session 的普通对话串行执行。命令本身不会写入 `Session.messages`，也不会生成长期记忆事件；`/goal` 在保存目标状态后会额外投递一个内部普通 turn，因此该 turn 会按常规流程写入会话和记忆事件。

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示当前已注册命令及简短说明。 |
| `/new` | 清空当前 session 的短期对话历史和摘要，开始新会话；不会删除 workspace 的长期记忆 `MEMORY.md`，也不会改变 session key。 |
| `/goal <objective>` | 为当前 session 创建并持久化一个 active goal。已有 active goal 时不会覆盖；已完成、失败或取消的目标可被替换。保存成功后，Agent 会在同一 session 中开始执行目标。 |
| `/stop` | 请求取消当前 session 正在运行的普通 Agent turn。没有活动 turn 时会明确提示。它不会取消后台 Subagent；请使用 `/subagents cancel`。 |
| `/compact` | 对当前 session 较早的完整对话轮次执行已有的摘要压缩；没有可压缩内容时只返回提示。 |
| `/memory` | 只读显示当前 workspace 的长期记忆 `MEMORY.md`。文件为空或不存在时返回提示；内容过长会截断。 |
| `/subagents` | 列出当前 session 创建的后台 Subagent 任务，包含任务 ID、状态和任务描述。 |
| `/subagents status <task_id>` | 查看当前 session 指定后台任务的状态，并在可用时显示结果摘要或错误信息。 |
| `/subagents cancel <task_id>` | 取消当前 session 中仍处于 `pending` 或 `running` 状态的后台任务。终态任务不能再次取消。 |

### 后台 Subagent 状态

`/subagents` 相关任务仅在当前 Agent 进程内保存，应用重启后不会恢复。状态含义如下：

| 状态 | 含义 |
| --- | --- |
| `pending` | 已创建，等待事件循环开始执行。 |
| `running` | 子 Agent 正在执行。 |
| `completed` | 子 Agent 已完成；Manager 会将结果作为内部消息投递回主 Agent。 |
| `failed` | 子 Agent 执行失败；Manager 会尝试投递失败通知。 |
| `cancelled` | 被 `/subagents cancel` 或应用关闭取消；不会再发布成功结果。 |
| `timeout` | 超过后台子任务的运行时限；Manager 会尝试投递无法及时完成的内部通知。 |

后台子任务由主 Agent 通过 `spawn` 工具创建：`wait=true` 会同步等待结果，`wait=false` 会立即返回任务 ID，并在完成后通过现有 `MessageBus → AgentLoop` 链路发回同一 session。

## 会话范围

所有聊天命令都以当前 session 为边界：优先使用消息携带的 `session_id`；如果为空，则使用 `channel:chat_id`。因此一个会话无法通过 `/subagents` 查看或取消另一个会话创建的后台任务。
