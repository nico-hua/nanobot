import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  applyServerEvent,
  beginUserMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
  replaceChatHistory,
} from "../.test-build/hooks/chatState.js";
import {
  createWebSocketClientMessage,
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

test("the viewport layout keeps scrolling inside the conversation", async () => {
  const [appStyles, globalStyles] = await Promise.all([
    readFile(new URL("../src/App.css", import.meta.url), "utf8"),
    readFile(new URL("../src/index.css", import.meta.url), "utf8"),
  ]);

  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?display:\s*flex;/);
  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?flex-direction:\s*column;/);
  assert.match(appStyles, /\.app-shell\s*\{[\s\S]*?height:\s*100dvh;/);
  assert.match(appStyles, /\.app-workspace\s*\{[\s\S]*?grid-template-columns:/);
  assert.match(appStyles, /\.app-workspace\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\);/);
  assert.match(appStyles, /\.session-list\s*\{[\s\S]*?overflow-y:\s*auto;/);
  assert.match(appStyles, /\.chat-panel\s*\{[\s\S]*?flex:\s*1 1 auto;/);
  assert.match(appStyles, /\.conversation-scroll\s*\{[\s\S]*?flex:\s*1 1 auto;/);
  assert.match(appStyles, /\.conversation-scroll\s*\{[\s\S]*?overflow-y:\s*auto;/);
  assert.match(appStyles, /\.composer\s*\{[\s\S]*?padding:\s*0\.65rem 0\.75rem;/);
  assert.match(appStyles, /\.message--user \.message__content\s*\{[\s\S]*?background:/);
  assert.match(appStyles, /\.message--assistant \.message__content\s*\{[\s\S]*?border-left:/);
  assert.match(appStyles, /@media \(max-width: 40rem\)/);
  assert.match(globalStyles, /body\s*\{[\s\S]*?overflow:\s*hidden;/);
  assert.match(globalStyles, /#root\s*\{[\s\S]*?width:\s*100%;/);
});
