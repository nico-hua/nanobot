import { type FormEvent, useState } from "react";

import {
  type ConnectionStatus,
  useNanobotWebSocket,
} from "./hooks/useNanobotWebSocket";
import "./App.css";

const CHAT_ID = "webui-default-chat";
const SESSION_ID = "webui-default-session";

const STATUS_LABELS: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  connected: "Connected",
  disconnected: "Disconnected",
  error: "Connection error",
};

function App() {
  const [draft, setDraft] = useState("");
  const {
    connectionStatus,
    error,
    isSending,
    messages,
    sendMessage,
  } = useNanobotWebSocket({
    url: import.meta.env.VITE_NANOBOT_WEBSOCKET_URL,
    chatId: CHAT_ID,
    sessionId: SESSION_ID,
  });

  const canSend = Boolean(
    connectionStatus === "connected" && !isSending && draft.trim(),
  );

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendMessage(draft)) {
      setDraft("");
    }
  }

  return (
    <main className="app-shell" aria-label="Nanobot chat">
      <header className="app-header">
        <p className="eyebrow">LOCAL AGENT</p>
        <h1>Nanobot Web UI</h1>
        <p className="subtitle">
          A local chat surface connected to the Nanobot WebSocket channel.
        </p>
      </header>

      <section className="chat-panel" aria-labelledby="conversation-title">
        <div className="chat-panel__header">
          <h2 id="conversation-title">Conversation</h2>
          <span className={`status-badge status-badge--${connectionStatus}`}>
            {STATUS_LABELS[connectionStatus]}
          </span>
        </div>

        {error !== null ? (
          <p className="connection-error" role="alert">
            {error}
          </p>
        ) : null}

        {messages.length === 0 ? (
          <div className="chat-empty-state">
            <p>No messages yet.</p>
            <span>
              {connectionStatus === "connected"
                ? "Send a message to start a conversation."
                : "Waiting for the local Nanobot connection."}
            </span>
          </div>
        ) : (
          <ol className="message-list" aria-live="polite">
            {messages.map((message) => (
              <li
                key={message.id}
                className={`message message--${message.role}`}
              >
                <span className="message__author">
                  {message.role === "user" ? "You" : "Nanobot"}
                </span>
                <p className={message.isStreaming ? "message__content is-streaming" : "message__content"}>
                  {message.content || "Thinking..."}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>

      <form className="composer" aria-label="Message composer" onSubmit={handleSubmit}>
        <label htmlFor="message">Message</label>
        <div className="composer__controls">
          <textarea
            id="message"
            name="message"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask Nanobot anything..."
            rows={3}
            disabled={isSending}
          />
          <button type="submit" disabled={!canSend}>
            {isSending ? "Sending..." : "Send"}
          </button>
        </div>
      </form>
    </main>
  );
}

export default App;
