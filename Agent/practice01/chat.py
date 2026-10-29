#!/usr/bin/env python3
"""单轮大模型问答示例 - 从 config.ini 读取配置，调用 OpenAI 兼容接口"""

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

# 获取用户输入
prompt = input("请输入提示词：")

print("---")

# 调用大模型
response = client.chat.completions.create(
    model=model_name,
    messages=[{"role": "user", "content": prompt}]
)

# 打印回答
print(response.choices[0].message.content)
