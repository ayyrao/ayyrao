# AnythingLLMMCP

把 AnythingLLM 的知识库能力封装成 MCP Server，供 Agent 直接调用。
传输方式：Streamable HTTP，默认监听 `http://127.0.0.1:9090/mcp`。

## 1. 能力与工具

| 能力类别 | 工具 | 作用 |
|---|---|---|
| 工作区管理 | `list_workspaces` | 列出工作区、名称与文档数 |
| 工作区管理 | `create_workspace` | 新建工作区（1–80 字符） |
| 文档入库 | `upload_and_vectorize` | 上传本地文档后立即 `update-embeddings` |
| 知识检索 | `query_knowledge_base` | 向量检索，返回片段与来源 |
| 对话问答 | `chat_with_workspace` | 基于工作区文档回答，附引用来源 |

对应上游接口：

- `GET /api/v1/workspaces`
- `POST /api/v1/workspace/new`
- `POST /api/v1/document/upload` + `POST /api/v1/workspace/{slug}/update-embeddings`
- `POST /api/v1/workspace/{slug}/vector-search`
- `POST /api/v1/workspace/{slug}/chat`

## 2. 前置条件

- AnythingLLM 已启动（默认 `http://localhost:3001`）。
- 在 Settings → API Keys 生成密钥。
- Settings → Vector Database 已配置嵌入模型，否则 `update-embeddings` 会失败。

## 3. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANYTHINGLLM_BASE_URL` | `http://localhost:3001` | 上游地址 |
| `ANYTHINGLLM_API_KEY` | 无（必填） | 密钥，只从环境变量读取 |
| `ANYTHINGLLM_WORKSPACE` | 空 | 默认工作区 slug |
| `ANYTHINGLLM_TIMEOUT` | `30` | 请求超时（秒） |
| `ANYTHINGLLM_MAX_UPLOAD_BYTES` | `20971520` | 单文件上限，默认 20MB |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `9090` | MCP Server 监听 |
| `MCP_PATH` | `/mcp` | Streamable HTTP 路径 |

```bash
cp .env.example .env
# 填 ANYTHINGLLM_API_KEY
```

> 密钥不写在 `config.py` 里。历史提交中曾出现过明文 Key，已在其后台轮换；
> 本版本只从环境变量读取。

## 4. 启动

```bash
pip install -r requirements.txt
python main.py
```

## 5. 调用示例（MCP 工具入参）

```json
{"tool": "upload_and_vectorize", "arguments": {"file_path": "D:/docs/pets.txt", "workspace_slug": "default"}}
```

```json
{"tool": "query_knowledge_base", "arguments": {"query": "慢性肾病随访", "limit": 4}}
```

```json
{"tool": "chat_with_workspace", "arguments": {"message": "这周有几只猫复诊？", "mode": "chat"}}
```

返回统一是 MCP 的 `content` 结构；失败时文本以「调用失败：」开头，带可操作的原因。

## 6. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 提示未配置 `ANYTHINGLLM_API_KEY` | 没设环境变量 | 填 `.env` 或导出变量后重启 |
| 401 | 密钥错误 / 已在后台轮换 | 重新生成 Key |
| `update-embeddings` 失败 | 未配置向量数据库 | 先在 AnythingLLM 配嵌入模型 |
| 检索无结果 | 文档未向量化 | 重新 `upload_and_vectorize` |

## 7. 验证

```bash
python tests/run_smoke.py
```

模拟上游覆盖 13 项：五工具注册、列表/新建/上传/检索/对话的正常路径，
以及缺密钥、空名称、文件不存在、类型不支持、空检索词、非法 mode 的拦截。
