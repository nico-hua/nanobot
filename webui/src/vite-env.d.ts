/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_NANOBOT_WEBSOCKET_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
