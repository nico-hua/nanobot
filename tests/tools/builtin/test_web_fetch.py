"""Tests for the bounded public-web fetch tool without real network access."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping
from typing import Any

import aiohttp
from typing_extensions import Self

from nanobot.tools import ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import WebFetchTool, WebSearchTool


class WebFetchToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_http_and_https_text_urls(self) -> None:
        for url in ("http://example.test/page", "https://example.test/page"):
            tool, session = _tool_with_routes(
                {url: _FakeResponse(200, b"Public page", "text/plain; charset=utf-8")}
            )

            result = await tool.execute(url)

            self.assertTrue(result.success)
            self.assertIn(f"Final URL: {url}", result.content)
            self.assertIn("HTTP status: 200", result.content)
            self.assertIn("Public page", result.content)
            self.assertEqual(session.requests, [(url, False)])

    async def test_rejects_unsupported_or_non_public_urls(self) -> None:
        tool, session = _tool_with_routes({})

        for url in (
            "file:///tmp/secret.txt",
            "data:text/plain,hello",
            "javascript:alert(1)",
            "not a URL",
            "http://localhost/private",
            "http://127.0.0.1/private",
            "http://192.168.1.2/private",
        ):
            result = await tool.execute(url)
            self.assertFalse(result.success, url)
            self.assertIsNotNone(result.error)

        self.assertEqual(session.requests, [])

    async def test_converts_html_to_visible_readable_text(self) -> None:
        url = "https://example.test/article"
        html = b"""
            <html><head><title>Example</title><style>.hidden { color: red; }</style>
            <script>window.secret = 'never show';</script></head>
            <body><h1>Hello <strong>world</strong></h1><p>Useful paragraph.</p></body></html>
        """
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, html, "text/html; charset=utf-8")}
        )

        result = await tool.execute(url)

        self.assertTrue(result.success)
        self.assertIn("Example", result.content)
        self.assertIn("Hello world", result.content)
        self.assertIn("Useful paragraph.", result.content)
        self.assertNotIn("window.secret", result.content)
        self.assertNotIn("color: red", result.content)
        self.assertNotIn("<h1>", result.content)

    async def test_returns_plain_text_content(self) -> None:
        url = "https://example.test/notes"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"first line\nsecond line", "text/plain")}
        )

        result = await tool.execute(url)

        self.assertTrue(result.success)
        self.assertIn("first line\nsecond line", result.content)

    async def test_returns_error_for_non_success_status(self) -> None:
        url = "https://example.test/missing"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(404, b"not found", "text/plain")}
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Request returned HTTP status 404")

    async def test_rejects_binary_responses(self) -> None:
        url = "https://example.test/archive"
        tool, _ = _tool_with_routes(
            {
                url: _FakeResponse(
                    200,
                    b"not a downloadable archive",
                    "application/octet-stream",
                )
            }
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Response content type is not supported: application/octet-stream",
        )

    async def test_returns_error_when_request_times_out(self) -> None:
        url = "https://example.test/slow"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"", "text/plain", enter_error=asyncio.TimeoutError())}
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Request timed out after 10 seconds")

    async def test_truncates_long_text_result(self) -> None:
        url = "https://example.test/long"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"abcdefghijklmnop", "text/plain")},
            max_text_chars=12,
        )

        result = await tool.execute(url)

        self.assertTrue(result.success)
        self.assertIn("abcdefghijkl", result.content)
        self.assertIn("[Text truncated after 12 characters]", result.content)
        self.assertIn("Result was truncated", result.content)

    async def test_refuses_a_body_larger_than_the_download_limit(self) -> None:
        url = "https://example.test/oversized"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"123456", "text/plain")},
            max_response_bytes=5,
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Response body exceeds the maximum allowed size of 5 bytes",
        )

    async def test_returns_error_for_an_undecodable_text_response(self) -> None:
        url = "https://example.test/invalid-text"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"\xff\xfe", "text/plain; charset=utf-8")}
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Response body is not valid text in its declared encoding",
        )

    async def test_returns_error_for_network_failure(self) -> None:
        url = "https://example.test/unavailable"
        tool, _ = _tool_with_routes(
            {
                url: _FakeResponse(
                    200,
                    b"",
                    "text/plain",
                    enter_error=aiohttp.ClientConnectionError(),
                )
            }
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Network request failed (ClientConnectionError)")

    async def test_rejects_redirect_to_a_private_address(self) -> None:
        url = "https://example.test/start"
        tool, session = _tool_with_routes(
            {
                url: _FakeResponse(
                    302,
                    b"",
                    "text/plain",
                    headers={"Location": "http://127.0.0.1/private"},
                )
            }
        )

        result = await tool.execute(url)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "URL host is not publicly routable")
        self.assertEqual(session.requests, [(url, False)])

    async def test_registry_executes_the_registered_tool(self) -> None:
        url = "https://example.test/registry"
        tool, _ = _tool_with_routes(
            {url: _FakeResponse(200, b"registry content", "text/plain")}
        )
        registry = ToolRegistry((tool,))

        result = await registry.execute("web_fetch", {"url": url})

        self.assertTrue(result.success)
        self.assertIn("registry content", result.content)

    def test_loader_discovers_web_fetch_without_a_workspace(self) -> None:
        registry = ToolRegistry()

        names = ToolLoader().load(registry, ToolContext())

        self.assertEqual(names, ("web_fetch", "web_search"))
        self.assertIsInstance(registry.get("web_fetch"), WebFetchTool)
        self.assertIsInstance(registry.get("web_search"), WebSearchTool)


def _tool_with_routes(
    routes: Mapping[str, _FakeResponse],
    **tool_arguments: Any,
) -> tuple[WebFetchTool, _FakeSession]:
    session = _FakeSession(routes)
    return (
        WebFetchTool(
            session_factory=lambda *, timeout: session,
            resolver=_resolve_to_public_address,
            **tool_arguments,
        ),
        session,
    )


async def _resolve_to_public_address(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("8.8.8.8",)


class _FakeSession:
    def __init__(self, routes: Mapping[str, _FakeResponse]) -> None:
        self._routes = dict(routes)
        self.requests: list[tuple[str, bool]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback

    def get(self, url: str, *, allow_redirects: bool) -> _FakeResponse:
        self.requests.append((url, allow_redirects))
        return self._routes[url]


class _FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: Mapping[str, str] | None = None,
        enter_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.content = _FakeContent(body)
        self._enter_error = enter_error

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


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, maximum_bytes: int = -1) -> bytes:
        if not self._body:
            return b""
        if maximum_bytes < 0:
            maximum_bytes = len(self._body)
        chunk = self._body[:maximum_bytes]
        self._body = self._body[maximum_bytes:]
        return chunk
