/** Shared UI state for one rendered user or assistant message. */
export type ToolCall = {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
};

export type UserMessage = {
  id: string;
  role: "user";
  content: string;
  isStreaming: false;
};

export type AssistantMessage = {
  id: string;
  role: "assistant";
  content: string;
  isStreaming: boolean;
  toolCalls: ToolCall[];
};

export type ChatMessage = UserMessage | AssistantMessage;

/** A persisted user/assistant message returned by the Session HTTP API. */
export type PersistedUserMessage = {
  role: "user";
  content: string;
};

export type PersistedAssistantMessage = {
  role: "assistant";
  content: string;
  toolCalls: ToolCall[];
};

export type PersistedSessionMessage =
  | PersistedUserMessage
  | PersistedAssistantMessage;

export type SessionInfo = {
  sessionId: string;
  updatedAt: string;
  messageCount: number;
  preview: string;
};

export type SessionHistory = Pick<SessionInfo, "sessionId" | "updatedAt"> & {
  messages: PersistedSessionMessage[];
};

/** Existing client-to-server WebSocket message format. */
export type WebSocketClientMessage = {
  type: "message";
  chat_id: string;
  session_id: string;
  content: string;
};

export type ReadyEvent = {
  type: "ready";
};

type RoutedEvent = {
  chat_id: string;
  session_id: string;
};

type RoutedTextEvent = RoutedEvent & {
  content: string;
};

export type DeltaEvent = RoutedTextEvent & {
  type: "delta";
};

export type ToolCallEvent = RoutedEvent & {
  type: "tool_call";
  tool_call: ToolCall;
};

export type TurnEndEvent = RoutedTextEvent & {
  type: "turn_end";
  metadata: Record<string, unknown>;
};

export type ServerMessageEvent = RoutedTextEvent & {
  type: "message";
};

export type ServerErrorEvent = {
  type: "error";
  code: string;
  message: string;
};

export type ServerEvent =
  | ReadyEvent
  | DeltaEvent
  | ToolCallEvent
  | TurnEndEvent
  | ServerMessageEvent
  | ServerErrorEvent;

export function createWebSocketClientMessage(
  chatId: string,
  sessionId: string,
  content: string,
): WebSocketClientMessage {
  return {
    type: "message",
    chat_id: chatId,
    session_id: sessionId,
    content,
  };
}

/** Parse the current backend wire contract without accepting arbitrary JSON. */
export function parseServerEvent(value: unknown): ServerEvent | null {
  if (!isRecord(value) || typeof value.type !== "string") {
    return null;
  }

  if (value.type === "ready") {
    return { type: "ready" };
  }
  if (value.type === "error") {
    return typeof value.code === "string" && typeof value.message === "string"
      ? { type: "error", code: value.code, message: value.message }
      : null;
  }

  if (value.type === "tool_call") {
    const routed = parseRoutedEvent(value);
    const toolCall = parseToolCall(value.tool_call);
    return routed !== null && toolCall !== null
      ? { type: "tool_call", ...routed, tool_call: toolCall }
      : null;
  }

  const routed = parseRoutedTextEvent(value);
  if (routed === null) {
    return null;
  }
  if (value.type === "delta") {
    return { type: "delta", ...routed };
  }
  if (value.type === "message") {
    return { type: "message", ...routed };
  }
  if (value.type === "turn_end" && isRecord(value.metadata)) {
    return { type: "turn_end", ...routed, metadata: value.metadata };
  }
  return null;
}

/** Route only text events belonging to the session currently shown in the UI. */
export function isEventForSession(event: ServerEvent, sessionId: string): boolean {
  switch (event.type) {
    case "delta":
    case "tool_call":
    case "message":
    case "turn_end":
      return event.session_id === sessionId;
    case "error":
    case "ready":
      return true;
  }
}

function parseRoutedTextEvent(value: Record<string, unknown>): RoutedTextEvent | null {
  const routed = parseRoutedEvent(value);
  return routed !== null &&
    typeof value.content === "string"
    ? {
        ...routed,
        content: value.content,
      }
    : null;
}

function parseRoutedEvent(value: Record<string, unknown>): RoutedEvent | null {
  return typeof value.chat_id === "string" && typeof value.session_id === "string"
    ? { chat_id: value.chat_id, session_id: value.session_id }
    : null;
}

function parseToolCall(value: unknown): ToolCall | null {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    !isRecord(value.arguments)
  ) {
    return null;
  }
  return {
    id: value.id,
    name: value.name,
    arguments: value.arguments,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
