"""A small wrapper around an MCP stdio session, for tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent


def result_text(result: CallToolResult) -> str:
    """Concatenate the text blocks of a tool result."""
    return "\n".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


class MCPClient:
    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def list_tools(self) -> list[str]:
        result = await self._session.list_tools()
        return [tool.name for tool in result.tools]

    async def call(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> CallToolResult:
        return await self._session.call_tool(name, arguments or {})

    async def text(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> str:
        result = await self.call(name, arguments)
        assert not result.isError, f"{name} errored: {result_text(result)}"
        return result_text(result)


@asynccontextmanager
async def connect_stdio_client(
    command: str, args: list[str], env: dict[str, str], cwd: str
) -> AsyncIterator[MCPClient]:
    params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPClient(session)
