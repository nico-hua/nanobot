# Nanobot Web UI

This directory is an independent React + TypeScript + Vite frontend for the
Nanobot project. It connects to the existing WebSocket Channel for normal and
streaming assistant responses, including tool-call progress, and uses the local
HTTP API to list and load persisted sessions.

## Configuration

The frontend reads the following browser-visible values from the repository-root
`.env` file:

- `VITE_NANOBOT_WEBSOCKET_URL` must match the host, port, and `/ws` path
  configured in `channel.websocket` in `.nanobot/nanobot.json`.
- `VITE_NANOBOT_API_URL` must match the local `api.host` and `api.port` in
  `.nanobot/nanobot.json`. It is used only to list saved sessions and load a
  selected transcript.

## Sessions

The left sidebar displays persisted sessions returned by `GET /v1/sessions`.
Selecting one loads its user/assistant transcript through
`GET /v1/sessions/{session_id}` and uses that ID for subsequent WebSocket
messages. Assistant tool calls are shown with the historical response, while
tool results remain internal. **New session** creates a browser-side unique ID
and clears only the current UI; it is persisted by Nanobot after the first
message is sent.

## Streaming events

For a streaming response, the WebSocket protocol can send `tool_call` events
before text `delta` events. The UI attaches each tool's name and formatted
arguments to the current assistant response, then uses `turn_end` to finalize
the complete text without creating a duplicate message.

## Prerequisites

Use a Node.js version supported by Vite 8. The project is developed with Node
22.

## Install

```bash
npm install
```

## Develop

```bash
npm run dev
```

Vite prints the local development-server URL after startup.

## Production build

```bash
npm run build
```

This performs strict TypeScript checking and creates `dist/`.

## Verify

```bash
npm test
```

This runs Node's built-in test runner against WebSocket and session UI state,
covering connection status, client message shape, tool-call and delta
accumulation, turn completion, session API parsing, session switching, and
error handling. No browser-test dependency is required.

If npm reports an internal npm `edgesOut` error after dependencies change,
remove the generated install state and install again:

```powershell
Remove-Item -Recurse -Force node_modules
Remove-Item -Force package-lock.json
npm install
```

The test command uses Node's built-in test runner. The Vite development and
production commands therefore do not depend on a browser-test framework.

## Preview a production build

```bash
npm run preview
```
