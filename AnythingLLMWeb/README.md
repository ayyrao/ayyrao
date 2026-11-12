# AnythingLLM 网页端（Knowledge Console）

一个真正调用 AnythingLLM API 的轻量控制台：选工作区、上传文档并向量化、就文档提问。
**密钥只存在服务端**，浏览器拿不到任何 API Key。

## 1. 功能

| 能力 | 说明 | 状态反馈 |
|---|---|---|
| 工作区列表 | 读取 `/api/v1/workspaces`，显示每个工作区的文档数 | 加载中 / 成功 / 失败 |
| 新建工作区 | 调用 `/api/v1/workspace/new` | 成功提示 / 失败原因 |
| 上传并向量化 | `/api/v1/document/upload` 上传后紧接 `update-embeddings` | 上传百分比 → 向量化完成 |
| 对话 | 优先走 `/api/v1/workspace/{slug}/stream-chat`（SSE 流式） | 逐字输出；流式失败自动退回 `/chat` 一次性接口 |
| 引用来源 | 非流式回答会带 `sources`，在气泡下方展示 | — |

## 2. 目录结构

```text
AnythingLLMWeb/
├── app.py              # FastAPI 路由：静态页 + API 代理
├── anythingllm.py      # AnythingLLM API 客户端（唯一持有密钥的地方）
├── requirements.txt
├── .env.example        # 配置模板（真实 .env 不入库）
└── static/
    ├── index.html
    ├── style.css
    └── app.js
```

## 3. 前置条件

- 已安装并启动 AnythingLLM（默认 `http://localhost:3001`）。
- 在 AnythingLLM 里生成一个 API Key（Settings → API Keys）。
- Python 3.10+。

## 4. 配置

```bash
cp .env.example .env
# 编辑 .env，至少填 ANYTHINGLLM_API_KEY
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ANYTHINGLLM_BASE_URL` | 否 | `http://localhost:3001` | AnythingLLM 服务地址 |
| `ANYTHINGLLM_API_KEY` | 是 | 无 | 只被后端读取，不下发前端 |
| `ANYTHINGLLM_WORKSPACE` | 否 | 空 | 默认工作区 slug；留空由页面选择 |

也可以不用 `.env`，直接导出环境变量，效果一致。

## 5. 启动

```bash
pip install -r requirements.txt
python app.py
# 打开 http://127.0.0.1:8092/
```

## 6. 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 探活 + 判断密钥是否就绪（不泄露密钥本身） |
| GET | `/api/workspaces` | 工作区列表 |
| POST | `/api/workspaces` | 新建工作区，body `{"name": "..."}` |
| POST | `/api/upload` | 表单 `workspace` + `file`，上传后立即向量化 |
| POST | `/api/chat` | body `{"workspace": "...", "message": "..."}` |
| POST | `/api/chat/stream` | 同上，返回 `text/event-stream` |

失败统一返回 `{"ok": false, "code": "...", "message": "..."}`：

- `config_error`（503）：没配密钥，提示去哪配；
- `upstream_unreachable`（502）：AnythingLLM 没起来或地址错；
- `upstream_http_error`（502）：上游返回非 2xx，带截断的错误正文。

## 7. 验证清单

1. `curl http://127.0.0.1:8092/api/health` 返回 `ok: true`。
2. 页面下拉能看到工作区；点“刷新”列表更新。
3. 传一个 txt，状态依次显示“正在上传 x%”→“已上传并完成向量化”。
4. 提问后回答逐字出现；断开 AnythingLLM 再提问，页面显示明确错误而不是白屏。
5. 查看浏览器 Network，请求里没有 `Authorization` 头，也没有任何 API Key 字样。

## 8. 安全说明

- 前端只与本站 `/api/*` 通信，API Key 由后端在转发时注入 `Authorization: Bearer`。
- `.env` 已写入 `.gitignore`，仓库只提交 `.env.example`。
- 上传大小上限 20MB，超限直接 413。
- 所有渲染走 `textContent`，不拼 HTML，避免 XSS。

## 9. FAQ

**Q：页面提示 config_error？**
没有 `ANYTHINGLLM_API_KEY`。检查 `.env` 是否与 `app.py` 同目录，或改用环境变量启动。

**Q：上传成功但向量化失败？**
AnythingLLM 需要在 Settings → Vector Database 配好嵌入模型；未配置时 `update-embeddings` 会报错，错误正文会显示在页面状态条里。

**Q：流式没有逐字效果？**
部分部署的 `stream-chat` 被代理缓冲。此时前端会自动退回一次性 `/api/chat`，回答仍然完整。
