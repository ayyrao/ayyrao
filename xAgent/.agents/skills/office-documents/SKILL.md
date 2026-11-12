---
name: office-documents
description: >-
  生成/编辑 Word 文档（.docx）与 PPT 演示文稿（.pptx）。当用户需要产出
  Word、PPT、docx、pptx 文件，例如写报告/总结/方案/周报、做演示文稿、
  做幻灯片、生成“包含 N 页内容的 PPT”时使用。
license: MIT
metadata:
  author: xAgent
  version: "1.0"
---

# Office Documents（Word / PPT 生成）

本技能把用户的文档需求翻译成 OfficeCli 命令，生成真正的 `.docx` / `.pptx`
文件。**不要手写 OOXML，也不要用 python-docx/python-pptx（项目环境未安装）**。

## 调用前提

| 项目 | 值 |
| --- | --- |
| OfficeCli 可执行文件 | `E:\sp\OfficeCli_#SULP#_26sc1d\officecli-win-x64.exe` |
| 封装脚本 | `D:\LLM\Agent\xAgent\.agents\skills\office-documents\scripts\office_doc.py` |
| 推荐输出目录 | `D:\LLM\Agent\xAgent\workspace\office-documents\` |
| 运行方式 | `RunCli`，`command="python"`，参数放 `args` 数组 |
| 依赖 | 无第三方 Python 包；脚本内部用 `subprocess` 调 OfficeCli |
| 超时 | RunCli 默认 30 秒；脚本通常 2～5 秒完成 |

- 输出文件必须放在 xAgent 工作目录 `D:\LLM\Agent\xAgent\workspace` 内。
- OfficeCli 路径含 `#` 字符，**只能作为 RunCli 的独立参数传递**（不经过 shell，
  不要拼接成命令行字符串）。
- 已有同名文件会被覆盖；若文件正被 Word / PowerPoint / WPS 打开，脚本会报
  `output_locked`，此时先让用户关闭文件。

## 工作流程

