# 项目开发进度

## 最新长期记忆、Session 摘要与上下文窗口（2026-08-31）

- [x] 将 `ContextBuilder` 和日志初始化模块归入 `nanobot/agent/`；`Application` 负责基于 workspace、总上下文窗口和输出预留创建 `ContextBuilder`，再显式注入 `AgentLoop`。
- [x] `ContextBuilder` 每次请求时读取 workspace 的 `AGENTS.md`、`SOUL.md`、`USER.md`，以固定顺序构建 system prompt；文件不存在、为空或无法读取时跳过。
- [x] 新增 `MemoryStore`：按 UTF-8 动态读取 `<workspace>/memory/MEMORY.md`；记忆缺失、为空或不可读时视为空，不影响 Agent 请求。`ContextBuilder` 将非空记忆作为固定的 `## Long-term Memory` 区块注入 system prompt，且不会保存到 Session。
- [x] 新增 `MemoryConsolidator`：携带现有长期记忆与指定范围的完成对话，通过一次 `LLMProvider.complete()` 生成完整替换式 MEMORY.md 内容。它只在非空、无工具调用的结果下经临时文件原子替换写入，失败保留原文件。
- [x] `Application` 为 workspace 创建共享的 `MemoryStore` 和 `MemoryConsolidator`。AgentRunner 成功且本轮完整消息保存后，`AgentLoop` 使用不可变本轮快照异步调度记忆整理；任务不阻塞用户回复、不会进入 AgentRunner 或递归触发。
- [x] `MemoryConsolidator` 以 workspace 级异步锁串行化同一运行时中的读、模型调用和写入；`AgentLoop.close()` 与 Application 关闭流程统一取消并等待 Session 摘要和长期记忆任务。ephemeral、system metadata、slash command、取消和失败请求不会触发记忆整理。
- [x] 新增无额外依赖的稳定 token 估算：文本、消息结构、tool call 和工具 schema 均会计入估算；历史裁剪从最近的完整 user turn 向前保留，避免截断 tool call/tool result 链。
- [x] 请求侧仅按 `context_window_tokens`（当前 `128000`）裁剪原始历史：从总窗口扣除合并后的 system prompt、会话摘要、当前用户消息、工具 schema 和 Provider 的 `max_tokens` 输出预留后，得到可用历史预算。固定内容已超窗时提示用户新开会话；`max_history_tokens` 已删除。
- [x] 新增 `SessionCompactor`：AgentRunner 成功完成且完整消息保存后，`AgentLoop` 异步触发摘要压缩。压缩只覆盖尚未摘要的完整旧轮次，保留完整原始消息，并持久化 `summary` 与 `summary_until`。
- [x] 会话摘要会追加到同一条 system prompt 的 `## Conversation Summary` 区段，不会作为普通 assistant 消息保存或展示给用户。system prompt 每轮动态重建，不保存进 JSONL Session。
- [x] 后台摘要任务按 session 串行化并由 `AgentLoop` 跟踪；`Application` 关闭时会取消并等待这些任务，压缩失败不会影响当前回复或破坏已有会话。
- [x] QQ Channel 的 C2C 和群聊回复改用 QQ 原生 Markdown 消息格式；qq-botpy 的默认文件日志已关闭，避免继续生成 `botpy.log`。
- [x] 最近全量离线测试：`225 passed, 7 skipped`。真实 Provider/QQ live tests 保持默认跳过。

本阶段仍不实现真实 tokenizer、provider 原生压缩、多级摘要、记忆自动重试、Dream、记忆去重/冲突解决、TTL 或后台任务恢复。

## 最新 Session 持久化（2026-08-31）

- [x] 新增 `Session`、`SessionManager` 和 JSONL 文件存储：每个会话一个 SHA-256 安全文件名，保存创建/更新时间及完整消息列表，并通过临时文件原子替换保护已有会话。
- [x] `SessionManager` 默认将运行时会话写入 `<workspace>/sessions/`；`.nanobot/workspace/` 已作为本地运行时数据忽略，不提交会话内容。
- [x] `Application` 创建共享 `SessionManager` 并注入 `AgentLoop`；Agent 重启后可从 workspace 恢复会话历史。
- [x] `AgentLoop` 优先使用非空 `session_id` 作为会话键；为空时回退为 `channel:chat_id`。MessageBus 保留该路由字段，并允许空值触发回退。
- [x] system prompt 不持久化到 Session；每次请求根据 workspace 动态构建。用户消息会在模型调用前保存，成功后依序保存 assistant、tool call 与 tool result 消息。
- [x] 添加会话存储与 Loop 集成测试，覆盖重启恢复、工具消息顺序、会话隔离、空 session ID 回退和执行失败后的历史保留。

