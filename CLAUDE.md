# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`nanobot` is an educational, from-scratch reimplementation of an Agent system. The goal is clarity, readability, and testability of the key Agent boundaries — not a 1:1 port of a production framework. See `AGENTS.md` for the full development principles and workflow; `DEVELOPMENT_PROGRESS.md` is a dated log of what has been built and what is intentionally left out.

## Commands

There is no build step, linter, or dependency lockfile (`pyproject.toml`/`requirements.txt` are absent). Dependencies are installed manually into the environment. Tests use the stdlib `unittest` runner.

```bash
# Run the app (PowerShell, as documented in README.md)
python -m nanobot --config .nanobot/nanobot.json
python -m nanobot --config .nanobot/nanobot.json --workspace <workspace-path>

# Full offline test suite — set both live-test gates to 0 (PowerShell)
$env:RUN_DEEPSEEK_LIVE_TESTS='0'
$env:NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS='0'
python -B -m unittest discover -s tests -t . -p "test*.py"

# Same, in bash (the shell used by this environment)
RUN_DEEPSEEK_LIVE_TESTS=0 NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=0 python -B -m unittest discover -s tests -t . -p "test*.py"

# Run one test module / one test case
python -B -m unittest tests.agent.test_loop
python -B -m unittest tests.agent.test_loop.AgentLoopTest.test_some_method
```

Provider/QQ live tests are skipped by default and only run when `RUN_DEEPSEEK_LIVE_TESTS=1` (DeepSeek provider smoke tests) or `NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=1` (QQ end-to-end) is set with local credentials. Credentials never enter the repo.

## Dependencies

Runtime: `pydantic` (v2 API: `model_validate`, `field_validator`, `AliasChoices`), `openai`, `anthropic`, `mcp`, `httpx`. Optional at runtime: `qq-botpy` (only needed to start the QQ channel). Test-only: `fastmcp` (`mcp.server.fastmcp`, used by the MCP stdio integration test). Code targets Python 3.14 and uses `from __future__ import annotations` throughout.

## Architecture

`Application` (`nanobot/cli/application.py`) is the composition root: it wires every component together, owns the lifecycle (`start`/`run`/`close`), and supervises the long-running tasks. `python -m nanobot` → `nanobot/cli/main.py` → load config → build `Application` → `asyncio.run`.

Data flow (from README.md):

```text
Channel -> MessageBus -> AgentLoop -> AgentRunner -> LLMProvider
                              |             |
                              |             +-> ToolRegistry -> builtin / MCP tools
                              |
                              +-> SessionManager -> workspace/sessions
                              +-> MemoryStore -> workspace/memory
                              +-> SessionCompactor / MemoryConsolidator (background)
                              +-> CronService -> CronMessagePublisher -> MessageBus
```

| Subsystem | Responsibility |
|---|---|
| `config/` | Pydantic schemas (`schema.py`) + loader that merges non-sensitive `.nanobot/nanobot.json` with secrets from `.env` (`loader.py`) |
| `providers/` | `LLMProvider` ABC, vendor-neutral `LLMResponse`/message types (`messages.py`), OpenAI-compat and Anthropic-compat implementations, factory |
| `tools/` | `Tool` ABC + `ToolParameter` (scalar-only), `ToolRegistry` (validate + execute), `ToolLoader` (discover builtins in `tools/builtin/`) |
| `bus/` | `asyncio.Queue`-based `MessageBus` with `InboundMessage`/`OutboundMessage` routing records |
| `channels/` | `BaseChannel`, `ChannelManager` (lifecycle + outbound dispatch), `FakeChannel`, `qq/` QQ channel |
| `agent/` | `AgentRunner` (minimal tool-call loop), `AgentLoop` (bus consumption, per-session locking, persistence, background tasks), `ContextBuilder` (system prompt), `CommandRouter` (slash commands), logging setup |
| `session/` | `Session` model, `SessionManager`, atomic JSONL storage, `SessionCompactor`, dependency-free token estimation |
| `memory/` | `MemoryStore` (MEMORY.md + durable event queue + cursor), `MemoryConsolidator` (LLM-backed), `MemoryEventConsumer` (background) |
| `skills/` | `SkillsLoader`: static `SKILL.md` discovery, `$skill-name` activation, `nanobot.requires` dependency checks |
| `mcp/` | `MCPProvider` + `MCPToolWrapper`; stdio/SSE/Streamable HTTP, dynamically registers tools into the shared `ToolRegistry` |
| `cron/` | `CronService` (in-memory scheduler) + atomic JSON storage; due tasks are republished as inbound bus messages |

## Key invariants (span multiple files)

- **Two distinct message families.** `providers/messages.py` defines LLM-facing frozen dataclasses (`SystemMessage`, `HumanMessage`, `AIMessage`, `ToolMessage`). `bus/messages.py` defines routing records (`InboundMessage`, `OutboundMessage` with `channel`/`chat_id`/`sender_id`/`session_id`/`metadata`). They are converted at the boundaries — do not mix them.
- **The system prompt is rebuilt every request** in `ContextBuilder.build_request_messages()`. It is assembled from workspace files (`AGENTS.md`, `SOUL.md`, `USER.md`), long-term memory (`<workspace>/memory/MEMORY.md`), and Skills. It is never persisted: sessions store only user/assistant/tool messages. Session summary and memory are appended to the fresh prompt, not saved as messages.
- **Session identity.** A session key is a non-empty `session_id`, else `channel:chat_id` (`_session_key` in `agent/loop.py`). Files are SHA-256-hashed JSONL under `<workspace>/sessions/`, atomically replaced via temp file + `os.replace`.
- **Memory event queue + cursor.** Completed turns append immutable snapshots to `<workspace>/memory/history.jsonl`; consolidation writes `MEMORY.md` first and only then advances `.memory_cursor`. A failed consolidation leaves the cursor unchanged so it retries on next start.
- **Compaction is turn-granular.** Automatic (background) triggers at `compaction_threshold_tokens`; `/compact` triggers when raw history exceeds `compaction_recent_tokens`. Only complete turns are summarized; a tool call and its tool result are never split.
- **Slash commands are routed before the LLM** in `CommandRouter` (`/new`, `/stop`, `/help`, `/compact`, `/memory`). `/stop` is special-cased in `AgentLoop` to bypass the per-session lock. Unknown commands return help text without hitting the provider, session, or memory queue.
- **Tool factories.** Builtin tools are discovered by `ToolLoader` and instantiated via the `Tool.enabled(context)` / `Tool.create(context)` classmethod protocol with a shared `ToolContext`. `ToolParameter` supports only scalar `string`/`integer`/`number`/`boolean`; `ToolRegistry.execute` converts unknown tools, invalid arguments, and unexpected failures into `ToolResult` errors.
- **Logging.** The CLI initializes `nanobot.agent.logging.configure_logging()` before config load, then `configure_logging_from_config()`. Runtime modules use `logging.getLogger(__name__)`, never `print`. Never log message content, tool arguments, file paths, or credentials. Never swallow `asyncio.CancelledError` — a coroutine may clean up but must re-raise.
- **User-facing strings** (slash-command replies, error messages) are written in Chinese; log messages and identifiers are English.

## Development conventions

From `AGENTS.md` (authoritative; abbreviated here): prefer simple, explicit implementations; do not add abstractions for hypothetical future requirements; keep changes small and reviewable; do not silently refactor unrelated code. Before a non-trivial change, inspect the relevant code, explain the intended design, identify files to change, and state the interfaces/invariants — then implement. Important behavior must have tests covering normal behavior, edge cases, expected failures, and core invariants; do not claim a task is complete with failing tests.
