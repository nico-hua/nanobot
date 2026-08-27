# 项目开发进度

最后更新：2026-08-27

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
- [x] 实现最小单轮 `AgentLoop` 和内存 `SessionStore`：读取 session 历史、追加用户消息、运行 Agent，并仅在成功后保存完整消息历史。
- [x] 实现基于 `asyncio.Queue` 的内存 `MessageBus`：支持带 channel、chat ID、session ID 的入站/出站消息发布和消费。
- [x] 将 `AgentLoop.run()` 接入 `MessageBus`：持续消费入站消息、最多等待一秒后继续轮询，并将最终回答发布为出站消息；`process_direct()` 保留单条显式路由消息的直接处理入口。
- [x] 实现最小 `BaseChannel`、`FakeChannel` 与 `ChannelManager`：Channel 负责外部消息和 `MessageBus` 的转换，Manager 统一管理生命周期并将出站消息路由到目标 Channel。
- [x] 实现 QQ 文本 Channel：基于可选依赖 `qq-botpy` 支持 C2C 与群聊 @ 消息，保留 QQ 路由字段和原始 `message_id`，并按聊天类型发送文本回复；SDK 缺失时仅在启动时给出明确错误。
- [x] 增加 `QQChannelConfig`：提供 QQ App ID、Secret 和 `allow_from` 用户 OpenID 白名单配置。
- [x] 增加默认跳过的 QQ → Agent → DeepSeek → 本地工具 → QQ 手工端到端测试：凭据仅从本地 `.env`/环境变量读取，验证模型工具调用、工具结果回传、会话历史和 QQ 文本回复。

### 当前测试状态

最近一次记录的离线测试结果：

```text
Ran 130 tests in 4.258s
OK (skipped=6)
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

当前 `AgentRunner` 已通过 `AgentRunSpec` 接收 Provider、消息、`ToolRegistry` 和最大迭代数，并在每轮模型工具调用后按顺序执行工具、追加 assistant/tool 消息；`AgentRunResult` 返回最终文本、完整消息历史、已尝试调用的工具、累计 usage 与停止原因。`AgentLoop` 使用内存 `SessionStore` 为每个 session 保存成功运行后的完整消息历史；失败时不会覆盖已有历史。

当前 `MessageBus` 由独立的入站和出站 `asyncio.Queue` 组成。`InboundMessage` 和 `OutboundMessage` 都保留 channel、chat ID、session ID 和内容；`AgentLoop.run()` 仅消费总线消息并发布结果，`process_direct()` 则处理一条显式传入的路由消息。总线为空时，Loop 每次最多等待一秒后继续轮询；取消 Loop 不会留下它创建的后台任务。

仍未实现：

- 完整 JSON Schema 的运行时校验；
- 流式、并行工具调度、上下文注入和自动重试；
- 会话持久化、并发访问控制和长期记忆。
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

### 5. 错误和可观测性需要完善

当前已使用 `ProviderError` 对底层异常进行统一包装，但还没有标准化错误类型、错误码、请求上下文、重试判断和日志字段。

### 6. 消息模型的边界校验需要补充

目前消息类型已经覆盖 system、user、assistant 和 tool 四类基本消息，但以下约束仍主要依赖调用方保证：

- 工具调用 ID 是否存在且唯一；
- `ToolMessage` 是否对应某个 `ToolCallRequest`；
- assistant 工具调用参数是否为合法对象；
- 不同 Provider 对消息顺序和内容块的额外要求。

## 待解决的问题

1. 如何在不破坏简单 `ToolParameter` 模型的前提下，扩展数组、嵌套对象、枚举和默认值等复杂参数能力。
2. 是否需要让 `AgentLoop` 支持持久化 session，以及如何定义 session 的并发访问边界。
3. 如何统一处理不同厂商的流式事件，尤其是文本、工具调用片段、思考内容和最终 usage。
4. 如何处理 Anthropic、OpenAI 及其他兼容协议在 `max_tokens`、thinking、finish reason 和 usage 字段上的差异。
5. 是否需要支持多轮工具调用，以及如何保证工具结果、调用 ID 和消息历史的一致性。
6. 如何在不泄露凭据的前提下组织 live tests，并在 CI 中默认只运行离线测试。
7. 如何在现有 QQ 文本 Channel 之外接入更多真实 Channel，并加入可靠投递和长期记忆模块，同时保持现有单轮执行边界清晰。

## 当前明确不实现的能力

当前阶段暂不实现以下内容：

- AgentRunner 内的 streaming、并行工具调度和自动重试；
- 除 QQ 文本消息外的真实 Channel、消息重试、优先级、总线持久化和复杂并发控制；
- 多 Provider 自动路由和 fallback；
- 重试、限流、熔断和成本控制；
- 多模态输入、音频、图像和文件内容；
- 完整 JSON Schema 校验和结构化输出强制解码；
- 复杂的思考过程流式事件暴露；
- 生产级日志、指标、追踪和持久化。

## 下一步建议

1. 为 `SessionStore` 设计持久化接口，并在实际需要时加入 session 并发访问控制。
2. 扩展 QQ Channel 的错误处理和路由测试，或在相同 `BaseChannel` 边界上接入下一个真实 Channel。
3. 根据 AgentRunner 的实际需求，再扩展 `ToolContext`、Provider 配置、流式事件和工具 Schema。
