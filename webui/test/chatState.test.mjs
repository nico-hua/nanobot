import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  applyServerEvent,
  beginStopRequest,
  beginUserMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
  markReconnectFailed,
  markReconnecting,
  replaceChatHistory,
} from "../.test-build/hooks/chatState.js";
import {
  DEFAULT_RECONNECT_DELAYS_MS,
  createWebSocketConnection,
} from "../.test-build/hooks/webSocketConnection.js";
import {
  createWebSocketClientMessage,
  createWebSocketStopMessage,
  isEventForSession,
  parseServerEvent,
} from "../.test-build/types/protocol.js";
import {
  createSessionId,
  fetchSessionHistory,
  fetchSessionSummaries,
  SessionApiError,
} from "../.test-build/api/sessions.js";
import { MessageContent } from "../.test-build/components/MessageContent.js";
import { isNearConversationBottom } from "../.test-build/conversationScroll.js";

test("connection state reports connecting, connected, disconnected, and errors", () => {
  let state = createInitialChatState();
  assert.equal(state.connectionStatus, "connecting");

  state = markConnected(state);
  assert.equal(state.connectionStatus, "connected");

  state = markDisconnected(state);
  assert.equal(state.connectionStatus, "disconnected");
  assert.equal(state.error, "The Nanobot WebSocket connection was closed.");

  state = markConnectionError(state, "Could not connect to Nanobot.");
  assert.equal(state.connectionStatus, "error");
  assert.equal(state.error, "Could not connect to Nanobot.");
});

test("a stream interruption discards unconfirmed output before persisted history replaces it", () => {
  let state = beginUserMessage(createInitialChatState(), "Unconfirmed user message");
  state = applyServerEvent(state, {
    type: "delta",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Unconfirmed assistant delta",
  });

  state = markReconnecting(state);
  assert.equal(state.connectionStatus, "reconnecting");
  assert.equal(state.isSending, false);
  assert.deepEqual(
    state.messages.map(({ role, content }) => ({ role, content })),
    [{ role: "user", content: "Unconfirmed user message" }],
  );

  state = replaceChatHistory(state, [
    { role: "user", content: "Persisted user message" },
    { role: "assistant", content: "Persisted reply", toolCalls: [] },
  ]);
  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[1].content, "Persisted reply");

  state = markReconnectFailed(state);
  assert.equal(state.connectionStatus, "error");
  assert.match(state.error, /Could not reconnect/);
});

