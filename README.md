# nanobot

`nanobot` 是一个从零实现的 Python Agent 学习项目。它的重点是把 Agent 的关键边界做得清晰、可阅读、可测试，而不是一比一复刻生产级 Agent 框架。

## 当前能力

- 统一的 `LLMProvider` 抽象，以及 OpenAI-compatible 和 Anthropic-compatible Provider。
- Provider 无关的消息、工具调用和 `LLMResponse` 模型。
- 内置 workspace 工具：读取、写入、精确编辑、列目录和一次性执行命令；四个文件工具统一位于 `tools/builtin/filesystem.py`，共用 workspace 路径安全边界。
- `ToolRegistry`、`ToolLoader` 与 MCP tools 接入；MCP 支持 stdio、SSE 和 Streamable HTTP。
- 支持文本流式与非流式调用的 AgentRunner 工具调用循环，以及基于 `asyncio.Queue` 的 MessageBus。
- QQ 文本 Channel、最小 WebSocket Channel，以及独立的 React + TypeScript Web UI；均复用 ChannelManager、Application 生命周期和 `python -m nanobot` CLI 入口。WebSocket 默认仅监听本机，连接后经现有 `MessageBus` 与 AgentLoop 通信。
- 基于 `aiohttp` 的最小本地 HTTP API：`GET /health` 和 `POST /v1/messages`。请求经 `AgentLoop` 处理并同步返回结果，保留 Session、命令、目标模式和工具调用行为。
- workspace 下的 JSONL Session 持久化、请求侧上下文裁剪和 Session 摘要压缩。当前 turn 仅在 `AgentRunner` 成功返回完整结果后原子保存，失败或取消不会留下半截历史。
- Session 级持续目标：`GoalState` 独立持久化；`/goal <objective>` 或普通模式下的 `create_goal` 工具保存目标后，都会在同一 session 中投递一次基于当前上下文的目标 turn。`create_goal` 仅负责创建与调度确认，实际目标执行由后续内部消息完成；目标模式中的 `update_goal` 可更新或停止当前目标。目标达到 `max_iterations` 时，会先持久化完整工具批次，再通过内部 continuation 继续执行，并受每个目标的续跑上限约束；中间结果不会发送给用户。目标仅在返回非空文本时标记为 `completed`，空结果和执行异常标记为 `failed`；运行期间的普通用户输入按 session 合并并在工具调用安全点注入当前 Runner，不会并发启动第二个 Runner；`/goal status` 可查询状态，`/goal stop` 会取消 active goal 及其正在执行的目标 turn，进行中的目标会阻止 `/new` 重置会话。
- 长期记忆：`MEMORY.md` 读取、LLM 整理，以及由 `history.jsonl` 和 `.memory_cursor` 驱动的可恢复后台事件队列。每个成功持久化的 Agent turn 都会进入该队列。
- workspace Skills：静态 Skill 发现、always-active 指令、`$skill-name` 当前请求激活和环境依赖可用性检查。

## 结构概览

```text
QQ / WebSocket Channel <- Web UI
QQ / WebSocket Channel -> MessageBus -> AgentLoop -> AgentRunner -> LLMProvider
HTTP API ----------------------------^             |
                                    +-> ToolRegistry -> builtin / MCP tools
                                    |
                                    +-> SessionManager -> workspace/sessions
                                    +-> MemoryStore -> workspace/memory
```

HTTP API 为了返回当前请求的响应，会直接调用 `AgentLoop.process_inbound()`，而不是把原始 HTTP 请求再次发布到 `MessageBus`；两种入口仍共享同一套 Agent 执行路径。`Application` 只负责组装组件与生命周期；Provider、工具、Channel 和 Agent 执行循环保持独立职责。

## 配置

- `.env` 主要保存敏感配置，例如 `NANOBOT_API_KEY` 和 QQ 凭据；可从 `.env.example` 开始填写。`VITE_NANOBOT_WEBSOCKET_URL` 是 Web UI 需要读取的浏览器可见地址，不应填写密钥，并需与 `channel.websocket` 的 host、port 和 `/ws` 路径保持一致。
- `.nanobot/nanobot.json` 保存非敏感运行配置。`workspace` 是共享根目录；`agent` 包含上下文与压缩预算，`cron` 包含时区，`channel` 包含默认 Channel、QQ/WebSocket 设置及其 `streaming` 开关，`mcp.servers` 保存 MCP Server 列表；Provider、日志和本地 HTTP API 分别位于 `provider`、`logging` 与 `api` 区块。
- workspace 是 Agent 可操作与存储运行时数据的范围。Session、长期记忆和记忆事件默认写入 workspace，项目的 `/.nanobot/workspace/` 已被 Git 忽略。

