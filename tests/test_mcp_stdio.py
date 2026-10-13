"""Starts the MCP server over stdio and checks tools are listed and that
vizard_status works with or without a running app."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
	params = StdioServerParameters(command=sys.executable, args=['-m', 'vizard_mcp'])
	async with stdio_client(params) as (r, w):
		async with ClientSession(r, w) as s:
			await s.initialize()
			tools = await s.list_tools()
			names = sorted(t.name for t in tools.tools)
			print('TOOLS', names)
			res = await s.call_tool('vizard_status', {})
			print('STATUS', res.content[0].text[:800])


asyncio.run(main())
