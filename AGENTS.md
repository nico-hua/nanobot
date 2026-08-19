
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
