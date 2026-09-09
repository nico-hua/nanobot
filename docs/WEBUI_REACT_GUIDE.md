# Web UI React 代码导读

这份文档面向需要审查或维护 Nanobot 前端代码的后端开发者与 React 初学者。目标是帮助你顺着当前代码理解页面、状态和网络请求如何协作，而不是系统讲解 React 的所有概念。

本文以当前 `webui/src` 的实现为准。阅读时建议直接打开对应文件，对照本文中的职责和调用关系查看。

## 建议的阅读顺序

1. `webui/src/main.tsx`：浏览器入口，确认 React 从哪里启动。
2. `webui/src/App.tsx`：页面编排、会话选择、输入框和主要交互。
3. `webui/src/types/protocol.ts`：前后端 WebSocket 和 Session API 的数据形状。
4. `webui/src/hooks/useNanobotWebSocket.ts`：连接、认证、发送和接收事件。
5. `webui/src/hooks/chatState.ts`：聊天状态如何被纯函数更新。
6. `webui/src/hooks/webSocketConnection.ts`：断线重连和旧连接回调隔离。
7. `webui/src/api/sessions.ts`、`components/` 与 `commands.ts`：会话 API、消息展示和命令提示。

## 1. 本项目实际用到的 React 概念

### 组件与 JSX

React 组件通常是“接收数据，返回界面描述”的函数。返回值中的 `<section>`、`<button>` 这类写法叫 JSX，可以把它理解为写在 TypeScript 中的 HTML 模板。

例如 `App.tsx` 的默认导出 `App` 是整个页面组件；`MessageContent` 和 `CommandSuggestionPanel` 是被 `App` 组合使用的小组件：

```tsx
<MessageContent {...message} />

<CommandSuggestionPanel
  suggestions={commandSuggestions}
  onSelect={handleCommandSelection}
/>
```

React 在状态变化后会重新执行相关组件函数，并将新的 JSX 差异更新到浏览器页面。组件不应直接手工拼接或修改 DOM；本项目只有滚动和输入框聚焦这类浏览器交互通过 `ref` 访问 DOM。

### Props：父组件传给子组件的数据

Props 是组件的输入参数。`CommandSuggestionPanel` 的 Props 明确声明为：

```ts
type CommandSuggestionPanelProps = {
  suggestions: readonly SlashCommandSuggestion[];
  onSelect: (insertText: string) => void;
};
```

因此它只负责渲染建议列表，并在用户点击时调用父组件给它的 `onSelect`。它不持有输入框状态，也不会直接发送 WebSocket 消息。`MessageContent` 同样只接收一条用户或助手消息并负责显示。

这种拆分是审查前端代码时的重要边界：展示组件应尽量不拥有网络和业务状态。

### State：会触发重新渲染的数据

`useState` 用于保存组件运行期间会改变、且改变后需要反映到界面的数据。例如 `App.tsx` 中：

```ts
const [draft, setDraft] = useState("");
const [sessionId, setSessionId] = useState(() => createSessionId());
const [sessions, setSessions] = useState<SessionInfo[]>([]);
```

- `draft` 是输入框当前文字；`setDraft` 后，受控的 `<textarea value={draft}>` 会显示新值。
- `sessionId` 是浏览器当前选择的会话标识。切换会话或点击 **New session** 会更新它。
- `sessions` 是左侧栏的会话摘要列表。

聊天消息和连接状态没有散落在 `App` 中，而是由 `useNanobotWebSocket` 内部的 `ChatState` 管理。这样状态更新规则可以独立测试。

### Hook：复用带状态或生命周期的逻辑

Hook 是以 `use` 开头的函数，用来在函数组件中复用状态、生命周期或浏览器能力。本项目主要使用以下几类：

- `useState`：保存状态。
- `useEffect`：在页面渲染后执行副作用，例如请求历史、建立 WebSocket、设置清理逻辑。
- `useCallback`：稳定函数引用，避免 `useEffect` 因函数每次渲染重新创建而重复执行。
- `useRef`：保存不需要触发重渲染的可变值，或拿到 DOM 节点。
- 自定义 Hook `useNanobotWebSocket`：将 WebSocket 生命周期封装成可供页面调用的状态和函数。

