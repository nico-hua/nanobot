import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  applyServerEvent,
  beginUserMessage,
  createClientMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
} from "../.test-build/hooks/chatState.js";
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
  assert.deepEqual(createClientMessage("chat-1", "session-1", "Hello"), {
    type: "message",
    chat_id: "chat-1",
    session_id: "session-1",
    content: "Hello",
  });
});

test("deltas accumulate into one assistant message and turn_end does not duplicate it", () => {
  let state = beginUserMessage(createInitialChatState(), "Hello");
  state = applyServerEvent(state, { type: "delta", content: "Hello " });
  state = applyServerEvent(state, { type: "delta", content: "Nanobot" });

  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages[1], {
    id: "assistant-2",
    role: "assistant",
    content: "Hello Nanobot",
    isStreaming: true,
  });

  state = applyServerEvent(state, {
    type: "turn_end",
    content: "Hello Nanobot",
  });
  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages[1], {
    id: "assistant-2",
    role: "assistant",
    content: "Hello Nanobot",
    isStreaming: false,
  });
  assert.equal(state.isSending, false);
});

test("server errors complete an active turn and remain visible", () => {
  let state = beginUserMessage(createInitialChatState(), "Hello");
  state = applyServerEvent(state, {
    type: "error",
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
    }),
  );

  assert.match(markup, /<h1>Heading<\/h1>/);
  assert.match(markup, /<strong>bold<\/strong>/);
  assert.match(markup, /<code>code<\/code>/);
  assert.match(markup, /<table>/);
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
