import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPClient:
    def __init__(self, command: str, args: list[str]):
        self.server_params = StdioServerParameters(command=command, args=args)
        self.session: ClientSession | None = None

    async def start(self):
        read, write = await stdio_client(self.server_params)
        self.session = ClientSession(read, write)
        await self.session.initialize()

    async def list_tools(self):
        if not self.session:
            raise RuntimeError("session未启动")
        res = await self.session.list_tools()
        return res.tools

    async def call_tool(self, tool_name: str, arguments: dict):
        if not self.session:
            raise RuntimeError("session未启动")
        return await self.session.call_tool(tool_name, arguments)

    async def close(self):
        if self.session:
            await self.session.close()

async def main():
    # 示例：如果你要连本地mcp服务，改这里command和args
    client = MCPClient(command="python", args=["mcp_server.py"])
    await client.start()
    tools = await client.list_tools()
    print("可用工具：", [t.name for t in tools])
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