`useEffect` 的关键点是它可以返回清理函数。`useNanobotWebSocket.ts` 建立连接后返回一个清理函数，组件卸载或 URL 改变时会关闭旧连接，避免后台残留连接。

### `useRef`：不触发重渲染的“盒子”

`useRef` 返回一个 `{ current: ... }` 对象；修改 `current` 不会让页面重新渲染。当前项目的典型用途：

- `conversationRef`：指向对话滚动容器，以便滚动到底部。
- `draftInputRef`：命令建议被选中后让输入框重新获得焦点。
- `historyRequestRef`：递增的请求序号。较早的历史请求即使晚返回，也不会覆盖刚切换到的新会话。
- `connectionRef`：保存当前 `WebSocketConnection`，供发送、停止和手动重连调用。
- `activeSessionRef`：让异步 WebSocket 回调始终读取最新 `sessionId`，过滤已切走会话的迟到事件。

如果一个值会改变页面显示，应优先用 State；如果只是供异步回调、DOM 操作或去重使用，才考虑 Ref。

### 事件处理

React 通过 JSX 属性注册浏览器事件。当前页面中的例子：

- 表单 `onSubmit={handleSubmit}`：阻止浏览器默认提交，然后调用 `sendMessage(draft)`。
- 文本框 `onChange={(event) => setDraft(event.target.value)}`：把用户输入写回 `draft` State。
- 停止按钮 `onClick={handleStop}`：调用 Hook 返回的 `stopGeneration()`。
- 会话按钮 `onClick={() => selectSession(session.sessionId)}`：切换当前会话。

事件处理函数只做本层职责。比如 `handleStop` 不知道 `/stop` 的网络格式；这个细节由 `useNanobotWebSocket` 和 `protocol.ts` 负责。

### 条件渲染与列表渲染

条件渲染是根据状态决定是否返回一段 JSX。项目常用三元表达式：

```tsx
{isSending ? <button type="button">Stop</button> : null}
```

因此停止按钮仅在当前流式 turn 进行时出现。命令面板也只在 `getSlashCommandSuggestions(draft)` 有结果时显示。

列表渲染使用数组的 `.map()`：

```tsx
{messages.map((message) => (
  <li key={message.id}>
    <MessageContent {...message} />
  </li>
))}
```

`key` 是 React 用来识别同一列表项的稳定标识。这里使用前端生成的 `message.id`；会话列表使用后端返回的 `session.sessionId`。

### TypeScript 联合类型与穷尽检查

前端没有使用 `any` 来处理网络 JSON。`protocol.ts` 定义 `ServerEvent` 联合类型，例如 `delta`、`tool_call`、`turn_end`。`parseServerEvent()` 先校验未知 JSON，再返回可信的联合类型。

`chatState.ts` 用 `switch (event.type)` 分支处理事件。`default` 分支赋给 `never`，使 TypeScript 在新增事件却漏处理时给出编译错误。这是本项目避免前后端协议分支遗漏的重要手段。

### 当前没有使用的 React 能力

当前 Web UI 是单页应用，未使用以下能力：

- 没有 React Router：没有 URL 路由和多页面视图。
- 没有 React Context：没有全局 Provider 注入状态。
- 没有 Redux、Zustand 等外部状态库：页面状态由 `useState` 和 `ChatState` 纯函数维护。

不要为了“更像 React 项目”而提前引入这些机制；只有多页面导航、跨层共享状态等真实需求出现时再考虑。

## 2. `webui/src` 目录结构

```text
webui/
├── public/
│   └── nanobot-logo.png          # 被 App 顶栏通过 /nanobot-logo.png 引用
├── src/
│   ├── api/
│   │   └── sessions.ts           # 只读 Session HTTP API 客户端
│   ├── components/
│   │   ├── CommandSuggestionPanel.tsx
│   │   └── MessageContent.tsx
│   ├── hooks/
│   │   ├── chatState.ts          # 纯聊天状态转换函数，不是 React 组件
│   │   ├── useNanobotWebSocket.ts
│   │   └── webSocketConnection.ts
│   ├── types/
│   │   └── protocol.ts           # 前后端传输类型、构造和解析
│   ├── App.tsx                   # 唯一页面组件与页面级编排
│   ├── App.css                   # 页面、消息、命令面板的局部样式
│   ├── commands.ts               # 前端命令提示目录与过滤函数
│   ├── conversationScroll.ts     # 自动滚动判断的纯工具函数
│   ├── index.css                 # 全局视口和基础样式
│   ├── main.tsx                  # React 浏览器入口
│   └── vite-env.d.ts             # Vite 环境变量类型声明
├── test/
│   └── chatState.test.mjs        # Node 内置测试运行器的前端状态测试
├── index.html                    # Vite HTML 壳，含 #root
├── vite.config.ts                # Vite 与仓库根 .env 设置
└── package.json                  # npm 脚本和前端依赖
```

