import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  createSessionId,
  fetchSessionHistory,
  fetchSessionSummaries,
  SessionApiError,
} from "./api/sessions";
import { MessageContent } from "./components/MessageContent";
import { isNearConversationBottom } from "./conversationScroll";
import {
  type ConnectionStatus,
  useNanobotWebSocket,
} from "./hooks/useNanobotWebSocket";
import type { SessionInfo } from "./types/protocol";
import "./App.css";

const CHAT_ID = "webui-default-chat";
const API_BASE_URL =
  import.meta.env.VITE_NANOBOT_API_URL ?? "http://127.0.0.1:8000";

const STATUS_LABELS: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  connected: "Connected",
  reconnecting: "Reconnecting",
  disconnected: "Disconnected",
  error: "Connection error",
};

function App() {
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState(() => createSessionId());
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [isLoadingSessions, setIsLoadingSessions] = useState(true);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const conversationRef = useRef<HTMLDivElement>(null);
  const shouldFollowLatestRef = useRef(true);
  const historyRequestRef = useRef(0);
  const wasSendingRef = useRef(false);
  const lastConnectionVersionRef = useRef(0);
  const activeSessionIdRef = useRef(sessionId);
  activeSessionIdRef.current = sessionId;
  const {
    connectionStatus,
    connectionVersion,
    error,
    isSending,
    isStopping,
    messages,
    sendMessage,
    stopGeneration,
    reconnect,
    replaceMessages,
  } = useNanobotWebSocket({
    url: import.meta.env.VITE_NANOBOT_WEBSOCKET_URL,
    chatId: CHAT_ID,
    sessionId,
  });

  const refreshSessions = useCallback(async () => {
    try {
      const summaries = await fetchSessionSummaries(API_BASE_URL);
      setSessions(summaries);
      setSessionError(null);
    } catch (caughtError) {
      setSessionError(errorMessage(caughtError));
    } finally {
      setIsLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const loadSessionHistory = useCallback((targetSessionId: string) => {
    const requestId = historyRequestRef.current + 1;
    historyRequestRef.current = requestId;
    setIsLoadingHistory(true);

    void fetchSessionHistory(API_BASE_URL, targetSessionId)
      .then((history) => {
        if (historyRequestRef.current !== requestId) {
          return;
        }
        replaceMessages(history.messages);
        shouldFollowLatestRef.current = true;
        setSessionError(null);
      })
      .catch((caughtError: unknown) => {
        if (historyRequestRef.current !== requestId) {
          return;
        }
        if (caughtError instanceof SessionApiError && caughtError.status === 404) {
          // A browser-created session does not exist on disk until its first turn.
          replaceMessages([]);
          return;
        }
        replaceMessages([]);
        setSessionError(errorMessage(caughtError));
      })
      .finally(() => {
        if (historyRequestRef.current === requestId) {
          setIsLoadingHistory(false);
        }
      });
  }, [replaceMessages]);

  useEffect(() => {
    loadSessionHistory(sessionId);
  }, [loadSessionHistory, sessionId]);

  useEffect(() => {
    if (connectionStatus !== "connected" || connectionVersion === 0) {
      return;
    }

    const previousVersion = lastConnectionVersionRef.current;
    if (previousVersion === connectionVersion) {
      return;
    }
    lastConnectionVersionRef.current = connectionVersion;
    if (previousVersion !== 0) {
      // A new socket cannot replay deltas. Reload only persisted history once
      // the replacement connection has opened.
      loadSessionHistory(activeSessionIdRef.current);
    }
  }, [connectionStatus, connectionVersion, loadSessionHistory]);

  useEffect(() => {
    if (wasSendingRef.current && !isSending) {
      void refreshSessions();
    }
    wasSendingRef.current = isSending;
  }, [isSending, refreshSessions]);

  const canSend = Boolean(
    connectionStatus === "connected" &&
      !isSending &&
      !isLoadingHistory &&
      draft.trim(),
  );
  const isComposerDisabled =
    isSending || isLoadingHistory || connectionStatus !== "connected";

  useEffect(() => {
    const conversation = conversationRef.current;
    if (conversation === null || !shouldFollowLatestRef.current) {
      return;
    }
    conversation.scrollTop = conversation.scrollHeight;
  }, [messages]);

  function handleConversationScroll() {
    const conversation = conversationRef.current;
    if (conversation !== null) {
      shouldFollowLatestRef.current = isNearConversationBottom(conversation);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendMessage(draft)) {
      // A sent message intentionally returns the reader to the active turn.
      shouldFollowLatestRef.current = true;
      setDraft("");
    }
  }

  function handleStop() {
    if (stopGeneration()) {
      shouldFollowLatestRef.current = true;
    }
  }

  function handleReconnect() {
    reconnect();
  }

  function selectSession(nextSessionId: string) {
    if (nextSessionId === sessionId) {
      return;
    }
    shouldFollowLatestRef.current = true;
    replaceMessages([]);
    setSessionId(nextSessionId);
  }

  function createNewSession() {
    shouldFollowLatestRef.current = true;
    replaceMessages([]);
    setSessionError(null);
    setSessionId(createSessionId());
  }

  return (
    <main className="app-shell" aria-label="Nanobot chat">
      <header className="app-header">
        <img
          className="app-logo"
          src="/nanobot-logo.png"
          alt="Nanobot logo"
        />
        <h1>Nanobot Web UI</h1>
      </header>

      <div className="app-workspace">
        <aside className="session-sidebar" aria-label="Sessions">
          <div className="session-sidebar__header">
            <div>
              <h2>Sessions</h2>
              <p>Saved conversations</p>
            </div>
            <button type="button" onClick={createNewSession}>
              New session
            </button>
          </div>

          {sessionError !== null ? (
            <p className="session-error" role="alert">
              {sessionError}
            </p>
          ) : null}

          <div className="session-list" aria-live="polite">
            {isLoadingSessions ? <p>Loading sessions...</p> : null}
            {!isLoadingSessions && sessions.length === 0 ? (
              <p>No saved sessions yet.</p>
            ) : null}
            <ol>
              {sessions.map((session) => (
                <li key={session.sessionId}>
                  <button
                    type="button"
                    className={
                      session.sessionId === sessionId
                        ? "session-item session-item--active"
                        : "session-item"
                    }
                    onClick={() => selectSession(session.sessionId)}
                    aria-current={
                      session.sessionId === sessionId ? "page" : undefined
                    }
                  >
                    <span className="session-item__id">{session.sessionId}</span>
                    <span className="session-item__preview">
                      {session.preview || "No visible messages"}
                    </span>
                    <span className="session-item__meta">
                      {session.messageCount} message{session.messageCount === 1 ? "" : "s"}
                      {" · "}
                      {formatUpdatedAt(session.updatedAt)}
                    </span>
                  </button>
                </li>
              ))}
            </ol>
          </div>
        </aside>

        <div className="chat-workspace">
          <section className="chat-panel" aria-labelledby="conversation-title">
            <div className="chat-panel__header">
              <h2 id="conversation-title">Conversation</h2>
              <div className="chat-panel__connection">
                <span className={`status-badge status-badge--${connectionStatus}`}>
                  {STATUS_LABELS[connectionStatus]}
                </span>
                {connectionStatus === "error" ||
                connectionStatus === "disconnected" ? (
                  <button
                    type="button"
                    className="connection-retry"
                    onClick={handleReconnect}
                  >
                    Reconnect
                  </button>
                ) : null}
              </div>
            </div>

            <div className="chat-panel__body">
              {error !== null ? (
                <p className="connection-error" role="alert">
                  {error}
                </p>
              ) : null}

              <div
                ref={conversationRef}
                className="conversation-scroll"
                onScroll={handleConversationScroll}
              >
                {isLoadingHistory && messages.length === 0 ? (
                  <div className="chat-empty-state">
                    <p>Loading conversation...</p>
                  </div>
                ) : null}
                {!isLoadingHistory && messages.length === 0 ? (
                  <div className="chat-empty-state">
                    <p>No messages yet.</p>
                    <span>
                      {connectionStatus === "connected"
                        ? "Send a message to start a conversation."
                        : "Waiting for the local Nanobot connection."}
                    </span>
                  </div>
                ) : null}
                {messages.length > 0 ? (
                  <ol className="message-list" aria-live="polite">
                    {messages.map((message) => (
                      <li
                        key={message.id}
                        className={`message message--${message.role}`}
                      >
                        <MessageContent {...message} />
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            </div>
          </section>

          <form
            className="composer"
            aria-label="Message composer"
            onSubmit={handleSubmit}
          >
            <div className="composer__controls">
              <textarea
                id="message"
                name="message"
                aria-label="Message"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Ask Nanobot anything..."
                rows={1}
                disabled={isComposerDisabled}
              />
              <div className="composer__actions">
                {isSending ? (
                  <button
                    type="button"
                    className="composer__stop"
                    onClick={handleStop}
                    disabled={isStopping}
                  >
                    {isStopping ? "Stopping..." : "Stop"}
                  </button>
                ) : null}
                <button type="submit" disabled={!canSend}>
                  {isSending ? "Sending..." : "Send"}
                </button>
              </div>
            </div>
          </form>
        </div>
      </div>
    </main>
  );
}

function formatUpdatedAt(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return "Recently updated";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

function errorMessage(caughtError: unknown): string {
  return caughtError instanceof Error
    ? caughtError.message
    : "Could not load saved sessions.";
}

export default App;
