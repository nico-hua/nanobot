# 项目开发进度

这是一个从零实现的 Agent 架构学习项目。目标是用较小、清晰、可测试的代码理解 Agent 的核心边界，而不是完整复刻生产系统。

## 项目开发进度

### 2026-08-28 之前（早期记录未标注具体日期）

- [x] 建立厂商无关的 LLM 抽象：`LLMProvider`、`LLMResponse`、`ToolCallRequest`、`TokenUsage`、`ProviderError` 与统一消息类型（system、user、assistant、tool）。Provider 支持 `complete`、`stream`、工具定义、`max_tokens`、`temperature` 和流式文本回调。
- [x] 实现 OpenAI-compatible 与 Anthropic-compatible Provider，覆盖普通调用、流式文本、工具调用、结束原因、Token 使用量和异常包装；增加默认跳过的 DeepSeek live smoke tests。
- [x] 建立工具基础设施：`ToolParameter`、`Tool`、`ToolResult`、`ToolContext`、`ToolRegistry` 与 `ToolLoader`。工具可转换为 OpenAI function calling 和 Anthropic tool use schema，注册表负责稳定顺序、参数校验和异步执行。
- [x] 实现 workspace 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`exec`。文件工具复用 workspace 边界校验；`ExecTool` 支持工作目录、最小环境、超时、进程清理和输出截断。
- [x] 实现 MCP tools 接入：支持 stdio、SSE、Streamable HTTP；`MCPProvider` 连接服务端后将 `MCPToolWrapper` 动态注册到共享 `ToolRegistry`。已覆盖 mock transport 与本地 FastMCP stdio 集成测试；当前仅处理 tools 的文本结果。
- [x] 实现最小非流式 `AgentRunner`、`AgentRunSpec` 与 `AgentRunResult`：模型工具调用按顺序执行，assistant/tool 消息回传模型直至获得最终文本。

### 2026-08-28

- [x] 完成配置分层：本地 `.nanobot/nanobot.json` 统一保存后端 Agent 配置，包括 Provider API key 与 QQ 凭据；根目录 `.env` 仅保留 Web UI 的 `VITE_*` 浏览器配置。真实配置文件已被 Git 忽略，仓库提供 `.nanobot/nanobot.example.json`；启动时仅解析和校验 `channel.default` 选中的 Channel 配置，未选中 Channel 可保留未完成配置。
- [x] 实现 `Application` 与 `python -m nanobot` CLI：组装共享 `MessageBus`、Provider、工具、MCP、`AgentLoop` 与默认 Channel；处理 SIGINT/SIGTERM、后台任务监督、启动失败清理和幂等关闭。
- [x] 建立 `MessageBus`、`BaseChannel`、`FakeChannel`、`ChannelManager` 和 QQ 文本 Channel。QQ 支持 C2C 与群聊 @ 消息、原生 Markdown 输出、`allow_from` 白名单，qq-botpy 文件日志默认关闭。
- [x] 建立统一日志规范与初始化流程：CLI/宿主初始化包级日志，运行时模块使用模块级 logger，不记录消息内容、工具参数、文件内容或凭据；关键异常边界保留 traceback，取消信号继续传播。

### 2026-08-31

- [x] 实现 `Session`、`SessionManager` 和 JSONL 持久化存储。每个 session 使用安全文件名并通过临时文件 + 原子替换保存；默认目录为 `<workspace>/sessions/`，重启后可恢复完整 user、assistant、tool call 与 tool result 历史。
- [x] 将 Session 接入 `AgentLoop`：非空 `session_id` 优先作为 session key，否则使用 `channel:chat_id`；当前用户消息在模型调用前保存，成功后按顺序保存本轮 assistant/tool 消息。system prompt 不写入 Session。
- [x] 实现 `ContextBuilder`：每次请求动态读取 workspace 的 `AGENTS.md`、`SOUL.md`、`USER.md`，并注入身份与 workspace 信息。
- [x] 实现历史裁剪与稳定 Token 估算：文本、消息结构、tool call 与工具 schema 都计入估算；system prompt、summary、当前用户消息和输出预留先占用总窗口，剩余预算仅用于完整历史轮次，避免拆开 tool call/tool result。
- [x] 实现 `SessionCompactor`：AgentRunner 成功且完整消息保存后异步压缩较早完整轮次，持久化 `summary` 与 `summary_until`，保留完整原始历史和最近原始消息；摘要以 `## Conversation Summary` 合并进当轮 system prompt。
- [x] 实现长期记忆第一、二阶段：`MemoryStore` 动态读取 `<workspace>/memory/MEMORY.md`；`MemoryConsolidator` 通过一次专用 Provider 请求生成完整替换式记忆内容，只在有效非空结果下原子写入。