不要把 API key、QQ secret、Session 内容或 workspace 运行时数据提交到仓库。

## Skills

Skill 位于 `<workspace>/skills/<skill_name>/SKILL.md`。普通 Skill 只作为可用能力摘要提供；用户可在当前消息中使用 `$skill-name` 注入其正文。标记为 `always: true` 的可用 Skill 会在每轮请求中自动注入。

依赖统一声明在 `nanobot.requires` 命名空间：

```yaml
name: github
description: Interact with GitHub through gh.
nanobot:
  requires:
    bins: ["gh"]
    env: ["GITHUB_TOKEN"]
```

每次请求都会使用当前 `PATH` 和进程环境检查这些依赖。缺少依赖的 Skill 不会被自动或显式注入；Skill 文件中的命令、脚本和安装建议不会被执行。

## 运行

完成本地依赖安装并填写配置后，可通过以下命令启动：

```powershell
python -m nanobot --config .nanobot/nanobot.json
```

可临时覆盖 workspace：

```powershell
python -m nanobot --config .nanobot/nanobot.json --workspace <workspace-path>
```

启用 `.nanobot/nanobot.json` 中的 `api.enabled` 后，可从本机调用：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health

Invoke-RestMethod http://127.0.0.1:8000/v1/messages `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"session_id":"example-session","content":"你好"}'
```

`POST /v1/messages` 返回 `session_id` 和最终 `content`。`session_id` 会作为稳定的会话标识；请求超时、输入校验和 Agent 处理失败会返回对应的 JSON HTTP 错误。

当 `channel.default` 为 `websocket` 时，可连接 `ws://127.0.0.1:8765/ws`。连接成功会收到 `ready` 事件；客户端可发送：

```json
{"type":"message","chat_id":"example-chat","content":"你好"}
```

服务会将同一 session 的 Agent 输出返回对应连接：普通回复为 `message`；启用文本流式时，按顺序返回多个 `delta`，并以一个 `turn_end` 结束本轮。`turn_end` 包含最终文本，以及 `metadata.tools_used`、`metadata.token_usage` 和 `metadata.stop_reason`。QQ 默认关闭流式，WebSocket 默认开启，可通过 `channel.qq.streaming` 和 `channel.websocket.streaming` 调整。当前不提供认证或重连恢复，因此仅适合受信任的本地开发环境。

## Web UI

`webui/` 是与 Python 后端解耦的 React + TypeScript + Vite 前端。它通过既有 WebSocket Channel 建立一个固定的浏览器 session，发送现有 `message` 协议，并在 `delta` 与 `turn_end` 事件间累积展示流式回复。当前只提供最小聊天闭环，不包含历史加载、认证、自动重连或多会话管理。

```powershell
cd webui
npm install
npm test
npm run build
npm run dev
```

## 测试

完整离线测试：

```powershell
$env:RUN_DEEPSEEK_LIVE_TESTS='0'
$env:NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS='0'
python -B -m unittest discover -s tests -t . -p "test*.py"
```

真实 Provider 或 QQ 测试默认不应依赖网络或真实凭据；仅在本地明确设置对应环境变量后再运行。

Web UI 的协议状态测试与生产构建：

```powershell
cd webui
npm test
npm run build
```

## 有意留到后续的能力

- 并行工具调度、重试与 fallback，以及工具调用和 reasoning 的流式事件。
- 真实 tokenizer、上下文摘要的多级策略和长期记忆冲突解决。
- 多进程/分布式锁、记忆事件归档与可靠任务恢复。
- 除 QQ 和 WebSocket 外的真实 Channel、消息可靠投递与总线持久化。
- HTTP API 的认证、流式响应、异步任务查询、限流与完整 OpenAI 兼容协议；WebSocket/Web UI 的认证、多会话订阅、广播、历史加载与重连恢复。
- 完整 JSON Schema 校验、工具插件生态及更复杂的安全沙箱。
- Skill 的自动选择、安装/更新、脚本执行、权限控制与插件来源。

详细开发进度和已知限制见 [DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md)，协作与开发规范见 [AGENTS.md](AGENTS.md)。
