import os
import json
import time
from openai import OpenAI

# 项目根目录，防止越界访问外部文件夹
BASE_DIR = os.path.abspath(".")

# 路径安全校验：只能在当前项目文件夹内操作
def safe_path(path):
    full_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not full_path.startswith(BASE_DIR):
        return None, "禁止访问项目以外的路径！"
    return full_path, ""

# ---------------------- 10个文件工具函数 ----------------------
# 1. 获取当前工作目录
def get_cwd():
    return os.getcwd()

# 2. 列出目录内容
def list_dir(path="."):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    try:
        return str(os.listdir(full_path))
    except Exception as e:
        return str(e)

# 3. 获取文件类型（后缀名）
def get_file_type(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    ext = os.path.splitext(full_path)[1]
    return f"文件后缀：{ext}"

# 4. 读取纯文本文件，二进制文件拒绝读取
def read_text_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    # 只允许文本格式
    suffix = os.path.splitext(full_path)[1]
    binary_suffix = [".jpg", ".png", ".gif", ".exe", ".zip", ".rar"]
    if suffix in binary_suffix:
        return "非文本文件，禁止读取"
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return str(e)

# 5. 创建文件，自动创建父文件夹
def create_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    parent_dir = os.path.dirname(full_path)
    os.makedirs(parent_dir, exist_ok=True)
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            pass
        return "文件创建成功"
    except Exception as e:
        return str(e)

# 6. 编辑文本文件内容
def edit_text_file(path, content):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return "内容修改成功"
    except Exception as e:
        return str(e)

# 7. 创建文件夹
def make_dir(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    os.makedirs(full_path, exist_ok=True)
    return "目录创建成功"

# 8. 删除空目录，非空目录不让删
def remove_dir(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isdir(full_path):
        return "目录不存在"
    if len(os.listdir(full_path)) > 0:
        return "目录不为空，禁止删除"
    os.rmdir(full_path)
    return "空目录删除成功"

# 9. 删除文件
def remove_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    os.remove(full_path)
    return "文件删除成功"

# 10. 重命名文件/文件夹（加异常捕获）
def rename_item(old_path, new_path):
    old_full, msg = safe_path(old_path)
    new_full, msg2 = safe_path(new_path)
    if not old_full or not new_full:
        return "路径非法"
    try:
        os.rename(old_full, new_full)
        return "重命名完成"
    except Exception as e:
        return f"重命名失败：{str(e)}"

# ---------------------- 工具定义（function call格式） ----------------------
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_cwd",
            "description": "获取当前工作目录",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录下所有文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_type",
            "description": "获取指定文件的文件类型",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "读取纯文本文件，二进制文件拒绝读取",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "新建文件，自动创建父目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_text_file",
            "description": "编辑纯文本文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "创建新目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_dir",
            "description": "只删除空目录，非空目录禁止删除",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_file",
            "description": "删除指定文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_item",
            "description": "重命名文件或者目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string"},
                    "new_path": {"type": "string"}
                },
                "required": ["old_path", "new_path"]
            }
        }
    }
]

# 工具分发调用
def run_tool(name, arguments):
    args = json.loads(arguments)
    if name == "get_cwd":
        return get_cwd()
    elif name == "list_dir":
        return list_dir(args.get("path", "."))
    elif name == "get_file_type":
        return get_file_type(args["path"])
    elif name == "read_text_file":
        return read_text_file(args["path"])
    elif name == "create_file":
        return create_file(args["path"])
    elif name == "edit_text_file":
        return edit_text_file(args["path"], args["content"])
    elif name == "make_dir":
        return make_dir(args["path"])
    elif name == "remove_dir":
        return remove_dir(args["path"])
    elif name == "remove_file":
        return remove_file(args["path"])
    elif name == "rename_item":
        return rename_item(args["old_path"], args["new_path"])
    else:
        return "工具不存在"

# 主Agent对话逻辑
if __name__ == "__main__":
    client = OpenAI(
        api_key="sk-d27d4449de5c47acb39f09bfeecb41f0",
        base_url="https://api.deepseek.com"
    )
    messages = []
    while True:
        user_msg = input("请输入指令：")
        messages.append({"role": "user", "content": user_msg})
        try:
            # 工具调用可能有多轮，循环直到模型给出最终回答
            for _ in range(10):
                resp = client.chat.completions.create(
                    model="deepseek-flash",
                    messages=messages,
                    tools=tools
                )
                time.sleep(0.8) # 限流缓冲
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    print(msg.content)
                    # 把最终回答也存入历史，保证多轮对话上下文完整
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    break
                # 必须先把带 tool_calls 的 assistant 消息放回历史，
                # 否则后续 tool 消息会被接口判定为无对应调用
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments
                            }
                        }
                        for call in msg.tool_calls
                    ]
                })
                for call in msg.tool_calls:
                    res = run_tool(call.function.name, call.function.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": res
                    })
                time.sleep(0.8)
            else:
                print("工具调用轮数过多，已停止本轮请求")
        except Exception as err:
            print(f"请求出错：{err}")
