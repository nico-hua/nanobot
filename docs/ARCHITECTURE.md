# Nanobot 架构说明

本文面向继续开发、审查或学习本仓库的开发者。内容以当前源码为准，重点解释调用链、数据流、状态归属和设计边界，而不是逐行讲解实现。

相关文档：

- [开发进度](DEVELOPMENT_PROGRESS.md)：完成记录、已验证测试和后续事项。
- [命令说明](COMMANDS.md)：CLI 参数与聊天斜杠命令。
- [Web UI React 代码导读](WEBUI_REACT_GUIDE.md)：前端组件、状态与协议的实现级说明。

## 1. 系统整体结构

Nanobot 是一个单进程、异步运行的 Agent 学习项目。它将外部消息适配、Agent 运行、模型协议、工具执行、会话持久化和前端入口拆开，让新增 Channel、工具或运行模式时不需要在每个入口复制业务逻辑。

启动入口在 **nanobot/cli/main.py**。CLI 读取 **.nanobot/nanobot.json** 并创建 **nanobot/cli/application.py** 中的 **Application**。Application 是组合根：它创建 Provider、ToolRegistry、MCPProvider、SessionManager、AgentLoop、MessageBus、默认 Channel、CronService、HTTP API 与 SubagentManager，但不处理某一条具体消息的业务决策。

~~~text
python -m nanobot
        |
        v
config loader -> Application（组装、启动、关闭）
        |
        +-- Provider / ToolRegistry / MCPProvider
        +-- SessionManager / ContextBuilder / SessionCompactor
        +-- MemoryStore / MemoryEventConsumer
        +-- AgentLoop
        +-- MessageBus <----> ChannelManager <----> default Channel
        +-- CronService ----> CronMessagePublisher --+
        +-- HttpApiService --------------------------+

Web UI -- HTTP Session API
       -- WebSocket Channel -- MessageBus -- AgentLoop
QQ     -- QQChannel          -- MessageBus -- AgentLoop
~~~

当前运行时只创建 **channel.default** 指向的一个默认 Channel。QQ 和 WebSocket 配置可同时存在，但配置加载只校验、解析并创建被选中的 Channel。

### 关键组件与状态归属

| 组件 | 解决的问题 | 谁调用它 | 它调用/依赖什么 | 持有的主要状态 |
| --- | --- | --- | --- | --- |
| Application | 组合所有运行时组件并统一生命周期 | CLI 或嵌入宿主 | MCP、ChannelManager、Cron、API、AgentLoop | 启动任务、关闭锁、停止事件 |
| MessageBus | 解耦入站 Channel 与出站投递 | Channel、AgentLoop、Cron、工具 | asyncio 队列 | 入站与出站内存队列 |
| AgentLoop | 路由消息、串行化 Session、构建上下文、保存完整 turn | MessageBus 或 HTTP API | CommandRouter、ContextBuilder、AgentRunner、SessionManager | session 锁、活动任务、Goal 队列、后台任务 |
| AgentRunner | 执行一次模型—工具循环 | AgentLoop、SubagentManager | LLMProvider、ToolRegistry | 当前请求的 LLM 消息序列与累计用量 |
| SessionManager | 保存和恢复一个完整会话 | AgentLoop、命令、工具、API | JsonlSessionStorage | workspace 中的 Session 文件 |
| ChannelManager | 将出站消息投递到具体渠道 | Application | BaseChannel、MessageBus | 一个 dispatcher task |

## 2. 核心消息链路

### 2.1 QQ 与 WebSocket 的入站消息

QQ 的入口在 **nanobot/channels/qq/channel.py**：

1. qq-botpy 事件调用 **QQChannel.handle_c2c_message** 或 **handle_group_at_message**。
2. Channel 校验 allow list，构造 **InboundMessage**。
3. metadata 附带 QQ chat type、原始 message ID 和当前 Channel 的 streaming 开关。
4. QQChannel 将消息发布到 **MessageBus.publish_inbound**。

WebSocket 的入口在 **nanobot/channels/websocket/channel.py**：

1. aiohttp 在本地的 **/ws** 建立连接后先发送 **ready**。
2. 认证启用时，客户端必须先发送 **authenticate**，未认证的普通消息不会转发给 Agent。
3. 合法 **message** JSON 被转换为 InboundMessage；没有 session_id 时使用 **websocket:chat_id**。
4. **BaseChannel.receive_external** 将消息发布到 MessageBus，并保留本 Channel 的 streaming metadata。

~~~text
QQ event / WebSocket JSON
          |
          v
   concrete Channel
          |
          v
InboundMessage(channel, chat_id, sender_id, session_id, content, metadata)
          |
          v
 MessageBus inbound queue
          |
          v
      AgentLoop.run()
~~~

**InboundMessage** 和 **OutboundMessage** 定义在 **nanobot/bus/messages.py**。它们是路由层对象，不是发送给 LLM 的对话消息：前者表示“谁向哪个会话发送了什么”，后者表示“应向哪个渠道投递什么”。

