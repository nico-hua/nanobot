"""Bounded public-web search and fetch tools."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
SessionFactory = Callable[..., AbstractAsyncContextManager[Any]]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")
_SUPPORTED_APPLICATION_TYPES = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
    }
)
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_HEADER_CHARSET_PATTERN = re.compile(
    r"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    flags=re.IGNORECASE,
)
_META_CHARSET_PATTERN = re.compile(
    rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    flags=re.IGNORECASE,
)


class _ResponseTooLargeError(ValueError):
    """Raised when a response exceeds the configured download limit."""


class _ResponseContent(Protocol):
    async def read(self, n: int = -1) -> bytes:
        """Read up to ``n`` bytes from an HTTP response body."""


class _ReadableHTMLParser(HTMLParser):
    """Extract visible text without executing or preserving page markup."""

    _BLOCK_TAGS = frozenset(
        {
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "p",
            "pre",
            "section",
            "table",
            "tr",
        }
    )
    _SKIPPED_TAGS = frozenset({"noscript", "script", "style", "svg", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipped_tag_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.casefold()
        if normalized_tag in self._SKIPPED_TAGS:
            self._skipped_tag_depth += 1
            return
        if self._skipped_tag_depth == 0 and normalized_tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._SKIPPED_TAGS:
            self._skipped_tag_depth = max(0, self._skipped_tag_depth - 1)
            return
        if self._skipped_tag_depth == 0 and normalized_tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipped_tag_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


class WebFetchTool(Tool):
    """Fetch a public HTTP(S) page and return bounded, readable text only."""

    REQUEST_TIMEOUT_SECONDS = 10
    MAX_RESPONSE_BYTES = 1_000_000
    MAX_TEXT_CHARS = 20_000
    MAX_REDIRECTS = 5
    _READ_CHUNK_BYTES = 64 * 1024

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        resolver: HostResolver | None = None,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_text_chars: int = MAX_TEXT_CHARS,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("WebFetchTool timeout_seconds must be a positive integer")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("WebFetchTool max_response_bytes must be a positive integer")
        if (
            isinstance(max_text_chars, bool)
            or not isinstance(max_text_chars, int)
            or max_text_chars <= 0
        ):
            raise ValueError("WebFetchTool max_text_chars must be a positive integer")

        self._session_factory = session_factory or _create_fetch_session
        self._resolver = resolver or _resolve_host_addresses
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_text_chars = max_text_chars
        super().__init__(
            name="web_fetch",
            description=(
                "Fetch readable text from a public HTTP or HTTPS webpage. "
                "Use this for a known URL; it cannot search the web, execute "
                "page JavaScript, or download binary files."
            ),
            parameters=(
                ToolParameter(
                    name="url",
                    description="The public HTTP or HTTPS URL to fetch.",
                    type="string",
                    required=True,
                ),
            ),
        )

    async def execute(self, url: str) -> ToolResult:
        """Fetch one public URL, following only checked HTTP(S) redirects."""

        checked_url = await self._validate_public_url(url)
        if isinstance(checked_url, ToolResult):
            return checked_url

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with self._session_factory(timeout=timeout) as session:
                current_url = checked_url
                for redirect_count in range(self.MAX_REDIRECTS + 1):
                    async with session.get(
                        current_url,
                        allow_redirects=False,
                    ) as response:
                        if 300 <= response.status < 400:
                            location = response.headers.get("Location")
                            if not isinstance(location, str) or not location.strip():
                                return _tool_error(
                                    "Redirect response did not include a Location header"
                                )
                            if redirect_count == self.MAX_REDIRECTS:
                                return _tool_error(
                                    f"Too many redirects (maximum {self.MAX_REDIRECTS})"
                                )
                            next_url = await self._validate_public_url(
                                urljoin(current_url, location)
                            )
                            if isinstance(next_url, ToolResult):
                                return next_url
                            current_url = next_url
                            continue

                        if not 200 <= response.status < 300:
                            return _tool_error(
                                f"Request returned HTTP status {response.status}"
                            )

                        content_type_header = response.headers.get("Content-Type", "")
                        content_type = _content_type(content_type_header)
                        if not _is_supported_text_content_type(content_type):
                            display_type = content_type or "unknown"
                            return _tool_error(
                                f"Response content type is not supported: {display_type}"
                            )

                        body = await _read_bounded_body(
                            response.content,
                            self._max_response_bytes,
                        )
                        if b"\x00" in body:
                            return _tool_error("Response body is not supported as text")
                        text = _decode_response_text(
                            body,
                            content_type_header,
                            is_html=content_type in _HTML_CONTENT_TYPES,
                        )
                        if content_type in _HTML_CONTENT_TYPES:
                            text = _extract_html_text(text)
                        text, truncated = _truncate_text(text, self._max_text_chars)
                        return _success_result(
                            current_url,
                            response.status,
                            content_type,
                            text,
                            truncated,
                        )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return _tool_error(
                f"Request timed out after {self._timeout_seconds} seconds"
            )
        except _ResponseTooLargeError:
            return _tool_error(
                "Response body exceeds the maximum allowed size "
                f"of {self._max_response_bytes} bytes"
            )
        except (LookupError, UnicodeDecodeError):
            return _tool_error("Response body is not valid text in its declared encoding")
        except aiohttp.ClientError as error:
            return _tool_error(f"Network request failed ({type(error).__name__})")
        except OSError:
            return _tool_error("Network request failed")
        except ValueError:
            return _tool_error("Unable to parse the fetched HTML response")

    async def _validate_public_url(self, url: object) -> str | ToolResult:
        """Normalize and reject non-public targets before every request hop."""

        if not isinstance(url, str) or not url.strip():
            return _tool_error("URL must be a non-empty string")

        try:
            parsed = urlsplit(url.strip())
            port = parsed.port
        except ValueError:
            return _tool_error("URL contains an invalid port")

        scheme = parsed.scheme.casefold()
        if scheme not in _ALLOWED_SCHEMES:
            return _tool_error("Only http and https URLs are supported")
        if not parsed.hostname:
            return _tool_error("URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            return _tool_error("URL must not include credentials")

        host = parsed.hostname.casefold().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            _LOCAL_HOST_SUFFIXES
        ):
            return _tool_error("URL host is not publicly routable")

        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            literal_address = None

        if literal_address is not None:
            if not literal_address.is_global:
                return _tool_error("URL host is not publicly routable")
        else:
            resolved_port = port or (443 if scheme == "https" else 80)
            try:
                addresses = await self._resolver(host, resolved_port)
            except asyncio.CancelledError:
                raise
            except OSError:
                return _tool_error("Unable to resolve URL host")

            if not addresses or any(not _is_global_address(address) for address in addresses):
                return _tool_error("URL host does not resolve to a public address")

        # Fragments are client-side only and should not influence the request
        # or the final URL shown to the model.
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _create_fetch_session(*, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": "Nanobot web_fetch"},
    )


async def _resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({record[4][0] for record in records}))


def _is_global_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def _read_bounded_body(
    content: _ResponseContent,
    maximum_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await content.read(min(WebFetchTool._READ_CHUNK_BYTES, maximum_bytes + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum_bytes:
            raise _ResponseTooLargeError
        chunks.append(chunk)


def _content_type(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.split(";", maxsplit=1)[0].strip().casefold()


def _is_supported_text_content_type(content_type: str) -> bool:
    return (
        not content_type
        or content_type.startswith("text/")
        or content_type in _SUPPORTED_APPLICATION_TYPES
    )


def _decode_response_text(
    body: bytes,
    content_type_header: object,
    *,
    is_html: bool,
) -> str:
    charset = _charset_from_header(content_type_header)
    if charset is None and is_html:
        charset = _charset_from_html(body)
    return body.decode(charset or "utf-8")


def _charset_from_header(content_type_header: object) -> str | None:
    if not isinstance(content_type_header, str):
        return None
    match = _HEADER_CHARSET_PATTERN.search(content_type_header)
    return match.group(1) if match else None


def _charset_from_html(body: bytes) -> str | None:
    match = _META_CHARSET_PATTERN.search(body[:4096])
    if match is None:
        return None
    return match.group(1).decode("ascii")


def _extract_html_text(html: str) -> str:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def _truncate_text(text: str, maximum_characters: int) -> tuple[str, bool]:
    if len(text) <= maximum_characters:
        return text, False
    return (
        text[:maximum_characters].rstrip()
        + f"\n\n[Text truncated after {maximum_characters} characters]",
        True,
    )


def _success_result(
    final_url: str,
    status: int,
    content_type: str,
    text: str,
    truncated: bool,
) -> ToolResult:
    content = "\n".join(
        (
            f"Final URL: {final_url}",
            f"HTTP status: {status}",
            f"Content type: {content_type or 'text/plain (assumed)'}",
            "",
            text or "(empty response)",
        )
    )
    if truncated:
        content += "\n\nResult was truncated to the configured text limit."
    return ToolResult(content=content)


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)


class WebSearchTool(Tool):
    """Search the public web through Tavily and return bounded source snippets."""

    TAVILY_SEARCH_URL = "https://api.tavily.com/search"
    REQUEST_TIMEOUT_SECONDS = 10
    DEFAULT_MAX_RESULTS = 5
    MAX_RESULTS = 10
    MAX_SNIPPET_CHARS = 2_000

    def __init__(
        self,
        *,
        tavily_api_key: str = "",
        session_factory: SessionFactory | None = None,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(tavily_api_key, str):
            raise TypeError("WebSearchTool tavily_api_key must be a string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("WebSearchTool timeout_seconds must be a positive integer")

        self._tavily_api_key = tavily_api_key.strip()
        self._session_factory = session_factory or _create_search_session
        self._timeout_seconds = timeout_seconds
        super().__init__(
            name="web_search",
            description=(
                "Search the public web with Tavily and return source titles, URLs, "
                "and short summaries. Use web_fetch when you need the full text of "
                "one known result URL."
            ),
            parameters=(
                ToolParameter(
                    name="query",
                    description="The search query to send to Tavily.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    description=(
                        "Maximum number of results to return, from 1 to "
                        f"{self.MAX_RESULTS}. Defaults to {self.DEFAULT_MAX_RESULTS}."
                    ),
                    type="integer",
                ),
            ),
        )

    @classmethod
    def create(cls, context: ToolContext) -> WebSearchTool:
        """Create the Tavily client from runtime-owned tool configuration."""

        return cls(tavily_api_key=context.web_search_tavily_api_key)

    async def execute(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> ToolResult:
        """Request one bounded Tavily search without fetching result pages."""

        normalized_query = _normalize_search_query(query)
        if isinstance(normalized_query, ToolResult):
            return normalized_query
        if not _is_valid_search_result_limit(max_results):
            return _tool_error(
                f"max_results must be an integer from 1 to {self.MAX_RESULTS}"
            )
        if not self._tavily_api_key:
            return _tool_error("Tavily API key is not configured")

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        request_payload = {
            "query": normalized_query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with (
                self._session_factory(timeout=timeout) as session,
                session.post(
                    self.TAVILY_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {self._tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                ) as response,
            ):
                if not 200 <= response.status < 300:
                    return _tool_error(
                        f"Tavily request returned HTTP status {response.status}"
                    )
                response_payload = await response.json()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return _tool_error(
                f"Tavily search request timed out after {self._timeout_seconds} seconds"
            )
        except aiohttp.ClientError as error:
            return _tool_error(
                f"Tavily search request failed ({type(error).__name__})"
            )
        except OSError:
            return _tool_error("Tavily search request failed")
        except (TypeError, ValueError):
            return _tool_error("Tavily returned an invalid JSON response")

        return _format_search_results(response_payload, max_results)


def _create_search_session(*, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=timeout)


def _normalize_search_query(query: object) -> str | ToolResult:
    if not isinstance(query, str) or not query.strip():
        return _tool_error("query must be a non-empty string")
    return query.strip()


def _is_valid_search_result_limit(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= WebSearchTool.MAX_RESULTS
    )


def _format_search_results(
    response_payload: object,
    maximum_results: int,
) -> ToolResult:
    if not isinstance(response_payload, Mapping):
        return _tool_error("Tavily returned an invalid response format")

    raw_results = response_payload.get("results")
    if not isinstance(raw_results, list):
        return _tool_error("Tavily response did not include a results list")
    if not raw_results:
        return ToolResult(content="No search results found.")

    formatted_results: list[str] = []
    for raw_result in raw_results:
        if len(formatted_results) >= maximum_results:
            break
        if not isinstance(raw_result, Mapping):
            continue

        title = _required_result_text(raw_result.get("title"))
        url = _required_result_text(raw_result.get("url"))
        if title is None or url is None:
            continue
        summary = _search_summary_text(raw_result.get("content"))
        formatted_results.append(
            f"{len(formatted_results) + 1}. {title}\n"
            f"URL: {url}\n"
            f"Summary: {summary}"
        )

    if not formatted_results:
        return _tool_error("Tavily response did not contain usable search results")
    return ToolResult(content="Search results:\n\n" + "\n\n".join(formatted_results))


def _required_result_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _search_summary_text(value: object) -> str:
    if not isinstance(value, str):
        return "(No summary returned.)"
    summary = " ".join(value.split())
    if not summary:
        return "(No summary returned.)"
    if len(summary) <= WebSearchTool.MAX_SNIPPET_CHARS:
        return summary
    return (
        summary[: WebSearchTool.MAX_SNIPPET_CHARS].rstrip()
        + " [Summary truncated]"
    )
