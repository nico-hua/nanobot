/**
 * Small imperative lifecycle around one browser WebSocket.  Keeping retry
 * ownership here makes stale socket callbacks easy to ignore and test without
 * coupling connection management to React rendering.
 */

export type WebSocketLike = {
  readonly readyState: number;
  close(): void;
  send(data: string): void;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
};

type TimerHandle = ReturnType<typeof setTimeout>;

export type WebSocketConnectionCallbacks = {
  onOpen: (reconnected: boolean) => void;
  onMessage: (data: unknown) => void;
  onReconnecting: (attempt: number, delayMs: number) => void;
  onReconnectFailed: () => void;
};

export type WebSocketConnectionOptions = {
  url: string;
  callbacks: WebSocketConnectionCallbacks;
  createSocket?: (url: string) => WebSocketLike;
  schedule?: (callback: () => void, delayMs: number) => TimerHandle;
  clearScheduled?: (handle: TimerHandle) => void;
  reconnectDelaysMs?: readonly number[];
};

/** Keep retries bounded and predictable: 0.5s, 1s, then 1.5s. */
export const DEFAULT_RECONNECT_DELAYS_MS = [500, 1_000, 1_500] as const;

const OPEN = 1;

export type WebSocketConnection = {
  start: () => void;
  reconnect: () => void;
  close: () => void;
  isOpen: () => boolean;
  send: (data: string) => boolean;
};

/** Create one connection manager without creating multiple concurrent sockets. */
export function createWebSocketConnection({
  url,
  callbacks,
  createSocket = (connectionUrl) => new WebSocket(connectionUrl),
  schedule = (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearScheduled = (handle) => globalThis.clearTimeout(handle),
  reconnectDelaysMs = DEFAULT_RECONNECT_DELAYS_MS,
}: WebSocketConnectionOptions): WebSocketConnection {
  let socket: WebSocketLike | null = null;
  let reconnectTimer: TimerHandle | null = null;
  let connectionToken = 0;
  let reconnectAttempt = 0;
  let hasOpened = false;
  let closed = false;

  function isCurrent(candidate: WebSocketLike, token: number): boolean {
    return !closed && socket === candidate && connectionToken === token;
  }

  function cancelReconnectTimer(): void {
    if (reconnectTimer !== null) {
      clearScheduled(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function scheduleReconnect(): void {
    const delayMs = reconnectDelaysMs[reconnectAttempt];
    if (delayMs === undefined) {
      callbacks.onReconnectFailed();
      return;
    }

    reconnectAttempt += 1;
    callbacks.onReconnecting(reconnectAttempt, delayMs);
    reconnectTimer = schedule(() => {
      reconnectTimer = null;
      connect();
    }, delayMs);
  }

  function endConnection(candidate: WebSocketLike, token: number): void {
    if (!isCurrent(candidate, token)) {
      return;
    }

    socket = null;
    try {
      candidate.close();
    } catch {
      // The connection is already discarded; retry handling remains valid.
    }
    scheduleReconnect();
  }

  function connect(): void {
    if (closed || socket !== null || reconnectTimer !== null) {
      return;
    }

    const token = connectionToken + 1;
    connectionToken = token;
    let candidate: WebSocketLike;
    try {
      candidate = createSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }

    socket = candidate;
    candidate.onopen = () => {
      if (!isCurrent(candidate, token)) {
        return;
      }
      const reconnected = hasOpened;
      hasOpened = true;
      reconnectAttempt = 0;
      callbacks.onOpen(reconnected);
    };
    candidate.onmessage = (event) => {
      if (isCurrent(candidate, token)) {
        callbacks.onMessage(event.data);
      }
    };
    candidate.onerror = () => endConnection(candidate, token);
    candidate.onclose = () => endConnection(candidate, token);
  }

  function closeCurrentSocket(): void {
    const currentSocket = socket;
    socket = null;
    connectionToken += 1;
    if (currentSocket !== null) {
      try {
        currentSocket.close();
      } catch {
        // A failed close must not keep an obsolete socket active.
      }
    }
  }

  return {
    start(): void {
      closed = false;
      connect();
    },

    reconnect(): void {
      cancelReconnectTimer();
      closeCurrentSocket();
      closed = false;
      reconnectAttempt = 0;
      connect();
    },

    close(): void {
      closed = true;
      cancelReconnectTimer();
      closeCurrentSocket();
    },

    isOpen(): boolean {
      return socket?.readyState === OPEN;
    },

    send(data: string): boolean {
      const currentSocket = socket;
      if (currentSocket === null || currentSocket.readyState !== OPEN) {
        return false;
      }
      try {
        currentSocket.send(data);
        return true;
      } catch {
        endConnection(currentSocket, connectionToken);
        return false;
      }
    },
  };
}