### 2.2 MessageBus 如何解耦 Channel 与 AgentLoop

**nanobot/bus/message_bus.py** 维护独立的入站和出站 asyncio 队列：

- Channel 只负责发布 InboundMessage，以及发送接收到的 OutboundMessage。
- AgentLoop 只消费 InboundMessage、生成 OutboundMessage，不知道 QQ SDK 或 WebSocket 连接对象。
- **ChannelManager** 在 **nanobot/channels/manager.py** 中消费出站队列，按 OutboundMessage.channel 找到 Channel 并调用 send。单个消息投递失败不会结束 dispatcher。

Cron、后台 Subagent 和 MessageTool 因此能够复用同一链路，而不是绕过会话、上下文、工具权限和渠道路由。

### 2.3 AgentLoop 到 AgentRunner

普通消息进入 **AgentLoop._run_turn** 后，以 session 为单位持有锁，完成以下范围：

~~~text
读取 Session
  -> 构造当前 HumanMessage
  -> ContextBuilder.build_request_messages(...)
  -> AgentRunner.run(...) 或 run_stream(...)
  -> 仅在 Runner 返回完整消息序列后保存 Session
  -> 触发记忆事件与后台摘要压缩
  -> 生成最终 OutboundMessage 或流式 turn_end
~~~

session 锁覆盖“读取历史 → 请求模型 → 保存结果”，避免同一会话的普通 turn 交错读写；不同 session 可以并发处理。

### 2.4 普通回复、delta、tool_call 与 turn_end

非流式 Channel（默认 QQ）中，AgentLoop 从 AgentRunner 获得完整 **AgentRunResult** 后返回一个普通 OutboundMessage，ChannelManager 再投递它。

流式 WebSocket 入站消息中，AgentLoop 将回调放入 **AgentRunSpec**：

1. Provider 产生文本 chunk，AgentRunner 调用 on_delta。
2. AgentLoop 等待 **MessageBus.publish_outbound** 完成，并发布 metadata.event 为 **delta** 的 OutboundMessage。等待发布是保持 chunk 顺序的关键。
3. Runner 即将执行工具时调用 on_tool_call；AgentLoop 发布 metadata.event 为 **tool_call** 的进度事件。
4. Runner 结束且完整 Session 保存成功后，AgentLoop 发布一次 metadata.event 为 **turn_end** 的消息，其中包含最终 content、tools_used、token_usage 和 stop_reason。

WebSocketChannel 将这些 OutboundMessage 转换为浏览器的 **delta**、**tool_call**、**turn_end** 事件。流式场景不会再投递一条重复的完整普通回复。

### 2.5 HTTP API 的同步入口

**nanobot/api/service.py** 的 **HttpApiService** 提供请求/响应适配。POST **/v1/messages** 构造 channel 为 **api** 的 InboundMessage，然后直接调用 **AgentLoop.process_inbound**，而不是先放入 MessageBus。

这使 HTTP 请求能在同一个响应中获得最终文本，但仍会复用 AgentLoop 的命令、session 锁、Goal、工具和 AgentRunner 流程。HTTP API 没有绕过 AgentLoop；它只是绕过“入站队列等待”和“具体 Channel 回送”。

## 3. AgentLoop 与 AgentRunner

### 3.1 为什么拆分

**AgentLoop**（**nanobot/agent/loop.py**）解决应用层问题：消息属于哪个 session、是否是命令、是否处于 Goal 模式、何时保存、何时取消、何时把结果送回 MessageBus。

**AgentRunner**（**nanobot/agent/runner.py**）解决一次 Agent 推理问题：给定已经准备好的 LLM 消息和工具，如何反复调用模型、执行工具、保持 assistant/tool 的正确顺序，并返回完整结果。

若把两者合成一个类，Provider 工具循环会与 Channel 路由、Session 持久化、命令和后台任务分支混在一起；Subagent 也无法复用一个不依赖 Session 的 Runner。当前拆分让 AgentRunner 可以在不加载 MessageBus 或会话存储时独立执行。

### 3.2 AgentLoop 的输入、输出和状态

输入是 InboundMessage，输出是 OutboundMessage 或流式出站事件。其主要状态包括：

- **_session_locks**：每个 session 一个 asyncio.Lock。
- **_active_turn_tasks**：可被 /stop 取消的运行任务。
- **_streaming_turns**：活动流式任务与原始入站路由的映射，用于取消时补发 cancelled turn_end。
- **_goal_turn_tasks** 和 **_pending_user_messages**：目标运行与用户追加输入的会话级状态。
- **_compaction_tasks** 与 MemoryEventConsumer：完成保存后的非阻塞后处理。