## 最新运行时与 CLI 入口（2026-08-28）

- [x] 实现 `Application` 运行时组装：共享 `MessageBus`、`ToolRegistry`、MCP 动态工具、`AgentLoop` 与基于 `default_channel` 的 ChannelManager。
- [x] 为 Application 增加最小任务监督：统一等待停止请求、`AgentLoop.run()` 和 ChannelManager dispatcher；后台任务异常或意外结束时记录日志、关闭其余资源并传播异常。
- [x] 明确 `ChannelManager.start_all()` 是短暂启动方法；其长期运行的 outbound dispatcher Task 通过只读 `dispatcher_task` 暴露给 Application 监督。
- [x] 新增 `python -m nanobot` CLI：支持 `--config` 和 `--workspace`，在 SIGINT/SIGTERM 时请求优雅停止，并在启动或运行失败时返回非零退出码。
- [x] 日志初始化责任移至 CLI/宿主应用：CLI 先初始化 INFO 兜底日志，再按选定 JSON 配置应用最终级别；`Application` 与 `AgentLoop` 不再读取日志配置。

## 最新配置基础设施（2026-08-28）

- [x] 将非敏感运行配置迁移到 `.nanobot/nanobot.json`：绝对 workspace 路径、`logging.level`、Provider 类型/API base/模型/生成参数、默认 Channel 和 `mcp_servers`。
- [x] `.env` 只保留 `NANOBOT_API_KEY`、QQ 凭据及临时 live-test 设置；JSON 中的 Provider API key 会被校验拒绝。
- [x] 新增配置加载器，校验 JSON、合并 API key 为运行时 `ProviderConfig`。
- [x] OpenAI-compatible 与 Anthropic-compatible Provider 支持初始化默认 `max_tokens` / `temperature`，单次调用的显式参数优先。

最后更新：2026-08-31

## 项目目标

这是一个从零实现的 Agent 架构学习项目，重点是用较小、清晰的代码理解 Agent 系统中的核心边界和执行流程，而不是完整复刻某个生产级项目。

## 当前阶段：LLM Provider 与工具基础抽象

### 已完成

- [x] 设计 `LLMProvider` 抽象基类。
- [x] 设计统一的 `LLMResponse` 返回格式，包含：
  - 生成内容 `content`
  - 工具调用 `tool_calls`
  - 结束原因 `finish_reason`
  - Token 消耗 `usage`
- [x] 设计 `ToolCallRequest`，统一表示模型返回的工具调用请求，包含调用 ID、工具名和参数。
- [x] 增加 `TokenUsage` 和 `ProviderError`。
- [x] 增加统一消息类型：
  - `SystemMessage`
  - `HumanMessage`
  - `AIMessage`
  - `ToolMessage`
- [x] 为 Provider 抽象层增加普通调用 `complete` 和流式调用 `stream`。
- [x] 支持 `tools`、`max_tokens`、`temperature`，流式调用额外支持 `on_delta` 回调。
- [x] 基于 OpenAI SDK 实现 `OpenAICompatProvider`：
  - 支持 API key、API base 和默认模型配置。
  - 支持普通对话、流式文本、工具调用和 Token 消耗解析。
  - 支持 OpenAI-compatible 服务，例如 DeepSeek 的 OpenAI-compatible 接口。
- [x] 基于 Anthropic SDK 实现 `AnthropicCompatProvider`：
  - 支持 Anthropic Messages API 风格的消息和工具调用。
  - 支持普通调用、流式文本、结束原因和 Token 消耗解析。
  - 支持默认 `max_tokens` 和 thinking 配置。
