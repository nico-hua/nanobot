# nanobot

`nanobot` 是一个从零实现的 Python Agent 学习项目。它的重点是把 Agent 的关键边界做得清晰、可阅读、可测试，而不是一比一复刻生产级 Agent 框架。

## 当前能力

- 统一的 `LLMProvider` 抽象，以及 OpenAI-compatible 和 Anthropic-compatible Provider。
- Provider 无关的消息、工具调用和 `LLMResponse` 模型。
- 内置 workspace 工具：读取、写入、精确编辑、列目录和一次性执行命令；四个文件工具统一位于 `tools/builtin/filesystem.py`，共用 workspace 路径安全边界。
- `ToolRegistry`、`ToolLoader` 与 MCP tools 接入；MCP 支持 stdio、SSE 和 Streamable HTTP。
- 最小 AgentRunner 工具调用循环，以及基于 `asyncio.Queue` 的 MessageBus。
- QQ 文本 Channel、ChannelManager、Application 生命周期与 `python -m nanobot` CLI 入口。
- workspace 下的 JSONL Session 持久化、请求侧上下文裁剪和 Session 摘要压缩。当前 turn 仅在 `AgentRunner` 成功返回完整结果后原子保存，失败或取消不会留下半截历史。
- Session 级持续目标：`GoalState` 独立持久化；`/goal <objective>` 保存目标后，会在同一 session 中启动一次基于当前上下文的目标 turn。目标成功后标记为 `completed`，执行失败标记为 `failed`；`/goal status` 可查询状态，`/goal stop` 会取消 active goal 及其正在执行的目标 turn，进行中的目标会阻止 `/new` 重置会话。
- 长期记忆：`MEMORY.md` 读取、LLM 整理，以及由 `history.jsonl` 和 `.memory_cursor` 驱动的可恢复后台事件队列。每个成功持久化的 Agent turn 都会进入该队列。
- workspace Skills：静态 Skill 发现、always-active 指令、`$skill-name` 当前请求激活和环境依赖可用性检查。

## 结构概览

```text
Channel -> MessageBus -> AgentLoop -> AgentRunner -> LLMProvider
                              |             |
                              |             +-> ToolRegistry -> builtin / MCP tools
                              |
                              +-> SessionManager -> workspace/sessions
                              +-> MemoryStore -> workspace/memory
```

`Application` 只负责组装组件与生命周期；Provider、工具、Channel 和 Agent 执行循环保持独立职责。

## 配置

- `.env` 保存敏感配置，例如 `NANOBOT_API_KEY` 和 QQ 凭据；可从 `.env.example` 开始填写。
- `.nanobot/nanobot.json` 保存非敏感运行配置，例如 workspace、Provider 类型与模型、上下文窗口、日志等级、默认 Channel 和 MCP Server。
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

## 测试

完整离线测试：

```powershell
$env:RUN_DEEPSEEK_LIVE_TESTS='0'
$env:NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS='0'
python -B -m unittest discover -s tests -t . -p "test*.py"
```

真实 Provider 或 QQ 测试默认不应依赖网络或真实凭据；仅在本地明确设置对应环境变量后再运行。

## 有意留到后续的能力

- 流式 AgentRunner、并行工具调度、重试与 fallback。
- 真实 tokenizer、上下文摘要的多级策略和长期记忆冲突解决。
- 多进程/分布式锁、记忆事件归档与可靠任务恢复。
- 除 QQ 外的真实 Channel、消息可靠投递与总线持久化。
- 完整 JSON Schema 校验、工具插件生态及更复杂的安全沙箱。
- Skill 的自动选择、安装/更新、脚本执行、权限控制与插件来源。

详细开发进度和已知限制见 [DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md)，协作与开发规范见 [AGENTS.md](AGENTS.md)。