这里的目录边界很明确：`types` 描述数据，`api` 描述 HTTP 读取，`hooks` 描述连接和状态，`components` 只做局部展示，`App.tsx` 将它们组合为页面。

## 3. 关键文件与依赖关系

| 文件 | 负责什么 | 主要导出 | 被谁使用 | 主要依赖 |
| --- | --- | --- | --- | --- |
| `main.tsx` | 找到 `#root` 并启动 React | 无业务导出 | `index.html` | `react-dom/client`、`App`、`index.css` |
| `App.tsx` | 页面编排、会话选择、输入与滚动 | 默认 `App` | `main.tsx` | Session API、WebSocket Hook、组件、样式 |
| `types/protocol.ts` | 定义并校验浏览器与后端交换的数据 | 事件/消息类型、解析与构造函数 | Hook、Session API、测试 | 无 UI 依赖 |
| `hooks/chatState.ts` | 聊天、连接、流式状态的纯转换 | `ChatState` 与多个状态函数 | `useNanobotWebSocket`、测试 | `protocol.ts` 类型 |
| `hooks/useNanobotWebSocket.ts` | React 层的连接、认证、发送、接收 | `useNanobotWebSocket` | `App.tsx` | 状态函数、协议函数、连接管理器 |
| `hooks/webSocketConnection.ts` | 命令式 WebSocket 生命周期与有限重连 | `createWebSocketConnection` | WebSocket Hook、测试 | 浏览器 `WebSocket`、计时器 |
| `api/sessions.ts` | 调用只读会话 API 并解析响应 | `fetchSessionSummaries`、`fetchSessionHistory`、`createSessionId` | `App.tsx`、测试 | `fetch`、协议类型 |
| `components/MessageContent.tsx` | 渲染一条消息、工具调用和 Markdown | `MessageContent` | `App.tsx` | `react-markdown`、`remark-gfm` |
| `components/CommandSuggestionPanel.tsx` | 显示可点击的斜杠命令建议 | `CommandSuggestionPanel` | `App.tsx` | `commands.ts` 类型 |
| `commands.ts` | 前端命令提示数据与前缀过滤 | `SLASH_COMMAND_SUGGESTIONS`、`getSlashCommandSuggestions` | `App.tsx`、测试 | 无网络依赖 |
| `conversationScroll.ts` | 判断用户是否接近对话底部 | `isNearConversationBottom` | `App.tsx`、测试 | 无 React 依赖 |
| `App.css` / `index.css` | 视口布局、消息样式和全局基础样式 | 无 | 入口和 `App` | 浏览器 CSS |

### `main.tsx`：应用从这里进入 React

Vite 加载 `index.html`，其中的 `<script type="module" src="/src/main.tsx">` 执行后：

1. `main.tsx` 查找 HTML 中的 `<div id="root">`；缺失时立即报错。
2. `createRoot(root).render(...)` 将 `App` 挂载到该节点。
3. 外层 `StrictMode` 是开发期辅助检查。它要求 Effect 的建立与清理逻辑能够重复执行而不留下连接；因此 WebSocket Hook 必须正确关闭旧连接。

### `App.tsx`：页面级编排，不直接实现协议细节

`App` 是当前唯一的页面级组件。它拥有页面范围的 State：

