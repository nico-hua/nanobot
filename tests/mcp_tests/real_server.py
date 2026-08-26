"""A real stdio MCP server used only by local integration tests."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("Nanobot MCP integration test server")


@server.tool()
def add_numbers(left: int, right: int) -> str:
    """Add two integer values."""

    return str(left + right)


if __name__ == "__main__":
    server.run(transport="stdio")
