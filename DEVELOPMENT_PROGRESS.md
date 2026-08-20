# 项目开发进度

最后更新：2026-08-20

## 项目目标

这是一个从零实现的 Agent 架构学习项目，重点是用较小、清晰的代码理解 Agent 系统中的核心边界和执行流程，而不是完整复刻某个生产级项目。

## 当前阶段：LLM Provider 抽象层

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

### 当前测试状态

最近一次离线测试结果：

```text
Ran 23 tests in 0.030s
OK (skipped=6)
```

6 个跳过的测试是需要显式配置 API key 和环境变量后才运行的 live tests，默认不会访问网络。

## 待优化的点

### 1. Provider 的 tools 参数格式不统一

当前两个 Provider 接收的 `tools` 都是原始 `Mapping`：

- OpenAI Provider 期望 OpenAI 风格的包装结构，例如 `type=function` 加 `function` 字段。
- Anthropic Provider 期望 Anthropic 风格的结构，例如 `name`、`description` 和 `input_schema`。

这会导致上层 AgentRunner 必须了解具体 Provider 的协议，不利于厂商无关的调用。

后续实现工具系统时，建议新增统一的领域模型，例如 `ToolDefinition`，由上层只提供工具名称、描述和参数 Schema，再由各 Provider 在边界处转换为厂商协议。`ToolCallRequest` 已经作为模型输出的统一格式保留。

### 2. 工具注册与执行边界尚未建立

目前 Provider 只能传递工具描述并返回工具调用请求，还没有：

- 工具注册表或 `ToolRegistry`；
- 工具处理函数的统一接口；
- 工具参数校验；
- AgentRunner 执行工具并将结果转换为 `ToolMessage` 的流程。

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

1. 是否在工具系统落地时引入统一的 `ToolDefinition`，以及统一 Schema 采用 OpenAI 风格、JSON Schema 风格，还是定义自己的最小领域模型。
2. AgentRunner 应该接收统一工具定义，还是由 Provider 层负责兼容旧的厂商原始格式。
3. 如何统一处理不同厂商的流式事件，尤其是文本、工具调用片段、思考内容和最终 usage。
4. 如何处理 Anthropic、OpenAI 及其他兼容协议在 `max_tokens`、thinking、finish reason 和 usage 字段上的差异。
5. 是否需要支持多轮工具调用，以及如何保证工具结果、调用 ID 和消息历史的一致性。
6. 如何在不泄露凭据的前提下组织 live tests，并在 CI 中默认只运行离线测试。
7. AgentRunner、工具系统、上下文管理、会话和记忆模块尚未实现，Provider 目前还没有接入完整 Agent 执行循环。

## 当前明确不实现的能力

在 AgentRunner 和工具系统完成前，暂不实现以下内容：

- 自动工具发现、注册和执行；
- 多 Provider 自动路由和 fallback；
- 重试、限流、熔断和成本控制；
- 多模态输入、音频、图像和文件内容；
- 结构化输出校验和 JSON Schema 强制解码；
- 复杂的思考过程流式事件暴露；
- 生产级日志、指标、追踪和持久化。

## 下一步建议

1. 先设计最小 `ToolDefinition` 和工具注册接口。
2. 将 `AgentRunner` 接入 `LLMProvider`，实现一次完整的“模型请求 → 工具调用 → 工具结果 → 模型再次请求”循环。
3. 根据 AgentRunner 的实际需求，再收敛 Provider 配置、流式事件和工具 Schema 的统一设计。
