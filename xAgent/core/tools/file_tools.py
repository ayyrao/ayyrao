"""10 个文件管理工具的具体实现。

本模块只做文件系统操作与参数校验，不包含任何界面代码。
所有方法失败时抛 ToolError，由 ToolRegistry 统一转成字符串结果。
"""

from __future__ import annotations

from pathlib import Path

from ..security import PathSandbox, SecurityError
from .registry import ToolError

# 直接拒读的常见二进制后缀
BINARY_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico",
    ".exe", ".dll", ".so", ".bin", ".zip", ".rar", ".7z", ".gz",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".pyc",
}

# 单次读取上限，避免把超大文件灌进模型上下文
MAX_READ_BYTES = 1024 * 1024  # 1 MiB


class FileTools:
    """绑定到某个沙箱根目录的一组文件工具。"""

    def __init__(self, sandbox: PathSandbox) -> None:
        self._sandbox = sandbox

    # ---------- 内部辅助 ----------

    def _resolve(self, raw: str | None, *, allow_root: bool = True) -> Path:
        try:
            return self._sandbox.resolve(raw, allow_root=allow_root)
        except SecurityError as exc:
            raise ToolError(str(exc)) from exc

    def _display(self, path: Path) -> str:
        return self._sandbox.relative(path)

    # ---------- 1. 获取当前工作目录 ----------

    def get_cwd(self) -> str:
        """返回 Agent 沙箱根目录（即工作目录）。"""
        return str(self._sandbox.root)

    # ---------- 2. 列出目录 ----------

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"目录不存在：{self._display(target)}")
        if not target.is_dir():
            raise ToolError(f"目标不是目录：{self._display(target)}")

        try:
            entries = sorted(
                target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as exc:
            raise ToolError(f"读取目录失败：{exc}") from exc

        if not entries:
            return f"{self._display(target)} 为空目录。"
        lines = [
            f"[DIR]  {entry.name}" if entry.is_dir() else f"[FILE] {entry.name}"
            for entry in entries
        ]
        header = f"{self._display(target)} 共 {len(entries)} 项："
        return header + "\n" + "\n".join(lines)

    # ---------- 3. 获取文件类型 ----------

    def get_file_type(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"文件不存在：{self._display(target)}")
        suffix = target.suffix.lower()
        return f"文件后缀：{suffix}" if suffix else "文件后缀：无（无扩展名）"

    # ---------- 4. 读取纯文本文件 ----------

    def read_text_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{self._display(target)}")
        if target.suffix.lower() in BINARY_SUFFIXES:
            raise ToolError("非文本文件，禁止读取。")

        try:
            size = target.stat().st_size
            if size > MAX_READ_BYTES:
                raise ToolError(
                    f"文件过大（{size} 字节），超过 {MAX_READ_BYTES} 字节上限，禁止读取。"
                )
            data = target.read_bytes()
        except OSError as exc:
            raise ToolError(f"读取文件失败：{exc}") from exc

        if b"\x00" in data:
            raise ToolError("非文本文件，禁止读取。")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("文件不是 UTF-8 文本，禁止读取。") from exc

    # ---------- 5. 新建文件 ----------

    def create_file(self, path: str) -> str:
        target = self._resolve(path)
        if target.exists():
            raise ToolError(
                f"文件已存在：{self._display(target)}；如需修改内容请使用 edit_text_file。"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=False)
        except OSError as exc:
            raise ToolError(f"创建文件失败：{exc}") from exc
        return f"文件创建成功：{self._display(target)}"

    # ---------- 6. 编辑文本内容 ----------

    def edit_text_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{self._display(target)}")
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入文件失败：{exc}") from exc
        return f"内容修改成功：{self._display(target)}"

    # ---------- 7. 创建目录 ----------

    def make_dir(self, path: str) -> str:
        target = self._resolve(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"创建目录失败：{exc}") from exc
        return f"目录创建成功：{self._display(target)}"

    # ---------- 8. 删除目录（仅空目录） ----------

    def remove_dir(self, path: str) -> str:
        target = self._resolve(path, allow_root=False)
        if not target.exists():
            raise ToolError(f"目录不存在：{self._display(target)}")
        if not target.is_dir():
            raise ToolError(f"目标不是目录：{self._display(target)}")
        try:
            if any(target.iterdir()):
                raise ToolError("目录不为空，禁止删除。")
            target.rmdir()
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"删除目录失败：{exc}") from exc
        return f"空目录删除成功：{self._display(target)}"

    # ---------- 9. 删除文件 ----------

    def remove_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{self._display(target)}")
        try:
            target.unlink()
        except OSError as exc:
            raise ToolError(f"删除文件失败：{exc}") from exc
        return f"文件删除成功：{self._display(target)}"

    # ---------- 10. 重命名文件/目录 ----------

    def rename_item(self, old_path: str, new_path: str) -> str:
        source = self._resolve(old_path, allow_root=False)
        target = self._resolve(new_path)
        if not source.exists():
            raise ToolError(f"源路径不存在：{self._display(source)}")
        if target.exists():
            raise ToolError(f"目标路径已存在，禁止覆盖：{self._display(target)}")
        try:
            source.rename(target)
        except OSError as exc:
            raise ToolError(f"重命名失败：{exc}") from exc
        return f"重命名完成：{self._display(source)} -> {self._display(target)}"
