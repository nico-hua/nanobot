import type {
  PersistedSessionMessage,
  SessionHistory,
  SessionInfo,
  ToolCall,
} from "../types/protocol.js";

export type { SessionHistory, SessionInfo } from "../types/protocol.js";

export class SessionApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "SessionApiError";
  }
}

/** Fetch persisted chat summaries through Nanobot's read-only HTTP API. */
export async function fetchSessionSummaries(
  apiBaseUrl: string,
): Promise<SessionInfo[]> {
  const payload = await requestJson(apiBaseUrl, "/v1/sessions");
  if (!isRecord(payload) || !Array.isArray(payload.sessions)) {
    throw invalidResponse();
  }

  return payload.sessions.map(parseSessionSummary);
}

/** Fetch one UI-visible persisted transcript through Nanobot's HTTP API. */
export async function fetchSessionHistory(
  apiBaseUrl: string,
  sessionId: string,
): Promise<SessionHistory> {
  const payload = await requestJson(
    apiBaseUrl,
    `/v1/sessions/${encodeURIComponent(sessionId)}`,
  );
  if (!isRecord(payload) || !Array.isArray(payload.messages)) {
    throw invalidResponse();
  }

  const responseSessionId = requiredText(payload.session_id);
  const updatedAt = requiredText(payload.updated_at);
  return {
    sessionId: responseSessionId,
    updatedAt,
    messages: payload.messages.map(parseHistoryMessage),
  };
}

/** Create a browser-owned identifier without persisting a session eagerly. */
export function createSessionId(
  createUuid: () => string = () => crypto.randomUUID(),
): string {
  return `webui-${createUuid()}`;
}

async function requestJson(apiBaseUrl: string, path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl.replace(/\/+$/, "")}${path}`);
  } catch {
    throw new SessionApiError(
      0,
      "Could not reach the local Nanobot session API.",
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new SessionApiError(
      response.status,
      "Nanobot returned an invalid session API response.",
    );
  }

  if (!response.ok) {
    throw new SessionApiError(response.status, errorMessage(payload));
  }
  return payload;
}

function parseSessionSummary(value: unknown): SessionInfo {
  if (!isRecord(value)) {
    throw invalidResponse();
  }
  const messageCount = value.message_count;
  if (typeof messageCount !== "number" || !Number.isFinite(messageCount)) {
    throw invalidResponse();
  }
  return {
    sessionId: requiredText(value.session_id),
    updatedAt: requiredText(value.updated_at),
    messageCount,
    preview: typeof value.preview === "string" ? value.preview : "",
  };
}

function parseHistoryMessage(value: unknown): PersistedSessionMessage {
  if (!isRecord(value) || (value.role !== "user" && value.role !== "assistant")) {
    throw invalidResponse();
  }
  if (value.role === "user") {
    return {
      role: "user",
      content: requiredText(value.content),
    };
  }
  return {
    role: "assistant",
    content: requiredText(value.content),
    toolCalls: parseToolCalls(value.tool_calls),
  };
}

function parseToolCalls(value: unknown): ToolCall[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw invalidResponse();
  }
  return value.map((toolCall) => {
    if (
      !isRecord(toolCall) ||
      typeof toolCall.id !== "string" ||
      typeof toolCall.name !== "string" ||
      !isRecord(toolCall.arguments)
    ) {
      throw invalidResponse();
    }
    return {
      id: toolCall.id,
      name: toolCall.name,
      arguments: toolCall.arguments,
    };
  });
}

function requiredText(value: unknown): string {
  if (typeof value !== "string") {
    throw invalidResponse();
  }
  return value;
}

function errorMessage(payload: unknown): string {
  if (!isRecord(payload) || !isRecord(payload.error)) {
    return "Nanobot could not load session data.";
  }
  return typeof payload.error.message === "string"
    ? payload.error.message
    : "Nanobot could not load session data.";
}

function invalidResponse(): SessionApiError {
  return new SessionApiError(502, "Nanobot returned invalid session data.");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
