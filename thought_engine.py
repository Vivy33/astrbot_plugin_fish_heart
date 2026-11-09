"""念头引擎：把"九维驱动"翻译成"她现在想要什么"，被满足时结算心情。

纯函数模块，不依赖 AstrBot，方便单独测试。

念头（v2）在 state.json 里是这样一个 dict：
    {"kind": "praise", "text": "想让他夸一句", "weight": 0.8,
     "created": 1710000000.0, "expires": 1710010800.0,
     "pending": True, "attempts": 0, "mood": ""}

生命周期：
    spawn  → 驱动没被满足，长出一个"想要"
    live   → 注入进本轮请求，影响说话方式；主动开口时尝试一次
    settle → 用户的话/事件把那个"想要"喂饱了 → pending=False，写入 mood
    fade   → 一直没被喂，到点凉掉 → 对应驱动轻微回落（release）
"""

from __future__ import annotations

import random
import time
from typing import Any

DRIVES = (
    "attachment", "curiosity", "reflection", "duty", "social",
    "fatigue", "intimacy", "stress", "joy",
)

# 念头的存活时长（秒）：越"贴身"的念头留得越久
TTL = {
    "praise": 3 * 3600,
    "miss": 4 * 3600,
    "hug": 4 * 3600,
    "talk": 2.5 * 3600,
    "ask_day": 2 * 3600,
    "task": 4 * 3600,
    "quiet": 1.5 * 3600,
    "dwell": 1.2 * 3600,
    "company": 3 * 3600,
    "deep": 4 * 3600,
}

# 旧版（v1）固定的五条念头池 → 新版 kind，迁移时用
OLD_TEXT_TO_KIND = {
    "想问他今天吃了什么": "ask_day",
    "想让他夸一句": "praise",
    "想知道他有没有想我": "miss",
    "想找点事做": "task",
    "想安静一会儿": "quiet",
}


def _g(drives: dict, key: str) -> float:
    return float(drives.get(key, 50.0))


def _calm(mode: str) -> bool:
    return mode == "calm"