CommandRouter 在调用 Runner 前拦截斜杠命令。普通消息由 ContextBuilder 生成 LLM 上下文，再创建 AgentRunSpec。AgentLoop 还根据运行模式从 Provider 可见工具和 Runner 的可执行工具中移除被禁止的工具。

### 3.3 AgentRunner 的工具循环

**AgentRunSpec** 包含消息、Provider、ToolRegistry、最大迭代次数、被禁用工具名、流式回调和可选 Goal 注入回调。**AgentRunResult** 包含最终文本、完整新增消息序列、已使用工具、累计 TokenUsage、stop_reason 与可选 error；Provider 请求失败时，error 带有安全的用户可见原因，新增消息序列保持为空。

每次迭代遵循以下顺序：

1. 调用 Provider.complete，或通过 Provider.stream 接收文本 delta。
2. 没有工具调用时，追加最终 AIMessage；Goal 模式下可检查待注入用户输入，存在时继续下一次模型调用。
3. 有工具调用时，先追加带 ToolCallRequest 的 AIMessage。
4. 逐个通过 ToolRegistry.execute 执行，并为每个调用追加 ToolMessage，包括被禁止、参数错误和执行失败产生的工具错误。
5. 整个工具批次完成后，Goal 模式才允许追加合并后的用户输入，保证 tool call 与 tool result 不会被打断。

达到 max_iterations 时，Runner 返回 stop_reason 为 **max_iterations** 的正常结果，不会抛出未处理异常或制造半截消息链。AgentLoop 决定普通会话提示达到上限，还是 Goal 创建 continuation。

若 Provider 返回 `LLMResponse.error`，Runner 会立即返回 `AgentRunResult.error`：不执行工具、不追加 assistant/tool 消息、不进入最终化请求，也不触发后续 Goal continuation。

### 3.4 运行模式

| 场景 | AgentLoop 行为 | AgentRunner 行为 | 结果处理 |
| --- | --- | --- | --- |
| 普通非流式 turn | session 锁、构建上下文、保存完整结果 | complete 与顺序工具循环 | 普通 OutboundMessage |
| WebSocket 流式 turn | 增加 delta/tool_call 回调 | stream 与同一工具循环 | delta/tool_call 后一次 turn_end |
| Goal turn | source 为 goal，启用队列和权限限制 | 工具批次边界可注入用户输入 | 更新 GoalState，必要时继续 |
| /stop | 不等待 session 锁 | 取消对应 asyncio task | 流式发送 cancelled turn_end，不保存不完整 turn |
| Provider 错误 | 不执行工具或后续模型调用 | 返回 `AgentRunResult.error` | 非流式发送一条错误消息；流式发送一次 `event="error"` |

取消不是只改变前端 loading 状态。AgentLoop 按 session 定位任务并取消；由于 Session 只在 Runner 成功返回后写入，取消中的 user、tool call、tool result 不会留下不完整持久化链。

## 4. Provider 层

