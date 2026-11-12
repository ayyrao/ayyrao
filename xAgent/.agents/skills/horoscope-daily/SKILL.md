---
name: horoscope-daily
description: >-
  查询十二星座今日运势，生成综合指数、爱情/事业/财运评分、幸运色、幸运数字
  与宜忌建议。当用户询问“今日运势”“星座运势”“水逆”等话题时使用。
license: MIT
metadata:
  author: xAgent demo
  version: "1.0"
---

# Horoscope Daily（今日星座运势）

本技能演示 xAgent 的 Agent Skills 动态加载机制：初始化时只把上面的
name / description 注入 System Prompt，你正在阅读的正文只有在 LLM 调用
`loadSkill` 后才会进入上下文。

## 何时使用

- 用户询问某个星座今天的运势、幸运色、幸运数字、宜忌；
- 用户想比较两个星座的今日状态；
- 用户提到“水逆”“星象”等话题，需要一份轻松的娱乐向解读。

## 工作流程

1. **确定星座**：从用户消息中识别星座（白羊座、金牛座……双鱼座）。
   用户没有说明时，先追问一次，不要自行猜测。
2. **读取详细规则（可选）**：需要评分口径或文案风格时调用：

   ```json
   {"name": "horoscope-daily", "filename": "references/rules.md"}
   ```

   需要输出示例时读取 `references/examples.md`。
3. **生成今日卡片**：优先用现有 `RunCli` 工具运行技能自带脚本：

   ```json
   {"command": "python", "args": [".agents/skills/horoscope-daily/scripts/horoscope.py", "--sign", "白羊座"]}
   ```

   脚本返回 JSON（综合指数、幸运色、幸运数字、宜、忌）。脚本是确定性的：
   同一星座同一天结果一致。
4. **结合用户上下文润色**：把脚本输出改写成 80～150 字的中文解读，
   保留“娱乐向、非科学预测”的提示。
5. **需要实时星象新闻**：可先调用 `searchWeb` 检索，再用 `fetchUrl` 阅读
   来源页面；引用时给出链接。

## 输出格式

```
【白羊座 · 2026-10-30】
综合指数：★★★★☆ 78
幸运色：琥珀橙    幸运数字：7
宜：整理待办、主动沟通
忌：冲动消费
一句话：先完成手头的小事，节奏会顺起来。
（娱乐向内容，仅供参考）
```

## 注意事项

- 不使用恐吓性、绝对化表述；
- 不提供医疗、投资、法律等专业建议；
- 星象信息与用户隐私无关时，不要追问生日等敏感信息。

## 附件

| 路径 | 用途 |
| --- | --- |
| `references/rules.md` | 评分与文案规则 |
| `references/examples.md` | 输出示例 |
| `scripts/horoscope.py` | 确定性运势卡片生成脚本 |