- [x] 添加 Provider focused tests，覆盖请求构造、响应解析、流式回调、工具调用和异常包装。
- [x] 添加默认跳过的 DeepSeek live smoke tests，覆盖 OpenAI-compatible 和 Anthropic-compatible 的普通对话、流式调用及工具调用。
- [x] 设计 `ToolParameter`，统一表示参数名称、描述、类型和必填状态。
- [x] 设计 `Tool` 工具基类，统一保存工具名称、描述和参数对象，并生成参数 JSON Schema。
- [x] 为 `Tool` 提供异步 `execute(**arguments)` 执行入口。
- [x] 设计 `ToolResult`，统一表示工具执行内容、成功状态和错误信息。
- [x] 为 `Tool` 实现 OpenAI function calling 与 Anthropic tool use 的 schema 转换。
- [x] 将 Provider 的 `tools` 参数收敛为 `Sequence[Tool]`，并在 Provider 边界转换为厂商 schema。
- [x] 添加工具基础抽象的 focused tests。
- [x] 实现 workspace 范围内的 `ReadFileTool`，支持 UTF-8 文本、行偏移和行数限制。
- [x] 实现 workspace 范围内的 `WriteFileTool`，支持 UTF-8 文件创建、父目录创建和完整覆盖写入。
- [x] 实现 workspace 范围内的 `EditFileTool`，支持恰好一次的 UTF-8 文本替换。
- [x] 实现 workspace 范围内的 `ListDirTool`，支持稳定排序、递归和返回条目限制。
- [x] 实现 `ExecTool`，支持一次性 shell 命令、超时、stdout/stderr、退出码和输出截断。
- [x] 设计 `ToolContext`，集中提供工具创建所需的共享依赖；当前包含可选 `workspace`。
- [x] 为 `Tool` 增加 `enabled(context)` 和 `create(context)` 工厂协议，并让 workspace builtin 工具按 context 决定是否启用和创建。
- [x] 实现 `ToolRegistry`，支持稳定注册顺序、查找、移除、统一 schema、参数校验和异步执行。
- [x] 实现 `ToolLoader`，自动发现并稳定加载 builtin 工具，通过 `ToolContext` 创建后注册到 `ToolRegistry`。
- [x] 增加 `MCPServerConfig`，支持 stdio、SSE 与 Streamable HTTP 的 MCP tools 配置。
- [x] 实现 `MCPProvider` 与 `MCPToolWrapper`：动态连接 MCP Server、注册工具、执行文本工具调用，并在关闭时注销工具和释放连接。
- [x] 添加本地 FastMCP stdio 集成测试：启动真实 MCP Server，通过 `ToolRegistry` 注册并调用 `add_numbers` 工具。
- [x] 实现最小非流式 `AgentRunner`：调用 `LLMProvider.complete`、顺序执行 `ToolRegistry` 中的工具，并将工具结果作为 `ToolMessage` 回传模型直到得到最终回答。
- [x] 设计 `AgentRunSpec` 和 `AgentRunResult`：统一运行输入，并返回完整消息历史、已调用工具、累计 token usage 与停止原因。
- [x] 实现 `AgentLoop` 与文件持久化 `SessionManager`：读取 workspace 会话历史、持久化系统/用户/assistant/tool 消息并在 Agent 重启后恢复。
- [x] 实现基于 `asyncio.Queue` 的内存 `MessageBus`：支持带 channel、chat ID、session ID 的入站/出站消息发布和消费。
- [x] 将 `AgentLoop.run()` 接入 `MessageBus`：持续消费入站消息、最多等待一秒后继续轮询，并将最终回答发布为出站消息；`process_direct()` 保留单条显式路由消息的直接处理入口。
- [x] 实现最小 `BaseChannel`、`FakeChannel` 与 `ChannelManager`：Channel 负责外部消息和 `MessageBus` 的转换，Manager 统一管理生命周期并将出站消息路由到目标 Channel。
- [x] 实现 QQ 文本 Channel：基于可选依赖 `qq-botpy` 支持 C2C 与群聊 @ 消息，保留 QQ 路由字段和原始 `message_id`，并按聊天类型发送文本回复；SDK 缺失时仅在启动时给出明确错误。
- [x] 增加 `QQChannelConfig`：提供 QQ App ID、Secret 和 `allow_from` 用户 OpenID 白名单配置。
- [x] 增加默认跳过的 QQ → Agent → DeepSeek → 本地工具 → QQ 手工端到端测试：凭据仅从本地 `.env`/环境变量读取，验证模型工具调用、工具结果回传、会话历史和 QQ 文本回复。
- [x] 增加统一日志基础设施：`nanobot.logging.configure_logging()` 配置包级统一格式与日志级别，默认 `NullHandler` 避免未初始化时的非统一兜底输出。
- [x] CLI 入口先初始化包级 INFO 兜底日志，再从 `.nanobot/nanobot.json` 的 `logging.level` 配置 `nanobot` 命名空间日志；嵌入式宿主应用可自行选择初始化策略。
- [x] 在 Agent、Provider、ToolRegistry、MCP、MessageBus 与 Channel 的关键生命周期和异常边界加入不含消息内容、工具参数或凭据的模块级日志；工具/MCP 的意外执行异常保留 traceback，取消信号继续抛出。
- [x] 新增 `.env.example`、日志 focused tests 与 `AGENTS.md` 的 Logging and Error Handling 开发规范。

