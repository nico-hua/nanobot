"""High-level lifecycle operations for persisted sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .goals import GoalState
from .models import Session, _validate_session_key
from .storage import JsonlSessionStorage


class SessionManager:
    """Create, save, delete, and list sessions in a workspace's sessions directory."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace)
        self._storage = JsonlSessionStorage(self._workspace / "sessions")

    @property
    def workspace(self) -> Path:
        """Return the workspace that owns this manager's session storage."""

        return self._workspace

    def get_or_create(self, session_key: str) -> Session:
        """Load a saved session or return a new empty session for the key."""

        _validate_session_key(session_key)
        return self._storage.load(session_key) or Session.create(session_key)

    def save(self, session: Session) -> Session:
        """Persist one complete session and return its timestamped version."""

        if not isinstance(session, Session):
            raise TypeError("SessionManager.save requires a Session")
        saved_session = session.with_updated_at(datetime.now(timezone.utc))
        self._storage.save(saved_session)
        return saved_session

    def create_goal(self, session: Session, objective: str) -> Session:
        """Create and persist one active goal for a session.

        A session may replace a terminal goal, but never an active one.  Keeping
        this state transition here lets slash commands and tools share the same
        validation and persistence path.
        """

        if not isinstance(session, Session):
            raise TypeError("SessionManager.create_goal requires a Session")
        normalized_objective = _normalize_goal_objective(objective)
        current = session.goal_state
        if current is not None and current.status == "active":
            raise ValueError(
                f"当前已有进行中的目标：{current.objective}。"
                "请等待目标完成或取消后再创建新的目标。"
            )
        return self.save(
            session.with_goal_state(GoalState.create(normalized_objective))
        )

    def update_goal(
        self,
        session: Session,
        *,
        objective: str | None = None,
        cancel: bool = False,
    ) -> Session:
        """Update or cancel the current active goal and persist the result."""

        if not isinstance(session, Session):
            raise TypeError("SessionManager.update_goal requires a Session")
        if not isinstance(cancel, bool):
            raise TypeError("cancel must be a boolean")

        current = session.goal_state
        if current is None:
            raise ValueError("当前会话没有目标。")
        if current.status != "active":
            raise ValueError(f"当前目标已处于 {current.status} 状态。")

        if cancel:
            if objective is not None:
                raise ValueError("停止目标时不能同时提供 objective")
            updated_goal = current.finish("cancelled")
        else:
            updated_goal = current.replace_objective(
                _normalize_goal_objective(objective)
            )
        return self.save(session.with_goal_state(updated_goal))

    def delete(self, session_key: str) -> bool:
        """Delete one saved session and report whether it existed."""

        return self._storage.delete(session_key)

    def list_sessions(self) -> tuple[Session, ...]:
        """Return all saved sessions in stable key order."""

        return self._storage.list_sessions()


def _normalize_goal_objective(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("目标描述不能为空。")
    return value.strip()
