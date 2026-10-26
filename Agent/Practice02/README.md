# 文件管理 Agent（chat.py）架构说明

本项目是一个基于 OpenAI SDK（DeepSeek 接口）的 **Function Calling 文件管理 Agent**。
`chat.py` 采用「声明工具 → 模型决策 → 本地分发执行 → 结果回灌」的经典 Agent 循环，
并用 `safe_path()` 做统一的路径越界防护（沙箱限定在项目根目录内）。

## 一、架构流程图

```mermaid
flowchart TB

%% ================= 1. 主 Agent 循环 =================
subgraph MAIN["① 主 Agent 循环层 __main__"]
    direction TB
    IN["用户输入<br/>input 请输入指令"]
    MSG["messages 追加<br/>role = user"]
    REQ["调用大模型<br/>client.chat.completions.create<br/>model = deepseek-flash<br/>tools = tools"]
    SLEEP1["time.sleep 0.8<br/>限流缓冲"]
    HAS{"模型返回<br/>tool_calls ?"}
    FINAL["打印最终回答 content<br/>assistant 消息入历史"]
    APPEND_A["assistant 消息入历史<br/>保留 tool_calls 原始 id"]
    EXEC["逐个遍历 tool_calls<br/>执行工具"]
    APPEND_T["tool 消息入历史<br/>tool_call_id + 执行结果"]
    ROUND{"本轮工具调用<br/>已达 10 轮 ?"}
    ABORT["提示：工具调用轮数过多，已停止"]
    EXC["异常捕获<br/>打印 请求出错"]
end

%% ================= 2. 工具分发层 =================
subgraph DISPATCH["② 工具分发层"]
    RT["run_tool name, arguments<br/>json.loads 解析参数"]
    SW{"按 name<br/>if / elif 路由"}
    NF["返回：工具不存在"]
end

%% ================= 3. 工具声明层 =================
subgraph SCHEMA["③ 工具声明层 tools"]
    TS["10 个 function call 定义<br/>name / description / parameters<br/>符合 JSON Schema"]
end

%% ================= 4. 路径安全层 =================
subgraph SAFE["④ 路径安全层"]
    BASE["BASE_DIR = os.path.abspath 当前项目根"]
    SP{"safe_path path<br/>拼接并规范化绝对路径"}
    OK["返回 full_path"]
    DENY["返回 None + 禁止访问项目以外的路径"]
end

%% ================= 5. 工具实现层 =================
subgraph TOOLS["⑤ 文件工具实现层 10 个 os 操作"]
    direction LR
    T1["get_cwd<br/>获取工作目录"]
    T2["list_dir<br/>列出目录"]
    T3["get_file_type<br/>取文件后缀"]
    T4["read_text_file<br/>仅文本 拒读二进制"]
    T5["create_file<br/>自动创建父目录"]
    T6["edit_text_file<br/>覆盖写入内容"]
    T7["make_dir<br/>创建目录"]
    T8["remove_dir<br/>仅允许删空目录"]
    T9["remove_file<br/>删除文件"]
    T10["rename_item<br/>重命名文件或目录"]
end

%% ================= 6. 文件系统 =================
subgraph FS["⑥ 文件系统沙箱"]
    OS[("项目根目录内<br/>文件 / 目录")]
end

%% ---------------- 主循环连线 ----------------
IN --> MSG --> REQ --> SLEEP1 --> HAS
HAS -- "否 最终回答" --> FINAL
FINAL -- "进入下一轮对话" --> IN
HAS -- "是 需要调用工具" --> APPEND_A --> EXEC --> APPEND_T --> ROUND
ROUND -- "未达上限 继续推理" --> REQ
ROUND -- "达到上限 强制结束" --> ABORT
ABORT --> IN

%% ---------------- 分发连线 ----------------
EXEC --> RT --> SW
SW -- "命中工具名" --> TOOLS
SW -- "未命中" --> NF
NF -- "作为 tool 结果返回" --> APPEND_T

%% ---------------- 声明 / 安全 / 依赖连线 ----------------
TS -. "随请求一起发送给模型" .-> REQ
TOOLS --> SP
SP -- "合法 基于 BASE_DIR 前缀校验" --> OK
SP -- "非法 越界访问" --> DENY
DENY -- "错误字符串回灌模型" --> APPEND_T
OK --> OS
T1 -. "无需路径校验" .-> OS
REQ -. "网络或解析异常" .-> EXC
EXEC -. "工具内部异常" .-> EXC
EXC --> IN

%% ---------------- 样式 ----------------
classDef llm fill:#e8f0fe,stroke:#4285f4,stroke-width:2px,color:#1a237e
classDef safe fill:#fff3e0,stroke:#fb8c00,stroke-width:2px,color:#e65100
classDef tool fill:#e8f5e9,stroke:#43a047,stroke-width:1.5px,color:#1b5e20
classDef io fill:#f3e5f5,stroke:#8e24aa,stroke-width:1.5px,color:#4a148c
class IN,MSG,REQ,SLEEP1,HAS,FINAL,APPEND_A,ROUND,ABORT,TS llm
class BASE,SP,OK,DENY safe
class RT,SW,NF,T1,T2,T3,T4,T5,T6,T7,T8,T9,T10 tool
class EXEC,APPEND_T,EXC,OS io
```