### 2026-09-01

- [x] 完成 workspace 级持久化长期记忆事件队列：已保存轮次的不可变 user/assistant/tool 消息快照写入 `<workspace>/memory/history.jsonl`；system prompt 与 Session summary 不进入队列。
- [x] 增加 `<workspace>/memory/.memory_cursor`。仅当记忆整理成功或模型明确返回“无记忆可写”时推进 cursor；Provider 或写入失败保留 cursor，以便重启后重试。`MemoryEventConsumer` 按事件 ID 合并待处理事件并串行消费，避免同一 workspace 并发写入 `MEMORY.md`。
- [x] 将记忆队列接入 AgentLoop：正常对话在 Session 保存和事件落盘后非阻塞唤醒消费者；AgentLoop 启动时继续消费 cursor 之后的遗留事件。失败、取消、ephemeral、system、slash command 和记忆整理来源的消息不会产生事件。
- [x] 实现命令路由：`/new`、`/stop`、`/help`、`/compact`、`/memory` 由 `CommandRouter` 处理，不进入 LLM、普通 Session 历史或记忆事件队列。`/stop` 不等待 session lock，其余命令与普通 turn 串行化。
- [x] 实现 Skills：仅扫描 `<workspace>/skills/<skill_name>/SKILL.md`；支持 frontmatter 的 name、description、always 与 `nanobot.requires.bins/env`。always Skill 注入完整正文，普通 Skill 仅注入摘要；`$skill-name` 可显式激活当前轮完整正文。Skill 只读，不执行命令、脚本或安装依赖。

### 2026-09-02

- [x] 完成 `nanobot/cron/`：`CronTask` 由 `CronSchedule`、`CronPayload`、`CronJobState` 组成；支持一次性与周期任务、UTC 毫秒时间戳、`Asia/Shanghai` 默认时区、统一 callback、状态记录和 `<workspace>/cron/tasks.json` 原子持久化。
- [x] 定时任务经 `CronMessagePublisher` 转换为普通 `InboundMessage`，携带原 session/channel/chat/sender 路由和 `source=cron`，继续走完整 Agent、Session、摘要与记忆流程。过期周期任务在重启后补跑一次；一次性任务完成后禁用。
- [x] 增加 `RequestContext`（ContextVar）：AgentLoop 在调用 AgentRunner 前绑定可信 session/channel/chat/sender/metadata，工具从上下文读取路由信息，模型参数不能覆盖这些字段。
- [x] 实现 `CronTool`：支持当前 session 范围内的 `add`、`list`、`remove`；任务创建时自动绑定 RequestContext 路由。`CronService` 和时区通过 `ToolContext` 注入。
- [x] QQ 将 `source=cron` 视为主动消息：保留聊天类型但不复用旧 `message_id`，避免 QQ 的 `msg_seq` 去重。
- [x] 记忆整理明确 `UPDATED`、`SKIPPED`、`FAILED` 结果；整理内容限制为 User Information、Preferences、Project Context、Important Notes 四类。

### 2026-09-03

