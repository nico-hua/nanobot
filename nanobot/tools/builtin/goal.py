"""Built-in tools for session-scoped sustained-goal state."""

from __future__ import annotations

import asyncio

from ...bus import InboundMessage, MessageBus
from ...session import SessionManager
from ...session.goals import build_goal_start_content
from ..base import Tool, ToolParameter, ToolResult
from ..context import RequestContext, ToolContext, get_request_context


class CreateGoalTool(Tool):
    """Create one active goal outside an active goal execution."""

    def __init__(
        self,
        session_manager: SessionManager,
        message_bus: MessageBus,
    ) -> None:
        if not isinstance(session_manager, SessionManager):
            raise TypeError("CreateGoalTool requires a SessionManager")
        if not isinstance(message_bus, MessageBus):
            raise TypeError("CreateGoalTool requires a MessageBus")
        self._session_manager = session_manager
        self._message_bus = message_bus
        super().__init__(
            name="create_goal",
            description=(
                "Create and schedule a sustained goal for the current session. "
                "Use this outside goal mode; it confirms creation immediately "
                "while the goal continues in a separate run."
            ),
            parameters=(
                ToolParameter(
                    name="objective",
                    description="The non-empty objective for the new sustained goal.",
                    type="string",
                    required=True,
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.session_manager is not None and context.message_bus is not None

    @classmethod
    def create(cls, context: ToolContext) -> CreateGoalTool:
        if context.session_manager is None or context.message_bus is None:
            raise ValueError("CreateGoalTool requires a SessionManager and MessageBus")
        return cls(context.session_manager, context.message_bus)

    async def execute(self, objective: str) -> ToolResult:
        request_context = get_request_context()
        if request_context is None:
            return _tool_error("create_goal requires an active request context")
        if request_context.is_goal_mode:
            return _tool_error("create_goal can only be used outside goal mode")
        try:
            session = self._session_manager.get_or_create(request_context.session_key)
            updated_session = self._session_manager.create_goal(session, objective)
        except (TypeError, ValueError) as error:
            return _tool_error(str(error))

        goal = updated_session.goal_state
        if goal is None:
            return _tool_error("create_goal did not persist goal state")
        try:
            await self._message_bus.publish_inbound(
                _goal_start_message(request_context, goal.objective)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            reason = str(error) or type(error).__name__
            return _tool_error(
                f"Goal was created but could not be scheduled: {reason}"
            )
        return ToolResult(
            content=(
                f"Goal created and scheduled: {goal.objective}\n\n"
                "Its progress will continue in a separate run."
            )
        )


class UpdateGoalTool(Tool):
    """Update or stop the active goal while its goal-mode turn is running."""

    def __init__(self, session_manager: SessionManager) -> None:
        if not isinstance(session_manager, SessionManager):
            raise TypeError("UpdateGoalTool requires a SessionManager")
        self._session_manager = session_manager
        super().__init__(
            name="update_goal",
            description=(
                "Update the active sustained goal, or cancel it when the goal "
                "should stop. Only available during goal mode."
            ),
            parameters=(
                ToolParameter(
                    name="action",
                    description="Use update to replace the objective, or stop to cancel it.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="objective",
                    description="The replacement objective required when action is update.",
                    type="string",
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.session_manager is not None

    @classmethod
    def create(cls, context: ToolContext) -> UpdateGoalTool:
        if context.session_manager is None:
            raise ValueError("UpdateGoalTool requires a SessionManager")
        return cls(context.session_manager)

    async def execute(
        self,
        action: str,
        objective: str | None = None,
    ) -> ToolResult:
        request_context = get_request_context()
        if request_context is None:
            return _tool_error("update_goal requires an active request context")
        if not request_context.is_goal_mode:
            return _tool_error("update_goal can only be used during goal mode")
        if not isinstance(action, str):
            return _tool_error("action must be update or stop")

        normalized_action = action.strip().lower()
        if normalized_action == "update":
            if objective is None:
                return _tool_error("Updating a goal requires a non-empty objective")
            cancel = False
        elif normalized_action == "stop":
            if objective is not None:
                return _tool_error("Stopping a goal does not accept objective")
            cancel = True
        else:
            return _tool_error("action must be update or stop")

        try:
            session = self._session_manager.get_or_create(request_context.session_key)
            updated_session = self._session_manager.update_goal(
                session,
                objective=objective,
                cancel=cancel,
            )
        except (TypeError, ValueError) as error:
            return _tool_error(str(error))

        goal = updated_session.goal_state
        if goal is None:
            return _tool_error("update_goal did not persist goal state")
        if cancel:
            return ToolResult(content=f"Cancelled goal: {goal.objective}")
        return ToolResult(content=f"Updated goal: {goal.objective}")


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)


def _goal_start_message(
    request_context: RequestContext,
    objective: str,
) -> InboundMessage:
    """Build the same internal goal turn used by the /goal command."""

    return InboundMessage(
        channel=request_context.channel,
        chat_id=request_context.chat_id,
        sender_id=request_context.sender_id,
        session_id=request_context.session_key,
        content=build_goal_start_content(objective),
        metadata={
            **request_context.metadata,
            "source": "goal",
        },
    )
