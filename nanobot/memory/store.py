"""Read and atomically replace long-term Agent memory in the workspace."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = logging.getLogger(__name__)


class MemoryStore:
    """Access the optional long-term memory file for one workspace."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("MemoryStore workspace must be a string or Path")
        self._memory_path = Path(workspace).resolve() / "memory" / "MEMORY.md"

    def read(self) -> str:
        """Return the current UTF-8 memory content, or an empty string when unavailable."""

        try:
            content = self._memory_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError):
            logger.warning("Skipping unreadable long-term memory file")
            return ""
        return content if content.strip() else ""

    def write(self, content: str) -> None:
        """Atomically replace the memory file with non-empty UTF-8 content."""

        if not isinstance(content, str):
            raise TypeError("MemoryStore content must be a string")
        if not content.strip():
            raise ValueError("MemoryStore content must not be blank")

        temporary_path: Path | None = None
        try:
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._memory_path.parent,
                prefix=f".{self._memory_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
            temporary_path.replace(self._memory_path)
        except (OSError, UnicodeError):
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
