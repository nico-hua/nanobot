"""Focused tests for the minimal AgentRunner loop."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from nanobot.agent import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.tools import Tool, ToolParameter, ToolRegistry, ToolResult


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.complete_calls: list[
            tuple[tuple[BaseMessage, ...], tuple[Tool, ...] | None]
        ] = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del max_tokens, temperature
        self.complete_calls.append(
            (tuple(messages), tuple(tools) if tools is not None else None)
        )
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Provider received an unexpected completion request") from exc

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("AgentRunner must not use streaming")


class RecordingTool(Tool):
    def __init__(self, name: str = "record") -> None:
        super().__init__(
            name=name,
            description="Record one value.",
            parameters=(
                ToolParameter(
                    name="value",
                    description="A value to record.",
                    type="string",
                    required=True,
                ),
            ),
        )
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **arguments: Any) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(content=f"recorded: {arguments['value']}")


class FailingTool(Tool):
    def __init__(self) -> None:
        super().__init__("failing", "Always raises an error.")

    async def execute(self, **arguments: Any) -> ToolResult:
        raise RuntimeError("service unavailable")


def tool_call(call_id: str, name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


class AgentRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_a_plain_text_response_without_tool_execution(self) -> None:
        provider = ScriptedProvider(
            (LLMResponse(content="Hello.", finish_reason="stop"),)
        )
        tool = RecordingTool()
        messages = (HumanMessage(content="Say hello."),)

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=messages,
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
            )
        )

        self.assertEqual(
            result,
            AgentRunResult(
                content="Hello.",
                messages=(
                    HumanMessage(content="Say hello."),
                    AIMessage(content="Hello."),
                ),
                tools_used=(),
                token_usage=None,
                stop_reason="stop",
            ),
        )
        self.assertEqual(tool.calls, [])
        self.assertEqual(provider.complete_calls, [(messages, (tool,))])

    async def test_executes_a_tool_and_returns_the_follow_up_response(self) -> None:
        request = tool_call("call-1", "record", value="Beijing")
        provider = ScriptedProvider(
            (
                LLMResponse(
                    content="I will check.",
                    tool_calls=(request,),
                    usage=TokenUsage(10, 2, 12),
                ),
                LLMResponse(
                    content="Beijing was recorded.",
                    finish_reason="stop",
                    usage=TokenUsage(15, 4, 19),
                ),
            )
        )
        tool = RecordingTool()
        original_messages = (HumanMessage(content="Record Beijing."),)

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=original_messages,
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
            )
        )

        self.assertEqual(result.content, "Beijing was recorded.")
        self.assertEqual(result.tools_used, (request,))
        self.assertEqual(result.token_usage, TokenUsage(25, 6, 31))
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(tool.calls, [{"value": "Beijing"}])
        self.assertEqual(original_messages, (HumanMessage(content="Record Beijing."),))
        self.assertEqual(provider.complete_calls[0][1], (tool,))
        self.assertEqual(
            provider.complete_calls[0][1][0].parameters_schema,
            tool.parameters_schema,
        )
        self.assertEqual(
            provider.complete_calls[1][0],
            (
                HumanMessage(content="Record Beijing."),
                AIMessage(content="I will check.", tool_calls=(request,)),
                ToolMessage(content="recorded: Beijing", tool_call_id="call-1"),
            ),
        )
        self.assertEqual(
            result.messages,
            (
                HumanMessage(content="Record Beijing."),
                AIMessage(content="I will check.", tool_calls=(request,)),
                ToolMessage(content="recorded: Beijing", tool_call_id="call-1"),
                AIMessage(content="Beijing was recorded."),
            ),
        )

    async def test_injects_goal_user_input_after_a_complete_tool_batch(self) -> None:
        request = tool_call("call-1", "record", value="Beijing")
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(request,)),
                LLMResponse(content="Updated goal response."),
            )
        )
        tool = RecordingTool()
        injected = HumanMessage(content="Please also include Shanghai.")
        callback_calls = 0

        async def inject_user_messages() -> tuple[HumanMessage, ...]:
            nonlocal callback_calls
            callback_calls += 1
            return (injected,) if callback_calls == 1 else ()

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Continue the goal."),),
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
                is_goal_mode=True,
                injection_callback=inject_user_messages,
            )
        )

        self.assertEqual(
            provider.complete_calls[1][0],
            (
                HumanMessage(content="Continue the goal."),
                AIMessage(content="", tool_calls=(request,)),
                ToolMessage(content="recorded: Beijing", tool_call_id="call-1"),
                injected,
            ),
        )
        self.assertEqual(result.messages[-2:], (injected, AIMessage(content="Updated goal response.")))
        self.assertEqual(callback_calls, 2)

    async def test_executes_multiple_tool_rounds_in_order(self) -> None:
        first_request = tool_call("call-1", "record", value="first")
        second_request = tool_call("call-2", "record", value="second")
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(first_request,)),
                LLMResponse(tool_calls=(second_request,)),
                LLMResponse(content="Both values were recorded."),
            )
        )
        tool = RecordingTool()

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Record two values."),),
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
            )
        )

        self.assertEqual(result.content, "Both values were recorded.")
        self.assertEqual(result.tools_used, (first_request, second_request))
        self.assertEqual(tool.calls, [{"value": "first"}, {"value": "second"}])
        self.assertEqual(
            provider.complete_calls[2][0][-2:],
            (
                AIMessage(content="", tool_calls=(second_request,)),
                ToolMessage(content="recorded: second", tool_call_id="call-2"),
            ),
        )

    async def test_returns_an_unknown_tool_error_to_the_model(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(tool_call("missing-1", "missing"),)),
                LLMResponse(content="The tool is unavailable."),
            )
        )

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Try the missing tool."),),
                provider=provider,
                tool_registry=ToolRegistry(),
            )
        )

        self.assertEqual(result.content, "The tool is unavailable.")
        self.assertEqual(
            provider.complete_calls[1][0][-1],
            ToolMessage(
                content="Error: Unknown tool: missing",
                tool_call_id="missing-1",
            ),
        )

    async def test_hides_and_rejects_a_blocked_tool(self) -> None:
        request = tool_call("blocked-1", "spawn", value="task")
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(request,)),
                LLMResponse(content="The tool is unavailable."),
            )
        )
        tool = RecordingTool(name="spawn")

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Create a subagent."),),
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
                blocked_tool_names=("spawn",),
            )
        )

        self.assertEqual(result.content, "The tool is unavailable.")
        self.assertEqual(provider.complete_calls[0][1], None)
        self.assertEqual(tool.calls, [])
        self.assertEqual(
            provider.complete_calls[1][0][-1],
            ToolMessage(
                content="Error: Tool is not available in this agent run: spawn",
                tool_call_id="blocked-1",
            ),
        )

    async def test_returns_a_tool_execution_error_to_the_model(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(tool_call("failure-1", "failing"),)),
                LLMResponse(content="The tool failed."),
            )
        )

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Try the failing tool."),),
                provider=provider,
                tool_registry=ToolRegistry((FailingTool(),)),
            )
        )

        self.assertEqual(result.content, "The tool failed.")
        tool_message = provider.complete_calls[1][0][-1]
        self.assertIsInstance(tool_message, ToolMessage)
        self.assertEqual(tool_message.tool_call_id, "failure-1")
        self.assertIn("Tool execution failed: failing", tool_message.content)

    async def test_returns_completed_tool_batches_at_the_iteration_limit(self) -> None:
        provider = ScriptedProvider(
            (LLMResponse(tool_calls=(tool_call("call-1", "record", value="loop"),)),)
        )
        tool = RecordingTool()

        result = await AgentRunner().run(
            AgentRunSpec(
                messages=(HumanMessage(content="Keep calling tools."),),
                provider=provider,
                tool_registry=ToolRegistry((tool,)),
                max_iterations=1,
            )
        )

        self.assertEqual(tool.calls, [{"value": "loop"}])
        self.assertEqual(len(provider.complete_calls), 1)
        self.assertIsNone(result.content)
        self.assertEqual(result.stop_reason, "max_iterations")
        self.assertEqual(
            result.messages,
            (
                HumanMessage(content="Keep calling tools."),
                AIMessage(
                    content="",
                    tool_calls=(tool_call("call-1", "record", value="loop"),),
                ),
                ToolMessage(content="recorded: loop", tool_call_id="call-1"),
            ),
        )
