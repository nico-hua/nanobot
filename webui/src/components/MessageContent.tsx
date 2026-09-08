import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type {
  AssistantMessage,
  UserMessage,
} from "../types/protocol.js";

type MessageContentProps =
  | Pick<UserMessage, "role" | "content" | "isStreaming">
  | Pick<
      AssistantMessage,
      "role" | "content" | "isStreaming" | "toolCalls"
    >;

/** Render user text literally and Agent output as safe GitHub-flavored Markdown. */
export function MessageContent(message: MessageContentProps) {
  const className = message.isStreaming
    ? "message__content is-streaming"
    : "message__content";

  if (message.role === "user") {
    return <p className={className}>{message.content}</p>;
  }

  return (
    <div className={`${className} message__markdown`}>
      {message.toolCalls.length > 0 ? (
        <section aria-label="Tool calls" className="tool-calls">
          {message.toolCalls.map((toolCall) => (
            <details className="tool-call" key={toolCall.id}>
              <summary>Called {toolCall.name}</summary>
              <pre>
                <code>{JSON.stringify(toolCall.arguments, null, 2)}</code>
              </pre>
            </details>
          ))}
        </section>
      ) : null}
      {message.content ? (
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
          {message.content}
        </ReactMarkdown>
      ) : message.toolCalls.length === 0 ? (
        <p>Thinking...</p>
      ) : null}
    </div>
  );
}