- [x] 实现第一阶段 `SubagentManager`：子 Agent 只接收独立 system prompt 和 task，不继承主 Agent 历史、不修改主 Session；使用独立工具注册表，默认不加载 `SpawnTool`，避免递归 Spawn。
- [x] `ContextBuilder.build_subagent_system_prompt()` 复用当前 Skill 区块，提供 Subagent 身份与 workspace 信息；`Application` 组装主、子 Agent 各自的工具环境，并通过 `ToolContext.subagent_manager` 启用主 Agent 的 Spawn 能力。
- [x] 实现 `SpawnTool` 同步与后台模式：`wait=true`（默认）等待子 Agent 并作为普通工具结果返回；`wait=false` 立即返回 task ID，`SubagentManager` 跟踪后台任务并在结束后经 `MessageBus` 投递新的内部 turn。子结果不伪装为原 tool call 的补充结果。
- [x] 后台子任务保留原 session/channel/chat/sender 与必要 metadata；完成或失败后由 AgentLoop 正常构建上下文、调用主 Agent 并产生最终 `OutboundMessage`。关闭 AgentLoop 或 SubagentManager 会取消已跟踪任务。
- [x] 修复 QQ 后台子任务回传：保留 `qq_chat_type`，并将 `source=subagent` 与 cron 一样按主动消息发送，不复用旧 `message_id`，避免路由缺失和 QQ 去重。
- [x] 最近一次全量离线测试：`340 passed, 7 skipped`。真实 Provider/QQ live tests 默认跳过；设置 `NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=1` 且提供本地凭据后才会访问真实服务。
- [x] 离线测试目录为 `tests/`；完整测试命令为 `python -B -m unittest discover -s tests -t . -p "test*.py"`。

### 2026-09-04

- [x] 完善后台 `SubagentManager` 生命周期：任务记录保留在内存中，支持 `pending`、`running`、`completed`、`failed`、`cancelled` 与 `timeout` 状态，并保存任务描述、所属 session、路由信息、创建/结束时间、错误和结果摘要。任务只会进入一次终态，成功、失败和超时结果最多通过 `MessageBus` 回传一次；取消后不会再发布成功结果。
- [x] 为后台子任务增加默认运行时策略：最多并发 4 个任务、默认超时 300 秒；支持按任务 ID 查询、按 session 列出、按 session 取消，以及 AgentLoop 关闭时统一取消和等待已有任务。同步 `SpawnTool(wait=true)` 保持原有行为。
- [x] `CommandRouter` 新增 `/subagents`、`/subagents status <task_id>` 和 `/subagents cancel <task_id>`。命令只处理当前 session 的内存任务，不进入 LLM、Session 普通消息或记忆事件队列；无法访问其他会话任务，终态任务不能再次取消。
- [x] 新增 `COMMANDS.md`，集中说明应用启动参数和当前聊天渠道的斜杠命令。最近一次完整离线测试为 `346 passed, 7 skipped`。
- [x] 增加 Session 独立 `goal_state`：`GoalState` 持久化 active、completed、cancelled、failed 状态、目标与时间边界，不使用通用 Session metadata。`/goal <objective>` 会保存或替换终态目标；已有 active goal 时不覆盖。
- [x] `/goal` 保存成功后向共享 `MessageBus` 发布带 `source=goal` 的内部 `InboundMessage`，使用当前 Session 上下文和可用工具启动一次普通 Agent turn。QQ 将 goal 结果作为主动消息发送，不复用旧 `message_id`。自动持续续跑仍留待后续阶段。
- [x] 完善目标控制命令：`/goal status` 只读返回当前目标状态，`/goal stop` 将 active goal 持久化为 `cancelled`；两者均不调用 LLM。`/new` 在存在 active goal 时拒绝重置并提示先完成或停止目标，其他情况下会同时清空短期会话状态和终态目标。
- [x] 整理内置文件工具：`ReadFileTool`、`WriteFileTool`、`EditFileTool` 和 `ListDirTool` 合并到 `tools/builtin/filesystem.py`，保留原有工具接口、UTF-8 处理、路径安全校验与错误约定；`ToolLoader` 按工具类名稳定排序，保持注册顺序不变。完整离线测试为 `358 passed, 7 skipped`。

### 2026-09-05

