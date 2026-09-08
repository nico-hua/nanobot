# Nanobot Web UI

This directory is an independent React + TypeScript + Vite frontend for the
Nanobot project. It connects to the existing WebSocket Channel and renders
normal and streaming assistant responses.

## Configuration

The frontend reads `VITE_NANOBOT_WEBSOCKET_URL` from the repository-root
`.env` file. Its value must match the host, port, and `/ws` path configured in
`.nanobot/nanobot.json`.

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

This runs Node's built-in test runner against the WebSocket protocol state,
covering connection status, client message shape, delta accumulation, turn
completion, and server errors. No browser-test dependency is required.

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
