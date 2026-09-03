"""Focused tests for the synchronous SpawnTool."""

from __future__ import annotations

import unittest

from nanobot.agent import AgentRunResult
from nanobot.providers import AIMessage
from nanobot.subagent import SubagentManager, SubagentRunResult
from nanobot.tools import RequestContext, ToolContext, bind_request_context
from nanobot.tools.builtin import SpawnTool


class RecordingSubagentManager(SubagentManager):
    def __init__(self, result: SubagentRunResult) -> None:
        self.result = result
        self.calls: list[tuple[str, RequestContext | None]] = []

    async def run(
        self,
        task: str,
        *,
        request_context: RequestContext | None = None,
    ) -> SubagentRunResult:
        self.calls.append((task, request_context))
        return self.result


class RaisingSubagentManager(RecordingSubagentManager):
    async def run(
        self,
        task: str,
        *,
        request_context: RequestContext | None = None,
    ) -> SubagentRunResult:
        del task, request_context
        raise RuntimeError("child failed")


class SpawnToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._request_context = RequestContext(
            session_key="session-1",
            channel="fake",
            chat_id="chat-1",
            sender_id="sender-1",
            metadata={"source": "test"},
        )

    def test_has_the_expected_schema_and_requires_a_manager(self) -> None:
        manager = RecordingSubagentManager(_success("unused"))
        tool = SpawnTool.create(ToolContext(subagent_manager=manager))

        self.assertTrue(SpawnTool.enabled(ToolContext(subagent_manager=manager)))
        self.assertFalse(SpawnTool.enabled(ToolContext()))
        self.assertEqual(tool.name, "spawn")
        self.assertEqual(tool.description, "Run a focused subagent task and return its final result.")
        self.assertEqual(
            tool.parameters_schema,
            {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The focused task for the subagent to complete.",
                    },
                },
                "required": ["task"],
            },
        )
        with self.assertRaisesRegex(ValueError, "SubagentManager"):
            SpawnTool.create(ToolContext())

    async def test_returns_the_subagent_final_text_and_forwards_request_context(self) -> None:
        manager = RecordingSubagentManager(_success("Child result."))
        tool = SpawnTool(manager)

        with bind_request_context(self._request_context):
            result = await tool.execute("  Investigate the issue.  ")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "Child result.")
        self.assertEqual(manager.calls, [("Investigate the issue.", self._request_context)])

    async def test_returns_clear_errors_for_missing_runtime_and_failed_subagent(self) -> None:
        failed_manager = RecordingSubagentManager(
            SubagentRunResult(error="Subagent could not complete the task.")
        )
        failed_tool = SpawnTool(failed_manager)

        missing_runtime = await failed_tool.execute("Task")
        with bind_request_context(self._request_context):
            failed = await failed_tool.execute("Task")

        self.assertFalse(missing_runtime.success)
        self.assertIn("user message", missing_runtime.error or "")
        self.assertFalse(failed.success)
        self.assertEqual(failed.error, "Subagent could not complete the task.")

    async def test_converts_unexpected_subagent_failures_to_tool_errors(self) -> None:
        tool = SpawnTool(RaisingSubagentManager(_success("unused")))

        with bind_request_context(self._request_context):
            result = await tool.execute("Task")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Subagent execution failed: RuntimeError")

    async def test_rejects_an_empty_task(self) -> None:
        tool = SpawnTool(RecordingSubagentManager(_success("unused")))

        with bind_request_context(self._request_context):
            result = await tool.execute(" ")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Task must be a non-empty string")


def _success(content: str) -> SubagentRunResult:
    return SubagentRunResult(
        agent_result=AgentRunResult(
            content=content,
            messages=(AIMessage(content=content),),
            tools_used=(),
            token_usage=None,
            stop_reason="stop",
        )
    )
