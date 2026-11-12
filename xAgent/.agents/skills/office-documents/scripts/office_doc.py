#!/usr/bin/env python3
"""office-documents 技能封装脚本：调用 OfficeCli 生成 Word(.docx) / PPT(.pptx)。

OfficeCli 本体是命令行程序，本脚本负责：
1. 把「JSON 规格文件」或「简短 CLI 参数」翻译成 officecli 的 add/table/... 命令；
2. 用 batch + UTF-8 JSON 文件一次提交，避免中文经 shell 传递时的编码问题；
3. close 落盘后校验文件，并打印生成文件的绝对路径。

用法示例（在 xAgent 项目根目录执行）：

    # 推荐：用 JSON 规格文件（支持标题、段落、列表、表格、分页、PPT 备注等）
    python .agents/skills/office-documents/scripts/office_doc.py --spec spec.json

    # 简单 Word：标题 + 若干段落
    python .agents/skills/office-documents/scripts/office_doc.py ^
        --output "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\报告.docx" ^
        --title "项目周报" --paragraph "本周完成……" --paragraph "下周计划……"

    # 简单 PPT：封面 + 内容页（"标题::要点1;要点2"）
    python .agents/skills/office-documents/scripts/office_doc.py ^
        --output "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\演示.pptx" ^
        --title-slide "季度总结::2026 Q3" ^
        --slide "本季度进展::完成集成;完成测试" ^
        --slide "下一步计划::推广试点"

    # 只查看将要执行的 OfficeCli 命令，不写文件
    python .agents/skills/office-documents/scripts/office_doc.py --spec spec.json --dry-run

输出：UTF-8 JSON（stdout），成功时 ok=true 且 output 为绝对路径；失败时
ok=false、exit code 1，错误原因在 error.type / error.message。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

# OfficeCli 可执行文件（可用 --officecli 或环境变量 OFFICECLI_EXE 覆盖）
DEFAULT_OFFICECLI = Path(r"E:\sp\OfficeCli_#SULP#_26sc1d\officecli-win-x64.exe")

SUPPORTED_FORMATS = ("docx", "pptx")
FORMAT_SUFFIX = {".docx": "docx", ".pptx": "pptx"}
DEFAULT_LOCALE = "zh-CN"
DEFAULT_PPT_FONT = "Microsoft YaHei"

# 正式实训报告封面的默认信息栏（可用 style.coverInfo 覆盖）
DEFAULT_COVER_INFO = [
    "课程名称：____  班级：____  姓名：____  学号：____  指导教师：____  日期：____"
]

# 16:9 幻灯片尺寸 960pt x 540pt = 33.87cm x 19.05cm
SLIDE_WIDTH_CM = 33.87
SLIDE_HEIGHT_CM = 19.05
BODY_X_CM = 1.8
BODY_W_CM = 30.27

MAX_ERR_CHARS = 1500


class SkillError(Exception):
    """带结构化类型的脚本错误。"""

    def __init__(self, error_type: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _configure_stdio() -> None:
    """统一输出编码。

    - 输出被管道捕获时（xAgent 的 RunCli 就是这种场景）强制 UTF-8；
    - 直接连控制台时保留控制台编码（Windows 控制台走 UTF-16 API，不会乱码）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _project_root() -> Path:
    """从脚本位置向上找包含 .agents 目录的项目根；找不到则退回当前目录。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".agents").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "workspace" / "office-documents"


def _resolve_officecli(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_value = os.environ.get("OFFICECLI_EXE")
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.append(DEFAULT_OFFICECLI)

    for name in ("officecli-win-x64.exe", "officecli.exe", "officecli"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved

    raise SkillError(
        "officecli_not_found",
        "找不到 OfficeCli 可执行文件。",
        tried=[str(item) for item in candidates],
        hint="用 --officecli 指定路径，或设置环境变量 OFFICECLI_EXE。",
    )


def _decode(raw: bytes | str | None) -> str:
    """OfficeCli 的 JSON 输出是 UTF-8；旧版控制台回退 GB18030。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _short(text: str, limit: int = MAX_ERR_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _run_officecli(
    exe: Path, args: list[str], timeout: float
) -> tuple[int, str, str, dict[str, Any] | None]:
    """执行 officecli，返回 (exit_code, stdout, stderr, parsed_json)。"""
    try:
        proc = subprocess.run(
            [str(exe), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillError(
            "officecli_timeout",
            f"OfficeCli 命令超时（>{timeout:g}s）：{' '.join(args[:3])}",
            stdout=_short(_decode(exc.stdout)),
            stderr=_short(_decode(exc.stderr)),
        ) from exc
    except OSError as exc:
        raise SkillError(
            "officecli_exec_error",
            f"无法执行 OfficeCli：{exc}",
            officecli=str(exe),
        ) from exc

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    parsed: dict[str, Any] | None = None
    if "--json" in args and stdout.strip():
        try:
            value = json.loads(stdout)
            if isinstance(value, dict):
                parsed = value
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, stdout, stderr, parsed


def _officecli_message(
    parsed: dict[str, Any] | None, stdout: str, stderr: str, code: int
) -> str:
    if parsed:
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("error")
            if message:
                return str(message)
            return json.dumps(error, ensure_ascii=False)
        if error:
            return str(error)
        message = parsed.get("message")
        if message and parsed.get("success") is False:
            return str(message)
    return _short(stderr or stdout) or f"OfficeCli 退出码 {code}"


def _call(
    exe: Path,
    args: list[str],
    timeout: float,
    *,
    expect_success: bool = True,
) -> dict[str, Any] | None:
    """执行命令；expect_success=True 时失败抛 SkillError。"""
    code, stdout, stderr, parsed = _run_officecli(exe, args, timeout)
    success = code in (0, 2) and not (
        isinstance(parsed, dict) and parsed.get("success") is False
    )
    if expect_success and not success:
        raise SkillError(
            "officecli_failed",
            _officecli_message(parsed, stdout, stderr, code),
            command=" ".join([str(exe.name), *args]),
            exit_code=code,
            stdout=_short(stdout),
            stderr=_short(stderr),
        )
    return parsed


# ---------------------------------------------------------------------------
# 输出路径
# ---------------------------------------------------------------------------


def _resolve_output(
    raw_output: str | None,
    fmt: str,
    spec_output: str | None,
) -> Path:
    if raw_output is None:
        raw_output = spec_output

    if raw_output:
        output = Path(raw_output).expanduser()
        if not output.is_absolute():
            output = Path.cwd() / output
        if output.suffix == "":
            output = output.with_suffix(f".{fmt}")
        if output.suffix.lower() not in FORMAT_SUFFIX:
            raise SkillError(
                "invalid_output",
                f"输出文件扩展名必须是 .docx 或 .pptx，收到：{output.suffix}",
                output=str(output),
            )
        suffix_format = FORMAT_SUFFIX[output.suffix.lower()]
        if suffix_format != fmt:
            raise SkillError(
                "format_mismatch",
                f"输出扩展名 {output.suffix} 与格式 {fmt} 不一致。",
                output=str(output),
                format=fmt,
            )
        return output.resolve()

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (DEFAULT_OUTPUT_DIR / f"office_{stamp}.{fmt}").resolve()


def _infer_format(args: argparse.Namespace, spec: dict[str, Any]) -> str:
    if args.format:
        fmt = args.format.lower()
        if fmt not in SUPPORTED_FORMATS:
            raise SkillError(
                "invalid_format", f"--format 只能是 docx 或 pptx，收到：{args.format}"
            )
        return fmt

    spec_format = spec.get("format")
    if isinstance(spec_format, str) and spec_format.lower() in SUPPORTED_FORMATS:
        return spec_format.lower()

    for raw in (args.output, spec.get("output")):
        if isinstance(raw, str) and Path(raw).suffix.lower() in FORMAT_SUFFIX:
            return FORMAT_SUFFIX[Path(raw).suffix.lower()]

    if "slides" in spec:
        return "pptx"
    return "docx"


# ---------------------------------------------------------------------------
# 规格文件
# ---------------------------------------------------------------------------


def _load_spec(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    spec_path = Path(path).expanduser()
    if not spec_path.is_absolute():
        spec_path = Path.cwd() / spec_path
    if not spec_path.is_file():
        raise SkillError(
            "spec_not_found", f"规格文件不存在：{spec_path}", spec=str(spec_path)
        )
    try:
        text = spec_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillError(
            "spec_encoding_error",
            "规格文件必须是 UTF-8 编码（可带 BOM）。",
            spec=str(spec_path),
        ) from exc
    except OSError as exc:
        raise SkillError(
            "spec_read_error", f"读取规格文件失败：{exc}", spec=str(spec_path)
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillError(
            "spec_invalid_json",
            f"规格文件 JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}",
            spec=str(spec_path),
        ) from exc
    if not isinstance(data, dict):
        raise SkillError(
            "spec_invalid_json", "规格文件的顶层必须是 JSON 对象 {...}。"
        )
    return data


# ---------------------------------------------------------------------------
# 构建 Docx 命令
# ---------------------------------------------------------------------------


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SkillError("invalid_spec", f"{field} 必须是字符串。")
    return value


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", ";", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _table_data(
    headers: list[Any] | None, rows: list[list[Any]], field: str
) -> str:
    if headers is not None and not isinstance(headers, list):
        raise SkillError("invalid_spec", f"{field}.headers 必须是字符串数组。")
    if not isinstance(rows, list) or not rows:
        raise SkillError("invalid_spec", f"{field}.rows 必须是非空二维数组。")

    width = len(headers) if headers else len(rows[0])
    if width == 0:
        raise SkillError("invalid_spec", f"{field} 至少需要一列。")
    all_rows: list[list[Any]] = []
    if headers:
        all_rows.append(headers)
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != width:
            raise SkillError(
                "invalid_spec",
                f"{field}.rows[{index}] 必须是长度为 {width} 的数组。",
            )
        all_rows.append(row)
    return ";".join(",".join(_csv_cell(cell) for cell in row) for row in all_rows)


def _font_size(value: Any, field: str, default: str) -> str:
    """把 12 / "12" / "12pt" 统一成 OfficeCli 接受的 "12pt"。"""
    if value is None:
        value = default
    if isinstance(value, bool):
        raise SkillError("invalid_spec", f"{field} 必须是字号数字。")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().lower().removesuffix("pt").strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise SkillError(
                "invalid_spec", f"{field} 必须是字号数字，收到：{value!r}"
            ) from exc
    else:
        raise SkillError("invalid_spec", f"{field} 必须是字号数字。")
    if number <= 0:
        raise SkillError("invalid_spec", f"{field} 必须大于 0。")
    return f"{number:g}pt"


def _docx_style_config(spec: dict[str, Any]) -> dict[str, Any]:
    """docx 排版默认值：宋体正文、黑体标题、封面独立成页、三线表。"""
    raw = spec.get("style") or {}
    if not isinstance(raw, dict):
        raise SkillError("invalid_spec", "style 必须是对象。")

    def flag(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if not isinstance(value, bool):
            raise SkillError("invalid_spec", f"style.{key} 必须是布尔值。")
        return value

    # 封面信息栏：未配置时用默认占位文字；显式传 [] 可关闭
    cover_info = raw.get("coverInfo")
    if cover_info is None:
        cover_info = list(DEFAULT_COVER_INFO)
    elif isinstance(cover_info, str):
        cover_info = [cover_info]
    elif isinstance(cover_info, list) and all(
        isinstance(item, str) for item in cover_info
    ):
        cover_info = list(cover_info)
    else:
        raise SkillError("invalid_spec", "style.coverInfo 必须是字符串数组。")

    return {
        "bodyFont": str(raw.get("bodyFont") or "宋体"),
        "headingFont": str(raw.get("headingFont") or "黑体"),
        "bodySize": _font_size(raw.get("bodySize"), "style.bodySize", "12"),
        "heading1Size": _font_size(
            raw.get("heading1Size"), "style.heading1Size", "16"
        ),
        "heading2Size": _font_size(
            raw.get("heading2Size"), "style.heading2Size", "14"
        ),
        "heading3Size": _font_size(
            raw.get("heading3Size"), "style.heading3Size", "12"
        ),
        "subtitleSize": _font_size(
            raw.get("subtitleSize"), "style.subtitleSize", "16"
        ),
        "tableFontSize": _font_size(
            raw.get("tableFontSize"), "style.tableFontSize", "9"
        ),
        "tableWidth": str(raw.get("tableWidth") or "100%"),
        "tableThreeLine": flag("tableThreeLine", True),
        "coverPageBreak": flag("coverPageBreak", True),
        "coverInfo": cover_info,
        "coverInfoSize": _font_size(
            raw.get("coverInfoSize"), "style.coverInfoSize", "14"
        ),
        "coverInfoSpaceBefore": str(
            raw.get("coverInfoSpaceBefore") or "180pt"
        ),
        "bodyFirstLineIndent": str(
            raw.get("bodyFirstLineIndent") or "24pt"
        ),
        "bodyLineSpacing": str(raw.get("bodyLineSpacing") or "1.5x"),
        "bodySpaceAfter": str(raw.get("bodySpaceAfter") or "0pt"),
        "footerSize": _font_size(raw.get("footerSize"), "style.footerSize", "10"),
        "pageNumberFooter": flag("pageNumberFooter", True),
    }


def _paragraph_props(
    block: dict[str, Any],
    default_style: str,
    font: str,
    size: str,
) -> dict[str, str]:
    props: dict[str, str] = {
        "text": _require_str(block.get("text"), "block.text"),
        "style": str(block.get("style") or default_style),
        "font": str(block["font"]) if block.get("font") else font,
        "size": _font_size(block.get("size"), "block.size", size),
    }
    for key in ("bold", "italic"):
        if key in block:
            props[key] = "true" if block[key] else "false"
    for key in ("color", "align"):
        if block.get(key) is not None:
            props[key] = str(block[key])
    if block.get("level") is not None:
        props["numLevel"] = str(block["level"])
    return props


def build_docx_commands(spec: dict[str, Any]) -> list[dict[str, Any]]:
    style = _docx_style_config(spec)
    commands: list[dict[str, Any]] = []

    paragraph_count = 0

    def add_paragraph(props: dict[str, str], *, body: bool = False) -> None:
        """追加段落；body=True 时再补一条 set，把首行缩进写成 2 字符。

        firstLineChars 只支持 set（不支持 add），所以先 add 再按段号 set，
        位置用 /body/p[N]（N 只统计 /body 的直接子段落，不含表格内段落）。
        """
        nonlocal paragraph_count
        commands.append(
            {"command": "add", "parent": "/body", "type": "paragraph", "props": props}
        )
        paragraph_count += 1
        if body:
            commands.append(
                {
                    "command": "set",
                    "path": f"/body/p[{paragraph_count}]",
                    "props": {"firstLineChars": "200"},
                }
            )

    # 全局默认字体/字号（没有显式 run 属性的内容也会继承）
    commands.append(
        {
            "command": "set",
            "path": "/",
            "props": {
                "docDefaults.font": style["bodyFont"],
                "docDefaults.font.eastAsia": style["bodyFont"],
                "docDefaults.font.hAnsi": style["bodyFont"],
                "docDefaults.fontSize": style["bodySize"],
            },
        }
    )

    blocks = spec.get("blocks", [])
    if blocks is not None and not isinstance(blocks, list):
        raise SkillError("invalid_spec", "blocks 必须是数组。")

    title = spec.get("title")
    subtitle = spec.get("subtitle")
    if title:
        add_paragraph(
            {
                "text": _require_str(title, "title"),
                "style": "Title",
                "align": "center",
                "font": style["headingFont"],
            }
        )
    if subtitle:
        add_paragraph(
            {
                "text": _require_str(subtitle, "subtitle"),
                "style": "Subtitle",
                "align": "center",
                "font": style["headingFont"],
                "size": style["subtitleSize"],
            }
        )

    # 封面信息栏：与标题区拉开距离，落在封面下方留白区
    if (title or subtitle) and style["coverInfo"]:
        for info_index, info_text in enumerate(style["coverInfo"]):
            info_props = {
                "text": info_text,
                "style": "Normal",
                "align": "center",
                "font": style["bodyFont"],
                "size": style["coverInfoSize"],
            }
            if info_index == 0:
                info_props["spaceBefore"] = style["coverInfoSpaceBefore"]
            add_paragraph(info_props)

    # 封面与正文之间插入真正的分页符（只写标题、没有正文时不插，避免空白第二页）
    if style["coverPageBreak"] and (title or subtitle) and blocks:
        commands.append(
            {"command": "add", "parent": "/body", "type": "pagebreak", "props": {}}
        )
        paragraph_count += 1

    table_count = 0

    for index, raw in enumerate(blocks):
        if not isinstance(raw, dict):
            raise SkillError("invalid_spec", f"blocks[{index}] 必须是对象。")
        kind = str(raw.get("type") or "paragraph").lower()

        if kind in ("heading", "title"):
            level = int(raw.get("level") or 1)
            if not 1 <= level <= 9:
                raise SkillError(
                    "invalid_spec", f"blocks[{index}].level 必须在 1..9 之间。"
                )
            if level == 1:
                size = style["heading1Size"]
            elif level == 2:
                size = style["heading2Size"]
            else:
                size = style["heading3Size"]
            add_paragraph(
                _paragraph_props(
                    raw, f"Heading{level}", style["headingFont"], size
                )
            )
        elif kind in ("paragraph", "text"):
            props = _paragraph_props(
                raw, "Normal", style["bodyFont"], style["bodySize"]
            )
            # 正式报告正文：首行缩进 2 字符、1.5 倍行距、段后 0
            props["firstLineIndent"] = style["bodyFirstLineIndent"]
            props["lineSpacing"] = style["bodyLineSpacing"]
            props["lineRule"] = "auto"
            props["spaceAfter"] = style["bodySpaceAfter"]
            add_paragraph(props, body=True)
        elif kind in ("bullet", "bullets", "unordered"):
            props = _paragraph_props(
                raw, "Normal", style["bodyFont"], style["bodySize"]
            )
            props["listStyle"] = "bullet"
            add_paragraph(props)
        elif kind in ("numbered", "ordered", "number"):
            props = _paragraph_props(
                raw, "Normal", style["bodyFont"], style["bodySize"]
            )
            props["listStyle"] = "ordered"
            add_paragraph(props)
        elif kind == "quote":
            add_paragraph(
                _paragraph_props(
                    raw, "Quote", style["bodyFont"], style["bodySize"]
                )
            )
        elif kind == "table":
            headers = raw.get("headers")
            rows = raw.get("rows")
            props = {
                "data": _table_data(headers, rows, f"blocks[{index}]"),
                "width": str(raw.get("width") or style["tableWidth"]),
                "layout": "autofit",
            }

            three_line = raw.get("threeLine")
            if three_line is None:
                three_line = style["tableThreeLine"]
            if not isinstance(three_line, bool):
                raise SkillError(
                    "invalid_spec", f"blocks[{index}].threeLine 必须是布尔值。"
                )

            if three_line:
                # 三线表：清除全部框线，只留上/下粗线；表头下细线逐格补
                props.update(
                    {
                        "border.all": "none",
                        "border.top": "single;12;000000",
                        "border.bottom": "single;12;000000",
                        "border.horizontal": "none",
                        "border.vertical": "none",
                    }
                )
            elif raw.get("style"):
                # 关闭三线表时才使用彩色表格样式
                props["style"] = str(raw["style"])
            commands.append(
                {"command": "add", "parent": "/body", "type": "table", "props": props}
            )

            # 表格字号只作用于单元格，逐格设置才能压到 9pt；
            # 三线表同时做垂直居中、表头加粗居中 + 表头下细线。
            table_count += 1
            row_count = len(rows) + (1 if headers else 0)
            col_count = len(headers) if headers else len(rows[0])
            for row_index in range(1, row_count + 1):
                for col_index in range(1, col_count + 1):
                    cell_props: dict[str, str] = {
                        "font": style["bodyFont"],
                        "size": style["tableFontSize"],
                        "valign": "center",
                    }
                    if three_line and headers and row_index == 1:
                        cell_props.update(
                            {
                                "bold": "true",
                                "align": "center",
                                "border.bottom": "single;6;000000",
                            }
                        )
                    commands.append(
                        {
                            "command": "set",
                            "path": (
                                f"/body/tbl[{table_count}]"
                                f"/tr[{row_index}]/tc[{col_index}]"
                            ),
                            "props": cell_props,
                        }
                    )
        elif kind == "pagebreak":
            commands.append(
                {"command": "add", "parent": "/body", "type": "pagebreak", "props": {}}
            )
            paragraph_count += 1
        elif kind == "spacer":
            add_paragraph(
                _paragraph_props(
                    {"text": " "}, "Normal", style["bodyFont"], style["bodySize"]
                )
            )
        else:
            raise SkillError(
                "invalid_spec",
                f"不支持的 blocks[{index}].type：{kind}",
                supported=[
                    "heading",
                    "paragraph",
                    "bullet",
                    "numbered",
                    "quote",
                    "table",
                    "pagebreak",
                    "spacer",
                ],
            )

    # 页脚页码：differentFirstPage（封面无页码），正文从 1 开始、
    # 共 N 页且不含封面。具体域由 _finalize_docx_footer() 用 raw-set 写入。
    if style["pageNumberFooter"]:
        footer_font = {"font": style["bodyFont"], "size": style["footerSize"]}
        commands.append(
            {
                "command": "set",
                "path": "/section[1]",
                "props": {"titlePage": "true"},
            }
        )
        # 默认页脚先建，保证路径是 /footer[1]（封面页脚随后建为 /footer[2]）
        commands.append(
            {
                "command": "add",
                "parent": "/",
                "type": "footer",
                "props": {"text": "第 ", "align": "center", **footer_font},
            }
        )
        commands.append(
            {
                "command": "add",
                "parent": "/",
                "type": "footer",
                "props": {"type": "first", "text": "", "align": "center"},
            }
        )
        # 让 Word 打开时重算 PAGE / NUMPAGES，避免显示生成时的旧缓存
        commands.append(
            {
                "command": "set",
                "path": "/",
                "props": {"updateFields": "true"},
            }
        )

    return commands


# ---------------------------------------------------------------------------
# 构建 Pptx 命令
# ---------------------------------------------------------------------------


def _shape_props(text: str, spec: dict[str, Any], *, bullet: bool) -> dict[str, str]:
    props: dict[str, str] = {
        "text": text,
        "x": f"{BODY_X_CM}cm",
        "width": f"{BODY_W_CM}cm",
        "font": str(spec.get("font") or DEFAULT_PPT_FONT),
        "size": str(spec.get("bodySize") or 22),
        "color": str(spec.get("bodyColor") or "333333"),
        "valign": "top",
    }
    if bullet:
        props["list"] = "bullet"
    return props


def build_pptx_commands(spec: dict[str, Any]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SkillError(
            "invalid_spec",
            "PPT 规格必须包含非空的 slides 数组。",
            hint='例如 {"slides":[{"title":"封面","subtitle":"2026"},{"title":"要点","bullets":["A","B"]}]}',
        )

    for index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            raise SkillError("invalid_spec", f"slides[{index}] 必须是对象。")
        title = slide.get("title")
        subtitle = slide.get("subtitle")
        bullets = slide.get("bullets")
        free_text = slide.get("text")
        table = slide.get("table")

        if bullets is not None and not (
            isinstance(bullets, list) and all(isinstance(item, str) for item in bullets)
        ):
            raise SkillError(
                "invalid_spec", f"slides[{index}].bullets 必须是字符串数组。"
            )

        slide_props: dict[str, str] = {}
        if title:
            slide_props["title"] = _require_str(title, f"slides[{index}].title")
        if slide.get("layout"):
            slide_props["layout"] = str(slide["layout"])
        elif subtitle:
            slide_props["layout"] = "Title Slide"
        if subtitle:
            slide_props["text"] = _require_str(
                subtitle, f"slides[{index}].subtitle"
            )
        if slide.get("background"):
            slide_props["background"] = str(slide["background"])

        commands.append(
            {"command": "add", "parent": "/", "type": "slide", "props": slide_props}
        )
        slide_path = f"/slide[{index + 1}]"

        # 标题幻灯片（有 subtitle）不再画正文框
        if not subtitle:
            body_y = 4.4 if title else 1.8
            if bullets:
                body_text = "\n".join(str(item) for item in bullets)
                height = 11.6 if not table else 6.4
                props = _shape_props(body_text, spec, bullet=True)
                props["y"] = f"{body_y}cm"
                props["height"] = f"{height}cm"
                commands.append(
                    {
                        "command": "add",
                        "parent": slide_path,
                        "type": "shape",
                        "props": props,
                    }
                )
            elif free_text:
                props = _shape_props(
                    _require_str(free_text, f"slides[{index}].text"),
                    spec,
                    bullet=False,
                )
                props["y"] = f"{body_y}cm"
                props["height"] = "11.6cm"
                commands.append(
                    {
                        "command": "add",
                        "parent": slide_path,
                        "type": "shape",
                        "props": props,
                    }
                )

            if table:
                if not isinstance(table, dict):
                    raise SkillError(
                        "invalid_spec", f"slides[{index}].table 必须是对象。"
                    )
                table_props: dict[str, str] = {
                    "data": _table_data(
                        table.get("headers"), table.get("rows"), f"slides[{index}].table"
                    ),
                    "x": f"{BODY_X_CM}cm",
                    "width": f"{BODY_W_CM}cm",
                    "height": "5.6cm",
                    "style": str(table.get("style") or "medium2"),
                    "firstRow": "true",
                }
                table_y = 12.4 if bullets else body_y
                table_props["y"] = f"{table_y}cm"
                commands.append(
                    {
                        "command": "add",
                        "parent": slide_path,
                        "type": "table",
                        "props": table_props,
                    }
                )

        notes = slide.get("notes")
        if notes:
            commands.append(
                {
                    "command": "add",
                    "parent": slide_path,
                    "type": "notes",
                    "props": {"text": _require_str(notes, f"slides[{index}].notes")},
                }
            )
    return commands


# ---------------------------------------------------------------------------
# CLI 参数 -> 规格
# ---------------------------------------------------------------------------


def _parse_slide_arg(value: str) -> dict[str, Any]:
    """'标题::要点1;要点2' -> {title, bullets}。"""
    title, separator, body = value.partition("::")
    slide: dict[str, Any] = {"title": title.strip()}
    bullets = [item.strip() for item in body.split(";") if item.strip()]
    if bullets:
        slide["bullets"] = bullets
    return slide


def _spec_from_cli(args: argparse.Namespace, fmt: str) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if args.title:
        spec["title"] = args.title
    if args.subtitle:
        spec["subtitle"] = args.subtitle

    if fmt == "docx":
        blocks = [{"type": "paragraph", "text": text} for text in args.paragraph]
        if blocks:
            spec["blocks"] = blocks
    else:
        slides: list[dict[str, Any]] = []
        if args.title_slide:
            title, separator, subtitle = args.title_slide.partition("::")
            slide: dict[str, Any] = {"title": title.strip()}
            if separator and subtitle.strip():
                slide["subtitle"] = subtitle.strip()
            slides.append(slide)
        slides.extend(_parse_slide_arg(value) for value in args.slide)
        if not slides and args.title:
            slides.append({"title": args.title})
        if slides:
            spec["slides"] = slides
    return spec


# ---------------------------------------------------------------------------
# 生成流程
# ---------------------------------------------------------------------------


def _create_document(
    exe: Path,
    output: Path,
    locale: str,
    timeout: float,
) -> None:
    # 清掉上一次同名文件（含 OfficeCli resident 仍持有的情况）
    _run_officecli(exe, ["close", str(output), "--json"], timeout)
    if output.exists():
        try:
            output.unlink()
        except OSError as exc:
            raise SkillError(
                "output_locked",
                f"无法覆盖已有文件（可能正被 Word/PowerPoint 打开）：{output}",
                output=str(output),
                detail=str(exc),
            ) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    _call(
        exe,
        ["create", str(output), "--force", "--locale", locale, "--json"],
        timeout,
    )


def _run_batch(
    exe: Path, output: Path, commands: list[dict[str, Any]], timeout: float
) -> dict[str, Any]:
    payload = json.dumps(commands, ensure_ascii=False)
    handle, temp_name = tempfile.mkstemp(
        prefix="officecli_batch_", suffix=".json", dir=str(output.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        parsed = _call(
            exe,
            ["batch", str(output), "--input", temp_name, "--json"],
            timeout,
        )
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass

    data = parsed.get("data") if isinstance(parsed, dict) else None
    summary = data.get("summary") if isinstance(data, dict) else None
    if isinstance(summary, dict) and summary.get("failed"):
        raise SkillError(
            "batch_failed",
            f"OfficeCli batch 有 {summary['failed']} 条命令失败（整批已回滚）。",
            summary=summary,
            detail=_short(json.dumps(data, ensure_ascii=False)),
        )
    return parsed or {}


def _docx_footer_xml() -> str:
    """生成“第 X 页 共 Y 页”的页脚段落 XML。

    X = PAGE - 1，Y = NUMPAGES - 1：封面占第 1 页但不显示页码，
    正文从第 1 页开始，总页数也不含封面。中文/字体名用数字字符引用，
    避免通过命令行传 XML 时的编码问题。
    """

    def text(value: str) -> str:
        return f'<w:r><w:t xml:space="preserve">{value}</w:t></w:r>'

    def field_char(kind: str) -> str:
        return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'

    def instruction(value: str) -> str:
        return (
            '<w:r><w:instrText xml:space="preserve">'
            f"{value}</w:instrText></w:r>"
        )

    def formula(field: str) -> str:
        # { = { FIELD } - 1 }，先写内层域再写外层公式域
        return (
            field_char("begin")
            + instruction(" = ")
            + field_char("begin")
            + instruction(f" {field} ")
            + field_char("separate")
            + text("0")
            + field_char("end")
            + instruction(" - 1 ")
            + field_char("separate")
            + text("0")
            + field_char("end")
        )

    return (
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        + text("&#31532; ")
        + formula("PAGE")
        + text(" &#39029; &#20849; ")
        + formula("NUMPAGES")
        + text(" &#39029;")
        + "</w:p>"
    )


def _finalize_docx_footer(
    exe: Path,
    output: Path,
    timeout: float,
    font: str,
    size: str,
) -> None:
    """用 raw-set 写入嵌套域页脚，再刷新域缓存（Word 不可用时忽略刷新）。"""
    _call(
        exe,
        [
            "raw-set",
            str(output),
            "/footer[1]",
            "--xpath",
            "/w:ftr/w:p[1]",
            "--action",
            "replace",
            "--xml",
            _docx_footer_xml(),
            "--json",
        ],
        timeout,
    )
    _call(
        exe,
        [
            "set",
            str(output),
            "/footer[1]",
            "--prop",
            f"font={font}",
            "--prop",
            f"size={size}",
            "--prop",
            "align=center",
            "--json",
        ],
        timeout,
    )
    _call(exe, ["close", str(output), "--json"], timeout, expect_success=False)
    try:
        _call(
            exe,
            ["refresh", str(output), "--json"],
            min(timeout, 20),
            expect_success=False,
        )
    except SkillError:
        # refresh 依赖 Word；没有 Word 时保留 updateFields，由打开者自行重算
        pass


def generate(args: argparse.Namespace) -> dict[str, Any]:
    spec = _load_spec(args.spec)
    if args.spec:
        # 规格文件优先；CLI 只覆盖输出路径与 OfficeCli 路径
        if args.title and not spec.get("title"):
            spec["title"] = args.title
    else:
        fmt_hint = _infer_format(args, spec)
        spec = _spec_from_cli(args, fmt_hint)

    fmt = _infer_format(args, spec)
    output = _resolve_output(args.output, fmt, spec.get("output"))

    if fmt == "docx":
        commands = build_docx_commands(spec)
    else:
        commands = build_pptx_commands(spec)

    exe = _resolve_officecli(args.officecli)
    locale = str(spec.get("locale") or DEFAULT_LOCALE)

    if args.dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "output": str(output),
            "format": fmt,
            "officecli": str(exe),
            "create": [
                str(exe),
                "create",
                str(output),
                "--force",
                "--locale",
                locale,
                "--json",
            ],
            "batch": commands,
        }

    _create_document(exe, output, locale, args.timeout)
    try:
        _run_batch(exe, output, commands, args.timeout)
        if fmt == "docx":
            docx_style = _docx_style_config(spec)
            if docx_style["pageNumberFooter"]:
                _finalize_docx_footer(
                    exe,
                    output,
                    args.timeout,
                    docx_style["bodyFont"],
                    docx_style["footerSize"],
                )
    finally:
        # 落盘并释放 resident，保证 Word / PowerPoint / python-docx 能读到最新内容
        _call(exe, ["close", str(output), "--json"], args.timeout, expect_success=False)

    parsed_validate = _call(
        exe, ["validate", str(output), "--json"], args.timeout, expect_success=False
    )
    validation = "Validation passed: no errors found."
    if isinstance(parsed_validate, dict):
        validation = str(parsed_validate.get("data") or parsed_validate.get("message"))
        if parsed_validate.get("success") is False:
            raise SkillError(
                "validation_failed",
                str(parsed_validate.get("error") or validation),
                output=str(output),
            )

    stats: dict[str, Any] | None = None
    parsed_stats = _call(
        exe, ["view", str(output), "stats", "--json"], args.timeout, expect_success=False
    )
    if isinstance(parsed_stats, dict) and isinstance(parsed_stats.get("data"), dict):
        stats = parsed_stats["data"]

    # 再次 close：validate/view 可能又启动了 resident
    _call(exe, ["close", str(output), "--json"], args.timeout, expect_success=False)

    if not output.is_file() or output.stat().st_size == 0:
        raise SkillError(
            "output_missing",
            f"OfficeCli 未生成有效文件：{output}",
            output=str(output),
        )

    return {
        "ok": True,
        "output": str(output),
        "format": fmt,
        "bytes": output.stat().st_size,
        "commands": len(commands),
        "validation": validation,
        "stats": stats,
        "message": f"已生成 {fmt.upper()} 文件：{output}",
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="调用 OfficeCli 生成 Word(.docx) / PPT(.pptx)，输出 JSON 结果。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--spec",
        help="JSON 规格文件路径（UTF-8）。支持标题、段落、列表、表格、分页、PPT 备注等。",
    )
    parser.add_argument(
        "--output",
        help=(
            "输出文件路径（.docx / .pptx）。相对路径按当前工作目录解析；"
            "省略时写入 <项目根>/workspace/office-documents/office_<时间戳>.<格式>"
        ),
    )
    parser.add_argument(
        "--format",
        choices=SUPPORTED_FORMATS,
        help="文档格式；通常由输出扩展名自动推断，无需指定。",
    )
    parser.add_argument("--title", help="文档标题 / 单页 PPT 标题。")
    parser.add_argument("--subtitle", help="Word 副标题。")
    parser.add_argument(
        "--paragraph",
        action="append",
        default=[],
        help="Word 正文段落，可重复：--paragraph 第一段 --paragraph 第二段",
    )
    parser.add_argument(
        "--title-slide",
        help='PPT 封面页，格式 "标题::副标题"，例如 "季度总结::2026 Q3"。',
    )
    parser.add_argument(
        "--slide",
        action="append",
        default=[],
        help='PPT 内容页，可重复，格式 "标题::要点1;要点2"。',
    )
    parser.add_argument(
        "--officecli",
        help="OfficeCli 可执行文件路径（默认使用技能内置路径，可被 OFFICECLI_EXE 覆盖）。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="单条 OfficeCli 命令超时秒数（默认 60）。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的 OfficeCli 命令，不创建/修改文件。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = generate(args)
    except SkillError as exc:
        error: dict[str, Any] = {"type": exc.error_type, "message": exc.message}
        error.update(exc.details)
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:  # 兜底：始终输出结构化 JSON，方便 Agent 解析
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "unexpected_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
