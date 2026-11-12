"""会话持久化。

每个会话保存为 `.agents/sessions/<id>.json`，包含完整消息历史、
创建时间、最后活跃时间和会话标题。只使用 Python 标准库：
    - JSON 读写
    - 原子替换（先写临时文件，再 os.replace）
    - 时间字符串统一为 ISO 8601（秒级）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_SUFFIX = ".json"
SESSION_VERSION = 1

# 与 core.agent.AGENTS_MD_PREFIX 保持一致；单独定义可避免循环 import
AGENTS_MD_MARKER = "【项目规范 · AGENTS.md 自动注入】"


class SessionError(RuntimeError):
    """会话文件读写失败。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_text(text: str) -> str:
    return " ".join(str(text).split())


def derive_title(messages: list[dict[str, Any]], max_chars: int = 30) -> str:
    """从第一条真实用户消息推导会话标题。

    AGENTS.md 注入消息、system 消息、工具消息都不参与标题生成。
    """
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or text.startswith(AGENTS_MD_MARKER):
            continue
        text = _clean_text(text)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text or "新会话"
    return "新会话"


def has_conversation(messages: list[dict[str, Any]]) -> bool:
    """是否包含至少一条真实用户消息或助手回复。"""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "user" and isinstance(content, str):
            if content.strip() and not content.startswith(AGENTS_MD_MARKER):
                return True
        if role == "assistant" and (content or message.get("tool_calls")):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """历史会话列表项（不加载完整消息历史）。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    path: Path


class SessionStore:
    """管理会话目录中的 JSON 文件。"""

    def __init__(self, session_dir: str | Path) -> None:
        self.session_dir = Path(session_dir).expanduser().resolve()

    # ---------- 目录与 id ----------

    def ensure_dir(self) -> Path:
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionError(
                f"无法创建会话目录 {self.session_dir}：{exc}"
            ) from exc
        return self.session_dir

    def new_session_id(self) -> str:
        """生成不与现有文件冲突的会话 id。"""
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = base
        index = 1
        while (self.session_dir / f"{candidate}{SESSION_SUFFIX}").exists():
            candidate = f"{base}-{index:02d}"
            index += 1
        return candidate

    # ---------- 保存 / 读取 ----------

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
        created_at: str | None = None,
        model: str = "",
        workspace: str = "",
    ) -> Path:
        """原子写入一个会话文件。"""
        directory = self.ensure_dir()
        now = _now()
        path = directory / f"{session_id}{SESSION_SUFFIX}"

        payload = {
            "version": SESSION_VERSION,
            "id": session_id,
            "title": title or derive_title(messages),
            "created_at": created_at or now,
            "updated_at": now,
            "model": model,
            "workspace": workspace,
            "messages": messages,
        }

        temp = directory / f".{session_id}{SESSION_SUFFIX}.tmp"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError(f"保存会话失败：{exc}") from exc
        return path

    def load(self, session_id: str) -> dict[str, Any]:
        """读取一个会话文件，并做最基本的格式校验。"""
        if not session_id or any(ch in session_id for ch in "\\/.."):
            raise SessionError(f"非法会话 id：{session_id!r}")

        path = self.session_dir / f"{session_id}{SESSION_SUFFIX}"
        if not path.is_file():
            raise SessionError(f"会话不存在：{session_id}")

        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"读取会话失败：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(
            data.get("messages"), list
        ):
            raise SessionError("会话文件格式不正确：缺少 messages 列表。")

        cleaned = [item for item in data["messages"] if isinstance(item, dict)]
        if not cleaned:
            raise SessionError("会话文件中没有可恢复的消息。")
        data["messages"] = cleaned
        data.setdefault("id", session_id)
        data.setdefault("title", derive_title(cleaned))
        data.setdefault("created_at", "")
        data.setdefault("updated_at", "")
        return data

    # ---------- 列表 ----------

    def list_sessions(self) -> list[SessionMeta]:
        """按最后活跃时间倒序列出全部会话；坏文件跳过。"""
        if not self.session_dir.is_dir():
            return []

        items: list[SessionMeta] = []
        try:
            paths = list(self.session_dir.glob(f"*{SESSION_SUFFIX}"))
        except OSError:
            return []

        for path in paths:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            messages = data.get("messages")
            if not isinstance(messages, list):
                continue
            items.append(
                SessionMeta(
                    id=str(data.get("id") or path.stem),
                    title=str(data.get("title") or "未命名会话"),
                    created_at=str(data.get("created_at") or ""),
                    updated_at=str(data.get("updated_at") or ""),
                    message_count=len(messages),
                    path=path,
                )
            )

        items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return items
