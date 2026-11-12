"""路径安全沙箱。

Agent 只能访问 workspace 根目录以内的路径。
使用 resolve() 展开符号链接后再做 is_relative_to 判断，避免前缀匹配漏洞
（例如 `xAgentEvil/` 曾经能骗过 `startswith("xAgent")`）。
"""

from __future__ import annotations

import os
from pathlib import Path


class SecurityError(ValueError):
    """越界访问或非法路径。"""


class PathSandbox:
    """把一个目录包装成不可越界的沙箱。"""

    def __init__(self, root: str | Path) -> None:
        self.root: Path = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, raw: str | Path | None, *, allow_root: bool = True) -> Path:
        """把用户/模型给的路径解析为沙箱内的绝对路径。

        - 空字符串按当前目录处理（即沙箱根目录）
        - 相对路径基于沙箱根目录
        - 越界、非法字符、解析失败均抛 SecurityError
        """
        text = "" if raw is None else str(raw).strip()
        if "\x00" in text:
            raise SecurityError("路径包含非法字符。")
        if not text:
            text = "."

        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate

        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:  # 例如 Windows 上的非法设备名
            raise SecurityError(f"路径无法解析：{raw}") from exc

        if not resolved.is_relative_to(self.root):
            raise SecurityError("禁止访问工作目录以外的路径！")
        if not allow_root and resolved == self.root:
            raise SecurityError("禁止操作工作目录本身！")
        return resolved

    def relative(self, path: Path) -> str:
        """返回相对沙箱根目录的 POSIX 风格路径，用于回显。"""
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
        return rel or "."

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PathSandbox(root={os.fspath(self.root)!r})"
