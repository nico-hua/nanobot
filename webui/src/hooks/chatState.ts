export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  isStreaming: boolean;
};

export type PersistedChatMessage = Pick<ChatMessage, "role" | "content">;

export type ServerEvent = Record<string, unknown> & { type: string };

export type ChatState = {
  connectionStatus: ConnectionStatus;
  error: string | null;
  isSending: boolean;
  messages: ChatMessage[];
  activeAssistantId: string | null;
  nextMessageSequence: number;
};

const INVALID_EVENT_ERROR = "Received an invalid message from Nanobot.";
const SESSION_EVENT_TYPES = new Set(["delta", "message", "turn_end"]);

export function createInitialChatState(): ChatState {
  return {
    connectionStatus: "connecting",
    error: null,
    isSending: false,
    messages: [],
    activeAssistantId: null,
    nextMessageSequence: 0,
  };
}

export function createClientMessage(
  chatId: string,
  sessionId: string,
  content: string,
) {
  return {
    type: "message",
    chat_id: chatId,
    session_id: sessionId,
    content,
  };
}

/** Replace only the visible transcript when the user selects another session. */
export function replaceChatHistory(
  state: ChatState,
  messages: readonly PersistedChatMessage[],
): ChatState {
  const visibleMessages = messages.map((message, index) => ({
    id: `${message.role}-${index + 1}`,
    role: message.role,
    content: message.content,
    isStreaming: false,
  }));
  return {
    ...state,
    error: null,
    isSending: false,
    messages: visibleMessages,
    activeAssistantId: null,
    nextMessageSequence: visibleMessages.length,
  };
}

/** Ignore late stream events belonging to a session that is no longer active. */
export function isEventForSession(event: ServerEvent, sessionId: string): boolean {
  if (!SESSION_EVENT_TYPES.has(event.type)) {
    return true;
  }
  return event.session_id === sessionId;
}

export function beginConnection(state: ChatState): ChatState {
  return {
    ...state,
    connectionStatus: "connecting",
    error: null,
  };
}

export function markConnected(state: ChatState): ChatState {
  return {
    ...state,
    connectionStatus: "connected",
    error: null,
  };
}

export function markDisconnected(state: ChatState): ChatState {
  return withAssistantFinished(
    state,
    null,
    "disconnected",
    "The Nanobot WebSocket connection was closed.",
  );
}

export function markConnectionError(state: ChatState, error: string): ChatState {
  return withAssistantFinished(state, null, "error", error);
}

export function markServerError(state: ChatState, error: string): ChatState {
  return withAssistantFinished(state, null, state.connectionStatus, error);
}

export function beginUserMessage(state: ChatState, content: string): ChatState {
  const { id, nextMessageSequence } = nextMessageId(state, "user");
  return {
    ...state,
    error: null,
    isSending: true,
    activeAssistantId: null,
    nextMessageSequence,
    messages: [
      ...state.messages,
      {
        id,
        role: "user",
        content,
        isStreaming: false,
      },
    ],
  };
}

export function applyServerEvent(state: ChatState, event: ServerEvent): ChatState {
  if (event.type === "ready") {
    return markConnected(state);
  }
  if (event.type === "error") {
    return markServerError(
      state,
      typeof event.message === "string" && event.message.trim()
        ? event.message
        : "Nanobot could not process the message.",
    );
  }

  const content = typeof event.content === "string" ? event.content : null;
  if (content === null) {
    return markServerError(state, INVALID_EVENT_ERROR);
  }
  if (event.type === "delta") {
    return appendAssistantDelta(state, content);
  }
  if (event.type === "turn_end" || event.type === "message") {
    // The final event has the complete response, so replace the accumulated
    // deltas instead of appending it a second time.
    return withAssistantFinished(state, content, state.connectionStatus, null);
  }
  return markServerError(state, INVALID_EVENT_ERROR);
}

function appendAssistantDelta(state: ChatState, content: string): ChatState {
  if (state.activeAssistantId === null) {
    const { id, nextMessageSequence } = nextMessageId(state, "assistant");
    return {
      ...state,
      activeAssistantId: id,
      nextMessageSequence,
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          content,
          isStreaming: true,
        },
      ],
    };
  }

  return {
    ...state,
    messages: state.messages.map((message) =>
      message.id === state.activeAssistantId
        ? { ...message, content: `${message.content}${content}` }
        : message,
    ),
  };
}

function withAssistantFinished(
  state: ChatState,
  content: string | null,
  connectionStatus: ConnectionStatus,
  error: string | null,
): ChatState {
  if (state.activeAssistantId !== null) {
    return {
      ...state,
      connectionStatus,
      error,
      isSending: false,
      activeAssistantId: null,
      messages: state.messages.map((message) =>
        message.id === state.activeAssistantId
          ? {
              ...message,
              content:
                content !== null && content.length > 0
                  ? content
                  : message.content,
              isStreaming: false,
            }
          : message,
      ),
    };
  }

  if (content !== null && content.length > 0) {
    const { id, nextMessageSequence } = nextMessageId(state, "assistant");
    return {
      ...state,
      connectionStatus,
      error,
      isSending: false,
      nextMessageSequence,
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          content,
          isStreaming: false,
        },
      ],
    };
  }

  return {
    ...state,
    connectionStatus,
    error,
    isSending: false,
    activeAssistantId: null,
  };
}

function nextMessageId(
  state: ChatState,
  role: ChatMessage["role"],
): { id: string; nextMessageSequence: number } {
  const nextMessageSequence = state.nextMessageSequence + 1;
  return {
    id: `${role}-${nextMessageSequence}`,
    nextMessageSequence,
  };
}