Provider 层在 **nanobot/providers/**。它只负责将不同模型厂商的请求和响应转为统一对象，不读取 Session、不投递 Channel、不执行 ToolRegistry。

### 统一接口

**LLMProvider**（**nanobot/providers/base.py**）定义：

- **complete(messages, tools, max_tokens, temperature)**：一次性返回 LLMResponse。
- **stream(..., on_delta)**：增量传递文本，并最终返回完整 LLMResponse。

| 对象 | 职责 |
| --- | --- |
| LLMResponse | 单次响应的文本、工具调用、结束原因、token 用量与可选 `error` |
| ToolCallRequest | 模型要求执行的工具 ID、名称和参数 |
| TokenUsage | prompt、completion、total token 计数 |
| BaseMessage 及 System/Human/AI/Tool 子类 | Provider 适配前后的统一对话消息模型 |
| ProviderError | Provider 无法完成请求时的统一异常边界 |
| ProviderTransientError | 明确标记可由共享重试包装层重试的 Provider 错误 |
| ProviderTimeoutError | 单次 Provider 请求超过 `provider.request_timeout_seconds` 时的 ProviderError 子类 |

### Factory 与实现

**ProviderFactory**（**nanobot/providers/factory.py**）按配置 type 创建实现。当前默认注册：

- **openai_compat** → **OpenAICompatProvider**
- **anthropic_compat** → **AnthropicCompatProvider**

它们分别位于 **openai_compat_provider.py** 和 **anthropic_compat_provider.py**，负责 SDK 调用、消息与工具 schema 转换、流式解析和异常封装。Provider 基类的共享调用包装层负责每次尝试的 `provider.request_timeout_seconds`（默认 60 秒）和有限 transient retry：`provider.max_retries` 默认 2，代表一次请求最多总计 3 次尝试，等待 1、2 秒。它仅重试 timeout、连接错误、HTTP 429/5xx 与显式 `ProviderTransientError`；兼容 SDK 的内置重试被关闭，避免重试预算叠加。最终失败转换为 `LLMResponse(error=..., finish_reason="error")`，而 `asyncio.CancelledError` 必须继续向上抛出。流式请求仅在尚未向外发布 delta 时重试；一旦已有可见输出，后续错误不会重放已发送文本。Provider 不决定上下文结构、工具循环或 Session 写入，AgentRunner 也不需要知道 Chat Completions 与 Anthropic Messages 的协议差异。

## 5. Tool 系统

工具系统位于 **nanobot/tools/**，用于分离“模型声明调用什么”与“项目如何验证和执行它”。

### 5.1 Tool、ToolResult、ToolRegistry 与 ToolLoader

**Tool**（**nanobot/tools/base.py**）定义 name、description、ToolParameter、execute，并能生成 OpenAI 与 Anthropic 的工具 schema。

**ToolResult** 使用 success、content 和可选 error 表示结果。**ToolRegistry**（**nanobot/tools/registry.py**）按稳定注册顺序保存工具、校验模型参数、调用 execute，并将未知工具、非法参数和执行异常转换为 ToolResult，而不是打断整个 Runner。

**ToolLoader**（**nanobot/tools/loader.py**）扫描 **nanobot.tools.builtin**，按类名稳定排序，依次调用 Tool.enabled、Tool.create 和 registry.register。工具创建期的依赖检查集中在这里，Runner 只看到已可用工具。

### 5.2 ToolContext 与 RequestContext

**ToolContext** 是 Application 组装工具时注入的长生命周期依赖，如 workspace、CronService、SessionManager、MessageBus、Tavily key 与 SubagentManager。

**RequestContext** 是一次 AgentRunner 执行期间的短生命周期路由信息：session_key、channel、chat_id、sender_id、入站 metadata 和是否为 Goal 模式。AgentLoop 在调用 Runner 前通过 **bind_request_context** 绑定 ContextVar，工具通过 **get_request_context** 读取。

这让模型的工具参数无法覆盖可信路由字段。CronTool、Goal 工具、SpawnTool 与 MessageTool 都可定位“当前会话和当前渠道”，但无需把这些字段暴露给模型 schema。

### 5.3 builtin、MCP 与主动消息

当前 builtin 工具包括 workspace 文件操作、exec、cron、goal、spawn、message、web_search 和 web_fetch。文件工具集中在 **nanobot/tools/builtin/filesystem.py**，网络工具集中在 **nanobot/tools/builtin/web.py**。

MCP 工具不同于 builtin：**MCPProvider**（**nanobot/mcp/provider.py**）在 Application 启动时按配置连接 stdio、SSE 或 Streamable HTTP server，列出工具后由 **MCPToolWrapper**（**nanobot/mcp/tool.py**）动态注册到共享 ToolRegistry。当前实现仅处理 MCP tools。

工具不直接调用 QQ SDK 或 WebSocket。即使主动通知用的 **MessageTool**，也只根据 RequestContext 构造 OutboundMessage 并发布到 MessageBus；实际渠道投递仍由 Channel 层决定。

## 6. Session、上下文和记忆

### 6.1 Session 是完整短期对话的持久化边界

**Session**（**nanobot/session/models.py**）保存稳定 key、时间、完整 LLM messages、可选 summary/summary_until 与可选 GoalState。

**SessionManager**（**nanobot/session/manager.py**）负责 get、get_or_create、save、list 和 Goal 的共享持久化操作。**JsonlSessionStorage**（**nanobot/session/storage.py**）将每个会话存入 workspace/sessions 中独立 JSONL 文件，文件名由 session key 的 SHA-256 生成；写入使用同目录临时文件和替换。

Session 保存完整 user、assistant、tool LLM 消息链。AgentLoop 不把每次重新构建的 system prompt 写入 Session，并且仅在 AgentRunner 返回完整序列后一次性保存历史、当前 user 与新增 assistant/tool 消息。

### 6.2 ContextBuilder 是请求态

**ContextBuilder**（**nanobot/agent/context.py**）每次请求新建 system prompt，并读取 workspace 下可选的 AGENTS.md、SOUL.md、USER.md、MEMORY.md 与 Skills。

它的输入是 Session 历史、当前 HumanMessage、已保存摘要和可用工具；输出是发送给 Provider 的 LLM 消息序列。它不保存 Session，也不执行 Skill 内容。

可用于原始历史的预算按下式计算：

~~~text
context_window_tokens
  - system prompt
  - summary
  - current user message
  - tool schema
  - output token reserve
  = available history budget
~~~

预算不足会抛出 ContextWindowExceededError。system prompt、摘要、当前用户消息和工具 schema 不会被裁掉。当前 token 估算是稳定的启发式算法，不是模型原生 tokenizer；历史裁剪按完整 user turn 分组，避免拆开工具调用和结果。

### 6.3 Session 摘要

**SessionCompactor**（**nanobot/session/compactor.py**）只处理 summary_until 之后的原始消息：

- 自动压缩阈值为 agent.compaction_threshold_tokens。
- 手动 /compact 仅在原始历史超过 agent.compaction_recent_tokens 时执行。
- 它总结较早的完整 turn，保留最近完整 turn。
- 它更新 summary 与 summary_until，但不删除完整原始 messages。

AgentLoop 在完整 turn 保存后异步调度压缩，重新获取同一个 session 锁再保存摘要，因此不会延迟当前回复，也不会覆盖更晚的 Session。

### 6.4 长期记忆与 Session 的区别

| 数据 | 位置 | 用途 | 进入普通请求上下文？ |
| --- | --- | --- | --- |
| Session.messages | workspace/sessions/*.jsonl | 当前会话完整短期历史与工具链 | 是，预算内裁剪后 |
| summary / summary_until | Session JSONL header | 压缩较早会话历史 | 是，合入 system prompt |
| MEMORY.md | workspace/memory/MEMORY.md | workspace 级稳定事实、偏好、约定 | 是，每次新读入 system prompt |
| history.jsonl | workspace/memory/history.jsonl | 长期记忆整理的持久事件队列 | 否 |
| .memory_cursor | workspace/memory/.memory_cursor | 最后成功消费的事件 ID | 否 |

**MemoryStore**（**nanobot/memory/store.py**）负责这些文件的读写。**MemoryEventConsumer** 在完整 turn 保存后追加不可变消息快照、后台消费，并在启动时继续处理 cursor 之后的遗留事件。

**MemoryConsolidator** 让 Provider 结合已有 MEMORY.md 与待处理事件生成完整替换内容。成功更新或明确返回空记忆时 cursor 才前移；失败时 cursor 保持不变，供下一次启动重试。长期记忆不会追加到 Session.messages，也不会和 Session 摘要相互覆盖。

### 6.5 Skills 是静态指令，不是可执行插件

**SkillsLoader**（**nanobot/skills/loader.py**）只扫描 workspace/skills/skill_name/SKILL.md。它读取 UTF-8 Markdown、尽力解析 frontmatter 中的 name、description、always 与 nanobot.requires.bins/env，并用当前 PATH 和进程环境报告可用性；无效 frontmatter、缺失文件或缺失依赖不会让一次 Agent 请求失败。

ContextBuilder 每次构建 system prompt 时重新扫描：

- always Skill 的完整正文进入 system prompt。
- 普通可用 Skill 仅以名称、描述和路径摘要列出。
- 用户当前消息中显式引用的 $skill-name，才将该 Skill 正文作为本次请求的运行时上下文加入。
- 已经 always 启用的 Skill 不会重复注入；不可用或未知名称不会按名称读取任意路径。

Skill 文件从不执行命令、脚本或外部程序，也不会写入 Session.messages。它的职责是给模型提供静态操作约定，而非提供新的运行时执行机制。

## 7. 命令、Goal、Cron 和 Subagent

### 7.1 CommandRouter

**CommandRouter**（**nanobot/agent/commands.py**）解析忽略首尾空格、名称大小写不敏感的斜杠命令。普通文本返回 None 后继续走 AgentRunner；未知或格式错误命令返回帮助提示，不交给模型。

| 命令 | 处理边界 |
| --- | --- |
| /new | 重置当前 Session 的历史、摘要与终态 Goal；不删除 MEMORY.md；active Goal 时拒绝 |
| /stop | 立即取消当前 session 的 Agent turn，不等待 session 锁 |
| /help | 根据注册表生成说明 |
| /goal、/goal status、/goal stop | 创建、查询或取消当前 session 的 Goal |
| /compact | 调用现有 SessionCompactor 整理当前 session |
| /memory | 只读且限长展示当前 workspace 的 MEMORY.md |
| /subagents、status、cancel | 查询或取消当前 session 所属后台 Subagent |

命令本身不写入 Session.messages，也不产生长期记忆事件。/goal 的后续 Goal 工作是例外：它先保存 GoalState，再通过 MessageBus 发布 source 为 **goal** 的内部 InboundMessage。

### 7.2 Goal 持续执行

**GoalState**（**nanobot/session/goals.py**）是 Session 的独立字段，保存一个 active 或终态目标，状态包括 active、completed、cancelled、failed，以及 continuation_count。

Goal 运行时：

1. /goal 或 create_goal 保存 active GoalState，并发布 source=goal 的内部消息。
2. AgentLoop 识别 Goal 模式，使用已有 Session 上下文运行 Runner。
3. 普通用户文本不启动第二个 Runner，而是加入该 session 的 pending queue。Runner 在完整工具批次后取出、合并并追加为新 user 消息。
4. Goal 模式禁用 create_goal；普通模式禁用 update_goal。限制同时体现在可用工具和 AgentRunSpec.blocked_tool_names。
5. Runner 返回 max_iterations 时，AgentLoop 先保存完整工具链，再增加 continuation_count，并发布 goal_continuation=true 的内部 continuation。
6. 非上限结束时，非空最终文本标记 completed；空文本或异常标记 failed。/goal stop 先保存 cancelled，再取消 Goal task。

Goal continuation 仍重新进入 AgentLoop，不直接递归调用 Runner，因此 Session、工具权限与取消语义不会分叉。

### 7.3 Cron

Cron 模块在 **nanobot/cron/**：

- CronSchedule、CronPayload、CronJobState 与 CronTask 将调度定义、路由 payload、运行状态分开。
- CronService 管理一次性 at 与周期 every 任务，使用 workspace/cron/tasks.json 原子持久化。
- 到期时 CronService 只调用构造期注入的 callback。
- CronMessagePublisher 将任务转为 source=cron 的 InboundMessage，并使用内部提示词要求在原会话中执行与报告。

因此定时任务会照常经过 AgentLoop、Session 保存、摘要、长期记忆和 Channel 投递，而不是由 CronService 直接调用 Provider 或 QQ。

### 7.4 Subagent

**SubagentManager**（**nanobot/subagent/manager.py**）为子任务构造独立 AgentRunSpec：只有 ContextBuilder 生成的 subagent system prompt 与 task HumanMessage，不继承主 Session 历史，也不写主 Session。

它使用独立 ToolRegistry，通过 ToolLoader 加载允许工具；spawn、create_goal、update_goal 被禁止，防止递归生成子 Agent 或修改主会话目标。

- SpawnTool 的 wait=true 同步等待子 Agent，最终文本作为普通 ToolResult 返回。
- wait=false 创建内存后台任务，记录 pending、running、completed、failed、cancelled、timeout 状态和原始路由。
- 后台任务完成或失败后，SubagentManager 发布带 source=subagent、task_id 和原始路由的内部 InboundMessage；主 Agent 再正常整理并回复用户。

后台任务状态不持久化；Application 与 AgentLoop 关闭时会取消并等待它们。

## 8. Channel、API 和 Web UI

### 8.1 Channel 抽象

**BaseChannel**（**nanobot/channels/base.py**）定义小接口：start、stop、send、receive_external。实现者只负责外部协议；基类负责构造入站消息。

**ChannelManager** 负责默认 Channel 的生命周期和一个出站 dispatcher。AgentLoop 不持有 QQ 或 WebSocket 对象。

### 8.2 QQ

QQChannel 支持 C2C 和群 @ 消息，保存入站 message ID 与 chat type 以便普通回复引用正确上下文。QQ 默认关闭 streaming，因此通常接收完整普通回复。

metadata.source 为 cron、goal、message、subagent 的消息被视为主动发送：仍按原 chat type 发送，但不复用缓存入站 message ID，以避免 QQ 的 msg_seq 去重。

### 8.3 WebSocket

WebSocketChannel 使用 aiohttp 在本地监听 **/ws**。一个 session 同时只绑定一个活动连接，避免结果发送到错误浏览器窗口。

