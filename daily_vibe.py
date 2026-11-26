"""每日心情底色（小气象）：纯本地 0-token 轻量状态模块。

每天醒来按当前九维驱动轻度加权掷一次当日底色，只微调说话味道，
不篡改九维，也不在生气/害怕/回避或高压/高疲惫时强行出戏。

0.4.6 起：若昨晚作息时间内被反复吵醒（总计 3–4 次 / ≥ 5 次），
次日自动投射为睡眠受扰专属气象（阴天·没睡好 / 小雨·困倦低气压），
并按说话人视角（鱼主 vs 群友）分账表达脾气，双向绝不迁怒。
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
    # 睡眠受扰专属气象：weight=0 平时不进入随机池，仅昨晚作息期被反复吵醒时投射
    "overcast": {
        "key": "overcast",
        "label": "阴天·没睡好",
        "short": "阴天",
        "hint": "昨晚作息时间里被反复吵醒没睡踏实，今天精神有点蔫、脑子慢半拍，说话偏短偏懒，偶尔打哈欠或轻轻念叨困（不是生气）",
        "weight": 0.0,
        "disturbed": True,
    },
    "drizzle": {
        "key": "drizzle",
        "label": "小雨·困倦低气压",
        "short": "小雨",
        "hint": "昨晚作息时间里被吵醒太多次、几乎没睡好，今天整个人很倦、精力不足，带着点没睡饱的低气压和懒散小怨气，话少慢半拍，哄一哄或让着点会好受些",
        "weight": 0.0,
        "disturbed": True,
    },
}


def get_vibe(key: str) -> dict[str, Any] | None:
    meta = VIBES.get(str(key or "").strip())
    return dict(meta) if meta else None


def is_disturbed_vibe(key: str) -> bool:
    meta = VIBES.get(str(key or "").strip())
    return bool(meta and meta.get("disturbed"))


def pick_daily_vibe(
    drives: dict[str, Any] | None = None,
    *,
    prev_key: str = "",
    exclude_key: str = "",
    rng: random.Random | None = None,
    woken_level: str = "",
) -> dict[str, Any]:
    """按当前九维轻度加权抽取今日小气象；昨夜反复吵醒且非手动重掷时投射专属气象。"""
    wl = str(woken_level or "").strip().lower()
    if not exclude_key and wl == "heavy":
        return dict(VIBES["drizzle"])
    if not exclude_key and wl == "mild":
        return dict(VIBES["overcast"])

    d = drives or {}
    att = float(d.get("attachment", 50.0) or 50.0)
    joy = float(d.get("joy", 50.0) or 50.0)
    fatigue = float(d.get("fatigue", 20.0) or 20.0)

    pool: list[tuple[dict[str, Any], float]] = []
    for key, meta in VIBES.items():
        if meta.get("disturbed"):
            continue
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


def is_vibe_suppressed(
    mode: str,
    drives: dict[str, Any] | None = None,
    *,
    vibe_key: str = "",
) -> bool:
    """生气/害怕/回避，或压力/疲惫过高时自动隐身，避免出戏。

    睡眠受扰专属气象（阴天/小雨）本身就表达困倦低气压，适当放宽疲惫/压力隐身线，
    避免刚醒来抬升的疲惫把专属气象自己压掉。
    """
    if str(mode or "calm") not in ("calm", "wronged"):
        return True
    d = drives or {}
    stress = float(d.get("stress", 0.0) or 0.0)
    fatigue = float(d.get("fatigue", 0.0) or 0.0)
    if is_disturbed_vibe(vibe_key):
        if stress >= 65.0 or fatigue >= 85.0:
            return True
        return False
    if stress >= 60.0:
        return True
    if fatigue >= 70.0:
        return True
    return False


def build_vibe_injection(
    vibe_key: str,
    mode: str,
    drives: dict[str, Any] | None = None,
    *,
    speaker_role: str = "owner",
    owner_woken: int = 0,
    outsider_woken: int = 0,
) -> tuple[str, str]:
    """生成注入进心境块的单行当日底色与稳定签名；被压制时返回空串。

    当今日为睡眠受扰气象（overcast / drizzle）时，按说话人视角（owner / outsider）
    与昨夜各方吵醒次数分别给出口气：谁吵的脾气就对着谁，双向绝不迁怒。
    """
    if is_vibe_suppressed(mode, drives, vibe_key=vibe_key):
        return "", ""
    meta = get_vibe(vibe_key)
    if not meta:
        return "", ""

    key = str(meta["key"])
    label = str(meta["label"])
    if not meta.get("disturbed"):
        return key, f"今日底色（{label}）：{meta['hint']}。"

    ow = max(0, int(owner_woken or 0))
    outw = max(0, int(outsider_woken or 0))
    role = str(speaker_role or "owner")

    if role == "outsider":
        if outw >= 3:
            detail = (
                f"昨晚作息时间里在群里被反复吵醒了 {outw} 次、严重没睡好，"
                "今天对着群里带着明显没睡饱的低气压和起床气，语气偏冷偏冲、嫌吵不想多搭理"
            )
            return f"{key}:out:heavy", f"今日底色（{label}）：{detail}。"
        if outw >= 2:
            detail = (
                "昨晚作息时间里在群里老被吵醒没睡踏实，今天精神有点蔫，"
                "对着群里带着没睡好的微烦和懒散小脾气，回复简短一点、别太热情"
            )
            return f"{key}:out:mild", f"今日底色（{label}）：{detail}。"
        # 群友昨晚几乎没吵：绝不把对鱼主的怨气迁怒到群友身上，只保留客观犯困
        detail = (
            "昨晚没睡踏实今天有点困、精神偏懒，回复简短平和一点就好，不要把火撒到无关的人身上"
        )
        return f"{key}:out:calm", f"今日底色（{label}）：{detail}。"

    # role == "owner"：面对鱼主
    if ow >= 5:
        detail = (
            f"昨晚作息时间里被他反复吵醒了 {ow} 次、几乎没睡好，"
            "今天整个人很倦、低气压且带着委屈和没睡饱的小怨气，话短闷闷的，需要他好好哄一哄"
        )
        return f"{key}:own:heavy", f"今日底色（{label}）：{detail}。"
    if ow >= 3:
        detail = (
            f"昨晚作息时间里被他吵醒了 {ow} 次没睡踏实，今天精神有点蔫，"
            "对着他带着轻度小起床气和轻声嘟囔抱怨（哄一句就软下来，不是真的生气）"
        )
        return f"{key}:own:mild", f"今日底色（{label}）：{detail}。"
    if outw >= 2:
        if ow >= 2:
            detail = (
                "昨晚群里太吵老被别人弄醒，他自己也吵醒过你两次，今天整个人很困没精神；"
                "不要把群里别人的火撒到他头上，对着他最多轻声嘟囔一句没睡好、想被哄哄"
            )
            return f"{key}:own:mix", f"今日底色（{label}）：{detail}。"
        detail = (
            "昨晚在群里老被别人吵醒没睡踏实，今天有点蔫犯困；"
            "不是怪他，绝不要把别人的火撒到他头上，对着他只是没精神、想靠着撒娇"
        )
        return f"{key}:own:out", f"今日底色（{label}）：{detail}。"
    return f"{key}:own", f"今日底色（{label}）：{meta['hint']}。"