1. 从用户需求中确定：格式（docx / pptx）、标题、要点、是否要表格/分页/备注。
2. 简单文档 → 用「方式 A：命令行参数」，一次 RunCli 完成。
3. 复杂文档（多级标题、表格、分页、PPT 备注）→ 用「方式 B：JSON 规格文件」：
   - 先用 `create_file` 创建 `office-documents\spec.json`（空文件）。
     注意：文件工具的根目录已经是 `D:\LLM\Agent\xAgent\workspace`，
     所以这里**不要**再写 `workspace\` 前缀；
   - 再用 `edit_text_file` 写入下面的 JSON（UTF-8）；
   - 最后 RunCli 执行脚本，`--spec` 用**绝对路径**
     `D:\LLM\Agent\xAgent\workspace\office-documents\spec.json`。
4. 读取脚本返回的 JSON：`ok=true` 时把 `output`（绝对路径）告诉用户；
   `ok=false` 时按 `error.type` / `error.message` 修正后重试。
5. 不要重复生成同一份文件；需要调整时修改规格后重跑，脚本会自动覆盖。

## 方式 A：命令行参数（简单文档）

### Word：标题 + 段落

```json
{
  "command": "python",
  "args": [
    "D:\\LLM\\Agent\\xAgent\\.agents\\skills\\office-documents\\scripts\\office_doc.py",
    "--output", "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\周报.docx",
    "--title", "本周工作周报",
    "--paragraph", "本周完成 OfficeCli 技能集成。",
    "--paragraph", "下周计划：补充模板与验收测试。"
  ]
}
```

### PPT：封面 + 内容页

`--title-slide "标题::副标题"` 生成封面；每个 `--slide "标题::要点1;要点2"`
生成一页内容。下面的例子正好 3 页：

```json
{
  "command": "python",
  "args": [
    "D:\\LLM\\Agent\\xAgent\\.agents\\skills\\office-documents\\scripts\\office_doc.py",
    "--output", "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\三页演示.pptx",
    "--title-slide", "项目季度总结::xAgent 项目组",
    "--slide", "本季度进展::完成 OfficeCli 集成;完成测试",
    "--slide", "下一步计划::推广试点;补充模板"
  ]
}
```

> 要点之间用英文分号 `;` 分隔，标题与要点用 `::` 分隔。
> 如果用户只说“生成一份 PPT”，可以只传 `--title`，生成一页标题页。

## 方式 B：JSON 规格文件（推荐，支持表格/分页/备注）

### B1. Word 规格

创建 `D:\LLM\Agent\xAgent\workspace\office-documents\spec.json`：

```json
{
  "output": "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\季度总结.docx",
  "locale": "zh-CN",
  "title": "2026 年第三季度工作总结",
  "subtitle": "xAgent 项目组 · 2026-11-05",
  "blocks": [
    {"type": "heading", "text": "一、工作概述", "level": 1},
    {"type": "paragraph", "text": "本季度完成了 OfficeCli 技能集成。"},
    {"type": "heading", "text": "二、主要成果", "level": 2},
    {"type": "bullet", "text": "完成 office-documents 技能"},
    {"type": "bullet", "text": "打通 docx / pptx 生成流程"},
    {"type": "table",
     "headers": ["指标", "目标", "实际"],
     "rows": [["成功率", "95%", "100%"], ["耗时", "10 秒", "4 秒"]],
     "style": "medium2"},
    {"type": "pagebreak"},
    {"type": "numbered", "text": "补充更多模板"},
    {"type": "quote", "text": "以上内容由技能自动生成。"}
  ]
}
```

然后执行：

```json
{
  "command": "python",
  "args": [
    "D:\\LLM\\Agent\\xAgent\\.agents\\skills\\office-documents\\scripts\\office_doc.py",
    "--spec", "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\spec.json"
  ]
}
```

Word 的 `blocks[].type` 支持：

| type | 关键字段 | 说明 |
| --- | --- | --- |
| `heading` | `text`, `level`(1–9) | 标题，使用 Word 内置 Heading1…9 |
| `paragraph` / `text` | `text` | 正文段落 |
| `bullet` | `text` | 无序列表（连续多条自动接续） |
| `numbered` | `text` | 有序列表 |
| `quote` | `text` | 引用样式 |
| `table` | `headers`, `rows`, `style`, `threeLine` | 默认三线表；`"threeLine": false` 时才使用 `style`（如 `medium2`） |
| `pagebreak` | — | 分页符 |
| `spacer` | — | 空行 |

段落可加样式字段：`bold`、`italic`、`size`（如 `14`）、`color`（`FF0000`）、
`align`（`left` / `center` / `right` / `justify`）、`font`。
顶层还可用 `title` / `subtitle` 生成标题页。

#### Word 默认排版（已内置，无需在 blocks 里逐条设置）

| 项目 | 默认值 |
| --- | --- |
| 全局字体 | 宋体（`docDefaults` 的 Latin / EastAsia / hAnsi 三个槽位都设为宋体） |
| 正文 | 宋体 12pt（小四）；首行缩进 2 字符、1.5 倍行距、段后 0 |
| 一级标题 | 黑体 16pt（三号） |
| 二级 / 三级标题 | 黑体 14pt / 黑体 12pt |
| 文档标题 | 黑体；沿用 Word Title 样式字号 |
| 封面副标题 | 黑体 16pt（三号） |
| 封面信息栏 | 宋体 14pt 居中，默认占位：`课程名称：____  班级：____  姓名：____  学号：____  指导教师：____  日期：____` |
| 表格 | **三线表**：宋体 9pt、宽度 100%、`layout=autofit`；上下 1.5pt 粗线、表头下 0.75pt 细线，无竖线/彩色底纹；表头加粗居中，全部单元格垂直居中 |
| 页脚 | differentFirstPage（封面无页码）；正文居中、宋体 10pt，`第 X 页 共 Y 页`，X 从 1 开始、Y 不含封面 |
| 封面 | 有 `title` / `subtitle` 且后面有正文时，**自动插入分页符**，封面独立成页 |

需要覆盖默认值时，在 Word 规格顶层加 `style` 对象（字段都可省略）：

```json
{
  "style": {
    "bodyFont": "宋体",
    "headingFont": "黑体",
    "bodySize": 12,
    "heading1Size": 16,
    "heading2Size": 14,
    "heading3Size": 12,
    "subtitleSize": 16,
    "tableFontSize": 9,
    "tableWidth": "100%",
    "tableThreeLine": true,
    "coverPageBreak": true,
    "coverInfo": [
      "课程名称：____  班级：____  姓名：____  学号：____  指导教师：____  日期：____"
    ],
    "coverInfoSize": 14,
    "coverInfoSpaceBefore": "180pt",
    "bodyFirstLineIndent": "24pt",
    "bodyLineSpacing": "1.5x",
    "bodySpaceAfter": "0pt",
    "footerSize": 10,
    "pageNumberFooter": true
  }
}
```

- 单个段落/标题仍可用 `font`、`size` 覆盖；`coverPageBreak=false` 可关闭封面分页。
- `coverInfo` 是字符串数组，一行一个居中段落；传 `[]` 可去掉信息栏。
- `tableThreeLine=false` 恢复彩色表格样式；单张表也可写 `"threeLine": false`，
  此时 `blocks[].style`（如 `medium2`）才会生效。
- 页码默认开启 differentFirstPage：封面不显示页码，正文显示“第 1 页 共 N 页”
  （N 已扣除封面）。`pageNumberFooter=false` 可关闭页脚。

### B2. PPT 规格

创建 `D:\LLM\Agent\xAgent\workspace\office-documents\spec_ppt.json`：

```json
{
  "output": "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\季度汇报.pptx",
  "locale": "zh-CN",
  "font": "Microsoft YaHei",
  "slides": [
    {
      "title": "2026 年第三季度总结",
      "subtitle": "xAgent 项目组 · 2026-11-05",
      "notes": "开场：30 秒说明本季度目标。"
    },
    {
      "title": "本季度进展",
      "bullets": ["完成 OfficeCli 集成", "打通 docx / pptx 生成", "完成验收测试"],
      "notes": "强调自动化带来的效率提升。"
    },
    {
      "title": "关键数据",
      "bullets": ["成功率 100%", "平均耗时 4 秒"],
      "table": {
        "headers": ["指标", "目标", "实际"],
        "rows": [["成功率", "95%", "100%"], ["耗时", "10 秒", "4 秒"]],
        "style": "medium2"
      }
    }
  ]
}
```

执行方式同 Word，只把 `--spec` 指向 PPT 规格文件。

每页 `slides[]` 支持：

| 字段 | 说明 |
| --- | --- |
| `title` | 页标题 |
| `subtitle` | 有该字段时按“封面页”生成（Title Slide 版式） |
| `bullets` | 字符串数组，生成项目符号列表（自动换行排版） |
| `text` | 无 `bullets` 时的自由文本 |
| `table` | `{headers, rows, style}`，放在页面下方 |
| `notes` | 演讲者备注 |
| `layout` | 自定义版式名，如 `Title and Content`；一般不用填 |
| `background` | 页面背景色，如 `1A1A2E` |

顶层可设 `font`（默认 `Microsoft YaHei`）、`bodySize`（默认 22）、
`bodyColor`（默认 `333333`）。

## 输出路径规则

1. 传了 `--output`（或规格里的 `output`）→ 使用该路径；相对路径按
   **当前工作目录**（通常是 `D:\LLM\Agent\xAgent`）解析。
2. 没传 → 自动生成到
   `D:\LLM\Agent\xAgent\workspace\office-documents\office_<YYYYmmdd_HHMMSS>.<docx|pptx>`。
3. 扩展名决定格式：`.docx` / `.pptx`；两者都没有时可用 `--format`。
4. 脚本成功后返回：

```json
{
  "ok": true,
  "output": "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\周报.docx",
  "format": "docx",
  "bytes": 5540,
  "commands": 3,
  "validation": "Validation passed: no errors found.",
  "stats": {"paragraphs": 3, "words": 5},
  "message": "已生成 DOCX 文件：..."
}
```

**向用户汇报时必须使用 `output` 里的绝对路径**，并说明页数/幻灯片数
（`stats` 里可读）。

## 自检（可选）

需要确认生成内容时，用 RunCli 调 OfficeCli 读取（路径作为独立参数）：

```json
{
  "command": "E:\\sp\\OfficeCli_#SULP#_26sc1d\\officecli-win-x64.exe",
  "args": ["view", "D:\\LLM\\Agent\\xAgent\\workspace\\office-documents\\周报.docx", "text"]
}
```

PPT 把文件换成 `.pptx` 即可；`stats` 模式可看页数、形状数。

## 常见报错与处理

| 返回的 `error.type` / 现象 | 原因 | 处理 |
| --- | --- | --- |
| `officecli_not_found` | exe 路径不对 | 检查 `E:\sp\OfficeCli_#SULP#_26sc1d\officecli-win-x64.exe`；用 `--officecli` 指定或设 `OFFICECLI_EXE` |
| `output_locked` | 目标文件被 Word/PPT/WPS 打开 | 请用户关闭文件后重试 |
| `spec_not_found` | `--spec` 路径错 | 用绝对路径；确认文件已创建 |
| `spec_invalid_json` | JSON 语法错（多余逗号、注释、中文引号） | 按提示的行列号修正；JSON 不允许注释 |
| `spec_encoding_error` | 规格文件不是 UTF-8 | 用 `edit_text_file` 重写为 UTF-8 |
| `invalid_spec` | 字段类型/结构不对（如 `rows` 不是二维数组） | 对照上面的规格表修正 |
| `batch_failed` | 某条 OfficeCli 命令被拒（属性名拼错等） | 读 `detail`；用 `officecli help <格式> <元素>` 查真实属性名 |
| `validation_failed` | 生成的文件未通过 OpenXML 校验 | 减少不支持的属性，或改用规格支持的块类型 |
| `officecli_timeout` | OfficeCli 进程被占用/卡住 | 先 `close` 该文件（用 exe 的 `close` 子命令），再重试 |
| 控制台中文乱码 | 直接复制命令到 GBK 终端 | 始终通过脚本 + JSON 文件传中文；脚本输出 UTF-8 |
| 提示“找不到文件”但路径存在 | 在非 xAgent 根目录使用了相对路径 | 一律改用 Windows 绝对路径 |

## 注意事项

- **不要**绕过脚本直接一条条执行 `officecli add ... --prop text="中文"`：
  cmd/GBK 控制台下中文容易乱码，且多步命令没有事务保护。
- **不要**修改 `core/agent.py` 的 Skill 加载逻辑；本技能只依赖 `loadSkill` +
  `RunCli` 两个现有工具。
- 生成的 `.docx` / `.pptx` 不要再用 `read_text_file` 读取（属于二进制后缀，
  会被拒绝）；用上面的 `officecli view` 自检。
- 一次需求只生成一份文件；用户要“3 页 PPT”时就安排 3 个 slide
  （封面 + 2 页内容，或 3 页内容），不要多生成。

## 附件

| 路径 | 用途 |
| --- | --- |
| `scripts/office_doc.py` | 参数解析、生成 OfficeCli batch、执行、校验并输出绝对路径 |