| State / Ref | 含义 | 为什么在 `App` |
| --- | --- | --- |
| `draft` | 输入框内容 | 既影响 textarea，又决定命令建议和发送按钮是否可用 |
| `sessionId` | 当前会话 | 同时决定 HTTP 历史读取和 WebSocket 发送的 session |
| `sessions` | 左侧摘要列表 | 属于侧栏展示数据 |
| `isLoadingSessions`、`isLoadingHistory`、`sessionError` | 会话 API 的页面状态 | 与连接 Hook 的网络状态分开，避免职责混杂 |
| `conversationRef`、`shouldFollowLatestRef` | 对话自动滚动 | 只影响 DOM 滚动，不需要每次变化重渲染 |
| `historyRequestRef` | 历史请求版本号 | 防止慢响应覆盖已切换的新会话 |
| `lastConnectionVersionRef` | 上次成功连接版本 | 重连后只重新加载一次持久化历史 |

它调用 `useNanobotWebSocket()` 并取得 `messages`、连接状态、`sendMessage`、`stopGeneration`、`reconnect` 和 `replaceMessages`。因此 `App` 能决定“什么时候渲染或加载会话”，而 Hook 决定“如何处理 socket”。

主要 Effect 的职责如下：

1. 初次挂载时调用 `refreshSessions()` 读取左侧会话摘要。
2. 每次 `sessionId` 变化时调用 `loadSessionHistory(sessionId)`；新生成但未持久化的 Session 返回 404 时显示空对话，而不是报错。
3. WebSocket 成功建立新连接且 `connectionVersion` 递增时，重新加载当前会话的持久化历史。WebSocket 不会重放 delta，因此未确认的流式片段不会被当成真实历史。
4. `isSending` 从 `true` 变为 `false` 时刷新左侧摘要，确保最新预览和更新时间出现。
5. `messages` 变化时，如果用户仍在对话底部附近，滚动到最新消息；用户主动向上滚动后不强制拉回底部。

`App` 不应直接手写 WebSocket JSON，也不应直接解析 API 的未知 JSON。这些细节分别属于 `protocol.ts` 和 `api/sessions.ts`。

### `types/protocol.ts`：前后端协议的唯一前端入口

该文件集中定义三类类型：

- UI 内部消息：`UserMessage`、`AssistantMessage`、`ChatMessage`。
- Session HTTP API 返回的可见历史：`PersistedSessionMessage`、`SessionInfo`、`SessionHistory`。
- WebSocket 客户端/服务端事件：`WebSocketClientMessage`、`WebSocketAuthenticationMessage`、`ServerEvent`。

它还提供三类函数：

- `createWebSocketClientMessage()`：构造普通 `message` 事件。
- `createWebSocketAuthenticationMessage()`：构造认证首事件。
- `createWebSocketStopMessage()`：复用普通 `message` 事件发送 `/stop`。
- `parseServerEvent()`：校验服务端 JSON，不接受缺少路由字段或工具调用字段不完整的对象。
- `isEventForSession()`：过滤不属于当前页面 session 的迟到事件。

新增 WebSocket 事件时，应先更新这里的联合类型和解析逻辑，再更新状态层和展示层。

### `hooks/chatState.ts`：可测试的状态转换层

尽管文件位于 `hooks/`，它并不调用 React Hook；它是一组输入 `ChatState`、返回新 `ChatState` 的纯函数。纯函数不访问 WebSocket、`fetch` 或 DOM，因此测试可以直接传入事件并断言结果。

状态包含：

- `connectionStatus`：连接、重连、断开和错误状态。
- `authenticationStatus`：认证检查、认证中、成功或失败。
- `messages`：当前正在显示的消息。
- `activeAssistantId`：当前流式助手消息的 ID；用于将多个 delta 拼到同一消息上。
- `isSending`、`isStopping`：驱动发送与停止按钮。
- `nextMessageSequence`：生成页面内稳定消息 ID。

事件处理的关键规则：

- `delta`：若不存在活动助手消息则创建一条；否则追加文本。
- `tool_call`：附着到活动助手消息的 `toolCalls`，不会创建普通文本消息。
- `turn_end` 或 `message`：用最终完整文本替换累积 delta，并结束流式状态，避免重复显示。
- 断线重连：删除未确认的活动助手消息；重连后由 HTTP 历史替换 UI，而不是尝试拼接旧 delta。

### `hooks/useNanobotWebSocket.ts`：React 与 WebSocket 的适配层

