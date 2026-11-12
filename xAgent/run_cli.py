"""CLI 启动入口：验证 core 可以完全脱离 Textual 独立运行。

这个文件只负责终端输入输出，不属于 core 包。
"""

from __future__ import annotations

import asyncio

from core.agent import Agent
from core.config import ConfigError, Settings, ensure_config
from core.events import (
    ContextUsage,
    ErrorEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from core.mcp_client import configure_mcp_logging

HELP = """可用命令：
  /help     显示帮助
  /compact  压缩当前上下文（先归档，再生成摘要）
  /clear    重置 Agent 上下文
  /skills   列出已发现的 Agent Skills
  /mcp      查看 MCP 服务连接状态
  /status   查看运行状态与上下文 Token
  /quit     退出"""


async def repl(config_path: str | None = None) -> int:
    try:
        settings = Settings.load(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    configure_mcp_logging()
    agent = Agent(settings=settings)
    # 启动时异步连接 MCP；单个服务失败不会阻断 REPL
    await agent.astart()
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
            if line == "/compact":
                print("正在压缩上下文，请稍候…")
                result = await agent.compact()
                if result.ok:
                    print(f"压缩成功。归档编号：{result.archive_id}")
                    print(f"归档文件：{result.archive_path}")
                    print(f"当前上下文消息数：{len(agent.history)}")
                else:
                    print(
                        f"[错误] 压缩失败：{result.error}"
                        "（原始上下文已完整保留）"
                    )
                print()
                continue
            if line == "/clear":
                agent.reset()
                print("上下文已清空。\n")
                continue
            if line == "/skills":
                skills = agent.skills.available_skills()
                print(f"Skills 根目录：{agent.skills.skills_root}")
                if not skills:
                    print("未发现任何 Skill。\n")
                else:
                    for skill in skills:
                        print(f"- {skill.name}: {skill.description}")
                    print()
                for warning in agent.skills.warnings:
                    print(f"[警告] {warning}")
                if agent.skills.warnings:
                    print()
                continue
            if line == "/mcp":
                if agent.mcp is None:
                    print("MCP 已禁用。\n")
                else:
                    for status_line in agent.mcp.status_lines():
                        print(status_line)
                    for warning in agent.mcp.warnings:
                        print(f"[警告] {warning}")
                    print()
                continue
            if line == "/status":
                stats = agent.get_context_stats()
                default_tag = "【默认】" if stats.window_is_default else ""
                print(f"模型：{settings.model}")
                print(f"沙箱：{agent.sandbox.root}")
                print(f"上下文消息数：{len(agent.history)}")
                print(
                    f"上下文：{stats.used} / {stats.window}{default_tag} "
                    f"| 占用 {stats.percent:.1f}%"
                )
                print(f"可用工具数：{len(agent.tools)}")
                print(f"可用技能数：{len(agent.skills.skills)}")
                if agent.mcp is not None:
                    connected = sum(
                        1 for server in agent.mcp.servers.values() if server.connected
                    )
                    print(
                        f"MCP 服务：{connected}/{len(agent.mcp.servers)} 已连接，"
                        f"工具 {len(agent.mcp.tool_names)} 个"
                    )
                print()
                continue

            line_open = False
            context_event: ContextUsage | None = None
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
                elif isinstance(event, ContextUsage):
                    context_event = event
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

            if context_event is not None:
                default_tag = (
                    "【默认】" if context_event.window_is_default else ""
                )
                print(
                    f"上下文：{context_event.used} / {context_event.window}"
                    f"{default_tag} | 占用 {context_event.percent:.1f}%"
                )
                if context_event.locked:
                    print("⚠ 上下文已锁定，请输入 /compact 压缩。")
                print()
    finally:
        await agent.aclose()
    return 0


def main() -> int:
    # Task 3：首次启动自动生成 config.ini；CLI 没有模态框，用文字提示后退出
    try:
        config_path, created = ensure_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    if created:
        print("已生成默认配置文件：", config_path)
        print("请先修改 [llm] api_key（以及需要时的 base_url / model_name），")
        print("然后重新启动 xAgent。在完成配置之前不会发起任何模型请求。")
        return 0

    try:
        return asyncio.run(repl(str(config_path)))
    except KeyboardInterrupt:
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