| 方向 | 事件 | 含义 |
| --- | --- | --- |
| 客户端 → 服务端 | message | chat_id、session_id、content；普通文本和斜杠命令共用 |
| 客户端 → 服务端 | authenticate | 静态认证启用时的首个事件 |
| 服务端 → 客户端 | ready | 连接建立，可包含 authentication_required |
| 服务端 → 客户端 | authenticated | token 校验成功 |
| 服务端 → 客户端 | message | 非流式完整回复 |
| 服务端 → 客户端 | delta | 流式文本 chunk |
| 服务端 → 客户端 | tool_call | 即将执行工具的 ID、名称、参数 |
| 服务端 → 客户端 | turn_end | 一轮流式回复的完整文本和元数据 |
| 服务端 → 客户端 | error | 协议、认证或投递错误 |

连接关闭时，WebSocketChannel 经既有 /stop 路径停止它最后绑定 session 的运行，避免浏览器离开后遗留流式任务。

### 8.4 HTTP API

HttpApiService 当前提供：

- GET /health：健康检查，不返回配置、token 或 Session 数据。
- POST /v1/messages：同步发送消息并得到最终文本。
- GET /v1/sessions：只读会话摘要列表。
- GET /v1/sessions/{session_id}：只读可见 user/assistant 历史；assistant tool call 可见，tool result 不暴露。