- [x] 调整 Session 写入边界：`AgentLoop` 不再在调用 `AgentRunner` 前单独保存当前 user 消息；仅在 Runner 成功返回后，才一次性持久化已有历史、当前 user 与本轮新增的 assistant/tool 消息。失败、取消和上下文窗口拒绝均不会留下不完整 turn，因此 `ContextBuilder` 不再承担历史补全或修复职责。
- [x] 完善目标执行模式：`source=goal` 的内部消息按 session 跟踪正在运行的目标 turn；成功后将 active goal 标记为 `completed`，执行失败或非显式取消时标记为 `failed`。目标执行期间，`/goal`、`/goal status` 与 `/goal stop` 可绕过 session lock 立即处理；`/goal stop` 会先持久化 `cancelled` 状态，再取消对应执行任务。
- [x] 支持目标执行期间的用户消息注入：每个运行中的目标 session 拥有独立 pending queue。普通外部文本不会启动第二个 Runner；`AgentRunner` 在每个工具完成后读取并合并队列中的输入，将其作为带边界标记的单个 user 消息追加在完整 tool result 批次之后，再继续模型调用。注入内容会随成功 turn 一起持久化，控制命令不会进入该队列。
- [x] 简化长期记忆事件规则：每个成功保存的 Agent turn 都会追加记忆事件。命令、失败和取消路径本身不会生成事件，移除了当前没有生产方的 `ephemeral`、`system` 与 `memory_consolidator` metadata 过滤。
- [x] 新增 `create_goal` 与 `update_goal` 内置工具：两者复用 `SessionManager` 的目标创建、更新、取消与持久化逻辑。`create_goal` 仅能在普通模式调用，成功后复用 `/goal` 的内部消息模板向 `MessageBus` 投递 `source=goal` 的后续 turn，并立即返回已调度确认；`update_goal` 仅能在目标模式调用，可替换目标或停止目标。消息总线发布失败会返回清晰原因，且不会吞掉取消信号。
- [x] 增加 Agent 运行模式的工具权限控制：普通 Agent run 通过 `AgentRunSpec.blocked_tool_names` 禁用 `update_goal`，目标 run 禁用 `create_goal`；被禁用工具同时从 ContextBuilder 的工具预算和 Provider schema 中移除。Subagent 的 Spec 固定禁用 `spawn`、`create_goal`、`update_goal`，避免递归创建子 Agent 或修改主会话目标。
- [x] 支持目标跨 `max_iterations` 自动续跑：`AgentRunner` 在完整 tool call/tool result 批次边界返回 `stop_reason="max_iterations"`，`AgentLoop` 先持久化本轮消息，再递增 `GoalState.continuation_count` 并投递带 `source=goal`、`goal_continuation=true` 的内部消息。中间结果不发送给用户；达到续跑上限或无法投递时将目标标记为 `failed`。普通会话达到迭代上限则返回明确提示，不自动续跑。
- [x] 完善目标终态判定：非 `max_iterations` 的目标 turn 只有在模型返回非空、非纯空白文本时标记为 `completed`；空文本或 `None` 标记为 `failed`。目标停止、取消和失效 continuation 不会重新启动已终止目标。
- [x] 最近一次完整离线测试：`379 passed, 7 skipped`。

### 2026-09-07

