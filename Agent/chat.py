#!/usr/bin/env python3
"""多轮大模型问答示例 - 从 config.ini 读取配置，调用 OpenAI 兼容接口"""

import configparser
from openai import OpenAI

# 读取配置
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

base_url = config['llm']['base_url']
model_name = config['llm']['model_name']
api_key = config['llm']['api_key']

# 初始化客户端
client = OpenAI(base_url=base_url, api_key=api_key)

# 存储聊天记录
messages = []

# 无限循环
while True:
    # 获取用户输入
    prompt = input("请输入提示词：")

    # 添加用户消息到历史记录
    messages.append({"role": "user", "content": prompt})

    print("---")

    # 调用大模型（流式输出）
    stream = client.chat.completions.create(
        model=model_name,
        messages=messages,
        stream=True
    )

    # 收集完整回答
    full_response = ""

    # 边生成边打印
    for chunk in stream:
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
            full_response += content
    print()  # 换行

    # 添加助手回复到历史记录
    messages.append({"role": "assistant", "content": full_response})
