"""Shared dependencies used while creating tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolContext:
    """Dependencies available to tool factories.

    Workspace is optional so tools can decide whether they are enabled for a
    particular agent configuration.
    """

    workspace: str | Path | None = None
