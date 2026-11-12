# PetHospitalMCP

把本地「宠物医院」REST 服务封装成 MCP Server，供 xAgent 之类的 MCP 客户端调用。
传输方式：Streamable HTTP，默认监听 `http://127.0.0.1:9091/mcp`。

## 1. 工具

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `list_pets` | 查询档案列表，支持筛选、排序、分页 | `species` `name` `doctor` `disease` `status` `owner_name` `owner_phone` `min_cost` `max_cost` `sort_by` `order` `page` `page_size` |
| `get_pet_detail` | 单只宠物的完整信息 + 历史病历 + 费用汇总 | `pet_id`（如 `PET-000001`） |
| `search_pets` | 跨字段全文检索（含病历全文），空格分词 AND | `query` `page` `page_size` |
| `get_hospital_stats` | 经营统计：档案数、总收入、客单价、种类/医生排行 | 无 |

`page_size` 上限 100，`order` 只接受 `asc`/`desc`，`status` 限定在
`待就诊` / `就诊中` / `住院中` / `已康复` / `慢性病随访`；越界参数直接返回
`VALIDATION_ERROR`，不会打到上游。

## 2. 前置条件

上游是仓库里的 `PetHospitalServer`（Go 单文件程序），默认监听 `http://127.0.0.1:8080`。

```bash
# Windows：直接双击或命令行启动
PetHospitalServer\pethospital.exe

# 确认端口通了
curl http://127.0.0.1:8080/health
```

> 仓库里的 `pethospital.exe` 是 Windows 可执行文件。Linux/macOS 需要在上游目录用
> `go build` 自行编译，或把 `PET_HOSPITAL_URL` 指向已在运行的实例。

## 3. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `PET_HOSPITAL_URL` | `http://127.0.0.1:8080` | 上游地址 |
| `PET_HOSPITAL_TIMEOUT` | `10` | 单次请求超时（秒） |
| `PET_HOSPITAL_RETRIES` | `2` | 失败重试次数（指数退避 0.3s / 0.6s） |
| `MCP_HOST` | `127.0.0.1` | MCP Server 监听地址 |
| `MCP_PORT` | `9091` | MCP Server 端口 |

模板见 `.env.example`，复制成 `.env` 后自行修改。

## 4. 启动

```bash
pip install -r requirements.txt
python main.py
# 日志会打印：upstream / timeout / retries / 已注册的工具列表
```

## 5. 返回格式

成功：

```json
{"ok": true, "data": {...}, "meta": {"page": 1, "page_size": 10}}
```

失败：

```json
{"ok": false, "error": {"code": "UPSTREAM_UNAVAILABLE", "message": "...", "retryable": true}}
```

| 错误码 | 含义 | 是否重试 |
|---|---|---|
| `VALIDATION_ERROR` | 参数不合法，本地拦截 | 否 |
| `NOT_FOUND` | 上游 404，档案不存在 | 否 |
| `UPSTREAM_REJECTED` | 上游 4xx，带错误正文 | 否 |
| `UPSTREAM_ERROR` | 上游 5xx | 是 |
| `UPSTREAM_UNAVAILABLE` | 连接失败 / 超时，重试耗尽 | 是 |
| `UPSTREAM_FORMAT` | 上游返回非 JSON | 否 |

## 6. 验证

```bash
python tests/run_smoke.py
```

测试会拉起一个模拟上游，覆盖 18 项：四个工具的正常路径、五种参数校验、
404 收敛、上游不可达的重试与耗时。

## 7. 日志

每条请求打一行结构化日志，便于排查：

```
2026-11-12 16:05:12,331 INFO     pet-hospital-mcp request method=GET path=/api/v1/pets status=200 elapsed_ms=12.4 attempt=1
```

失败会打 `request_failed`，带异常类型与已耗时。
