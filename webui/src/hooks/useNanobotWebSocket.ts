import { useCallback, useEffect, useRef, useState } from "react";

import {
  createWebSocketClientMessage,
  createWebSocketStopMessage,
  isEventForSession,
  parseServerEvent,
  type PersistedSessionMessage,
} from "../types/protocol";
import {
  applyServerEvent,
  beginConnection,
  beginStopRequest,
  beginUserMessage,
  createInitialChatState,
  markConnected,
  markConnectionError,
  markDisconnected,
  markReconnectFailed,
  markReconnecting,
  markServerError,
  replaceChatHistory,
} from "./chatState";
import {
  createWebSocketConnection,
  type WebSocketConnection,
} from "./webSocketConnection";

export type { ChatMessage } from "../types/protocol";
export type { ConnectionStatus } from "./chatState";

type UseNanobotWebSocketOptions = {
  url: string | undefined;
  chatId: string;
  sessionId: string;
};

const MISSING_URL_ERROR = "VITE_NANOBOT_WEBSOCKET_URL is not configured.";
const INVALID_EVENT_ERROR = "Received an invalid message from Nanobot.";

/** Connect one browser chat session to Nanobot's existing WebSocket Channel. */
export function useNanobotWebSocket({
  url,
  chatId,
  sessionId,
}: UseNanobotWebSocketOptions) {
  const [state, setState] = useState(createInitialChatState);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const connectionRef = useRef<WebSocketConnection | null>(null);
  const stopRequestedRef = useRef(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  const handleServerMessage = useCallback((data: unknown) => {
    if (typeof data !== "string") {
      setState((currentState) =>
        markServerError(currentState, INVALID_EVENT_ERROR),
      );
      return;
    }

    try {
      const event = parseServerEvent(JSON.parse(data));
      if (event === null) {
        setState((currentState) =>
          markServerError(currentState, INVALID_EVENT_ERROR),
        );
        return;
      }
      if (!isEventForSession(event, activeSessionRef.current)) {
        return;
      }
      if (
        event.type === "turn_end" ||
        event.type === "message" ||
        event.type === "error"
      ) {
        stopRequestedRef.current = false;
      }
      setState((currentState) => applyServerEvent(currentState, event));
    } catch {
      setState((currentState) =>
        markServerError(currentState, INVALID_EVENT_ERROR),
      );
    }
  }, []);

  useEffect(() => {
    if (!url) {
      setState((currentState) =>
        markConnectionError(currentState, MISSING_URL_ERROR),
      );
      return;
    }

    setState(beginConnection);
    const connection = createWebSocketConnection({
      url,
      callbacks: {
        onOpen: () => {
          stopRequestedRef.current = false;
          setState(markConnected);
          setConnectionVersion((currentVersion) => currentVersion + 1);
        },
        onMessage: handleServerMessage,
        onReconnecting: () => {
          stopRequestedRef.current = false;
          setState(markReconnecting);
        },
        onReconnectFailed: () => {
          stopRequestedRef.current = false;
          setState(markReconnectFailed);
        },
      },
    });
    connectionRef.current = connection;
    connection.start();

    return () => {
      connection.close();
      if (connectionRef.current === connection) {
        connectionRef.current = null;
      }
    };
  }, [handleServerMessage, url]);

  const sendMessage = useCallback(
    (content: string): boolean => {
      const text = content.trim();
      const connection = connectionRef.current;
      if (!text) {
        return false;
      }
      if (connection === null || !connection.isOpen()) {
        setState((currentState) =>
          markServerError(
            currentState,
            "Nanobot is not connected. Wait for the connection before sending.",
          ),
        );
        return false;
      }

      setState((currentState) => beginUserMessage(currentState, text));
      stopRequestedRef.current = false;
      return connection.send(
        JSON.stringify(createWebSocketClientMessage(chatId, sessionId, text)),
      );
    },
    [chatId, sessionId],
  );

  const stopGeneration = useCallback((): boolean => {
    const connection = connectionRef.current;
    if (!state.isSending || stopRequestedRef.current) {
      return false;
    }
    if (connection === null || !connection.isOpen()) {
      setState((currentState) =>
        markServerError(
          currentState,
          "Nanobot is not connected. The current generation could not be stopped.",
        ),
      );
      return false;
    }

    stopRequestedRef.current = true;
    setState(beginStopRequest);
    if (
      connection.send(
        JSON.stringify(createWebSocketStopMessage(chatId, sessionId)),
      )
    ) {
      return true;
    }

    stopRequestedRef.current = false;
    return false;
  }, [chatId, sessionId, state.isSending]);

  const reconnect = useCallback((): boolean => {
    const connection = connectionRef.current;
    if (!url || connection === null) {
      setState((currentState) =>
        markConnectionError(currentState, MISSING_URL_ERROR),
      );
      return false;
    }

    stopRequestedRef.current = false;
    setState(beginConnection);
    connection.reconnect();
    return true;
  }, [url]);

  const disconnect = useCallback(() => {
    stopRequestedRef.current = false;
    connectionRef.current?.close();
    setState(markDisconnected);
  }, []);

  const replaceMessages = useCallback(
    (messages: readonly PersistedSessionMessage[]) => {
      setState((currentState) => replaceChatHistory(currentState, messages));
    },
    [],
  );

  return {
    connectionStatus: state.connectionStatus,
    connectionVersion,
    error: state.error,
    isSending: state.isSending,
    isStopping: state.isStopping,
    messages: state.messages,
    sendMessage,
    stopGeneration,
    reconnect,
    disconnect,
    replaceMessages,
  };
}