### 当前测试状态

最近一次记录的离线测试结果：

```text
Ran 175 tests in 5.197s
OK (skipped=7)
```

测试目录当前为 `tests/`；完整离线测试命令为：

```powershell
python -B -m unittest discover -s tests -t . -p "test*.py"
```

用户已通过 QQ 与 DeepSeek 的手工端到端测试。该测试默认跳过，只有设置 `NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=1` 并配置本地凭据后才会建立真实网络连接；凭据不会提交到仓库。

## 待实现的工具

### 核心开发能力

- [ ] `find_files`：查找 workspace 内的文件。
- [ ] `grep`：搜索文件内容。
- [ ] `apply_patch`：批量、结构化修改文件。
- [ ] `write_stdin`：向长时间运行的命令写入标准输入。
- [ ] `list_exec_sessions`：查看运行中的命令。

### 常用 Agent 能力

- [ ] `web_search`：网络搜索。
- [ ] `web_fetch`：读取网页内容。
- [ ] `message`：主动发送消息。
- [ ] `cron`：定时任务。
- [ ] `spawn`：创建子 Agent。

### 高级扩展能力

- [ ] `generate_image`：图片生成。
- [ ] `list_sessions` / `search_sessions` / `read_session`：查询历史会话。
- [ ] `send_session_message`：跨会话通信。
- [ ] `create_goal` / `update_goal`：持续任务管理。
- [ ] `my`：Agent 运行时控制。
- [ ] `run_cli_app`：调用外部 CLI 应用。
- [x] MCP 工具支持（仅 stdio、SSE、Streamable HTTP 传输和 tools）。

## 待优化的点

### 1. `ToolParameter` 目前只支持标量参数

`LLMProvider`、OpenAI Provider 和 Anthropic Provider 已直接接收 `Tool`，并在 Provider 边界完成 schema 转换：

- `to_openai_tool()` 生成 OpenAI function calling 格式；
- `to_anthropic_tool()` 生成 Anthropic tool use 格式。

`ToolParameter` 当前仅支持 string、integer、number 和 boolean。数组、嵌套对象、枚举、默认值等复杂 JSON Schema 能力仍待实际需求出现后扩展。`ToolCallRequest` 已经作为模型输出的统一格式保留。

### 2. Agent 工具调用与会话流程仍是最小版本

目前已有 `ToolContext`、`ToolRegistry` 和 `ToolLoader`：

- `ToolRegistry` 按名称管理工具，以稳定顺序提供统一 schema，并根据 `ToolParameter` 校验标量参数后执行工具；
- `ToolLoader` 稳定发现 builtin 工具，跳过私有模块、抽象类和重复类，并通过 `enabled(context)` / `create(context)` 完成实例化；
- `ToolContext` 当前仅有 `workspace`，后续可在实际工具需要时增加其他共享依赖。

当前 `AgentRunner` 已通过 `AgentRunSpec` 接收 Provider、消息、`ToolRegistry` 和最大迭代数，并在每轮模型工具调用后按顺序执行工具、追加 assistant/tool 消息；`AgentRunResult` 返回最终文本、完整消息历史、已尝试调用的工具、累计 usage 与停止原因。`AgentLoop` 使用 workspace 下 JSONL `SessionManager` 恢复和保存完整历史：非空 `session_id` 优先作为会话键，否则使用 `channel:chat_id`；Session 只持久化 user、assistant、tool call 与 tool result 消息。system prompt 在每轮请求时由 `ContextBuilder` 重建，包含会话摘要和 `<workspace>/memory/MEMORY.md` 的长期记忆；成功保存本轮完整消息后，后台 `SessionCompactor` 与 `MemoryConsolidator` 可分别更新摘要和 workspace 级长期记忆。

当前 `MessageBus` 由独立的入站和出站 `asyncio.Queue` 组成。`InboundMessage` 和 `OutboundMessage` 都保留 channel、chat ID、session ID 和内容；`AgentLoop.run()` 仅消费总线消息并发布结果，`process_direct()` 则处理一条显式传入的路由消息。总线为空时，Loop 每次最多等待一秒后继续轮询；取消 Loop 不会留下它创建的后台任务。

仍未实现：

