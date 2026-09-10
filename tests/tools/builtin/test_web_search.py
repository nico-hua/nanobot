"""Tests for the Tavily-only web search tool without real network access."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping
from typing import Any

import aiohttp
from typing_extensions import Self

from nanobot.tools import ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import WebSearchTool


class WebSearchToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_requires_a_configured_tavily_api_key(self) -> None:
        tool, session = _tool_with_response(
            _FakeResponse(200, {"results": []}),
            tavily_api_key="",
        )

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Tavily API key is not configured")
        self.assertEqual(session.requests, [])

    async def test_sends_query_and_result_limit_to_tavily(self) -> None:
        tool, session = _tool_with_response(
            _FakeResponse(200, _search_response()),
        )

        result = await tool.execute("  current events  ", max_results=2)

        self.assertTrue(result.success)
        self.assertEqual(len(session.requests), 1)
        request = session.requests[0]
        self.assertEqual(request["url"], WebSearchTool.TAVILY_SEARCH_URL)
        self.assertEqual(request["json"], {
            "query": "current events",
            "max_results": 2,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        })
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-tavily-key")

    async def test_formats_titles_urls_and_summaries_within_the_requested_limit(self) -> None:
        tool, _ = _tool_with_response(
            _FakeResponse(
                200,
                _search_response(
                    {
                        "title": "Third source",
                        "url": "https://example.test/third",
                        "content": "Third summary",
                    }
                ),
            )
        )

        result = await tool.execute("Nanobot", max_results=2)

        self.assertTrue(result.success)
        self.assertIn("1. First source", result.content)
        self.assertIn("URL: https://example.test/first", result.content)
        self.assertIn("Summary: First summary", result.content)
        self.assertIn("2. Second source", result.content)
        self.assertNotIn("Third source", result.content)

    async def test_rejects_invalid_query_and_result_limit(self) -> None:
        tool, session = _tool_with_response(_FakeResponse(200, _search_response()))

        for query, max_results in (("", 5), ("Nanobot", 0), ("Nanobot", 11)):
            with self.subTest(query=query, max_results=max_results):
                result = await tool.execute(query, max_results=max_results)
                self.assertFalse(result.success)

        self.assertEqual(session.requests, [])

    async def test_returns_error_for_a_non_success_status(self) -> None:
        tool, _ = _tool_with_response(_FakeResponse(429, {"detail": "limited"}))

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Tavily request returned HTTP status 429")

    async def test_returns_error_when_the_request_times_out(self) -> None:
        tool, _ = _tool_with_response(
            _FakeResponse(200, _search_response(), enter_error=asyncio.TimeoutError())
        )

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Tavily search request timed out after 10 seconds")

    async def test_returns_error_for_a_network_failure(self) -> None:
        tool, _ = _tool_with_response(
            _FakeResponse(
                200,
                _search_response(),
                enter_error=aiohttp.ClientConnectionError(),
            )
        )

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Tavily search request failed (ClientConnectionError)",
        )

    async def test_returns_error_for_an_invalid_response_format(self) -> None:
        tool, _ = _tool_with_response(_FakeResponse(200, {"results": "not-a-list"}))

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Tavily response did not include a results list")

    async def test_returns_error_for_an_invalid_json_response(self) -> None:
        tool, _ = _tool_with_response(
            _FakeResponse(200, {}, json_error=ValueError("invalid JSON"))
        )

        result = await tool.execute("Nanobot")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Tavily returned an invalid JSON response")

    async def test_registry_executes_the_registered_search_tool(self) -> None:
        tool, _ = _tool_with_response(_FakeResponse(200, _search_response()))
        registry = ToolRegistry((tool,))

        result = await registry.execute("web_search", {"query": "Nanobot"})

        self.assertTrue(result.success)
        self.assertIn("First source", result.content)

    def test_create_reads_the_tavily_key_from_tool_context(self) -> None:
        tool = WebSearchTool.create(
            ToolContext(web_search_tavily_api_key="test-tavily-key")
        )

        self.assertIsInstance(tool, WebSearchTool)

    def test_loader_discovers_web_search_without_a_workspace(self) -> None:
        registry = ToolRegistry()

        names = ToolLoader().load(registry, ToolContext())

        self.assertIn("web_search", names)
        self.assertIsInstance(registry.get("web_search"), WebSearchTool)


def _tool_with_response(
    response: _FakeResponse,
    *,
    tavily_api_key: str = "test-tavily-key",
) -> tuple[WebSearchTool, _FakeSession]:
    session = _FakeSession(response)
    return (
        WebSearchTool(
            tavily_api_key=tavily_api_key,
            session_factory=lambda *, timeout: session,
        ),
        session,
    )


def _search_response(*additional_results: Mapping[str, str]) -> dict[str, Any]:
    return {
        "results": [
            {
                "title": "First source",
                "url": "https://example.test/first",
                "content": "First summary",
            },
            {
                "title": "Second source",
                "url": "https://example.test/second",
                "content": "Second summary",
            },
            *additional_results,
        ]
    }


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> _FakeResponse:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "json": dict(json),
            }
        )
        return self._response


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: object,
        *,
        enter_error: BaseException | None = None,
        json_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._enter_error = enter_error
        self._json_error = json_error

    async def __aenter__(self) -> Self:
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback

    async def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload
