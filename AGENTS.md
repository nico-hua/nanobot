
# Project Purpose

This repository is an educational reimplementation of an Agent system inspired by nanobot.

The goal is not to reproduce nanobot 1:1.
The primary goals are:

1. Understand the architecture and design decisions of a production Agent system.
2. Reimplement the important ideas in a smaller and clearer codebase.
3. Practice AI-assisted software development while keeping the code understandable and reviewable.
4. Prefer learning value and clarity over feature completeness.

# Development Principles

- Prefer simple and explicit implementations.
- Do not introduce abstractions for hypothetical future requirements.
- Do not implement features that are outside the current task.
- Keep subsystem boundaries clear.
- Avoid hidden global state.
- Prefer readable code over clever code.
- Production-oriented complexity from nanobot should only be reproduced when it is necessary for understanding the design.

# AI Coding Workflow

Before making a non-trivial change:

1. Inspect the relevant existing code.
2. Explain the intended design and change briefly.
3. Identify the files that need to change.
4. State the important interfaces and invariants.
5. Then implement the change.

Keep changes small and reviewable.

Do not implement multiple major subsystems in one task unless explicitly requested.

Do not silently refactor unrelated code.

# Architecture Learning

When implementing a subsystem inspired by nanobot, focus on:

- What problem the subsystem solves.
- Who calls it.
- What it calls.
- Inputs and outputs.
- State it owns.
- Important invariants.
- Main execution flow.
- Failure modes.
- Which parts of nanobot are essential design ideas and which are production glue.

Do not copy nanobot line by line.

Preserve the design idea when useful, but simplify the implementation where possible.

# Testing

Important behavior should have tests.

For each subsystem, test:

- normal behavior,
- important edge cases,
- expected failures,
- core invariants.

After modifying code, run the relevant tests.

Do not claim a task is complete if the relevant tests are failing.

# Logging and Error Handling

- `AgentLoop` initializes package logging with
  `nanobot.logging.configure_logging_from_config()` during construction. Set
  `logging.level` in `.nanobot/nanobot.json`; it defaults to `INFO`, and a
  missing configuration file also falls back to `INFO`. The setup only
  configures the `nanobot` logger and does not modify the host application's
  root logger.
- Runtime modules that log must create a module-level logger with
  `logging.getLogger(__name__)`. Do not use `print` for runtime diagnostics.
- Use `DEBUG` for bounded diagnostic metadata, `INFO` for major lifecycle
  transitions, `WARNING` for expected handled failures, and `ERROR` for an
  unexpected failure that affects the current operation.
- Use `logger.exception(...)` only while handling an unexpected exception at a
  subsystem boundary, so the traceback is retained. Do not add a traceback for
  expected validation failures or ordinary control flow.
- Preserve existing exception contracts. Validation and provider failures
  continue to raise their documented exceptions; do not convert every failure
  into a log message and continue execution.
- Tool-facing expected failures must continue to become `ToolResult` errors.
  `ToolRegistry` converts unknown tools, invalid arguments, and unexpected tool
  execution failures to that result while retaining the underlying traceback in
  an error log for unexpected execution failures.
- Never swallow `asyncio.CancelledError`. A coroutine may perform required
  cleanup, but it must re-raise the cancellation afterwards.
- Never log message content, tool arguments, file contents or paths, API keys,
  secrets, access tokens, authorization headers, or other sensitive values.
  Prefer counts, stable component names, and event types in logs.
- New runtime modules and tests must use this logging setup and follow these
  error-handling rules. Add focused tests when changing logging behavior or an
  exception boundary.

# Review Requirements

After implementation:

- Summarize the files changed.
- Explain the key design decisions.
- Mention any assumptions.
- Mention anything intentionally left unimplemented.
- Review the diff for unnecessary abstractions or unrelated changes.

# Repository Structure

Keep the project organized around clear subsystems.

Expected high-level areas may include:

- agent/
- model/
- tools/
- context/
- session/
- memory/
- tests/

Do not create all of these prematurely.
Create a subsystem only when the current implementation requires it.

# Definition of Done

A task is complete when:

- the requested behavior is implemented,
- the code is understandable,
- relevant tests pass,
- no unrelated functionality was added,
- the implementation can be explained clearly without relying on the reference repository.
