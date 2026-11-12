# 大语言模型技术与应用 · 终结性项目

一个围绕「MCP + 本地 Agent」做出来的完整小系统：自定义 Agent 能读写文件、跑命令行、
上网查资料，也能挂载本地 MCP Server 去操作宠物医院数据库和 AnythingLLM 知识库；
其中一个知识库还配了网页端。

不是把几个示例拼在一起：所有模块之间靠真实的 HTTP / MCP 协议通信，每个模块都带一份
可以独立跑起来的验收脚本。

## 1. 项目地图

```text
.
├── xAgent/                 # 自定义 Function Calling Agent（核心，D 维度）
│   ├── core/               # 主循环、LLM 客户端、工具注册表、MCP 客户端、会话压缩
│   ├── tui/                # Textual 终端界面（斜杠命令、会话切换）
│   ├── .agents/skills/     # 可动态加载的 Agent Skills
│   ├── acceptance_test.py  # 29 项工具验收
│   └── .mcp.json           # ⚠ 端口以本文件为准：Pet=9091，AnythingLLM=9090
├── PetHospitalMCP/         # 宠物医院 MCP Server（A 维度），监听 9091
│   └── tests/              # 18 项冒烟测试（自带模拟上游）
├── AnythingLLMMCP/         # AnythingLLM MCP Server（B 维度），监听 9090
│   └── tests/              # 13 项冒烟测试（自带模拟上游）
├── AnythingLLMWeb/         # AnythingLLM 网页端（C 维度），监听 8092
│   └── tests/              # 16 项冒烟测试（自带模拟上游）
├── PetHospitalServer/      # 宠物医院上游服务（Go 单文件程序，端口 8080）
├── Agent/practice01/       # 课程练习，与主项目无调用关系
├── Agent/Practice02/       # 课程练习
└── task4/                  # 课程任务
```

### 模块关系

```text
                ┌──────────────────────────────┐
                │   xAgent（DeepSeek + 工具循环）│
                │  17 个工具 / Skills / 会话压缩 │
                └───────┬──────────────┬───────┘
        MCP (9091)      │              │      MCP (9090)
                        ▼              ▼
          ┌──────────────────┐  ┌──────────────────────┐
          │ PetHospitalMCP   │  │ AnythingLLMMCP       │
          │ 4 个业务工具      │  │ 5 个知识库工具        │
          └────────┬─────────┘  └──────────┬───────────┘
                   │ HTTP 8080            │ HTTP 3001
                   ▼                      ▼
          ┌──────────────────┐  ┌──────────────────────┐
          │ PetHospitalServer│  │ AnythingLLM 本体     │
          │ （Go，本地数据）  │  │ （Docker / 桌面版）   │
          └──────────────────┘  └──────────┬───────────┘
                                           │ HTTP 3001
                                           ▼
                                ┌──────────────────────┐
                                │ AnythingLLMWeb 8092  │
                                │ 浏览器界面，密钥后端持有 │
                                └──────────────────────┘
```

## 2. 环境依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 全部 Python 模块 |
| `mcp` | 2.3.0 | 两个 MCP Server 的 SDK |
| `openai` | ≥1.40 | xAgent 调用 DeepSeek |
| `textual` | ≥0.80 | xAgent 终端界面 |
| `httpx` / `requests` | 近期版本 | 各模块 HTTP 调用 |
| `fastapi` / `uvicorn` | 近期版本 | AnythingLLMWeb |
| AnythingLLM | 任意近期版本 | B/C 维度的知识库上游 |
| Go（可选） | 1.20+ | 自行编译 PetHospitalServer |

## 3. 配置矩阵

| 模块 | 配置文件 | 必填项 | 端口 |
|---|---|---|---|
| xAgent | `xAgent/config.ini` | `[llm] api_key`、`base_url` | — |
| PetHospitalMCP | `PetHospitalMCP/.env` | 无（默认指向 8080） | 9091 |
| AnythingLLMMCP | `AnythingLLMMCP/.env` | `ANYTHINGLLM_API_KEY` | 9090 |
| AnythingLLMWeb | `AnythingLLMWeb/.env` | `ANYTHINGLLM_API_KEY` | 8092 |
| PetHospitalServer | 无 | 无 | 8080 |
| AnythingLLM | 其自身设置 | 向量数据库需配嵌入模型 | 3001 |