KIND_META: dict[str, dict] = {
    "praise": {
        "label": "夸夸",
        "text": "想让他夸一句",
        "hint": "想被夸一句，想被看见",
        "done": "被他夸了，心里甜甜的",
        "reward": {"joy": 6, "intimacy": 4, "attachment": 2},
        "release": {"joy": -1},
        # 明确夸奖和含蓄夸奖（"小棉袄""贴心""真乖"）都算把她夸到了
        "events": ("praise", "praise_soft"),
        "trigger": lambda d, e, w: (
            1.0 if _g(d, "attachment") >= 40 and _g(d, "joy") < 78 else 0.0
        ),
    },
    "miss": {
        "label": "想他",
        "text": "想知道他有没有想我",
        "hint": "想知道他有没有想你",
        "done": "听他说想我了，悬着的心放下了",
        "reward": {"attachment": 5, "intimacy": 3, "expectation": -100},
        "release": {"attachment": -2, "expectation": 1},
        # 只由"想你 / 想我 / 有没有想我 / 惦记"这类原话满足（见 KEYWORD_KINDS）。
        # 原来还挂了 ("seeking",)：他说"我委屈/我难受"就会把这条判成实现，
        # mood 写"听他说想我了"——可他从头到尾没提想她。
        "events": (),
        "trigger": lambda d, e, w: (
            1.2 if _g(d, "attachment") >= 50 and float(e) >= 15 else 0.0
        ),
    },
    "hug": {
        "label": "抱抱",
        "text": "想要抱抱",
        "hint": "有点想被抱住，想撒个娇",
        "done": "被抱了一下，整个人都松了",
        "reward": {"stress": -8, "attachment": 4, "intimacy": 3, "joy": 3},
        "release": {"attachment": -1},
        "events": ("comfort",),
        "trigger": lambda d, e, w: (
            1.1
            if (_g(d, "stress") >= 55 or _g(d, "attachment") >= 58)
            and _g(d, "fatigue") < 72
            else 0.0
        ),
    },
    "talk": {
        "label": "想讲",
        "text": "想把今天的事讲给他听",
        "hint": "攒了些话想讲给他听",
        "done": "跟他说完了，心里轻松",
        "reward": {"social": 5, "joy": 4, "intimacy": 2},
        "release": {"social": -3},
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            0.9 if _g(d, "social") >= 58 and _g(d, "joy") >= 45 else 0.0
        ),
    },
    "ask_day": {
        "label": "问他",
        "text": "想问他今天过得怎么样",
        "hint": "想问问你那边今天怎么样",
        "done": "知道他今天还好好的",
        "reward": {"expectation": -100, "social": 3},
        "release": {"social": -1},
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            0.8 if _g(d, "social") >= 45 and float(e) >= 10 else 0.0
        ),
    },
    "task": {
        "label": "想做事",
        "text": "想找点事做",
        "hint": "想找点能帮上忙的事做",
        "done": "帮上忙了，有点小得意",
        "reward": {"duty": 2, "joy": 3},
        "release": {"duty": -2},
        "events": ("task",),
        # 门槛要比基线（50）高出足够多，否则几次求助就长期锁定在"想做事"
        "trigger": lambda d, e, w: (
            0.9 if _g(d, "duty") >= 65 and _g(d, "fatigue") < 65 else 0.0
        ),
    },
    "quiet": {
        "label": "安静",
        "text": "想安静一会儿",
        "hint": "有点累，想安静一会儿，话可以少一点",
        "done": "安静地待了会儿，缓过来了",
        "reward": {"stress": -6, "fatigue": -4},
        "release": {"fatigue": -1},
        "events": ("quiet",),
        "trigger": lambda d, e, w: (
            1.0 if _g(d, "fatigue") >= 60 or _g(d, "stress") >= 50 else 0.0
        ),
    },
    "dwell": {
        "label": "等回话",
        "text": "在等他回话",
        "hint": "在等你回话，忍不住总看手机",
        "done": "他回我了，心里踏实了",
        "reward": {"expectation": -60, "joy": 2},
        "release": {"expectation": -3},
        "events": ("reply_fast",),
        "trigger": lambda d, e, w: (
            1.0 if w or float(e) >= 40 else 0.0
        ),
    },
    "company": {
        "label": "陪伴",
        "text": "想让他陪一会儿",
        "hint": "想让他放下手里的事，陪你说会儿话",
        "done": "他放下手里的事来陪我了",
        "reward": {"attachment": 3, "intimacy": 3, "expectation": -60},
        "release": {"expectation": -1},
        # 他回一句就算陪了；真要更准就靠 KEYWORD_KINDS 里的"陪你/陪陪"
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            1.05
            if _g(d, "attachment") >= 66
            and _g(d, "intimacy") >= 66
            and float(e) >= 30
            else 0.0
        ),
    },
    "deep": {
        "label": "走心",
        "text": "想问他一件关于我们的事",
        "hint": "想认真问你一件关于你们俩的事，不是查岗",
        "done": "他认真答了，心里踏实",
        "reward": {"reflection": -15, "attachment": 4, "intimacy": 3},
        "release": {"reflection": 2},
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            1.1
            if _g(d, "reflection") >= 55 and _g(d, "attachment") >= 55
            else 0.0
        ),
    },
}

# 从用户原话里直接反推"喂饱了哪个念头"（比事件名细）
# 顺序即优先级：先匹配到的生效
KEYWORD_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("miss", ("想你", "想我", "有没有想我", "惦记")),
    ("hug", ("抱", "贴贴", "搂", "亲亲", "撒娇")),
    ("company", ("陪你", "陪陪", "聊会儿", "说说话")),
    # 和 main.KW_PRAISE 保持一致（这里只做"反推是哪条念头"，
    # 真正的判定在 main._hits_praise；含蓄夸奖靠 praise_soft 事件喂饱）。
    # "真好" 不在这里："真好吃""真好玩"会被误当成夸她。
    ("praise", ("厉害", "真棒", "太棒", "好棒", "优秀", "了不起", "佩服",
                "可爱", "喜欢你", "爱你", "谢谢", "夸夸你", "夸你")),
    ("ask_day", ("过得", "怎么样", "怎么样呀", "顺利", "累不累", "还好吗")),
    ("task", ("帮我", "给你", "安排", "做点", "需要我", "帮个忙")),
    ("talk", ("今天", "跟你说", "告诉你", "我跟你讲")),
    ("quiet", ("歇", "休息", "睡", "安静", "别忙了")),
)

# 面板上的"怎么才会完成"——只做展示，不参与判定。
# 用户反馈"每个念头都没完成"，很多时候是不知道什么话才算数。
THOUGHT_HOW: dict[str, str] = {
    "praise": "夸她一句（厉害/真棒/可爱…）；夸到点子上也算：小棉袄/贴心/真乖",
    "miss": "说想她（想你/想我/惦记）",
    "hug": "哄她或抱抱（抱/贴贴/亲亲/撒娇）",
    "talk": "回她一句就行",
    "ask_day": "回她一句就行",
    "task": "找她帮个忙（帮我/安排…）",
    "quiet": "安静一会儿，疲劳/压力降下来会自动完成",
    "dwell": "及时回她",
    "company": "回她一句，或说陪陪她",
    "deep": "回她一句就行",
}

