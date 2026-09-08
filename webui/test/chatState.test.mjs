import assert from "node:assert/strict";
import test from "node:test";

import {
  applyServerEvent,
  beginUserMessage,
  createClientMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
} from "../.test-build/chatState.js";

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