统一约定：每个目录都有 `.env.example`，复制成 `.env` 再填；`.env` 已在 `.gitignore` 里。

## 4. 启动顺序

```bash
# 0) 依赖（按需装，建议各自建虚拟环境）
pip install -r xAgent/requirements.txt
pip install -r PetHospitalMCP/requirements.txt
pip install -r AnythingLLMMCP/requirements.txt
pip install -r AnythingLLMWeb/requirements.txt

# 1) 上游：宠物医院（Windows 直接运行 exe；其他平台需 go build）
cd PetHospitalServer && ./pethospital.exe      # 监听 8080

# 2) MCP：宠物医院（9091）
cd PetHospitalMCP && python main.py

# 3) MCP：AnythingLLM（9090），先配好 .env
cd AnythingLLMMCP && python main.py

# 4) 网页端（8092），先配好 .env
cd AnythingLLMWeb && python app.py

# 5) Agent 本体
cd xAgent && python run_tui.py     # 或 python run_cli.py "帮我查一下本周住院的猫"
```

## 5. 验证清单

每个模块都能不依赖上游跑起来（自带模拟上游）：

```bash
python xAgent/acceptance_test.py          # 29 项：文件工具 / fetchUrl / searchWeb / RunCli
python PetHospitalMCP/tests/run_smoke.py  # 18 项：4 工具 + 参数校验 + 重试
python AnythingLLMMCP/tests/run_smoke.py  # 13 项：5 工具 + 缺密钥 + 边界
python AnythingLLMWeb/tests/run_smoke.py  # 16 项：路由 / 流式 / 密钥不外泄
```

xAgent 的工具一次调用示例：

```bash
cd xAgent && python -X utf8 -c "
from core.agent import Agent
a = Agent()
print(len(a.tools))                                  # 17
print(a._dispatch_tool('list_dir', '{\"path\": \".\"}').ok)
"
```

## 6. 已知边界

- `PetHospitalServer/pethospital.exe` 是 Windows 二进制，也是这个上游唯一的实现产物，
  因此保留在仓库里；Linux/macOS 需在 `PetHospitalServer/` 内 `go build` 得到本机可执行文件。
- `PetHospitalServer/data/pet.db` 是运行时数据，不进仓库；需要演示数据时调用
  `POST /api/v1/admin/seed?force=true&count=100` 重新生成。
- AnythingLLM 的向量化依赖它自己配置的嵌入模型，未配置时 `update-embeddings` 会失败，
  本项目的两个模块都会把失败原因原样返回，不会卡住。
- AnythingLLM 的 API Key 曾经以明文提交过，现已改为环境变量；旧密钥需在
  AnythingLLM 后台轮换后使用新值。

## 7. FAQ

**Q：xAgent 启动直接报「尚未配置可用的 API Key」？**
编辑 `xAgent/config.ini` 的 `[llm] api_key`，或设置环境变量 `XAGENT_API_KEY`。

**Q：`.mcp.json` 里端口和代码对不上？**
以 `xAgent/.mcp.json` 为准：PetHospitalMCP = 9091，AnythingLLMMCP = 9090，两个
`main.py` 的默认端口与之一致，可用 `MCP_PORT` 环境变量覆盖。

**Q：网页端打开提示 config_error？**
`AnythingLLMWeb/.env` 没有 `ANYTHINGLLM_API_KEY`，或 `.env` 不在 `app.py` 同目录。

**Q：为什么仓库里有两个 `practice01`？**
`Agent/practice01` 与 `Agent/Practice02` 是课程练习，和主项目没有调用关系；
主项目只有 `xAgent/` 一份，之前的旧快照已删除，避免两套 README 互相打架。
