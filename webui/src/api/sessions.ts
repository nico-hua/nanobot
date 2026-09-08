export type SessionSummary = {
  sessionId: string;
  updatedAt: string;
  messageCount: number;
  preview: string;
};

export type SessionHistoryMessage = {
  role: "user" | "assistant";
  content: string;
};

export type SessionHistory = {
  sessionId: string;
  updatedAt: string;
  messages: SessionHistoryMessage[];
};

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
): Promise<SessionSummary[]> {
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

function parseSessionSummary(value: unknown): SessionSummary {
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

function parseHistoryMessage(value: unknown): SessionHistoryMessage {
  if (!isRecord(value) || (value.role !== "user" && value.role !== "assistant")) {
    throw invalidResponse();
  }
  return {
    role: value.role,
    content: requiredText(value.content),
  };
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
  return typeof value === "object" && value !== null;
}
