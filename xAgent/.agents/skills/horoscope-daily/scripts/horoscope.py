#!/usr/bin/env python3
"""今日星座运势卡片生成器（horoscope-daily 技能附件）。

用法：
    python .agents/skills/horoscope-daily/scripts/horoscope.py --sign 白羊座
    python .agents/skills/horoscope-daily/scripts/horoscope.py --sign 金牛座 --date 2026-10-30

输出：UTF-8 JSON，字段固定，便于 LLM 直接读取后组织自然语言回答。
结果只由「星座 + 日期」决定，同一天重复调用保持一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date

SIGNS = [
    "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座",
    "天秤座", "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座",
]

COLORS = [
    "琥珀橙", "松石绿", "雾霾蓝", "珊瑚粉", "柠檬黄",
    "月光银", "薰衣草紫", "深海蓝", "砖红", "奶油白",
]

GOOD = [
    "整理待办", "主动沟通", "慢慢推进", "复盘计划", "散步换气",
    "读几页书", "提前收尾", "给自己留白",
]

BAD = [
    "冲动消费", "临时改计划", "熬夜刷屏", "过度承诺", "情绪化回复",
    "同时开太多任务", "纠结细节", "硬撑社交",
]

TIPS = [
    "先完成手头的小事，节奏会顺起来。",
    "把注意力放回自己能控制的部分。",
    "今天适合减法：少做一件，反而更稳。",
    "遇到卡点时先停三分钟，再决定下一步。",
    "把想法写下来，答案会比想象中清楚。",
    "主动开口前先听一句，沟通会轻松很多。",
]


def _seed(sign: str, day: str) -> int:
    digest = hashlib.sha256(f"{sign}|{day}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _pick(items: list[str], seed: int, salt: int) -> str:
    return items[(seed // (salt + 7)) % len(items)]


def build_card(sign: str, day: str) -> dict[str, object]:
    seed = _seed(sign, day)
    overall = 55 + seed % 41  # 55 ~ 95
    stars = 3 if overall < 65 else (4 if overall < 80 else 5)
    return {
        "sign": sign,
        "date": day,
        "overall": overall,
        "stars": stars,
        "love": 50 + (seed // 11) % 50,
        "career": 50 + (seed // 23) % 50,
        "wealth": 50 + (seed // 37) % 50,
        "lucky_color": _pick(COLORS, seed, 3),
        "lucky_number": 1 + seed % 9,
        "good_for": _pick(GOOD, seed, 5),
        "avoid": _pick(BAD, seed, 7),
        "tip": _pick(TIPS, seed, 9),
        "disclaimer": "娱乐向内容，仅供参考",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成今日星座运势卡片（JSON）")
    parser.add_argument("--sign", required=True, help="中文星座名，如 白羊座")
    parser.add_argument("--date", default=None, help="ISO 日期，默认今天")
    args = parser.parse_args(argv)

    sign = args.sign.strip()
    if sign not in SIGNS:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_sign",
                        "message": f"未知星座：{sign}",
                        "available": SIGNS,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2

    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_date",
                        "message": f"日期格式应为 YYYY-MM-DD：{args.date}",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {"ok": True, **build_card(sign, day.isoformat())},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