test("connection lifecycle retries with bounded incremental delays and reconnects once", () => {
  const scheduler = createManualScheduler();
  const sockets = [];
  const events = [];
  const connection = createWebSocketConnection({
    url: "ws://127.0.0.1:8765/ws",
    reconnectDelaysMs: [10, 20],
    createSocket: () => {
      const socket = new FakeWebSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: scheduler.schedule,
    clearScheduled: scheduler.clear,
    callbacks: {
      onOpen: (reconnected) => events.push(`open:${reconnected}`),
      onMessage: (data) => events.push(`message:${data}`),
      onReconnecting: (attempt, delayMs) =>
        events.push(`retry:${attempt}:${delayMs}`),
      onReconnectFailed: () => events.push("failed"),
    },
  });

  connection.start();
  sockets[0].open();
  sockets[0].fail();

  assert.deepEqual(events, ["open:false", "retry:1:10"]);
  scheduler.runNext();
  assert.equal(sockets.length, 2);

  sockets[1].open();
  assert.equal(connection.isOpen(), true);
  assert.deepEqual(events, ["open:false", "retry:1:10", "open:true"]);
  assert.deepEqual(DEFAULT_RECONNECT_DELAYS_MS, [500, 1_000, 1_500]);
});

test("connection retry attempts are finite and manual close never schedules another socket", () => {
  const scheduler = createManualScheduler();
  const sockets = [];
  const events = [];
  const connection = createWebSocketConnection({
    url: "ws://127.0.0.1:8765/ws",
    reconnectDelaysMs: [5, 10],
    createSocket: () => {
      const socket = new FakeWebSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: scheduler.schedule,
    clearScheduled: scheduler.clear,
    callbacks: {
      onOpen: () => events.push("open"),
      onMessage: () => {},
      onReconnecting: (attempt) => events.push(`retry:${attempt}`),
      onReconnectFailed: () => events.push("failed"),
    },
  });

  connection.start();
  sockets[0].fail();
  scheduler.runNext();
  sockets[1].fail();
  scheduler.runNext();
  sockets[2].fail();

  assert.equal(sockets.length, 3);
  assert.deepEqual(events, ["retry:1", "retry:2", "failed"]);

  connection.reconnect();
  assert.equal(sockets.length, 4);
  sockets[3].open();
  assert.equal(connection.isOpen(), true);

  connection.close();
  scheduler.runAll();
  assert.equal(sockets.length, 4);
});

test("stale callbacks from an earlier socket cannot affect a manual reconnect", () => {
  const scheduler = createManualScheduler();
  const sockets = [];
  const messages = [];
  const retries = [];
  const connection = createWebSocketConnection({
    url: "ws://127.0.0.1:8765/ws",
    createSocket: () => {
      const socket = new FakeWebSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: scheduler.schedule,
    clearScheduled: scheduler.clear,
    callbacks: {
      onOpen: () => {},
      onMessage: (data) => messages.push(data),
      onReconnecting: (attempt) => retries.push(attempt),
      onReconnectFailed: () => {},
    },
  });

  connection.start();
  const firstSocket = sockets[0];
  firstSocket.open();
  firstSocket.fail();
  connection.reconnect();

  firstSocket.receive("stale");
  firstSocket.close();
  assert.deepEqual(messages, []);
  assert.deepEqual(retries, [1]);

  sockets[1].open();
  sockets[1].receive("current");
  assert.deepEqual(messages, ["current"]);
});

test("the client message uses the existing backend WebSocket protocol", () => {
  assert.deepEqual(
    createWebSocketClientMessage("chat-1", "session-1", "Hello"),
    {
      type: "message",
      chat_id: "chat-1",
      session_id: "session-1",
      content: "Hello",
    },
  );
});

test("the stop request reuses the existing slash-command WebSocket protocol", () => {
  assert.deepEqual(createWebSocketStopMessage("chat-1", "session-1"), {
    type: "message",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "/stop",
  });
});

test("the protocol parser accepts only the existing server event contract", () => {
  assert.deepEqual(
    parseServerEvent({
      type: "turn_end",
      chat_id: "chat-1",
      session_id: "session-1",
      content: "Complete response",
      metadata: { stop_reason: "stop" },
    }),
    {
      type: "turn_end",
      chat_id: "chat-1",
      session_id: "session-1",
      content: "Complete response",
      metadata: { stop_reason: "stop" },
    },
  );
  assert.equal(
    parseServerEvent({ type: "delta", content: "Missing route" }),
    null,
  );
  assert.deepEqual(
    parseServerEvent({
      type: "tool_call",
      chat_id: "chat-1",
      session_id: "session-1",
      tool_call: {
        id: "call-1",
        name: "read_file",
        arguments: { path: "README.md" },
      },
    }),
    {
      type: "tool_call",
      chat_id: "chat-1",
      session_id: "session-1",
      tool_call: {
        id: "call-1",
        name: "read_file",
        arguments: { path: "README.md" },
      },
    },
  );
});

test("saved-session history replaces visible chat state without mixing sessions", () => {
  let state = beginUserMessage(createInitialChatState(), "Old session message");
  state = applyServerEvent(state, {
    type: "message",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Old reply",
  });

  state = replaceChatHistory(state, [
    { role: "user", content: "New session message" },
    { role: "assistant", content: "New session reply", toolCalls: [] },
  ]);

  assert.deepEqual(
    state.messages.map(({ role, content, isStreaming }) => ({
      role,
      content,
      isStreaming,
    })),
    [
      { role: "user", content: "New session message", isStreaming: false },
      { role: "assistant", content: "New session reply", isStreaming: false },
    ],
  );
  assert.equal(state.isSending, false);
  assert.equal(state.activeAssistantId, null);
});

test("late stream events from an unselected session are ignored", () => {
  assert.equal(
    isEventForSession(
      {
        type: "delta",
        chat_id: "chat-1",
        session_id: "session-one",
        content: "Late",
      },
      "session-two",
    ),
    false,
  );
  assert.equal(
    isEventForSession(
      {
        type: "turn_end",
        chat_id: "chat-1",
        session_id: "session-two",
        content: "Current",
        metadata: {},
      },
      "session-two",
    ),
    true,
  );
  assert.equal(isEventForSession({ type: "ready" }, "session-two"), true);
});

test("the session API loads summaries and a selected transcript", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    if (String(url).endsWith("/v1/sessions")) {
      return new Response(
        JSON.stringify({
          sessions: [
            {
              session_id: "session-one",
              updated_at: "2026-09-08T12:00:00+00:00",
              message_count: 2,
              preview: "Latest reply",
            },
          ],
        }),
        { status: 200 },
      );
    }
    return new Response(
      JSON.stringify({
        session_id: "session two",
        updated_at: "2026-09-08T12:01:00+00:00",
        messages: [
          { role: "user", content: "Hello" },
          { role: "assistant", content: "Hi" },
        ],
      }),
      { status: 200 },
    );
  };

  try {
    const summaries = await fetchSessionSummaries("http://127.0.0.1:8000/");
    const history = await fetchSessionHistory(
      "http://127.0.0.1:8000",
      "session two",
    );

    assert.deepEqual(summaries, [
      {
        sessionId: "session-one",
        updatedAt: "2026-09-08T12:00:00+00:00",
        messageCount: 2,
        preview: "Latest reply",
      },
    ]);
    assert.deepEqual(history.messages, [
      { role: "user", content: "Hello" },
      { role: "assistant", content: "Hi", toolCalls: [] },
    ]);
    assert.deepEqual(requests, [
      "http://127.0.0.1:8000/v1/sessions",
      "http://127.0.0.1:8000/v1/sessions/session%20two",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("new sessions use a unique browser-owned identifier and do not require persistence", () => {
  assert.equal(createSessionId(() => "first"), "webui-first");
  assert.equal(createSessionId(() => "second"), "webui-second");
});

test("session API failures retain a clear status and error message", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ error: { message: "Session was not found" } }),
      { status: 404 },
    );

  try {
    await assert.rejects(
      fetchSessionHistory("http://127.0.0.1:8000", "missing"),
      (error) =>
        error instanceof SessionApiError &&
        error.status === 404 &&
        error.message === "Session was not found",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("deltas accumulate into one assistant message and turn_end does not duplicate it", () => {
  let state = beginUserMessage(createInitialChatState(), "Hello");
  state = applyServerEvent(state, {
    type: "delta",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Hello ",
  });
  state = applyServerEvent(state, {
    type: "delta",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Nanobot",
  });

  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages[1], {
    id: "assistant-2",
    role: "assistant",
    content: "Hello Nanobot",
    isStreaming: true,
    toolCalls: [],
  });

  state = applyServerEvent(state, {
    type: "turn_end",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Hello Nanobot",
    metadata: {},
  });
  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages[1], {
    id: "assistant-2",
    role: "assistant",
    content: "Hello Nanobot",
    isStreaming: false,
    toolCalls: [],
  });
  assert.equal(state.isSending, false);
});

test("a stop request preserves partial streamed text and restores input state", () => {
  let state = beginUserMessage(createInitialChatState(), "Hello");
  state = applyServerEvent(state, {
    type: "delta",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Partial reply",
  });
  state = beginStopRequest(state);
  assert.equal(state.isSending, true);
  assert.equal(state.isStopping, true);
  assert.equal(beginStopRequest(state), state);

  state = applyServerEvent(state, {
    type: "turn_end",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "",
    metadata: { stop_reason: "cancelled" },
  });

  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[1].content, "Partial reply");
  assert.equal(state.messages[1].isStreaming, false);
  assert.equal(state.isSending, false);
  assert.equal(state.isStopping, false);
});

test("tool calls attach to the current streaming assistant response", () => {
  let state = beginUserMessage(createInitialChatState(), "Read the README.");
  state = applyServerEvent(state, {
    type: "tool_call",
    chat_id: "chat-1",
    session_id: "session-1",
    tool_call: {
      id: "call-1",
      name: "read_file",
      arguments: { path: "README.md" },
    },
  });
  state = applyServerEvent(state, {
    type: "delta",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "The README says ",
  });
  state = applyServerEvent(state, {
    type: "turn_end",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "The README says hello.",
    metadata: {},
  });

  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages[1], {
    id: "assistant-2",
    role: "assistant",
    content: "The README says hello.",
    isStreaming: false,
    toolCalls: [
      {
        id: "call-1",
        name: "read_file",
        arguments: { path: "README.md" },
      },
    ],
  });
});

test("server errors complete an active turn and remain visible", () => {
  let state = beginUserMessage(createInitialChatState(), "Hello");
  state = applyServerEvent(state, {
    type: "error",
    code: "message_delivery_failed",
    message: "Message could not be accepted",
  });

  assert.equal(state.isSending, false);
  assert.equal(state.error, "Message could not be accepted");
});

test("assistant output renders safe GitHub-flavored Markdown", () => {
  const markup = renderToStaticMarkup(
    createElement(MessageContent, {
      role: "assistant",
      content: "# Heading\n\n**bold** and `code`\n\n| A | B |\n| - | - |\n| 1 | 2 |",
      isStreaming: false,
      toolCalls: [],
    }),
  );

  assert.match(markup, /<h1>Heading<\/h1>/);
  assert.match(markup, /<strong>bold<\/strong>/);
  assert.match(markup, /<code>code<\/code>/);
  assert.match(markup, /<table>/);
});

test("assistant tool calls render as details instead of an empty Markdown reply", () => {
  const markup = renderToStaticMarkup(
    createElement(MessageContent, {
      role: "assistant",
      content: "",
      isStreaming: false,
      toolCalls: [
        {
          id: "call-1",
          name: "read_file",
          arguments: { path: "README.md" },
        },
      ],
    }),
  );

  assert.match(markup, /Called read_file/);
  assert.match(markup, /README.md/);
  assert.doesNotMatch(markup, /Thinking/);
});

test("an empty assistant response without tool calls still renders Thinking", () => {
  const markup = renderToStaticMarkup(
    createElement(MessageContent, {
      role: "assistant",
      content: "",
      isStreaming: true,
      toolCalls: [],
    }),
  );

  assert.match(markup, /Thinking/);
});

test("user content remains plain text rather than Markdown", () => {
  const markup = renderToStaticMarkup(
    createElement(MessageContent, {
      role: "user",
      content: "**not bold** <script>ignored()</script>",
      isStreaming: false,
    }),
  );

  assert.doesNotMatch(markup, /<strong>/);
  assert.match(markup, /\*\*not bold\*\*/);
  assert.match(markup, /&lt;script&gt;ignored\(\)&lt;\/script&gt;/);
});

test("conversation only follows updates while the reader is near the bottom", () => {
  assert.equal(
    isNearConversationBottom({
      scrollHeight: 1000,
      scrollTop: 560,
      clientHeight: 400,
    }),
    true,
  );
  assert.equal(
    isNearConversationBottom({
      scrollHeight: 1000,
      scrollTop: 480,
      clientHeight: 400,
    }),
    false,
  );
});

test("the compact viewport layout keeps scrolling inside the conversation", async () => {
  const [appStyles, globalStyles, appSource] = await Promise.all([
    readFile(new URL("../src/App.css", import.meta.url), "utf8"),
    readFile(new URL("../src/index.css", import.meta.url), "utf8"),
    readFile(new URL("../src/App.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?display:\s*flex;/);
  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?flex-direction:\s*column;/);
  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?height:\s*100dvh;/);
  assert.match(
    appStyles,
    /\.app-workspace\s*\{[\s\S]*?grid-template-columns:\s*minmax\(11rem, 14rem\) minmax\(0, 1fr\);/,
  );
  assert.match(appStyles, /\.app-workspace\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\);/);
  assert.match(appStyles, /\.session-list\s*\{[\s\S]*?overflow-y:\s*auto;/);
  assert.match(appStyles, /\.chat-panel\s*\{[\s\S]*?flex:\s*1 1 auto;/);
  assert.match(appStyles, /\.conversation-scroll\s*\{[\s\S]*?flex:\s*1 1 auto;/);
  assert.match(appStyles, /\.conversation-scroll\s*\{[\s\S]*?overflow-y:\s*auto;/);
  assert.match(appStyles, /\.composer\s*\{[\s\S]*?padding:\s*0\.45rem 0\.55rem;/);
  assert.match(appStyles, /\.composer textarea\s*\{[\s\S]*?height:\s*2\.45rem;/);
  assert.match(appStyles, /\.app-logo\s*\{[\s\S]*?width:\s*2\.25rem;/);
  assert.match(appStyles, /\.message--user \.message__content\s*\{[\s\S]*?background:/);
  assert.match(appStyles, /\.message--assistant \.message__content\s*\{[\s\S]*?border-left:/);
  assert.match(appStyles, /@media \(max-width: 40rem\)/);
  assert.match(globalStyles, /body\s*\{[\s\S]*?overflow:\s*hidden;/);
  assert.match(globalStyles, /#root\s*\{[\s\S]*?width:\s*100%;/);
  assert.match(appSource, /src="\/nanobot-logo\.png"/);
  assert.doesNotMatch(appSource, /LOCAL AGENT/);
  assert.doesNotMatch(appSource, /A local chat surface connected/);
  assert.doesNotMatch(appSource, /message__author/);
});

class FakeWebSocket {
  readyState = 0;
  onopen = null;
  onmessage = null;
  onerror = null;
  onclose = null;

  open() {
    this.readyState = 1;
    this.onopen?.({});
  }

  receive(data) {
    this.onmessage?.({ data });
  }

  fail() {
    this.onerror?.({});
  }

  close() {
    if (this.readyState === 3) {
      return;
    }
    this.readyState = 3;
    this.onclose?.({});
  }

  send() {}
}

function createManualScheduler() {
  const pending = [];
  return {
    schedule(callback, delayMs) {
      const scheduled = { callback, delayMs, cancelled: false };
      pending.push(scheduled);
      return scheduled;
    },
    clear(scheduled) {
      scheduled.cancelled = true;
    },
    runNext() {
      while (pending.length > 0) {
        const scheduled = pending.shift();
        if (!scheduled.cancelled) {
          scheduled.callback();
          return;
        }
      }
    },
    runAll() {
      while (pending.length > 0) {
        this.runNext();
      }
    },
  };
}
