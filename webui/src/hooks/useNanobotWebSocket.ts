import { useCallback, useEffect, useRef, useState } from "react";

import {
  applyServerEvent,
  beginConnection,
  beginUserMessage,
  createClientMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
  markServerError,
  type ServerEvent,
} from "./chatState";

export type { ChatMessage, ConnectionStatus } from "./chatState";

type UseNanobotWebSocketOptions = {
  url: string | undefined;
  chatId: string;
  sessionId: string;
};

const MISSING_URL_ERROR = "VITE_NANOBOT_WEBSOCKET_URL is not configured.";
const INVALID_EVENT_ERROR = "Received an invalid message from Nanobot.";

function isServerEvent(value: unknown): value is ServerEvent {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    typeof value.type === "string"
  );
}

/** Connect one browser chat session to Nanobot's existing WebSocket Channel. */
export function useNanobotWebSocket({
  url,
  chatId,
  sessionId,
}: UseNanobotWebSocketOptions) {
  const [state, setState] = useState(createInitialChatState);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!url) {
      setState((currentState) =>
        markConnectionError(currentState, MISSING_URL_ERROR),
      );
      return;
    }

    let closedByEffect = false;
    let connectionFailed = false;
    setState(beginConnection);

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      setState((currentState) =>
        markConnectionError(
          currentState,
          "Could not create the Nanobot WebSocket connection.",
        ),
      );
      return;
    }

    socketRef.current = socket;
    socket.onopen = () => setState(markConnected);
    socket.onmessage = (messageEvent) => {
      if (typeof messageEvent.data !== "string") {
        setState((currentState) =>
          markServerError(currentState, INVALID_EVENT_ERROR),
        );
        return;
      }
      try {
        const event: unknown = JSON.parse(messageEvent.data);
        setState((currentState) =>
          isServerEvent(event)
            ? applyServerEvent(currentState, event)
            : markServerError(currentState, INVALID_EVENT_ERROR),
        );
      } catch {
        setState((currentState) =>
          markServerError(currentState, INVALID_EVENT_ERROR),
        );
      }
    };
    socket.onerror = () => {
      connectionFailed = true;
      setState((currentState) =>
        markConnectionError(
          currentState,
          "Could not connect to the Nanobot WebSocket service.",
        ),
      );
    };
    socket.onclose = () => {
      if (!closedByEffect && !connectionFailed) {
        setState(markDisconnected);
      }
    };

    return () => {
      closedByEffect = true;
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      socket.close();
    };
  }, [url]);

  const sendMessage = useCallback(
    (content: string): boolean => {
      const text = content.trim();
      const socket = socketRef.current;
      if (!text) {
        return false;
      }
      if (socket === null || socket.readyState !== WebSocket.OPEN) {
        setState((currentState) =>
          markServerError(
            currentState,
            "Nanobot is not connected. Wait for the connection before sending.",
          ),
        );
        return false;
      }

      setState((currentState) => beginUserMessage(currentState, text));
      try {
        socket.send(JSON.stringify(createClientMessage(chatId, sessionId, text)));
      } catch {
        setState((currentState) =>
          markConnectionError(
            currentState,
            "The message could not be sent to Nanobot.",
          ),
        );
        return false;
      }
      return true;
    },
    [chatId, sessionId],
  );

  return {
    connectionStatus: state.connectionStatus,
    error: state.error,
    isSending: state.isSending,
    messages: state.messages,
    sendMessage,
  };
}