- [x] 新增基于 `aiohttp` 的最小本地 HTTP API：`GET /health` 用于健康检查，`POST /v1/messages` 接收 `session_id` 与 `content`，并返回对应 session 的最终文本响应。路由、JSON 校验、请求体大小限制、超时和框架错误均在 `HttpApiService` 中集中处理。
- [x] HTTP 请求通过 `InboundMessage(channel="api")` 直接交给 `AgentLoop.process_inbound()`，不发布原始请求到 `MessageBus`，以便同步返回结果；但仍复用 AgentLoop 的命令、Session 串行化、目标模式、工具和 AgentRunner 流程，不会直接访问 Provider。
- [x] 增加 `ApiConfig`：非敏感 API 设置位于 `.nanobot/nanobot.json`，包含 `enabled`、`host`、`port` 与 `request_timeout_seconds`；`Application` 负责在 Cron 启动后启动 API，并在关闭时优先停止监听器。
- [x] 简化 `Application.close()`：以线性关闭步骤替代嵌套 `try/finally`，保持 API → Cron → Channel → AgentLoop → MCP 的关闭顺序。单个组件的普通关闭异常会记录后继续清理；取消会在全部资源获得清理机会后继续传播。
- [x] 新增最小 WebSocket Channel：基于 `aiohttp` 在本机监听 `/ws`，连接后发送 `ready` 事件；客户端 `message` JSON 经 `MessageBus` 进入 AgentLoop，按 session 将 `message`、`error` 与 `turn_end` 事件路由回原连接。Channel 不直接调用 AgentLoop 或 Provider，关闭时清理连接和后台任务。
- [x] 重组 `.nanobot/nanobot.json`：共享 workspace 保持顶层；上下文与压缩预算归入 `agent`，时区归入 `cron`，默认 Channel 与 WebSocket 设置归入 `channel`，MCP Server 列表归入 `mcp.servers`。解析后的 `NanobotConfig` 保留既有运行时字段，避免影响 Application 组装代码。
- [x] 为 HTTP 路由、WebSocket Channel、请求到 AgentLoop 的转换、同/不同 session 行为、异常映射以及 Application API 生命周期增加 fake-based 测试；关闭期间发生取消时仍完成后续资源清理。最新完整离线测试：`404 passed, 7 skipped`。

### 2026-09-08

- [x] 实现按 Channel 配置启用的文本流式调用：`channel.qq.streaming` 默认 `false`，`channel.websocket.streaming` 默认 `true`；Channel 将设置写入 `InboundMessage.metadata`，AgentLoop 据此选择 `AgentRunner.run()` 或 `run_stream()`。
- [x] WebSocket 流式协议通过 MessageBus 按顺序发送 `delta` 事件；完整结果保存后发送一个 `turn_end`，携带最终文本、`tools_used`、`token_usage` 和 `stop_reason`。普通 `message` 不再自动附带 `turn_end`。
- [x] 新增独立的 `webui/` React + TypeScript + Vite 项目，通过根目录 `.env` 中的 `VITE_NANOBOT_WEBSOCKET_URL` 连接既有 WebSocket Channel。页面提供连接状态、基础聊天输入、发送状态和错误提示；使用现有 `message`、`delta`、`turn_end` 协议，不修改 Python 后端。
- [x] Web UI 将 WebSocket 状态和协议处理封装为独立 Hook 与纯状态辅助函数；当前会话内可按顺序累积 delta，并以 `turn_end` 收束最终文本，避免重复展示。
- [x] 为 Web UI 增加会话列表与切换：`HttpApiService` 提供只读 `GET /v1/sessions` 和 `GET /v1/sessions/{session_id}`，经共享 `SessionManager` 读取持久化历史，并仅返回 user/assistant 可见消息。前端通过 `VITE_NANOBOT_API_URL` 加载列表和历史；新会话使用浏览器生成的唯一 session ID，首次发送后才持久化。单一 WebSocket 连接可在发送新会话消息时重新绑定，前端会忽略已切走会话的迟到流式事件。
- [x] 增加工具调用展示链路：`AgentRunSpec` 保留文本 `on_delta` 回调并新增独立 `on_tool_call` 回调；流式工具在实际执行前经 AgentLoop 与 MessageBus 依次发布 `tool_call`、`delta`、`turn_end`。WebSocket 转发工具 ID、名称和参数；Session 历史接口返回 assistant 的持久化 tool call（不暴露 tool result），Web UI 以折叠详情附着到当前 assistant 响应，避免空工具调用消息显示为 “Thinking” 或重复创建消息。
- [x] 为 Web UI 增加“停止当前生成”：流式回复期间前端复用 `/stop` 请求当前 session；AgentLoop 按 session 跟踪并取消活动任务，取消前经 MessageBus 发布 `turn_end(stop_reason="cancelled")`，保留已到达的 delta，且不持久化半截 user/tool 消息。WebSocket 连接关闭时也会沿同一路径请求停止其最后绑定的 session；非流式 QQ 行为不变。
- [x] 本轮聚焦验证：`tests.agent.test_loop_streaming`、`tests.agent.test_commands` 和 `tests.channels.websocket.test_channel` 共 `46` 项测试通过；Web UI 应用与测试 TypeScript 配置通过 `tsc --noEmit` 类型检查。
- [x] 此前聚焦验证：AgentRunner、AgentLoop 流式、WebSocket Channel 与 HTTP API 共 `39` 项测试通过；Web UI 应用和测试 TypeScript 配置通过无输出类型检查。
- [x] 此前完整离线测试：`415 passed, 7 skipped`。

