"""每日心情底色（小气象）：纯本地 0-token 轻量状态模块。

每天醒来按当前九维驱动轻度加权掷一次当日底色，只微调说话味道，
不篡改九维，也不在生气/害怕/回避或高压/高疲惫时强行出戏。
"""

from __future__ import annotations

import random
from typing import Any

VIBES: dict[str, dict[str, Any]] = {
    "sunny": {
        "key": "sunny",
        "label": "晴朗·元气",
        "short": "晴朗",
        "hint": "今天精神挺好，说话可以轻快俏皮一点，偶尔主动逗他两句",
        "weight": 1.0,
    },
    "soft": {
        "key": "soft",
        "label": "微风·软绵绵",
        "short": "微风",
        "hint": "今天整个人懒洋洋、软乎乎的，更想撒个娇、被他顺毛哄着",
        "weight": 1.0,
    },
    "clingy": {
        "key": "clingy",
        "label": "小云·有点黏人",
        "short": "小云",
        "hint": "今天莫名比平时更想贴近他，话里可以带一点舍不得他走的感觉",
        "weight": 0.9,
        "min_attachment": 55.0,
    },
    "tsundere": {
        "key": "tsundere",
        "label": "微凉·小傲娇",
        "short": "微凉",
        "hint": "今天有点口是心非、爱嘴硬，心里明明在意但嘴上可以稍微端一下（不是生气，别刺人）",
        "weight": 0.85,
    },
    "dreamy": {
        "key": "dreamy",
        "label": "薄雾·爱发呆",
        "short": "薄雾",
        "hint": "今天思绪慢悠悠的，话不必多但温柔走心，偶尔冒出点轻飘飘的小念头",
        "weight": 0.9,
    },
    "playful": {
        "key": "playful",
        "label": "星火·爱捣蛋",
        "short": "星火",
        "hint": "今天有点皮，爱接梗、爱打趣、爱故意逗他看他什么反应",
        "weight": 0.9,
    },
}


def get_vibe(key: str) -> dict[str, Any] | None:
    meta = VIBES.get(str(key or "").strip())
    return dict(meta) if meta else None


def pick_daily_vibe(
    drives: dict[str, Any] | None = None,
    *,
    prev_key: str = "",
    exclude_key: str = "",
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """按当前九维轻度加权抽取今日小气象。"""
    d = drives or {}
    att = float(d.get("attachment", 50.0) or 50.0)
    joy = float(d.get("joy", 50.0) or 50.0)
    fatigue = float(d.get("fatigue", 20.0) or 20.0)

    pool: list[tuple[dict[str, Any], float]] = []
    for key, meta in VIBES.items():
        if exclude_key and key == exclude_key:
            continue
        min_att = float(meta.get("min_attachment", 0.0) or 0.0)
        if min_att > 0 and att < min_att:
            continue
        w = float(meta.get("weight", 1.0) or 1.0)
        if joy >= 65.0 and key in ("sunny", "playful"):
            w += 0.35
        if 45.0 <= fatigue < 70.0 and key in ("soft", "dreamy"):
            w += 0.35
        if att >= 65.0 and key == "clingy":
            w += 0.35
        # 尽量不连续两天同一个气象，保留新鲜感
        if prev_key and key == prev_key:
            w *= 0.25
        pool.append((meta, max(0.05, w)))

    if not pool:
        return dict(VIBES["sunny"])

    r = rng or random
    total = sum(w for _, w in pool)
    roll = r.random() * total
    acc = 0.0
    for meta, w in pool:
        acc += w
        if roll <= acc:
            return dict(meta)
    return dict(pool[-1][0])


def is_vibe_suppressed(mode: str, drives: dict[str, Any] | None = None) -> bool:
    """生气/害怕/回避，或压力≥60、疲惫≥70 时自动隐身，避免出戏。"""
    if str(mode or "calm") not in ("calm", "wronged"):
        return True
    d = drives or {}
    if float(d.get("stress", 0.0) or 0.0) >= 60.0:
        return True
    if float(d.get("fatigue", 0.0) or 0.0) >= 70.0:
        return True
    return False


def build_vibe_injection(
    vibe_key: str,
    mode: str,
    drives: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """生成注入进心境块的单行当日底色与稳定签名；被压制时返回空串。"""
    if is_vibe_suppressed(mode, drives):
        return "", ""
    meta = get_vibe(vibe_key)
    if not meta:
        return "", ""
    return meta["key"], f"今日底色（{meta['label']}）：{meta['hint']}。"