# 给模型的"怎么给台阶"：只写进注入/主动 prompt，不显示在面板。
# 目标是让她主动递一个能让用户接住的机会，而不是直接索取。
THOUGHT_INDUCE: dict[str, str] = {
    "praise": "想被夸，但别直接要。可以卖个乖、自嘲一句，或者把刚做的一件小事说出来，等他接话；用你自己的话，别照抄固定句子",
    "miss": "想他就轻轻提一句，或者问他今天有没有空理你；别质问、别连问，给他接话的机会",
    "hug": "想被抱就撒个娇、说今天有点累想靠一下；用你平时的语气，别像念台词",
    "talk": "有想讲的就说，没有就别硬找话题；想说的时候先起个头，等他问再说",
    "ask_day": "想关心他就直接问，别绕；他答了就顺着聊，别查岗",
    "task": "想帮忙就问他有没有要搭把手的，别硬找活；没有就别说",
    "quiet": "累了就说想安静一会儿，不用解释；不想说也可以少说两句",
    "dwell": "想催就轻轻问一句在忙吗，别连问；他没回就先做自己的事",
    "company": "想让他陪就直说想聊会儿，或者问他现在方不方便；别绕弯子",
    "deep": "想问就问，但先确认他方便；他不想答就换话题",
}


def match_kind(text: str) -> str | None:
    """从用户一句话里反推她想被喂饱的念头。"""
    t = (text or "").strip()
    if not t:
        return None
    for kind, kws in KEYWORD_KINDS:
        if any(k in t for k in kws):
            return kind
    return None


def new_thought(kind: str, now: float | None = None, weight: float = 0.0) -> dict | None:
    meta = KIND_META.get(kind)
    if not meta:
        return None
    now = time.time() if now is None else now
    return {
        "kind": kind,
        "text": meta["text"],
        "weight": round(float(weight or 1.0), 2),
        "created": now,
        "expires": now + TTL.get(kind, 3 * 3600),
        "pending": True,
        "attempts": 0,
        "mood": "",
        # 这条念头是否已经给过"台阶"提示：每条只提示一次，避免同一句话反复出现
        "induced": False,
    }


def pick_kind(
    drives: dict,
    expectation: float,
    awaiting_reply: bool,
    exclude: tuple[str, ...] = (),
    rng: random.Random | None = None,
    min_weight: float = 0.0,
) -> tuple[str | None, float]:
    """按当下驱动状态加权抽一个念头；全都够不着时返回 (None, 0)。

    min_weight 是"入池门槛"：触发强度低于它的念头不参与抽取。
    各类型的触发强度落在 0–1.2，所以默认 0.2 等于全放行。
    """
    rng = rng or random
    floor = float(min_weight or 0.0)
    cands: list[tuple[str, float]] = []
    for kind, meta in KIND_META.items():
        if kind in exclude:
            continue
        w = float(meta["trigger"](drives, expectation, awaiting_reply))
        if w > 0 and w >= floor:
            cands.append((kind, w))
    if not cands:
        return None, 0.0
    total = sum(w for _, w in cands)
    r = rng.random() * total
    acc = 0.0
    for kind, w in cands:
        acc += w
        if r <= acc:
            return kind, w
    return cands[-1]


def active_kinds(thoughts: list[dict], now: float | None = None) -> tuple[str, ...]:
    now = time.time() if now is None else now
    return tuple(
        str(t.get("kind"))
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    )