## 待开发功能

### 核心开发工具

- [ ] `find_files`：查找 workspace 内文件。
- [ ] `grep`：搜索文件内容。
- [ ] `apply_patch`：批量、结构化修改文件。
- [ ] `write_stdin`：向长时间运行命令写入标准输入。
- [ ] `list_exec_sessions`：查看运行中的命令。

### 常用 Agent 能力

- [ ] `web_search`：网络搜索。
- [ ] `web_fetch`：读取网页内容。
- [ ] `message`：主动发送一般消息。
- [ ] 其他真实 Channel，以及媒体、文件和流式消息支持。
- [ ] HTTP API 的认证、流式响应、异步任务查询和完整 OpenAI 兼容协议。
- [ ] WebSocket Channel 的认证、多会话订阅、广播、重连恢复和媒体支持。

### 高级能力

- [ ] `generate_image`。
- [ ] `list_sessions` / `search_sessions` / `read_session`。
- [ ] `send_session_message`。
- [ ] `my`：运行时控制。
- [ ] `run_cli_app`。
- [ ] MCP resources、prompts、OAuth、重连、热加载、二进制结果与插件机制。

## 待优化项

- `ToolParameter` 目前只支持 string、integer、number、boolean；数组、嵌套对象、枚举、默认值和完整 JSON Schema 校验尚未具备。
- `AgentRunner` 已支持文本流式、顺序工具循环和工具调用进度事件；仍缺并行工具调度、retry、fallback、上下文注入、工具结果与 reasoning 流式事件。
- Provider 的超时、重试、代理、模型能力声明、可选 SDK 依赖和成本控制仍需统一。
- Session JSONL 尚无跨进程锁、损坏恢复、迁移、TTL 或缓存淘汰；摘要、记忆仍缺少多级压缩、自动重试、冲突解决和后台任务恢复。
- Cron 缺少 cron 表达式、编辑/启停、限长批处理、事件归档、可靠投递、重试及分布式调度。
- Subagent 后台任务尚无持久化、进程重启恢复、自动重试、结果在原始 tool call 中实时注入、LLM 可调用的任务管理工具或多 Agent 协作。
- `ExecTool` 不是安全沙箱；仍需要操作系统级 sandbox 来限制文件、网络、系统调用与进程权限。
- QQ 和 WebSocket 之外的 Channel、消息重试、可靠投递、总线持久化、优先级、结构化日志、指标、追踪和外部日志后端尚未实现。
- HTTP API 当前仅适合受信任的本地调用：虽然默认监听 `127.0.0.1`，但尚无认证、限流、审计日志、跨进程会话协调或生产部署策略。

## 待解决问题

1. 如何在保持 Tool 模型简单的同时支持复杂 schema，并验证工具调用 ID、参数对象和 tool result 配对关系？
2. 如何定义 JSONL Session 的并发访问、损坏处理和迁移边界？
3. 如何统一 OpenAI、Anthropic 及兼容 Provider 在流式事件、thinking、`max_tokens`、finish reason 和 usage 上的差异？
4. 如何在不泄露凭据的前提下组织 live tests，并在 CI 中稳定地只运行离线测试？
5. 如何为更多真实 Channel 提供可靠投递、主动消息、路由恢复与长任务结果回传？
6. 如何在记忆、Cron 和 Subagent 都具备持久化后台任务后，处理重启恢复、幂等性与跨进程协调？