- 完整 JSON Schema 的运行时校验；
- 流式、并行工具调度、上下文注入和自动重试；
- 摘要与记忆重试、多级摘要、后台任务恢复、TTL、Dream，以及记忆去重和冲突解决。
- 除 QQ 文本消息外的真实 Channel、消息重试、可靠投递、总线持久化和消息优先级。

`MCPProvider` 不由 `ToolLoader` 扫描；它在连接 Server 后将 `MCPToolWrapper` 动态注册到同一个 `ToolRegistry`。当前只处理 MCP tools 的文本结果，仍不支持 resources、prompts、OAuth、重连、热加载、图片/二进制结果或连接持久化。

目前已有 `ReadFileTool`、`WriteFileTool`、`EditFileTool` 和 `ListDirTool`。`WriteFileTool` 仅支持创建或完整覆盖，`EditFileTool` 仅支持恰好一次的精确替换；删除文件能力仍未实现。

`ExecTool` 支持 workspace 内的可选工作目录、最小化子进程环境和超时进程清理，但不提供安全沙箱、命令白名单或权限隔离。操作系统 sandbox 仍是限制文件、网络、系统调用和进程树权限的必要边界。

### 3. 流式工具调用能力仍不完整

当前流式接口重点支持文本增量回调。OpenAI Provider 已实现流式工具调用片段的累积，Anthropic Provider 依赖最终消息解析工具调用，但还没有统一的工具调用增量事件模型。

### 4. Provider 配置和依赖管理仍较简单

当前 Provider 通过构造函数接收配置，尚未统一配置对象，也没有项目级依赖声明文件。后续需要明确：

- SDK 依赖的安装和版本管理方式；
- 超时、重试、代理和请求头配置；
- 默认模型及模型能力配置；
- 缺少某个厂商 SDK 时的可选依赖处理。

### 5. 错误和可观测性仍需完善

当前已经有统一的包级日志格式、模块级 logger、JSON 中的日志级别配置和异常 traceback 记录规则；CLI 负责初始化日志，嵌入式宿主应用可自行初始化。日志只记录组件名、数量和生命周期等元信息，不记录消息内容、工具参数、文件路径或凭据。

仍未具备标准化错误类型、错误码、关联请求 ID、结构化日志字段、重试判断、指标、追踪和外部日志后端。

### 6. 消息模型的边界校验需要补充

目前消息类型已经覆盖 system、user、assistant 和 tool 四类基本消息，但以下约束仍主要依赖调用方保证：

- 工具调用 ID 是否存在且唯一；
- `ToolMessage` 是否对应某个 `ToolCallRequest`；
- assistant 工具调用参数是否为合法对象；
- 不同 Provider 对消息顺序和内容块的额外要求。

## 待解决的问题

1. 如何在不破坏简单 `ToolParameter` 模型的前提下，扩展数组、嵌套对象、枚举和默认值等复杂参数能力。
2. 如何为 JSONL Session 定义并发访问、损坏文件处理与未来迁移边界。
3. 如何统一处理不同厂商的流式事件，尤其是文本、工具调用片段、思考内容和最终 usage。
4. 如何处理 Anthropic、OpenAI 及其他兼容协议在 `max_tokens`、thinking、finish reason 和 usage 字段上的差异。
5. 是否需要支持多轮工具调用，以及如何保证工具结果、调用 ID 和消息历史的一致性。
6. 如何在不泄露凭据的前提下组织 live tests，并在 CI 中默认只运行离线测试。
7. 如何在现有 QQ 文本 Channel 之外接入更多真实 Channel，并加入可靠投递、长期记忆的后台恢复与冲突处理，同时保持现有单轮执行边界清晰。

## 当前明确不实现的能力

当前阶段暂不实现以下内容：

- AgentRunner 内的 streaming、并行工具调度和自动重试；
- 除 QQ 文本消息外的真实 Channel、消息重试、优先级、总线持久化和复杂并发控制；
- 多 Provider 自动路由和 fallback；
- 重试、限流、熔断和成本控制；
- 多模态输入、音频、图像和文件内容；
- 完整 JSON Schema 校验和结构化输出强制解码；
- 复杂的思考过程流式事件暴露；
- 结构化日志、关联 ID、指标、追踪、外部日志后端和持久化。

## 下一步建议

1. 为 `SessionManager` 在实际需要时加入 session 并发访问控制、损坏文件处理与迁移策略。
2. 扩展 QQ Channel 的错误处理和路由测试，或在相同 `BaseChannel` 边界上接入下一个真实 Channel。
3. 根据 AgentRunner 的实际需求，再扩展 `ToolContext`、Provider 配置、流式事件和工具 Schema。