def settle(
    thoughts: list[dict],
    events: list[str],
    text: str,
    mode: str,
    now: float | None = None,
    skip_text_kinds: tuple[str, ...] = (),
) -> dict | None:
    """本轮最多喂饱一个念头，返回 {kind, text, mood, reward} 或 None。

    `skip_text_kinds` 用来挡住"文本看着像、实际不算"的情况：
    比如他夸过了头，事件已经是 `praise_bored`，这句话里的"真棒"就不该再
    把「想被夸」喂饱。
    """
    now = time.time() if now is None else now
    evset = set(events or [])
    by_text = match_kind(text)
    if by_text in skip_text_kinds:
        by_text = None
    candidates = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not candidates:
        return None

    def hit(t: dict) -> bool:
        kind = str(t.get("kind"))
        meta = KIND_META.get(kind) or {}
        if by_text and by_text == kind:
            return True
        return bool(evset & set(meta.get("events") or ()))

    # 文本反推优先，其次按"最想要"（weight 高）的喂
    chosen = None
    soft = False
    if by_text:
        chosen = next((t for t in candidates if t.get("kind") == by_text), None)
    if chosen is None:
        hits = [t for t in candidates if hit(t)]
        if hits:
            chosen = max(hits, key=lambda t: float(t.get("weight", 0)))
    if chosen is None:
        # 她开口后他回了：哪怕没对上具体内容，"被听见了"也算半个满足
        if "reply_fast" in evset:
            attempted = [
                t for t in candidates if int(t.get("attempts", 0)) >= 1
            ]
            if attempted:
                chosen = max(attempted, key=lambda t: float(t.get("weight", 0)))
                soft = True
    if chosen is None:
        return None

    kind = str(chosen.get("kind"))
    meta = KIND_META[kind]
    chosen["pending"] = False
    chosen["mood"] = meta["done"]
    chosen["expires"] = now + 2 * 3600
    reward = dict(meta.get("reward") or {})
    if soft:
        reward = {k: round(float(v) * 0.5, 1) for k, v in reward.items()}
    if _calm(mode):
        reward["joy"] = float(reward.get("joy", 0)) + 2
        reward["intimacy"] = float(reward.get("intimacy", 0)) + 1
    return {"kind": kind, "text": meta["text"], "mood": meta["done"], "reward": reward}


def apply_reward(state: dict, payload: dict) -> None:
    """把 settle 的奖励落到 state 上（clamp 0-100）。"""
    drives = state.setdefault("drives", {})
    for key, dv in (payload.get("reward") or {}).items():
        if key == "expectation":
            state["expectation"] = max(
                0.0, min(100.0, float(state.get("expectation", 0.0)) + float(dv))
            )
        elif key in DRIVES:
            drives[key] = max(0.0, min(100.0, float(drives.get(key, 50.0)) + float(dv)))
    entry = {
        "kind": payload.get("kind"),
        "mood": payload.get("mood"),
        "ts": time.time(),
        "src": "settle",
    }
    # 最近完成改成列表：2 小时内的多次完成都要能显示，不能互相覆盖
    rows = state.get("recent_rewards")
    if not isinstance(rows, list):
        rows = []
    rows.append(entry)
    state["recent_rewards"] = rows[-8:]


def note_attempt(thoughts: list[dict], now: float | None = None) -> dict | None:
    """主动开口时记一次尝试；连续三次没被接住就放弃这条念头。

    放弃时返回 {kind, mood, ts}，给面板的「最近放下」用；普通尝试返回 None。
    """
    now = time.time() if now is None else now
    for t in thoughts or []:
        if not t.get("pending", True):
            continue
        if float(t.get("expires", 0)) <= now:
            continue
        t["attempts"] = int(t.get("attempts", 0)) + 1
        if int(t["attempts"]) >= 3:
            # 喊了三次没人应，就不再惦记了
            t["pending"] = False
            t["mood"] = "算了，先不想了"
            t["expires"] = now + 1800
            return {
                "kind": str(t.get("kind") or ""),
                "mood": "算了，先不想了",
                "ts": now,
            }
        return None
    return None


def take_expired(
    thoughts: list[dict], now: float | None = None
) -> tuple[list[dict], list[dict]]:
    """返回 (过期未满足的[需淡出], 过期已满足的[直接删])；不修改原列表。"""
    now = time.time() if now is None else now
    fading: list[dict] = []
    dropped: list[dict] = []
    for t in thoughts or []:
        if float(t.get("expires", 0)) > now:
            continue
        if t.get("pending", True):
            fading.append(t)
        else:
            dropped.append(t)
    return fading, dropped


def release_effect(thought: dict) -> dict:
    meta = KIND_META.get(str(thought.get("kind"))) or {}
    return dict(meta.get("release") or {})


def auto_complete(thought: dict, drives: dict) -> str | None:
    """有些念头不是"他做了某事"才满足的，而是状态自己缓过来。"""
    kind = str(thought.get("kind"))
    if kind == "quiet" and _g(drives, "fatigue") <= 45 and _g(drives, "stress") <= 35:
        return "安静了会儿，缓过来了"
    if kind == "task" and _g(drives, "duty") <= 40:
        return "事儿做完啦，心里踏实"
    return None


# 念头对"要不要开口"的影响：+1 更想说话，0 无关，−1 更不想说话
SPEAK_BIAS = {
    "praise": 1.0,
    "miss": 1.0,
    "hug": 1.0,
    "talk": 1.0,
    "ask_day": 1.0,
    "company": 1.0,
    "deep": 1.0,
    "dwell": 0.5,   # 等他回话：忍不住想催，但别太猛
    "task": 0.0,    # 想做事，跟说不说话没关系
    "quiet": -1.0,  # 想安静，反而更不该开口
}


