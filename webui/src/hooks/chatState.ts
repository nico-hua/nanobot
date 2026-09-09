import type {
  ChatMessage,
  PersistedSessionMessage,
  ServerEvent,
  ToolCall,
} from "../types/protocol.js";

export type { ChatMessage } from "../types/protocol.js";

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "error";

export type AuthenticationStatus =
  | "checking"
  | "not_required"
  | "authenticating"
  | "authenticated"
  | "failed";

export type ChatState = {
  connectionStatus: ConnectionStatus;
  authenticationStatus: AuthenticationStatus;
  error: string | null;
  isSending: boolean;
  isStopping: boolean;
  messages: ChatMessage[];
  activeAssistantId: string | null;
  nextMessageSequence: number;
};

export function createInitialChatState(): ChatState {
  return {
    connectionStatus: "connecting",
    authenticationStatus: "checking",
    error: null,
    isSending: false,
    isStopping: false,
    messages: [],
    activeAssistantId: null,
    nextMessageSequence: 0,
  };
}

/** Replace only the visible transcript when the user selects another session. */
export function replaceChatHistory(
  state: ChatState,
  messages: readonly PersistedSessionMessage[],
): ChatState {
  const visibleMessages = messages.map(
    (message, index): ChatMessage =>
      message.role === "user"
        ? {
            id: `user-${index + 1}`,
            role: "user",
            content: message.content,
            isStreaming: false,
          }
        : {
            id: `assistant-${index + 1}`,
            role: "assistant",
            content: message.content,
            isStreaming: false,
            toolCalls: message.toolCalls,
          },
  );
  return {
    ...state,
    error: null,
    isSending: false,
    isStopping: false,
    messages: visibleMessages,
    activeAssistantId: null,
    nextMessageSequence: visibleMessages.length,
  };
}

export function beginConnection(state: ChatState): ChatState {
  return {
    ...state,
    connectionStatus: "connecting",
    authenticationStatus: "checking",
    error: null,
  };
}

export function markConnected(state: ChatState): ChatState {
  return {
    ...state,
    connectionStatus: "connected",
    authenticationStatus: "checking",
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

export function beginAuthentication(state: ChatState): ChatState {
  return {
    ...state,
    authenticationStatus: "authenticating",
    error: null,
  };
}

export function markAuthenticationNotRequired(state: ChatState): ChatState {
  return {
    ...state,
    authenticationStatus: "not_required",
    error: null,
  };
}

export function markAuthenticated(state: ChatState): ChatState {
  return {
    ...state,
    authenticationStatus: "authenticated",
    error: null,
  };
}

export function markAuthenticationFailed(
  state: ChatState,
  error: string,
): ChatState {
  return {
    ...withAssistantFinished(state, null, "error", error),
    authenticationStatus: "failed",
  };
}

/**
 * Drop the unconfirmed streaming assistant message before reconnecting.  The
 * session API will replace visible history after a new connection succeeds,
 * so a partial delta can never be mistaken for persisted conversation state.
 */
export function markReconnecting(state: ChatState): ChatState {
  return discardInterruptedTurn(
    state,
    "reconnecting",
    "Connection interrupted. Reconnecting and restoring saved history.",
  );
}

/** End recovery after bounded retry attempts without retaining a partial turn. */
export function markReconnectFailed(state: ChatState): ChatState {
  return discardInterruptedTurn(
    state,
    "error",
    "Could not reconnect to the Nanobot WebSocket service.",
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
    isStopping: false,
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

/** Mark one active streaming turn as stopping without adding a user message. */
export function beginStopRequest(state: ChatState): ChatState {
  if (!state.isSending || state.isStopping) {
    return state;
  }
  return {
    ...state,
    error: null,
    isStopping: true,
  };
}

export function applyServerEvent(state: ChatState, event: ServerEvent): ChatState {
  switch (event.type) {
    case "ready":
      return event.authentication_required
        ? beginAuthentication(markConnected(state))
        : markAuthenticationNotRequired(markConnected(state));
    case "authenticated":
      return markAuthenticated(state);
    case "error":
      return markServerError(
        state,
        event.message.trim()
          ? event.message
          : "Nanobot could not process the message.",
      );
    case "delta":
      return appendAssistantDelta(state, event.content);
    case "tool_call":
      return appendAssistantToolCall(state, event.tool_call);
    case "message":
    case "turn_end":
      // The final event has the complete response, so replace the accumulated
      // deltas instead of appending it a second time.
      return withAssistantFinished(
        state,
        event.content,
        state.connectionStatus,
        null,
      );
    default: {
      const exhaustiveEvent: never = event;
      return exhaustiveEvent;
    }
  }
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
          toolCalls: [],
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

function appendAssistantToolCall(state: ChatState, toolCall: ToolCall): ChatState {
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
          content: "",
          isStreaming: true,
          toolCalls: [toolCall],
        },
      ],
    };
  }

  return {
    ...state,
    messages: state.messages.map((message) =>
      message.id === state.activeAssistantId && message.role === "assistant"
        ? {
            ...message,
            toolCalls: message.toolCalls.some((item) => item.id === toolCall.id)
              ? message.toolCalls
              : [...message.toolCalls, toolCall],
          }
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
      isStopping: false,
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
      isStopping: false,
      nextMessageSequence,
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          content,
          isStreaming: false,
          toolCalls: [],
        },
      ],
    };
  }

  return {
    ...state,
    connectionStatus,
    error,
    isSending: false,
    isStopping: false,
    activeAssistantId: null,
  };
}

function discardInterruptedTurn(
  state: ChatState,
  connectionStatus: ConnectionStatus,
  error: string,
): ChatState {
  const activeAssistantId = state.activeAssistantId;
  return {
    ...state,
    connectionStatus,
    error,
    isSending: false,
    isStopping: false,
    activeAssistantId: null,
    messages:
      activeAssistantId === null
        ? state.messages
        : state.messages.filter((message) => message.id !== activeAssistantId),
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
