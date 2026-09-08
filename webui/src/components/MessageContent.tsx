import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type MessageContentProps = {
  role: "user" | "assistant";
  content: string;
  isStreaming: boolean;
};

/** Render user text literally and Agent output as safe GitHub-flavored Markdown. */
export function MessageContent({
  role,
  content,
  isStreaming,
}: MessageContentProps) {
  const className = isStreaming
    ? "message__content is-streaming"
    : "message__content";

  if (role === "user") {
    return <p className={className}>{content}</p>;
  }

  return (
    <div className={`${className} message__markdown`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
        {content || "Thinking..."}
      </ReactMarkdown>
    </div>
  );
}