def _live_pending(thoughts: list[dict], now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]


def pending_kind(thoughts: list[dict], now: float | None = None) -> str:
    """当前最该惦记的那条念头是什么（和 pending_hint 选的是同一条）。"""
    live = _live_pending(thoughts, now)
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    return str(best.get("kind") or "")


def speak_bias(thoughts: list[dict], now: float | None = None) -> float:
    """这些念头让她更想开口，还是更不想。"""
    kind = pending_kind(thoughts, now)
    if not kind:
        return 0.0
    return float(SPEAK_BIAS.get(kind, 1.0))


def pending_hint(thoughts: list[dict], now: float | None = None) -> str:
    """给注入用的、当前最该惦记的一条。"""
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    meta = KIND_META.get(str(best.get("kind"))) or {}
    return str(meta.get("hint") or "")


def pending_induce(thoughts: list[dict], now: float | None = None) -> str:
    """和 pending_hint 选同一条念头，返回"怎么给他一个台阶"的一句话。

    每条念头只提示一次（`induced`），避免同一条话术在注入里反复出现。
    """
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    if best.get("induced"):
        return ""
    return THOUGHT_INDUCE.get(str(best.get("kind")), "")


def mark_induced(thoughts: list[dict], now: float | None = None) -> None:
    """把"已经被提示过台阶"记到同一条念头上。"""
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if live:
        max(live, key=lambda t: float(t.get("weight", 0)))["induced"] = True


def pending_text(thoughts: list[dict], now: float | None = None) -> str:
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    return str(best.get("text") or "")


def summarize(thoughts: list[dict], now: float | None = None, limit: int = 2) -> list[str]:
    """状态行用的摘要：未满足的"念头" + 已满足的"心情"。"""
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if float(t.get("expires", 0)) > now
    ]
    pending = [t for t in live if t.get("pending", True)]
    done = [t for t in live if not t.get("pending", True)]
    out: list[str] = []
    if pending:
        out.append("念头：" + "；".join(str(t.get("text")) for t in pending[-limit:]))
    if done:
        out.append("心情：" + "；".join(str(t.get("mood")) for t in done[-limit:]))
    return out


def migrate(raw: list[Any]) -> list[dict]:
    """v1 → v2：老念头只有 text/weight/expires，补 kind 与生命周期字段。

    v2 之后的记录**原样保留**（pending / mood / attempts / created / expires）。
    以前这里无条件把每条都当 v1 重置成"还没被喂饱"，后果是：
      - 每次重载插件，已经满足的念头又变回 pending（面板显示"还没被满足"）；
      - 已经过期的念头被 `new_thought` 续上一条新命，永远不会消失；
      - 已满足的 hug 不挡新 hug，重载几次就攒出两条一模一样的"想要抱抱"。
    """
    out: list[dict] = []
    now = time.time()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        # 有合法 kind 就按 v2 处理（即使缺 pending 也补默认值）；
        # 只有真 v1 才走 text → kind 的反推。
        is_v2 = kind in KIND_META
        if not is_v2:
            kind = OLD_TEXT_TO_KIND.get(str(item.get("text") or ""), "")
            if not kind:
                continue
        exp = float(item.get("expires", 0) or 0)
        if exp and exp <= now:
            continue                      # 过期的直接丢，别续命
        t = new_thought(kind, now=now, weight=item.get("weight", 0) or 1.0)
        if t is None:
            continue
        if exp > now:
            t["expires"] = exp
        t["created"] = float(item.get("created", now) or now)
        if is_v2:
            t["pending"] = bool(item.get("pending", True))
            t["mood"] = str(item.get("mood") or "")
            t["attempts"] = int(item.get("attempts", 0) or 0)
            # 台阶"只提示一次"必须跨重载保留，否则每次 reload 都会再提示一遍
            t["induced"] = bool(item.get("induced", False))
            t["induced"] = bool(item.get("induced", False))
        else:
            # 真正的 v1：只有 text/webhint，没有生命周期
            t["pending"] = True
            t["mood"] = ""
        out.append(t)

    # 同一种念头只留一条 pending（历史版本重载会攒出重复），保留最新的那条；
    # 已满足的照常留着显示"心情"，等它自己过期。
    newest: dict[str, dict] = {}
    for t in out:
        if not t.get("pending"):
            continue
        cur = newest.get(t["kind"])
        if cur is None or float(t.get("created", 0)) >= float(cur.get("created", 0)):
            newest[t["kind"]] = t
    deduped: list[dict] = []
    for t in out:
        if t.get("pending") and newest.get(t["kind"]) is not t:
            continue
        deduped.append(t)
    return deduped
