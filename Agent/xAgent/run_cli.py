"""CLI 启动入口：验证 core 可以完全脱离 Textual 独立运行。

这个文件只负责终端输入输出，不属于 core 包。
"""

from __future__ import annotations

import asyncio

from core.agent import Agent
from core.config import ConfigError, Settings
from core.events import (
    ErrorEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)

HELP = """可用命令：
  /help    显示帮助
  /clear   重置 Agent 上下文
  /status  查看运行状态
  /quit    退出"""


async def repl() -> int:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    agent = Agent(settings=settings)
    print("xAgent · 文件管理 Agent（CLI 模式）")
    print(f"模型：{settings.model}")
    print(f"沙箱：{agent.sandbox.root}")
    print("输入 /help 查看命令，/quit 退出。\n")

    try:
        while True:
            try:
                line = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line in {"/quit", "/exit", "/q"}:
                break
            if line == "/help":
                print(HELP)
                print()
                continue
            if line == "/clear":
                agent.reset()
                print("上下文已清空。\n")
                continue
            if line == "/status":
                print(f"模型：{settings.model}")
                print(f"沙箱：{agent.sandbox.root}")
                print(f"上下文消息数：{len(agent.history)}")
                print(f"可用工具数：{len(agent.registry)}\n")
                continue

            line_open = False
            async for event in agent.stream_turn(line):
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                    line_open = True
                elif isinstance(event, ToolCallStarted):
                    if line_open:
                        print()
                        line_open = False
                    print(f"[调用] {event.name}({event.raw_arguments})")
                elif isinstance(event, ToolCallFinished):
                    mark = "OK  " if event.ok else "FAIL"
                    print(f"[{mark}] {event.output}")
                elif isinstance(event, ErrorEvent):
                    if line_open:
                        print()
                        line_open = False
                    print(f"[错误] {event.message}")
                elif isinstance(event, TurnFinished):
                    if line_open:
                        print()
                        line_open = False
                    print()
    finally:
        await agent.aclose()
    return 0


def main() -> int:
    try:
        return asyncio.run(repl())
    except KeyboardInterrupt:
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