## 二、分层说明

| 层次 | 代码位置 | 职责 |
| --- | --- | --- |
| ① 主 Agent 循环层 | `if __name__ == "__main__":` 之内 | 维护 `messages` 多轮上下文，请求模型，判断 `tool_calls`，把工具结果回灌后再请求模型，最多 10 轮防止死循环 |
| ② 工具分发层 | `run_tool(name, arguments)` | 用 `json.loads` 解析模型给出的参数，按函数名 `if/elif` 路由到具体实现 |
| ③ 工具声明层 | `tools = [...]` | 以 OpenAI function call 格式向模型描述 10 个工具的入参 Schema |
| ④ 路径安全层 | `BASE_DIR` + `safe_path(path)` | 把相对路径拼接成绝对路径，校验是否仍在项目根目录内，越界直接拒绝 |
| ⑤ 工具实现层 | 10 个 `def` 函数 | 真正执行 `os` 层文件/目录操作，并返回字符串结果或错误信息 |
| ⑥ 文件系统沙箱 | 项目根目录 | Agent 可操作的真实文件系统范围 |

## 三、10 个工具清单

| # | 工具名 | 入参 | 关键行为 |
| --- | --- | --- | --- |
| 1 | `get_cwd` | 无 | 返回 `os.getcwd()`，本身不做路径校验 |
| 2 | `list_dir` | `path`（默认 `.`） | 列出目录下所有文件与子目录 |
| 3 | `get_file_type` | `path` | 返回文件后缀名 |
| 4 | `read_text_file` | `path` | UTF-8 读取；`.jpg/.png/.gif/.exe/.zip/.rar` 等二进制直接拒绝 |
| 5 | `create_file` | `path` | 自动 `makedirs` 父目录后创建空文件 |
| 6 | `edit_text_file` | `path, content` | 覆盖写入文本内容（非追加） |
| 7 | `make_dir` | `path` | `makedirs(exist_ok=True)` 创建目录 |
| 8 | `remove_dir` | `path` | 仅删除空目录，非空目录拒绝删除 |
| 9 | `remove_file` | `path` | 删除指定文件 |
| 10 | `rename_item` | `old_path, new_path` | 重命名文件/目录，`try/except` 捕获同名冲突等异常 |

## 四、关键设计要点

1. **协议顺序不可颠倒**：必须先把带 `tool_calls` 的 `assistant` 消息写回 `messages`，再追加 `role = tool` 的响应，
   否则接口会判定 tool 消息没有对应的调用而报错。
2. **双保险的结束条件**：`HAS` 判断模型不再请求工具即输出答案；同时用 `for _ in range(10)` 限制单轮最多 10 次工具往返。
3. **错误也是数据**：所有工具把异常转成字符串返回给模型（如「目录不为空，禁止删除」），
   让 Agent 能自行纠正并继续推理，而不是让程序崩溃。
4. **上下文完整**：最终回答也会 `append` 进 `messages`，保证多轮对话连贯。
5. **限流缓冲**：每次模型请求后 `time.sleep(0.8)`，降低触发 API 频率限制的概率。
6. **安全边界**：`safe_path` 只做前缀校验，属于轻量沙箱；若要更强隔离，可改用 `os.path.commonpath`
   或 `Path.is_relative_to()`，并补充符号链接解析（`os.path.realpath`）校验。

> ⚠️ 安全提示：源码中 `api_key` 为明文硬编码，建议改为读取环境变量或 `config.ini`，并注意不要提交到公开仓库。