认证开启时，除 /health 外的 HTTP 路由使用 Authorization: Bearer token。这是本地静态 token 认证，不是用户体系、角色权限或多租户授权。

### 8.5 Web UI

**webui/** 是独立 React + TypeScript + Vite 项目，不由 Python 后端托管静态资源。它从仓库根目录 .env 读取 VITE_NANOBOT_WEBSOCKET_URL、VITE_NANOBOT_API_URL 和可选 VITE_NANOBOT_AUTH_TOKEN。

~~~text
main.tsx
  -> App.tsx
      -> sessions.ts：读取会话列表与历史
      -> useNanobotWebSocket：连接、认证、发送、接收
          -> webSocketConnection：有限重连与过期回调隔离
          -> protocol.ts：严格解析服务端事件
          -> chatState.ts：纯状态变换
      -> MessageContent：用户纯文本；assistant 安全 Markdown 与工具详情
~~~

App 负责页面级 session 选择、历史加载、输入和滚动；useNanobotWebSocket 只处理连接、发送、接收和认证；chatState 只更新消息、流式状态和错误；sessions.ts 只负责 HTTP Session 请求。重连成功后，前端重新读取当前 Session 的已持久化历史，并丢弃未确认 delta，避免把中断片段误当成持久化对话。

前后端分离的好处是 Python Agent 服务可独立运行，Web UI 也能独立构建和迭代；二者只需维护明确的 HTTP 与 WebSocket 协议。

## 9. 配置和启动流程

### 9.1 nanobot.json 的层次

后端运行配置统一位于 **.nanobot/nanobot.json**，模板为 **.nanobot/nanobot.example.json**。

| 配置段 | 用途 |
| --- | --- |
| workspace | Session、记忆、Cron、Skills 等运行数据根目录 |
| agent | context_window_tokens、摘要阈值与最近历史预算 |
| cron | 默认 IANA 时区 |
| logging | nanobot 包日志级别 |
| api | 本地 HTTP API 监听与请求超时 |
| auth | 静态 token 开关与 token |
| provider | Provider 类型、模型、API 地址、密钥、默认生成参数、单次 LLM 请求超时与最大重试次数 |
| tools.web_search | Tavily key |
| channel | default、qq、websocket 配置 |
| mcp.servers | MCP server 连接与工具启用配置 |

**nanobot/config/loader.py** 只从 JSON 读取后端配置。auth.enabled 为 true 且 token 为空时会生成高强度 token，并原子写回配置文件；后续启动复用它。真实密钥和 token 不应提交、输出到日志或放进错误响应。

### 9.2 Application 启动顺序

Application 构造阶段创建 Provider、共享 ToolRegistry、ContextBuilder、SessionManager、SubagentManager、builtin tools、SessionCompactor、Memory 组件、MCPProvider、AgentLoop、HTTP API 与默认 Channel。

**Application.start** 的运行顺序：

1. MCPProvider.connect_all：逐个连接 MCP server，个别失败不会阻止其他连接。
2. 创建 AgentLoop.run 后台任务。
3. 启动 ChannelManager 和默认 Channel 的出站 dispatcher。
4. 启动 CronService。
5. 启动启用的 HttpApiService。

CLI 在加载配置前初始化包日志，Application 创建后再按 logging.level 调整日志。

### 9.3 Application 关闭顺序

**Application.close** 使用关闭锁保证幂等，按下列顺序尝试释放资源：

~~~text
HTTP API
  -> CronService
  -> ChannelManager（dispatcher 与连接）
  -> 取消 AgentLoop 主任务
  -> AgentLoop.close（入站、摘要、记忆、Goal、Subagent 后台任务）
  -> MCPProvider.close（注销动态工具并关闭 transport/session）
~~~

普通关闭异常会被记录，但后续资源仍有机会关闭；若关闭中收到取消，Application 先完成清理，再传播取消。这一顺序先停止外部流量，再停止调度和消费，最后关闭模型相关资源。

## 10. 关键不变量

继续开发时必须保持以下约束：

1. **工具消息顺序完整。** 每个 AIMessage 的 tool_calls 必须有按顺序追加的 ToolMessage；Goal 用户输入只能在完整工具批次后注入。
2. **Session 不保存不完整 turn。** AgentLoop 只在 AgentRunner 返回后保存 user、assistant、tool 消息；取消、Provider 错误和未完成工具批次不落盘。
3. **system prompt 不写入 Session。** system prompt、长期记忆、Skills 和摘要都是每次请求重建的上下文。
4. **同一 Session 串行。** 普通 turn 的读历史、模型调用和保存受同一 session lock 保护；不同 session 不得混入消息。
5. **流式事件有序且一次收束。** delta/tool_call 用 await 发布；正常流式 turn 只发一次 turn_end，取消只发一次 cancelled turn_end；Provider 错误只发一次 `event="error"`，不伪造 turn_end。
6. **AgentLoop 不依赖具体 Channel。** 它只处理消息对象和 MessageBus；新增 Channel 不应改写它的核心逻辑。
7. **Tool 不依赖具体 Channel。** 主动消息也经 MessageBus 投递，不直接调用 QQ SDK 或 WebSocket。
8. **Goal 权限按 Session 隔离。** Goal 模式禁用 create_goal，普通模式禁用 update_goal；/goal stop 必须保存终态并取消任务。
9. **后台结果回到原路由。** Cron、Subagent 和 MessageTool 保留原 session/channel/chat/sender，不能投递给其他会话。
10. **Memory cursor 只在确认后推进。** MEMORY.md 更新成功或模型明确无记忆可写时才跳过事件；失败必须保留 cursor。
11. **WebSocket 断开不会遗留任务。** 断开触发同一 session 的 /stop 路径，Channel.stop 清理连接和 handler。
12. **敏感值不得外泄。** API key、QQ secret、授权 token、Authorization header 和消息内容不得写进日志、错误响应或文档示例。

## 11. 设计取舍

本项目有意保持教学版复杂度；以下省略不是已经具备的生产保证。

| 当前取舍 | 原因与影响 |
| --- | --- |
| 仅有有限 Provider retry，没有 fallback、Retry-After、熔断或全局 Agent deadline | 当前为每次 Provider `complete`/`stream` 尝试提供可配置超时和受控 transient retry；生产环境仍需容量信号、熔断、模型切换、总请求 deadline 和成本控制。 |
| 没有跨进程 Session 锁 | 当前单进程 asyncio lock 足够说明顺序语义；多进程需文件锁、数据库事务或分布式协调。 |
| 没有 Pairing、登录或角色权限 | 当前只有面向本地服务的静态 token，不能当作完整身份授权。 |
| HTTP API 不完整兼容 OpenAI | 只提供本项目所需消息与 Session 读取接口，未实现 HTTP 流式、完整协议和异步任务查询。 |
| MCP 只支持 tools | resources、prompts、OAuth、热加载、复杂重连和二进制结果尚未接入。 |
| WebSocket 没有 turn_id、断点续传、多媒体或多会话订阅 | 当前用单 session 单连接与重连后读取持久化历史保持简单，无法恢复未保存 delta。 |
| token 估算不是真实 tokenizer | 去除模型特定依赖，换取稳定可测试性，但预算精度低于生产实现。 |
| Cron 不支持 cron 表达式、重试或分布式调度 | 当前只实现 at/every 与单进程持久化恢复。 |
| Subagent 状态仅在内存 | 便于说明任务生命周期和回传，但进程重启后不恢复。 |
| 长期记忆没有专门冲突/去重算法 | 当前依赖整理提示词生成完整替换内容，生产实现通常需要审计与冲突策略。 |

## 12. 已完成、暂缓与后续方向

### 已完成的核心能力

以 [开发进度](DEVELOPMENT_PROGRESS.md) 为准，当前已完成：

- Provider 抽象、OpenAI-compatible 与 Anthropic-compatible 实现、文本流式回调、单次请求超时、有限 transient retry 与统一错误结果。
- Tool 基础设施、builtin 文件/命令/网络/消息/Goal/Cron/Spawn 工具和 MCP 动态工具。
- JSONL Session、上下文预算裁剪、Session 摘要、持久化 GoalState。
- MEMORY.md、持久化记忆事件队列与 cursor 恢复。
- CommandRouter、Goal continuation、Goal 用户输入注入与会话级停止。
- 可持久化 at/every Cron 任务及其进入 Agent 的链路。
- 同步与后台 Subagent，以及状态与消息回传。
- QQ Channel、静态认证和流式协议的 WebSocket Channel、本地 HTTP API。
- 独立 React Web UI：会话列表、Markdown、工具调用展示、停止、认证、有限重连和斜杠命令提示。

最新完整离线 Python 测试为 **521 passed, 10 skipped**；前端构建和测试命令见 **webui/README.md**。

### 暂时跳过的功能

开发进度仍明确列为待开发或暂缓的能力包括：

- write_stdin、list_exec_sessions、图像生成和更多开发工具；
- 更多真实 Channel，以及媒体、文件和更完整流式支持；
- HTTP 流式响应、异步任务查询、完整 OpenAI API 兼容；
- WebSocket 多会话订阅、广播、断点续传和多媒体；
- MCP resources、prompts、OAuth、重连、热加载与插件；
- Provider fallback、Retry-After/熔断、并行工具、真实 tokenizer、长期记忆冲突处理；
- 跨进程 Session 协调、后台任务持久化、生产级 sandbox、可观测性和可靠投递。

### 推荐扩展路径

延续现有边界能保持代码可读：

1. 新 Channel：实现 BaseChannel，不直接改 AgentLoop。
2. 新 Tool：实现 Tool，使用 ToolContext/RequestContext，不直接访问具体 Channel。
3. 新 Provider：实现 LLMProvider，通过 ProviderFactory 注册，不将厂商格式泄漏到 Runner。
4. 新持久化或后台能力：先明确状态所有者，再纳入 Application 的启动/关闭顺序。
5. 新浏览器能力：先扩展协议类型和 WebSocketChannel，再分别调整 webui 的 hook、状态和渲染。

这也是当前项目最重要的学习目标：从清楚的调用链与职责边界出发逐步增加复杂度，而不是把生产系统的所有兼容 glue code 提前搬进来。