这个 Hook 是页面唯一的 WebSocket 入口。它向 `App` 暴露状态和操作函数，但不渲染 JSX。

连接后的事件路径是：

```text
浏览器 WebSocket message
  -> handleServerMessage
  -> JSON.parse + parseServerEvent
  -> isEventForSession
  -> applyServerEvent
  -> setState
  -> App 重新渲染
```

认证路径：服务端 `ready(authentication_required: true)` 后，Hook 从环境变量传入的 token 构造一次 `authenticate` 事件；收到 `authenticated` 后才允许 `sendMessage()`。认证失败会显示错误并关闭连接，不把 token 渲染到页面或错误文本中。

发送路径：

1. `sendMessage()` 先检查文本、连接和认证状态。
2. 调用 `beginUserMessage()`，立即把用户输入放到 UI，设置 `isSending`。
3. 用 `createWebSocketClientMessage(chatId, sessionId, text)` 构造 JSON 并发送。

停止路径：`stopGeneration()` 对当前 session 发送 `content: "/stop"`，并以 `stopRequestedRef` 防止重复请求。后端最终仍用 `turn_end` 结束该 turn；已收到的 delta 会保留。

### `hooks/webSocketConnection.ts`：有限重连与旧连接隔离

`createWebSocketConnection()` 是一个不依赖 React 的命令式对象，提供 `start`、`reconnect`、`close`、`isOpen` 与 `send`。

它持有：

- 当前 socket；
- 一个重连计时器；
- `connectionToken`，用于拒绝被替换 socket 的迟到回调；
- 有限重连次数，默认延迟为 500ms、1000ms、1500ms。

网页卸载时 Hook 调用 `close()`，这会清理计时器并禁止继续自动重连。手动点击 **Reconnect** 时，旧 socket 被失效，再创建一个新连接。这个模块不修改 React State；它通过回调通知 Hook。

### `api/sessions.ts`：只读 HTTP 会话客户端

页面经它访问：

- `GET /v1/sessions`：`fetchSessionSummaries()` 返回侧栏摘要。
- `GET /v1/sessions/{session_id}`：`fetchSessionHistory()` 返回当前会话的可见 user/assistant 历史与 assistant tool call。

该模块负责：拼接 API URL、在 token 存在时添加 `Authorization: Bearer ...`、解析 JSON、将网络/格式错误转换为 `SessionApiError`。它不调用 WebSocket，也不修改 React State。

`createSessionId()` 使用 `crypto.randomUUID()` 生成 `webui-` 前缀 ID。创建新会话只改变浏览器状态；首次正常消息完成后由后端 SessionManager 持久化。

### 展示组件与辅助模块

`MessageContent.tsx`：

- 用户消息以纯文本 `<p>` 渲染，不作为 Markdown 解释。
- 助手消息使用 `react-markdown` 和 `remark-gfm` 渲染 GitHub 风格 Markdown。
- 使用 `skipHtml`，不启用原始 HTML 渲染。
- tool call 使用 `<details>` 展示工具名称和格式化 JSON 参数；只有没有正文且没有 tool call 时才显示 `Thinking...`。

`CommandSuggestionPanel.tsx`：只显示建议并回调 `onSelect`。它没有发送能力；点击命令只填充输入框，用户仍需手动发送。

`commands.ts`：维护前端提示目录和前缀过滤。它不是后端 `CommandRouter` 的替代实现，不能在这里新增或改变命令业务语义。

`conversationScroll.ts`：用距底部 48px 的阈值判断是否继续自动滚动。它独立于 React，便于测试。

## 4. 一次完整运行流程

### 页面启动与初始数据

```text
index.html
  -> main.tsx 创建 React root
  -> <App /> 初始化浏览器 sessionId
  -> useNanobotWebSocket 建立 WebSocket
  -> App 请求 GET /v1/sessions
  -> App 请求当前 session 的 GET /v1/sessions/{id}
```

新生成的浏览器 Session 尚未落盘时，历史接口返回 404；`App` 将其视为一个空白会话，而不是错误。

### 认证与连接状态

```text
WebSocket open
  -> 服务端 ready
  -> 若 authentication_required：浏览器发送 authenticate
  -> 服务端 authenticated
  -> 输入框和发送按钮可用
```

