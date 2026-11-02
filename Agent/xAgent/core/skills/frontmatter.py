"""SKILL.md 的 YAML Frontmatter 解析。

初始化阶段只需要 name / description，因此这里**不读取正文**：

    read_frontmatter_block(path)  逐行读取，遇到第二个 `---` 立即停止
    parse_frontmatter(text)       优先 PyYAML；未安装时回退到内置最小解析器

内置解析器只覆盖 frontmatter 的常见写法（`key: value`、单/双引号字符串、
`|` / `>` 块标量、注释），足以读取 name / description。锚点、嵌套列表等
复杂 YAML 请安装 requirements.txt 中的 PyYAML（推荐且默认优先使用）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

try:  # 可选依赖：缺失时自动回退到最小解析器
    import yaml
except ImportError:  # pragma: no cover - 取决于运行环境
    yaml = None  # type: ignore[assignment]


# frontmatter 只应出现在文件头部，限制读取量可避免被超大文件拖慢初始化
MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_FRONTMATTER_LINES = 400

# `|`、`>` 及其 chomp / 缩进指示符，例如 `>-`、`|2`
_BLOCK_HEADER_RE = re.compile(r"^([|>])([+-]?)([1-9]?)$")
# YAML 行尾注释：只有当 # 前面有空白时才是注释
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


class FrontmatterError(ValueError):
    """Frontmatter 缺失、格式错误或无法解析。"""


def read_frontmatter_block(path: str | Path) -> str:
    """从 SKILL.md 头部读取 YAML Frontmatter 文本（不含分隔符）。

    只按行读取到结束分隔符为止，不会把 SKILL.md 全部正文读进内存，
    满足“初始化阶段只读取 name / description”的要求。

    规则：
    - 第一行必须是 `---`（允许 BOM 与行尾空白）；
    - 结束分隔符为独占一行的 `---` 或 `...`；
    - 缺少结束分隔符、文件不是 UTF-8、frontmatter 过大都会抛 FrontmatterError。
    """
    target = Path(path)
    try:
        # utf-8-sig 自动吃掉 BOM；newline=None 把 CRLF / CR 统一成 \n
        with target.open("r", encoding="utf-8-sig", newline=None) as handle:
            first = handle.readline()
            if not first:
                raise FrontmatterError("文件为空，缺少 YAML Frontmatter。")
            if first.strip() != "---":
                raise FrontmatterError(
                    "文件开头不是 YAML Frontmatter（第一行必须为 ---）。"
                )

            lines: list[str] = []
            total_bytes = 0
            for _ in range(MAX_FRONTMATTER_LINES):
                line = handle.readline()
                if not line:  # 读到 EOF 仍未遇到结束分隔符
                    raise FrontmatterError(
                        "YAML Frontmatter 缺少结束分隔符 ---。"
                    )
                total_bytes += len(line.encode("utf-8", errors="replace"))
                if total_bytes > MAX_FRONTMATTER_BYTES:
                    raise FrontmatterError(
                        f"YAML Frontmatter 超过 {MAX_FRONTMATTER_BYTES} 字节上限。"
                    )
                # 结束分隔符必须独占一行（允许前后空白）
                if line.strip() in {"---", "..."}:
                    return "".join(lines)
                lines.append(line)

            raise FrontmatterError(
                f"YAML Frontmatter 超过 {MAX_FRONTMATTER_LINES} 行上限。"
            )
    except UnicodeDecodeError as exc:
        raise FrontmatterError("SKILL.md 不是合法的 UTF-8 文本。") from exc
    except OSError as exc:
        raise FrontmatterError(f"读取 SKILL.md 失败：{exc}") from exc


def parse_frontmatter(
    text: str, *, use_pyyaml: bool = True
) -> dict[str, Any]:
    """把 Frontmatter 文本解析成字典。

    优先使用 PyYAML（safe_load）；未安装或显式关闭时使用内置最小解析器。
    解析结果必须是键值映射，否则抛 FrontmatterError。
    """
    if use_pyyaml and yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise FrontmatterError(f"YAML 解析失败：{exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise FrontmatterError("YAML Frontmatter 必须是键值映射。")
        return data

    return _parse_minimal(text)


# ---------------------------------------------------------------------------
# 内置最小解析器（PyYAML 缺失时的兜底）
# ---------------------------------------------------------------------------


def _parse_minimal(text: str) -> dict[str, Any]:
    """解析顶层 `key: value`，支持块标量与引号字符串。

    设计目标是读取 name / description，而不是完整实现 YAML：
    - 跳过空行、注释与缩进的嵌套内容；
    - 支持 `description: >-` / `description: |` 多行写法；
    - 支持 `name: "..."` / `name: '...'`；
    - 其余值按去除行尾注释后的纯文本处理。
    """
    data: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        index += 1

        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":  # 缩进行属于上一层的嵌套结构，这里不处理
            continue

        key, separator, rest = line.partition(":")
        if not separator:
            continue
        key = key.strip().strip("'\"")
        if not key:
            continue
        rest = rest.strip()

        header = _BLOCK_HEADER_RE.match(rest)
        if header is not None:
            block_lines, index = _read_block(lines, index)
            data[key] = _fold_block(
                block_lines, header.group(1), header.group(2)
            )
            continue

        if rest.startswith(('"', "'")):
            data[key] = _parse_quoted(rest)
        else:
            data[key] = _INLINE_COMMENT_RE.sub("", rest).strip()

    return data


def _read_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """读取块标量的内容行：空行或缩进行；遇到下一个顶层键停止。"""
    block: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        block.append(line)
        index += 1
    return block, index


def _fold_block(block: list[str], style: str, chomp: str) -> str:
    """还原 `|`（保留换行）与 `>`（折叠为空格）块标量。"""
    non_empty = [line for line in block if line.strip()]
    indent = (
        min(len(line) - len(line.lstrip(" \t")) for line in non_empty)
        if non_empty
        else 0
    )
    stripped = [line[indent:] if line.strip() else "" for line in block]

    if style == "|":
        value = "\n".join(stripped)
    else:
        parts: list[str] = []
        buffer: list[str] = []
        for line in stripped:
            if line.strip():
                buffer.append(line.strip())
            else:
                if buffer:
                    parts.append(" ".join(buffer))
                    buffer = []
                parts.append("")  # 空行代表段落分隔
        if buffer:
            parts.append(" ".join(buffer))
        value = "\n".join(parts)

    if chomp != "+":
        value = value.rstrip("\n")
    return value


def _parse_quoted(raw: str) -> str:
    """解析单行引号字符串；双引号借助 Python 字面量处理常见转义。"""
    if raw.startswith('"'):
        try:
            value = ast.literal_eval(raw)
            if isinstance(value, str):
                return value
        except (ValueError, SyntaxError):
            pass
        return raw.strip('"')

    # YAML 单引号：内部用 '' 表示一个单引号
    if len(raw) >= 2 and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw.strip("'")
