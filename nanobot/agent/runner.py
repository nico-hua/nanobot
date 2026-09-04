"""Minimal non-streaming agent execution loop."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from ..tools import ToolRegistry

logger = logging.getLogger(__name__)

InjectionCallback = Callable[[], Awaitable[Sequence[HumanMessage]]]


class AgentRunnerError(RuntimeError):
    """Raised when an agent run cannot reach a final model response."""


@dataclass(frozen=True)
class AgentRunSpec:
    """All dependencies and input required for one agent run."""

    messages: Sequence[BaseMessage]
    provider: LLMProvider
    tool_registry: ToolRegistry
    max_iterations: int = 30
    blocked_tool_names: Sequence[str] = ()
    is_goal_mode: bool = False
    injection_callback: InjectionCallback | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.messages, Sequence) or not all(
            isinstance(message, BaseMessage) for message in self.messages
        ):
            raise TypeError("messages must be a sequence of BaseMessage instances")
        if not isinstance(self.provider, LLMProvider):
            raise TypeError("AgentRunSpec provider must be an LLMProvider")
        if not isinstance(self.tool_registry, ToolRegistry):
            raise TypeError("AgentRunSpec tool_registry must be a ToolRegistry")
        if not isinstance(self.max_iterations, int) or isinstance(
            self.max_iterations,
            bool,
        ):
            raise TypeError("max_iterations must be an integer")
        if self.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if isinstance(self.blocked_tool_names, (str, bytes)) or not isinstance(
            self.blocked_tool_names,
            Sequence,
        ):
            raise TypeError("blocked_tool_names must be a sequence of strings")
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.blocked_tool_names
        ):
            raise ValueError("blocked_tool_names must not contain blank names")
        if not isinstance(self.is_goal_mode, bool):
            raise TypeError("is_goal_mode must be a boolean")
        if self.injection_callback is not None and not callable(self.injection_callback):
            raise TypeError("injection_callback must be callable or None")
        if self.is_goal_mode and self.injection_callback is None:
            raise ValueError("goal-mode runs require an injection_callback")
        if not self.is_goal_mode and self.injection_callback is not None:
            raise ValueError("injection_callback is only supported for goal-mode runs")

        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "blocked_tool_names", tuple(self.blocked_tool_names))


@dataclass(frozen=True)
class AgentRunResult:
    """The final response and conversation state from one agent run."""

    content: str | None
    messages: tuple[BaseMessage, ...]
    tools_used: tuple[ToolCallRequest, ...]
    token_usage: TokenUsage | None
    stop_reason: str | None


class AgentRunner:
    """Run provider completions and sequential tool calls to a final response."""

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """Run tool-call rounds until the provider returns a final response."""

        conversation = list(spec.messages)
        tools_used: list[ToolCallRequest] = []
        token_usage: TokenUsage | None = None
        blocked_tool_names = set(spec.blocked_tool_names)
        tools = tuple(
            tool
            for tool in spec.tool_registry.tools
            if tool.name not in blocked_tool_names
        )
        logger.info(
            "Agent run started (messages=%d, tools=%d, max_iterations=%d)",
            len(conversation),
            len(tools),
            spec.max_iterations,
        )
        for iteration in range(spec.max_iterations):
            logger.debug("Requesting provider completion (iteration=%d)", iteration + 1)
            response = await spec.provider.complete(
                conversation,
                tools=tools or None,
            )
            token_usage = _combine_token_usage(token_usage, response.usage)
            if not response.tool_calls:
                conversation.append(AIMessage(content=response.content or ""))
                injected_messages = await _take_injected_messages(spec)
                if injected_messages:
                    conversation.extend(injected_messages)
                    continue
                logger.info(
                    "Agent run completed (iterations=%d, tool_calls=%d, stop_reason=%s)",
                    iteration + 1,
                    len(tools_used),
                    response.finish_reason,
                )
                return AgentRunResult(
                    content=response.content,
                    messages=tuple(conversation),
                    tools_used=tuple(tools_used),
                    token_usage=token_usage,
                    stop_reason=response.finish_reason,
                )

            conversation.append(
                AIMessage(
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )
            tools_used.extend(response.tool_calls)
            injected_messages: list[HumanMessage] = []
            for tool_call in response.tool_calls:
                if tool_call.name in blocked_tool_names:
                    logger.warning("Blocked requested tool (name=%s)", tool_call.name)
                    conversation.append(
                        ToolMessage(
                            content=(
                                "Error: Tool is not available in this agent run: "
                                f"{tool_call.name}"
                            ),
                            tool_call_id=tool_call.id,
                        )
                    )
                else:
                    logger.info("Executing requested tool (name=%s)", tool_call.name)
                    result = await spec.tool_registry.execute(
                        tool_call.name,
                        tool_call.arguments,
                    )
                    conversation.append(
                        ToolMessage(
                            content=result.content,
                            tool_call_id=tool_call.id,
                        )
                    )
            injected_messages.extend(await _take_injected_messages(spec))
            # Keep each assistant tool-call batch contiguous. Provider protocols
            # require every requested tool result before the next user message.
            conversation.extend(injected_messages)

        logger.error("Agent run exceeded maximum iteration count (%d)", spec.max_iterations)
        raise AgentRunnerError(
            f"Agent exceeded the maximum iteration count: {spec.max_iterations}"
        )


def _combine_token_usage(
    current: TokenUsage | None,
    response_usage: TokenUsage | None,
) -> TokenUsage | None:
    if response_usage is None:
        return current
    if current is None:
        return response_usage
    return TokenUsage(
        prompt_tokens=current.prompt_tokens + response_usage.prompt_tokens,
        completion_tokens=current.completion_tokens + response_usage.completion_tokens,
        total_tokens=current.total_tokens + response_usage.total_tokens,
    )


async def _take_injected_messages(spec: AgentRunSpec) -> tuple[HumanMessage, ...]:
    """Read goal-mode user input without changing the original request messages."""

    if spec.injection_callback is None:
        return ()
    messages = await spec.injection_callback()
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise TypeError("injection_callback must return a sequence of HumanMessage")
    if not all(isinstance(message, HumanMessage) for message in messages):
        raise TypeError("injection_callback must return only HumanMessage instances")
    return tuple(messages)