认证关闭时，`ready` 没有 `authentication_required`，Hook 将状态设为 `not_required`，现有本地开发流程无需额外步骤。

### 用户发送、流式输出和最终完成

```text
用户输入 + Submit
  -> App.handleSubmit
  -> useNanobotWebSocket.sendMessage
  -> ChatState 立即加入 user 消息
  -> WebSocket { type: "message", chat_id, session_id, content }

服务端 tool_call / delta / turn_end
  -> protocol.ts 校验并过滤 session
  -> chatState.ts 更新同一条 assistant 消息
  -> React 重新渲染 MessageContent
  -> App 在合适时滚动到底部
```

`tool_call` 先显示工具名称和参数；多个 `delta` 连续追加；`turn_end` 携带完整最终文本，替换累积内容并将 `isSending` 设为 `false`。随后 `App` 刷新会话摘要列表。

### 切换、新建与重连

- 点击左侧 Session：先清空可见消息，再更新 `sessionId`；Effect 读取新历史。请求序号避免旧请求晚返回时覆盖新会话。
- 点击 **New session**：只生成新 ID 和清空当前 UI，不删除旧 Session，也不发送 `/new`。
- 网络断开：状态层丢弃未确认的助手流式消息，连接管理器有限次重连。
- 重连成功：`connectionVersion` 变化，`App` 重新读取当前 Session 的持久化历史。项目不实现流式断点续传，因此不会尝试恢复未持久化的 delta。

## 5. 样式与布局

`index.css` 负责全局基础：`html`、`body` 和 `#root` 固定为完整视口并隐藏浏览器外层滚动。

`App.css` 负责组件级布局：

- `.app-shell`：`100dvh` 页面容器。
- `.app-workspace`：桌面端左侧 Session 栏 + 右侧聊天区的 CSS Grid。
- `.conversation-scroll` 与 `.session-list`：各自内部滚动，避免整个页面随着消息增长滚动。
- `.message--user`、`.message--assistant`：区分用户气泡和助手 Markdown 区域。
- `.command-suggestion-panel`：相对于输入框向上浮出的命令提示面板。
- `@media (max-width: 40rem)`：移动端将左右布局改为上下布局。

页面顶部的图标来自 `webui/public/nanobot-logo.png`，通过 `/nanobot-logo.png` 访问；它不是从 `src` 编译导入的模块。

## 6. 测试、构建与审查建议

在 `webui/` 目录运行：

```powershell
npm test
npm run build
```

- `npm test` 先按 `tsconfig.test.json` 编译需要测试的 TypeScript 文件到 `.test-build/`，再用 Node 内置测试运行器执行 `test/chatState.test.mjs`。
- `npm run build` 先做严格 TypeScript 检查，再由 Vite 生成生产构建。

审查一个前端改动时，可按以下顺序检查：

1. 新增或变更 WebSocket/API 字段时，是否先更新了 `types/protocol.ts` 的类型与运行时解析？
2. 新事件是否在 `chatState.ts` 有明确状态转换，并保持 `tool_call`、`delta`、`turn_end` 的顺序语义？
3. Hook 是否只负责连接/请求，页面组件是否只负责编排和显示？
4. `useEffect` 是否有正确依赖和清理逻辑，异步结果是否会覆盖已切换的 session？
5. 新列表是否有稳定 `key`，新用户可见文本是否有加载、错误或空状态？
6. token、服务端错误原文或未验证的 JSON 是否被直接显示、记录或信任？
7. 现有 `npm test` 和 `npm run build` 是否仍能通过？

## 7. 当前职责边界

- Web UI 不直接读取 workspace 下的 Session JSONL，也不调用 Provider 或 AgentLoop。
- 前端只通过 WebSocket 发送普通 `message`、认证事件和复用 `/stop` 的停止请求；命令业务仍由后端 `CommandRouter` 处理。
- Session 列表和历史只通过只读 HTTP API 获取。
- WebSocket Hook 不直接渲染页面，`MessageContent` 不直接发请求，`chatState.ts` 不直接访问浏览器网络或 DOM。
- 当前没有路由、多用户、会话删除/重命名、流式断点续传或跨设备同步。

保持这些边界，能让后端开发者从协议、状态和展示三层独立审查前端变更。
