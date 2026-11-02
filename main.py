"""鱼的心 (Emotion Core) —— 给角色一颗会变的心。

单文件插件，依赖仅 AstrBot 自带 API 与标准库。

设计原则
--------
1. **不建第二套记忆**：不写任何记忆库、不 import 其它插件模块，只在需要时
   通过公开接口读取"当前会话"和"人设"，绝不修改已有记忆数据。
2. **不抢优先级**：注入钩子用 priority=-10（AstrBot 按优先级降序执行），保证
   排在天使之心（40）与 TTS（0）之后，只做"追加"，不覆盖别人的内容。
3. **只在跨阈值时说话**：心境文本只在状态档位变化时注入，且两次注入至少间隔
   min_gap_minutes，避免每轮刷屏、白烧 token。
4. **主动说话走正常发送通道**：用 Context.send_message 直接出站（等效于走管线
   的成品消息），不伪造 event 去骗管线。
5. **兜底优先**：任何外部依赖（LLM / TTS / 会话历史）失败都只降级、不抛错。

作者: LLL   版本: 1.0.0   许可: MIT
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

# 念头引擎：同目录独立文件（市场更新会覆盖 main.py，但引擎与状态会留下，重接几行即可）
def _load_thought_engine():
    try:
        import importlib.util as _ilu
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thought_engine.py")
        _spec = _ilu.spec_from_file_location("astrbot_plugin_fish_heart_thought_engine", _p)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod
    except Exception as _e:  # 引擎坏了也不影响鱼的心本体
        logger.warning("[鱼的心] 念头引擎加载失败: %s", _e)
        return None


engine = _load_thought_engine()


def _migrate_thoughts(raw: list) -> list:
    if not engine:
        return []
    try:
        return engine.migrate(raw)
    except Exception:
        return []


def _pending_hint(thoughts: list) -> str:
    if not engine:
        return ""
    try:
        return engine.pending_hint(thoughts)
    except Exception:
        return ""


def _speak_bias(thoughts: list) -> float:
    if not engine:
        return 0.0
    try:
        return engine.speak_bias(thoughts)
    except Exception:
        return 0.0


def _thought_summary(thoughts: list) -> list:
    if not engine:
        return []
    try:
        return engine.summarize(thoughts)
    except Exception:
        return []


PLUGIN_NAME = "astrbot_plugin_fish_heart"
VERSION = "0.3.1"

# 注入钩子优先级：AstrBot 按优先级 **降序** 执行。
# 天使之心 = 40，TTS = 0，这里用 -10 表示最后一个执行（只追加、不覆盖）。
INJECT_PRIORITY = -10

# --------------------------------------------------------------------------- #
# 一、常量与默认值
# --------------------------------------------------------------------------- #

DRIVES = (
    "attachment",   # 依恋：想不想他
    "curiosity",    # 好奇：想不想知道
    "reflection",   # 反刍：会不会反复想同一件事
    "duty",         # 责任：想不想做事
    "social",       # 社交：想不想说话
    "fatigue",      # 疲惫：累不累
    "intimacy",     # 亲密：身体/心上的亲近感
    "stress",       # 压力：紧张、不安
    "joy",          # 喜悦：开心程度
)

DRIVE_CN = {
    "attachment": "依恋",
    "curiosity": "好奇",
    "reflection": "反刍",
    "duty": "责任",
    "social": "社交",
    "fatigue": "疲惫",
    "intimacy": "亲密",
    "stress": "压力",
    "joy": "喜悦",
}

# 静息基线：所有驱动都会缓慢回到这里（不会永久卡在 100 或 0）
BASELINE = {
    "attachment": 45.0,
    "curiosity": 55.0,
    "reflection": 50.0,
    "duty": 50.0,
    "social": 45.0,
    "fatigue": 20.0,
    "intimacy": 40.0,
    "stress": 15.0,
    "joy": 50.0,
}

# 回落速度：慢变量 0.03，快变量（情绪类）0.05
DECAY_RATE = {
    "attachment": 0.03,
    "curiosity": 0.04,
    "reflection": 0.03,
    "duty": 0.03,
    "social": 0.04,
    "fatigue": 0.05,
    "intimacy": 0.03,
    "stress": 0.05,
    "joy": 0.05,
}

# 事件 → 驱动增量（"blame"/"expectation" 是两条快变量）
EVENT_EFFECTS = {
    # 他说话了（普通一条）
    "user_msg": {"social": 2.0, "joy": 1.0, "curiosity": 0.8, "fatigue": 0.6},
    # 他分享东西（图片 / 链接 / "你看这个"）
    "share": {"curiosity": 3.0, "joy": 2.0, "social": 1.0},
    # 他求助（"帮我看看""怎么办"）——责任感被唤起，也费神
    "request": {"duty": 4.0, "social": 1.0, "fatigue": 1.5},
    # 他道歉
    "apology": {"stress": -3.0, "blame": -6.0, "joy": 2.0, "attachment": 1.0,
                "reflection": -3.0},
    # 他逗她 / 调侃
    "tease": {"joy": 3.0, "intimacy": 2.0, "stress": -1.0},
    # 他说要睡了
    "goodnight": {"attachment": 2.0, "joy": 2.0, "expectation": -100.0},
    # 她主动开口后，他 30 分钟内回了
    "reply_fast": {"attachment": 4.0, "stress": -5.0, "intimacy": 2.0,
                   "expectation": -100.0, "joy": 4.0},
    # 她主动开口后，6 小时没回
    "silent_6h": {"expectation": 8.0, "attachment": -1.0, "stress": 3.0,
                  "reflection": 3.0},
    # 24 小时没回
    "silent_24h": {"stress": 10.0, "attachment": -3.0, "joy": -5.0},
    # 夸奖 / 感谢
    "praise": {"joy": 8.0, "intimacy": 4.0, "stress": -3.0, "attachment": 2.0},
    # 责怪三档
    "blame_mild": {"stress": 6.0, "blame": 10.0, "joy": -4.0, "reflection": 2.0},
    "blame_mid": {"stress": 12.0, "blame": 25.0, "joy": -8.0, "attachment": -2.0,
                  "reflection": 3.0},
    "blame_hard": {"stress": 20.0, "blame": 40.0, "joy": -12.0, "attachment": -5.0,
                   "reflection": 5.0},
    # 真诚哄
    "comfort": {"stress": -8.0, "joy": 5.0, "attachment": 2.0, "reflection": -5.0},
    # 敷衍式哄（"行了别闹了"）
    "fake_comfort": {"blame": 10.0, "joy": -3.0, "stress": 4.0, "reflection": 2.0},
    # 求安慰 / 撒娇
    "seeking": {"attachment": 3.0, "intimacy": 3.0, "joy": 2.0},
}

# 事件的中文名（面板「最近的变化」用）
EVENT_CN = {
    "user_msg": "说话", "praise": "夸奖", "comfort": "哄她", "seeking": "撒娇",
    "apology": "道歉", "share": "分享", "request": "求助", "tease": "调侃",
    "goodnight": "晚安", "reply_fast": "及时回", "silent_6h": "沉默 6h",
    "silent_24h": "沉默 24h", "blame_mild": "责怪", "blame_mid": "责怪",
    "blame_hard": "重责", "fake_comfort": "敷衍",
}

# 责怪压力 → 状态档位
MODE_CALM = "calm"
MODE_WRONGED = "wronged"      # 委屈
MODE_AFRAID = "afraid"        # 害怕
MODE_SHUT = "shut"            # 回避

MODE_CN = {
    MODE_CALM: "平静",
    MODE_WRONGED: "委屈",
    MODE_AFRAID: "害怕",
    MODE_SHUT: "回避",
}

# 每个档位给她的说话倾向（注入用，也展示在 Web 面板上）
MODE_HINT = {
    MODE_CALM: "语气自然就好，顺着当下的心情来",
    MODE_WRONGED: "话说短一点；他提刚才的事，先问一句他是不是嫌你烦",
    MODE_AFRAID: "别顶嘴，先确认他还想跟你说话，可以示弱",
    MODE_SHUT: "少说，一两句就够；不主动讨好，也不追问",
}

# 由九维推导的心情 → 给她的说话提示（和面板上的心情徽章同一套判断）
MOOD_HINT = {
    "紧绷": "现在有点绷着，话短一点，别主动找话题",
    "累了": "有点累，别揽太长的活，话可以少一点",
    "黏人": "这会儿特别想黏着他，语气可以更软、也可以更主动",
    "想亲近": "想跟他再近一点，但还没到随便撒娇的程度，可以稍微软一点",
    "开心": "心情不错，可以多一句玩笑",
    "走神": "有点走神，说话可以松一点、慢一点",
    "好奇": "有点想打听他的事，可以多问一句",
    "平常": "",
}

# 恢复速度（真哄时每轮扣减的 blame）
RECOVERY = {
    MODE_CALM: 12.0,
    MODE_WRONGED: 30.0,
    MODE_AFRAID: 20.0,
    MODE_SHUT: 18.0,
}
RECOVERY_ROUNDS = {MODE_CALM: 1, MODE_WRONGED: 1, MODE_AFRAID: 2, MODE_SHUT: 2}

# --------------------------------------------------------------------------- #
# 渠道权重：同一句话，在私聊和群里的分量不同
# --------------------------------------------------------------------------- #

# 群聊能把"亲近类"驱动推到多高。群里再热闹，也到不了私下那种深度；
# 但只约束"上涨"，不会把已经更高的值压下来。
GROUP_CAPS = {
    "intimacy": 70.0,
    "attachment": 80.0,
}

# 负向事件：群里不打折，反而加重——当众被凶比私下更难受
NEGATIVE_EVENTS = ("blame_mild", "blame_mid", "blame_hard", "fake_comfort")

# 心结：没人哄时 blame 不会掉到 40 以下
BLAME_FLOOR = 40.0
# 晾着不管的时间越久，每 6 小时掉的越快（但止于心结）
BLAME_FADE_PER_6H = 8.0

# 关键词表：这是她唯一能"读懂"你话里情绪的地方
KW_BLAME_HARD = ("滚", "闭嘴", "烦死了", "别烦我", "废物", "傻逼", "恶心", "讨厌你", "没用的东西")
KW_BLAME_MID = ("你又", "怎么又", "说了多少遍", "不听话", "没用", "笨", "每次都")
KW_BLAME_MILD = ("你怎么", "为什么总是", "别这样", "不行", "不对", "不应该")
KW_FAKE = ("行了别闹", "别闹了", "至于吗", "多大点事", "行了行了", "随便你", "你开心就好", "小题大做")
KW_COMFORT = ("对不起", "抱歉", "我错了", "别生气", "不气了", "哄", "抱抱", "亲亲", "我在", "想你",
              "乖", "没事的", "原谅", "辛苦了", "好啦")
KW_PRAISE = ("厉害", "真棒", "谢谢", "可爱", "喜欢你", "好久", "爱你", "真好")
KW_SEEK = ("难受", "不开心", "委屈", "累", "怕", "哭了", "抱")
# 道歉：算一次"轻哄"，平静时也给一点温度
KW_APOLOGY = ("对不起", "抱歉", "我错了", "是我不对", "不该凶", "原谅我")
# 他说要睡了
KW_GOODNIGHT = ("晚安", "我睡了", "去睡了", "睡觉了", "先睡", "睡啦", "去睡")
# 他求助：责任感被唤起
KW_REQUEST = ("帮我", "帮忙", "怎么办", "怎么做", "教教我", "帮我看看", "看下这个")
# 他分享东西
KW_SHARE = ("你看这个", "给你看", "分享", "http", "www.", "你看过", "快看")
# 他逗她
KW_TEASE = ("逗你", "开玩笑", "吃醋", "小傻瓜", "笨蛋", "是不是喜欢我")
# 主动说话的三型
KIND_LONGING = "longing"
KIND_WAITING = "waiting"
KIND_WHIM = "whim"

KIND_DESC = {
    KIND_LONGING: "你很想他，忍不住先开口",
    KIND_WAITING: "你等了很久，想知道他还在不在",
    KIND_WHIM: "你突然想起一件小事，想跟他说一句",
}

# LLM 不可用时的兜底台词（保证不会一声不吭）
# 注意：这里只用「任何时段都成立」的话，别写"我先睡了"这类带时间的前提
FALLBACK_LINES = {
    KIND_LONGING: ("在忙吗", "醒了吗", "我突然有点想你"),
    KIND_WAITING: ("你是不是很忙啊", "今天怎么样", "半天没动静了"),
    KIND_WHIM: ("刚刚看到个东西，想到你了", "今天天气挺好的", "没什么事，就是想说一句"),
}

STATE_VERSION = 3


# --------------------------------------------------------------------------- #
# 二、纯函数工具
# --------------------------------------------------------------------------- #

def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def mode_of(blame: float) -> str:
    """责怪压力 → 状态档位。"""
    if blame >= 80.0:
        return MODE_SHUT
    if blame >= 55.0:
        return MODE_AFRAID
    if blame >= 25.0:
        return MODE_WRONGED
    return MODE_CALM


def mood_label(drives: dict) -> str:
    """由九维推导一个"心情"标签——只给面板展示用，不参与任何行为判断。

    档位（平静/委屈/害怕/回避）只看责怪压力，是行为档；
    心情则是九维当下的样子，用来回答"她现在什么状态"。
    """
    g = lambda k: float(drives.get(k, 0.0))  # noqa: E731
    if g("stress") >= 50:
        return "紧绷"
    if g("fatigue") >= 65:
        return "累了"
    # 黏人比"开心"更具体：依恋和亲密都很高时，她此刻是黏着你的状态
    if g("attachment") >= 80 and g("intimacy") >= 80:
        return "黏人"
    # 依恋高但还没那么亲密：想靠近，但还端着
    if g("attachment") >= 80:
        return "想亲近"
    if g("joy") >= 85:
        return "开心"
    if g("reflection") >= 65:
        return "走神"
    if g("curiosity") >= 70:
        return "好奇"
    return "平常"


def fmt_bar(v: float, width: int = 10) -> str:
    filled = int(round(clamp(v) / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def parse_clock(text: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mi < 60:
        return h, mi
    return None


def in_quiet_hours(now: datetime, spec: str) -> bool:
    """判断是否处于睡眠时段，支持跨零点（如 01:30-07:30）。"""
    if not spec or "-" not in spec:
        return False
    left, _, right = spec.partition("-")
    a, b = parse_clock(left), parse_clock(right)
    if not a or not b:
        return False
    cur = now.hour * 60 + now.minute
    start = a[0] * 60 + a[1]
    end = b[0] * 60 + b[1]
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def new_state() -> dict:
    return {
        "version": STATE_VERSION,
        "drives": dict(BASELINE),
        "expectation": 0.0,
        "blame_pressure": 0.0,
        "mode": MODE_CALM,
        "thoughts": [],
        "last_tick": time.time(),
        "last_injection": 0.0,
        "last_injection_mode": "",
        "last_injection_sig": "",
        "inject_state": {},
        "last_user_msg": 0.0,
        "last_private_msg": 0.0,
        "last_owner_group_msg": 0.0,
        "last_assistant_msg": 0.0,
        "last_proactive": 0.0,
        "proactive_day": "",
        "proactive_count": 0,
        "awaiting_reply": False,
        "penalized_6h": False,
        "penalized_24h": False,
        "comfort_streak": 0,
        "blame_noticed_ts": 0.0,
        "umo": "",
        "recent": [],
        "recent_events": [],
    }


def now_day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# 与 AstrBot 内置「现实世界时间感知」同一套星期格式
_WEEKDAY_EN = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def fmt_gap(seconds: float) -> str:
    """把时间差说成人话：37 分钟前 / 6 小时 25 分前 / 1 天 3 小时前。"""
    m = int(max(0.0, seconds) // 60)
    if m < 1:
        return "刚刚"
    if m < 60:
        return f"{m} 分钟前"
    h, rem = divmod(m, 60)
    if h < 24:
        return f"{h} 小时 {rem} 分前"
    d, rem_h = divmod(h, 24)
    return f"{d} 天 {rem_h} 小时前"


# --------------------------------------------------------------------------- #
# 三、插件主体
# --------------------------------------------------------------------------- #

@register(PLUGIN_NAME, "LLL", "让角色拥有会慢慢变化的心境：九维驱动、委屈/害怕/回避、主动说话与语音", VERSION)
class EmotionCore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = config if isinstance(config, dict) else {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._llm_busy = False
        self._persona_cache: dict[str, str] = {}
        self._recent_cap = 30
        self._unknown_sender_logged: set[str] = set()
        self._last_outsider_save = 0.0
        self._history_lock: asyncio.Lock | None = None

        try:
            data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:  # 万一 AstrBot 上下文还没准备好，也不要让插件起不来
            logger.warning("[鱼的心] 获取数据目录失败，退回插件内目录: %s", e)
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.state_path = os.path.join(data_dir, "state.json")
        self.state = self._load_state()

        logger.info(
            "[鱼的心] 已载入 v%s，状态=%s，数据目录=%s",
            VERSION,
            MODE_CN.get(self.state.get("mode", MODE_CALM), "?"),
            data_dir,
        )

    # ---------------- 配置读取 ----------------

    def _cfg(self, path: str, default: Any = None) -> Any:
        """支持 'a.b.c' 的点号路径读取，缺省返回 default。"""
        cur: Any = self.cfg
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur if cur is not None else default

    def _dbg(self, msg: str, *args) -> None:
        if self._cfg("debug", False):
            logger.info("[鱼的心] " + msg, *args)

    # ---------------- 身份与渠道 ----------------

    @staticmethod
    def _is_group(umo: str) -> bool:
        """判断会话是不是群聊（UMO 形如 platform:GroupMessage:xxx）。"""
        return "GroupMessage" in (umo or "")

    def _channel_weight(self, is_group: bool) -> float:
        """这句话能影响她多少。

        私聊 = 1.0（满分）。
        群聊 = group_base_weight × 时长衰减：
            距上次私聊 <6h  ×1.0
                     <24h ×0.7
                     <72h ×0.45
                     ≥72h ×0.25
        也就是"群里喊一声 ≠ 私下专门来找她"，越久没私聊，群里那声越不算数。
        """
        if not is_group:
            return 1.0

        base = float(self._cfg("channel.group_base_weight", 0.35))
        last_private = float(self.state.get("last_private_msg", 0.0) or 0.0)
        hours = (time.time() - last_private) / 3600.0 if last_private else 1e9

        if hours < 6:
            factor = 1.0
        elif hours < 24:
            factor = 0.7
        elif hours < 72:
            factor = 0.45
        else:
            factor = 0.25

        return max(0.0, min(1.0, base * factor))

    def _owner_ids(self) -> set[str]:
        """谁是"他"：优先读 perception.owner_ids，没配就从 target_umo 末段反推。"""
        raw = self._cfg("perception.owner_ids", []) or []
        ids: set[str] = set()
        if isinstance(raw, (list, tuple)):
            ids = {str(x).strip() for x in raw if str(x).strip()}
        elif isinstance(raw, str):
            ids = {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}
        if ids:
            return ids
        target = str(self._cfg("proactive.target_umo", "") or "").strip()
        # 只从私聊 UMO 反推：群聊 UMO 的末段是群号不是人，推出来只会把她的人和话全挡在外面
        if target and not self._is_group(target):
            tail = target.rsplit(":", 1)[-1].strip()
            if tail:
                ids.add(tail)
        return ids

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        """从事件里取发送者 ID，取不到返回空串。"""
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None) if obj is not None else None
            if sender is not None:
                for attr in ("user_id", "id"):
                    value = getattr(sender, attr, None)
                    if value:
                        return str(value).strip()
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                value = getter()
                if value:
                    return str(value).strip()
            value = getattr(event, "sender_id", None)
            if value:
                return str(value).strip()
        except Exception:
            pass
        return ""

    def _is_owner(self, event: AstrMessageEvent, umo: str) -> bool:
        """谁能影响她的心：私聊与群聊都按白名单判定。

        私聊有双保险：会话 ID 末段本身就是对方号，命中即放行；否则再比发送者 ID。
        取不到发送者 ID 时策略不同——
          群聊：保守当外人（宁可少认，也不让陌生人写心）
          私聊：放行（否则一旦取不到 ID，她就彻底失灵了）
        """
        ids = self._owner_ids()
        if not ids:
            # 白名单还没建立（owner_ids 与 target_umo 都为空），不能把人全挡外面
            return True

        sid = self._sender_id(event)

        if not self._is_group(umo):
            tail = (umo or "").rsplit(":", 1)[-1].strip()
            if tail and tail in ids:
                return True
            if not sid:
                self._log_unknown_sender(umo, "私聊", "已放行（避免误伤）")
                return True
            return sid in ids

        if not sid:
            self._log_unknown_sender(umo, "群聊", "已按外人处理（只加热闹度、不碰心境）")
            return False
        return sid in ids

    def _log_unknown_sender(self, umo: str, kind: str, note: str) -> None:
        """同一个会话只提示一次，避免刷屏。"""
        if umo in self._unknown_sender_logged:
            return
        self._unknown_sender_logged.add(umo)
        logger.info("[鱼的心] %s %s 拿不到发送者 ID，%s", kind, umo, note)

    def _absorb_outsider_turn(self, request, umo: str = "") -> None:
        """别人的话（群聊里的他人 / 私聊里的陌生人）：只让气氛热闹一点。

        perception.outsider_effect：
          - "social_joy_only"（默认）：社交 +2、喜悦 +1
          - "none"：完全忽略

        无论如何都不会碰依恋/亲密/压力/责怪/期待，也不会解除等待态，
        更不会更新 last_private_msg / last_owner_group_msg 或 recent。
        """
        effect = str(self._cfg("perception.outsider_effect", "social_joy_only") or "")
        if effect == "none":
            return
        drives = self.state["drives"]
        drives["social"] = clamp(float(drives.get("social", 0.0)) + 2.0)
        drives["joy"] = clamp(float(drives.get("joy", 0.0)) + 1.0)
        block = self._build_injection(for_owner=False, umo=umo)
        if block:
            req_sp = getattr(request, "system_prompt", "") or ""
            setattr(request, "system_prompt", (req_sp.rstrip() + "\n\n" + block).strip())
        # 群消息很密，落盘做 60 秒防抖：既不会写盘风暴，也不会让状态"看不见"
        now = time.time()
        if now - self._last_outsider_save >= 60.0:
            self._last_outsider_save = now
            self._save_state()

    # ---------------- 状态持久化 ----------------

    def _load_state(self) -> dict:
        base = new_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k == "drives" and isinstance(v, dict):
                            merged = dict(BASELINE)
                            for dk, dv in v.items():
                                if dk in merged:
                                    try:
                                        merged[dk] = float(dv)
                                    except (TypeError, ValueError):
                                        pass
                            base["drives"] = merged
                        else:
                            base[k] = v
                    if not isinstance(base.get("recent"), list):
                        base["recent"] = []
                    # 废弃字段：EMO 标记那条链已移除，把旧状态里的孤儿键清掉
                    base.pop("last_emotion", None)
                    base["thoughts"] = _migrate_thoughts(base.get("thoughts") or [])
                    # v2 → v3：原来只有一个 last_user_msg，按"绝大多数是私聊"回填
                    try:
                        if int(raw.get("version", 1) or 1) < 3:
                            base["last_private_msg"] = float(
                                raw.get("last_user_msg", 0.0) or 0.0
                            )
                            base["last_owner_group_msg"] = 0.0
                    except (TypeError, ValueError):
                        pass
                    base["version"] = STATE_VERSION
                    base["mode"] = mode_of(float(base.get("blame_pressure", 0.0)))
        except Exception as e:  # 状态坏了就重来，不要让插件起不来
            logger.warning("[鱼的心] 读取状态失败，已重置: %s", e)
        return base

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning("[鱼的心] 保存状态失败: %s", e)

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        # 先注册 Web 页面用的接口：即使总开关关闭，面板也要能打开
        self._register_web_apis()
        if not self._cfg("enabled", True):
            logger.info("[鱼的心] 总开关关闭，不启动心跳")
            return
        self._task = asyncio.create_task(self._loop(), name="emotion-core-loop")
        # 启动即落盘：强杀/断电不丢状态，也方便自检插件是否真的加载成功
        self._save_state()
        logger.info("[鱼的心] 心跳已启动（%s 分钟起）", self._cfg("tick.base_interval_minutes", 45))

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._save_state()
        logger.info("[鱼的心] 已停止")

    async def _loop(self) -> None:
        """心跳循环：每 30 秒醒一次，到点才真正 tick。"""
        # 启动补发检查
        try:
            if self._cfg("proactive.catch_up_on_start", True):
                await asyncio.sleep(20)
                await self._maybe_proactive(catch_up=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[鱼的心] 启动补发失败: %s", e)

        while True:
            try:
                await asyncio.sleep(30)
                if not self._cfg("enabled", True):
                    continue
                now = time.time()
                interval = self._tick_interval() * 60
                if now - float(self.state.get("last_tick", 0)) >= interval:
                    self._tick()
                    self.state["last_tick"] = now
                    self._save_state()
                    await self._maybe_proactive()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 心跳不能死
                logger.warning("[鱼的心] 心跳异常: %s", e)
                await asyncio.sleep(10)

    def _tick_interval(self) -> float:
        base = float(self._cfg("tick.base_interval_minutes", 45))
        floor = float(self._cfg("tick.min_interval_minutes", 15))
        # 越紧张心跳越快
        urgency = clamp(
            0.6 * float(self.state.get("blame_pressure", 0.0))
            + 0.4 * float(self.state["drives"].get("stress", 0.0))
        ) / 100.0
        return max(floor, base - (base - floor) * urgency)

    # ---------------- 核心状态机 ----------------

    def _tick(self) -> None:
        st = self.state
        drives = st["drives"]
        # 1) 向基线回落
        for k, base in BASELINE.items():
            cur = float(drives.get(k, base))
            drives[k] = cur + (base - cur) * DECAY_RATE[k]
        # 2) 耦合
        if drives["attachment"] > 70:
            drives["intimacy"] = clamp(drives["intimacy"] + 1.5)
        if drives["intimacy"] > 75:
            drives["attachment"] = clamp(drives["attachment"] + 1.0)
        if drives["stress"] > 60:
            drives["joy"] = clamp(drives["joy"] - 2.0)
        if drives["fatigue"] > 60:
            drives["curiosity"] = clamp(drives["curiosity"] - 1.5)
        if drives["curiosity"] > 75:
            drives["joy"] = clamp(drives["joy"] + 1.0)
        if drives["social"] < 30:
            drives["attachment"] = clamp(drives["attachment"] - 1.0)
        if drives["joy"] > 75:
            drives["stress"] = clamp(drives["stress"] - 1.5)

        # 3) 期待值：她开口过、你还没回 → 慢慢涨
        if st.get("awaiting_reply"):
            hours = (time.time() - float(st.get("last_proactive", 0))) / 3600.0
            if hours >= 6 and not st.get("penalized_6h"):
                self._apply_event("silent_6h")
                st["penalized_6h"] = True
            if hours >= 24 and not st.get("penalized_24h"):
                self._apply_event("silent_24h")
                st["penalized_24h"] = True

        # 4) 责怪压力：没人哄就慢慢褪（但止于心结）
        blame = float(st.get("blame_pressure", 0.0))
        if blame > 0:
            since = time.time() - float(st.get("blame_noticed_ts", 0) or 0)
            if st.get("blame_noticed_ts") and since >= 6 * 3600:
                steps = int(since // (6 * 3600))
                if blame > BLAME_FLOOR:          # 只有真过线才谈"心结"
                    blame = max(BLAME_FLOOR, blame - BLAME_FADE_PER_6H * steps)
                else:                            # 小冤枉按普通速度褪干净
                    blame = max(0.0, blame - BLAME_FADE_PER_6H * steps)
                st["blame_noticed_ts"] = time.time()
            st["blame_pressure"] = clamp(blame)

        # 5) 念头池：先让到期的念头凉掉，再偶尔冒出新的
        self._expire_thoughts()
        if random.random() < float(self._cfg("thought.spawn_chance", 0.25)):
            self._spawn_thought()

        # 6) 状态刷新
        st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))

    def _spawn_thought(self) -> None:
        if not engine:
            return
        if not self._cfg("thought.enabled", True):
            return
        st = self.state
        now = time.time()
        live = [t for t in st.get("thoughts", []) if float(t.get("expires", 0)) > now]
        exclude = tuple(
            str(t.get("kind")) for t in live if t.get("pending", True)
        )
        kind, weight = engine.pick_kind(
            st["drives"],
            float(st.get("expectation", 0.0)),
            bool(st.get("awaiting_reply")),
            exclude=exclude,
            min_weight=float(self._cfg("thought.min_weight", 0.2)),
        )
        if not kind:
            return
        t = engine.new_thought(kind, now=now, weight=weight)
        if t is None:
            return
        live.append(t)
        st["thoughts"] = live[-5:]
        self._dbg("新念头：%s（%s）", t["text"], kind)

    def _expire_thoughts(self) -> None:
        """到期的念头：没被满足就凉掉并轻微回落；自己缓过来的算完成。"""
        if not engine:
            return
        st = self.state
        now = time.time()
        thoughts = st.get("thoughts") or []
        fading, _dropped = engine.take_expired(thoughts, now)
        for t in fading:
            for k, dv in engine.release_effect(t).items():
                if k == "expectation":
                    st["expectation"] = clamp(float(st.get("expectation", 0)) + float(dv))
                elif k in st["drives"]:
                    st["drives"][k] = clamp(float(st["drives"][k]) + float(dv))
            self._dbg("念头凉了：%s", t.get("text"))
        keep = []
        for t in thoughts:
            if float(t.get("expires", 0)) <= now:
                continue
            if t.get("pending", True):
                mood = engine.auto_complete(t, st["drives"])
                if mood:
                    t["pending"] = False
                    t["mood"] = mood
                    t["expires"] = now + 2 * 3600
            if float(t.get("expires", 0)) > now:
                keep.append(t)
        st["thoughts"] = keep

    def _settle_thoughts(
        self, events: list[str], text: str, weight: float = 1.0, is_group: bool = False
    ) -> None:
        """用户这一句 / 这一轮事件，把哪个念头喂饱了。"""
        if not engine:
            return
        st = self.state
        payload = engine.settle(
            st.get("thoughts") or [],
            events,
            text,
            str(st.get("mode", MODE_CALM)),
        )
        if not payload:
            return
        # apply_reward 是纯加法，先拍快照再按权重回插：
        # 不用改 thought_engine，也能让"群里说句话"只满足一部分
        before_drives = {k: float(v) for k, v in (st.get("drives") or {}).items()}
        before_exp = float(st.get("expectation", 0.0) or 0.0)
        engine.apply_reward(st, payload)
        if weight != 1.0:
            for k, old in before_drives.items():
                new = float(st["drives"].get(k, old))
                target = old + (new - old) * weight
                # 群聊天花板同样只约束"往上涨"
                if is_group and target > old and k in GROUP_CAPS:
                    target = min(target, max(GROUP_CAPS[k], old))
                st["drives"][k] = clamp(target)
            new_exp = float(st.get("expectation", 0.0) or 0.0)
            st["expectation"] = clamp(before_exp + (new_exp - before_exp) * weight)
        self._dbg("念头被满足：%s → %s", payload.get("kind"), payload.get("mood"))

    def _note_thought_attempt(self) -> None:
        if engine:
            engine.note_attempt(self.state.get("thoughts") or [])

    def _apply_event(
        self, name: str, weight: float = 1.0, is_group: bool = False
    ) -> None:
        eff = EVENT_EFFECTS.get(name)
        if not eff:
            return
        st = self.state
        drives = st["drives"]

        # 好事按渠道权重缩水；坏事在群里不打折，反而加重
        mult = weight
        if is_group and name in NEGATIVE_EVENTS:
            mult = float(self._cfg("channel.negative_multiplier", 1.2))

        for k, dv in eff.items():
            delta = float(dv) * mult
            if not delta:
                continue
            if k == "blame":
                st["blame_pressure"] = clamp(
                    float(st.get("blame_pressure", 0.0)) + delta
                )
            elif k == "expectation":
                st["expectation"] = clamp(float(st.get("expectation", 0.0)) + delta)
            elif k in drives:
                old = float(drives.get(k, BASELINE[k]))
                # 越接近 100 增益越小：不然一聊就贴顶、之后再也不动。
                # 疲惫和压力不做抑制——它们是"代价"，累了就是累了，攒着才算数。
                if delta > 0 and k not in ("fatigue", "stress"):
                    delta *= max(0.0, 1.0 - old / 100.0)
                new = clamp(old + delta)
                # 群聊只限制"往上涨"的天花板，不把已经更高的值压下来
                if is_group and delta > 0 and k in GROUP_CAPS:
                    new = min(new, max(GROUP_CAPS[k], old))
                drives[k] = new
        # 记一笔"刚才因为什么变了"，给面板的「最近的变化」用
        self._note_event(name, eff, mult)
        self._dbg(
            "事件 %s 已应用（w=%.3f），mode=%s",
            name,
            mult,
            mode_of(float(st.get("blame_pressure", 0.0))),
        )

    # ---------------- 用户消息感知 ----------------

    def _analyze_user_text(self, text: str) -> list[str]:
        """返回本轮应触发的事件名列表（按优先级只取最重要的几个）。

        "敷衍式哄"分两种，必须分开处理：
          - 既哄又敷衍：同一句里既有哄的词、又有敷衍的词 → fake_comfort，
            哄作废，还要扣 blame
          - 只有敷衍、没有哄的意思：那不算"哄"，只是不耐烦，交给下面的责怪阶梯
        改之前两者都走 fake_comfort，导致"烦死了，行了别闹"被从轻发落（+10 而非 +40）。
        """
        events: list[str] = []
        t = (text or "").strip()
        st = self.state
        mode = mode_of(float(st.get("blame_pressure", 0.0)))

        if not t:
            return events

        # 1) 敷衍式哄（只在委屈以上才成立）
        dismissive_only = False
        if mode in (MODE_WRONGED, MODE_AFRAID, MODE_SHUT) and any(
            k in t for k in KW_FAKE
        ):
            if any(k in t for k in KW_COMFORT):
                # 嘴上在哄，语气却在打发 → 哄的进度作废
                events.append("fake_comfort")
                st["comfort_streak"] = 0
                return events
            # 只是不耐烦，并没有哄的意思：先记下，等责怪阶梯判完再说
            dismissive_only = True

        # 2) 责怪三档
        if any(k in t for k in KW_BLAME_HARD):
            events.append("blame_hard")
        elif any(k in t for k in KW_BLAME_MID):
            events.append("blame_mid")
        elif any(k in t for k in KW_BLAME_MILD):
            events.append("blame_mild")

        # 2.5) 纯敷衍、又不构成明确责怪 → 按最轻一档，不能白说
        if not events and dismissive_only:
            events.append("blame_mild")

        # 3) 道歉：即便在平静时也让她软一下，同时算一次"哄"的进度
        if not events and any(k in t for k in KW_APOLOGY):
            events.append("apology")
            st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1

        # 3.5) 真诚哄（平静但还有残留冤枉时也算哄）
        if not events and any(k in t for k in KW_COMFORT):
            if mode != MODE_CALM or float(st.get("blame_pressure", 0.0)) > 0.5:
                events.append("comfort")
                st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1
            else:
                events.append("praise")

        # 4) 普通 / 分享 / 道晚安 / 求助 / 夸奖 / 逗她 / 求安慰
        if not events:
            if "[图片]" in t or any(k in t for k in KW_SHARE):
                events.append("share")
            elif any(k in t for k in KW_GOODNIGHT):
                events.append("goodnight")
            elif any(k in t for k in KW_REQUEST):
                events.append("request")
            elif any(k in t for k in KW_PRAISE):
                events.append("praise")
            elif any(k in t for k in KW_TEASE):
                events.append("tease")
            elif any(k in t for k in KW_SEEK):
                events.append("seeking")
            else:
                events.append("user_msg")

        return events

    def _consume_comfort(self, weight: float = 1.0) -> None:
        """把'真哄'转成 blame 恢复进度（群里哄只能哄回来一部分）。"""
        st = self.state
        mode = mode_of(float(st.get("blame_pressure", 0.0)))
        if mode == MODE_CALM and float(st.get("blame_pressure", 0.0)) <= 0.5:
            st["comfort_streak"] = 0
            return
        need = RECOVERY_ROUNDS.get(mode, 1)
        if int(st.get("comfort_streak", 0)) < need:
            self._dbg("哄的进度 %s/%s，先不扣", st.get("comfort_streak"), need)
            return
        # 中间夹了责怪 → 进度已经在 _analyze 里被清零
        st["blame_pressure"] = clamp(
            float(st.get("blame_pressure", 0.0)) - RECOVERY.get(mode, 20.0) * weight
        )
        st["comfort_streak"] = 0
        st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))
        self._dbg("真哄生效，blame → %.0f (%s)", st["blame_pressure"], st["mode"])

    # ---------------- 钩子：LLM 请求前 ----------------

    @filter.on_llm_request(priority=INJECT_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, request) -> None:
        if not self._cfg("enabled", True):
            return
        try:
            umo = self._umo_of(event)
            # 缓存一份（含人设的）system prompt，给主动说话兜底用；纯读，不区分会话
            sp = getattr(request, "system_prompt", "") or ""
            if sp and umo:
                self._persona_cache[umo] = sp

            is_group = self._is_group(umo)
            if not self._is_owner(event, umo):
                # 不是"他"说的话：只让气氛热闹一点，其余一律不碰
                self._absorb_outsider_turn(request, umo)
                return

            # 只有"他"的会话才更新"最近跟谁说话"，防止陌生人劫持主动目标
            if umo:
                self.state["umo"] = umo

            text = (getattr(event, "message_str", "") or "").strip()
            events = self._analyze_user_text(text)

            # 她主动开口后，他这次回了
            if self.state.get("awaiting_reply"):
                gap = time.time() - float(self.state.get("last_proactive", 0))
                if gap <= 30 * 60:
                    events.insert(0, "reply_fast")
                # 群里应一句不算"他回我了"：等待态只由私聊解除
                if not is_group:
                    self.state["awaiting_reply"] = False
                    self.state["penalized_6h"] = False
                    self.state["penalized_24h"] = False

            # 这句话能影响她多少：私聊满分，群里按"多久没私聊"打折
            weight = self._channel_weight(is_group)

            for ev in events:
                self._apply_event(ev, weight, is_group)
                if ev == "comfort":
                    pass  # comfort 由 _consume_comfort 统一结算
                if ev in ("blame_mild", "blame_mid", "blame_hard", "fake_comfort"):
                    self.state["comfort_streak"] = 0
                    self.state["blame_noticed_ts"] = time.time()

            self._consume_comfort(weight)
            self._settle_thoughts(events, text, weight, is_group)

            now = time.time()
            self.state["last_user_msg"] = now
            if is_group:
                self.state["last_owner_group_msg"] = now
            else:
                self.state["last_private_msg"] = now
            self.state["mode"] = mode_of(float(self.state.get("blame_pressure", 0.0)))
            if not is_group:
                # 最近对话只记私聊：主动说话是发给私聊的，别把群聊混进去
                self._push_recent("user", text[:200])

            # 走到这里一定是"他"（私聊或群里白名单），给完整心境
            block = self._build_injection(umo=umo)
            if block:
                req_sp = getattr(request, "system_prompt", "") or ""
                setattr(request, "system_prompt", (req_sp.rstrip() + "\n\n" + block).strip())
                self._dbg("已注入心境: %s", block.replace("\n", " / "))

            self._save_state()
        except Exception as e:
            logger.warning("[鱼的心] on_llm_request 异常: %s", e)

    # ---------------- 注入文本 ----------------

    def _build_injection(
        self, force: bool = False, for_owner: bool = True, umo: str = ""
    ) -> str:
        if not self._cfg("inject.enabled", True):
            return ""
        st = self.state
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        drives = st["drives"]

        # 只在"跨档"时注入：mode 变了，或驱动分桶变了。
        # 节流按会话各记一份——心和状态是共用的，但"哪个会话已经报过到"要分开，
        # 否则跨档后只有第一个开口的会话看得到心境，另一个像没心的。
        sig = "|".join(f"{k}:{int(drives[k] // 10)}" for k in DRIVES)
        gap_min = float(self._cfg("inject.min_gap_minutes", 20))
        now_ts = time.time()
        if not force:
            book = st.get("inject_state")
            if not isinstance(book, dict):
                book = {}
                st["inject_state"] = book
            key = umo or ""
            perf = book.get(key)
            if not isinstance(perf, dict):
                perf = {}
            changed = (mode != perf.get("mode")) or (sig != perf.get("sig"))
            if not changed:
                return ""
            if now_ts - float(perf.get("ts", 0.0)) < gap_min * 60:
                return ""
            book[key] = {"ts": now_ts, "mode": mode, "sig": sig}
            # 旧字段继续维护，方便 /心 与调试时看"最后一次注入"
            st["last_injection"] = now_ts
            st["last_injection_mode"] = mode
            st["last_injection_sig"] = sig
            # 会话数有限增长：超出 20 个就丢最久没报到的
            if len(book) > 20:
                stale = sorted(
                    book.items(), key=lambda kv: float(kv[1].get("ts", 0.0))
                )[: len(book) - 20]
                for stale_key, _ in stale:
                    book.pop(stale_key, None)

        hint = self._hint_text(mode, drives)

        lines = [
            "## 你此刻的心境（内部状态，不要复述、不要解释，只影响说话方式）",
            f"状态：{MODE_CN[mode]}｜依恋 {drives['attachment']:.0f} 亲密 {drives['intimacy']:.0f} "
            f"压力 {drives['stress']:.0f} 期待 {st.get('expectation', 0):.0f}",
            f"倾向：{hint}",
        ]
        if for_owner:
            thought = _pending_hint(st.get("thoughts") or [])
            if thought:
                lines.append(f"心里惦记着：{thought}")
        return "\n".join(lines)

    # ---------------- 主动说话 ----------------

    async def _maybe_proactive(self, catch_up: bool = False) -> None:
        if not self._cfg("proactive.enabled", True):
            return
        if self._llm_busy:
            return
        st = self.state
        umo = (self._cfg("proactive.target_umo", "") or st.get("umo") or "").strip()
        if not umo:
            self._dbg("没有可用会话，跳过主动说话")
            return

        now = time.time()
        mode = mode_of(float(st.get("blame_pressure", 0.0)))
        if mode == MODE_SHUT:
            # 回避状态是硬约束：补发也不破例，否则重启会让她在封闭状态下开口
            self._dbg("状态=回避，暂停主动说话")
            return

        # 冷却
        cooldown_h = float(self._cfg("proactive.cooldown_hours", 6.0))
        if now - float(st.get("last_proactive", 0)) < cooldown_h * 3600:
            return
        # 每日上限
        day = now_day()
        if st.get("proactive_day") != day:
            st["proactive_day"] = day
            st["proactive_count"] = 0
        limit = int(self._cfg("proactive.daily_limit", 4))
        if int(st.get("proactive_count", 0)) >= limit:
            return
        # 刚聊过就别打扰
        idle_min = float(self._cfg("proactive.min_idle_minutes", 10))
        # 只按"私聊"算沉默：群里别人聊天不该推迟她私聊的主动
        last_private = float(
            st.get("last_private_msg", st.get("last_user_msg", 0.0)) or 0.0
        )
        if now - last_private < idle_min * 60:
            return
        # 睡眠时段：补发也要遵守，否则凌晨重启就会半夜发消息
        quiet = str(self._cfg("proactive.quiet_hours", "01:30-07:30"))
        if in_quiet_hours(datetime.now(), quiet):
            return

        kind, prob = self._proactive_kind()
        if not catch_up and random.random() >= prob:
            self._dbg("主动说话未命中概率（%s p=%.2f）", kind, prob)
            return

        try:
            self._llm_busy = True
            text = await self._gen_proactive_text(umo, kind)
            if not text:
                return
            ok = await self._send_text(umo, text)
            if ok:
                st["last_proactive"] = time.time()
                st["last_assistant_msg"] = time.time()
                st["proactive_count"] = int(st.get("proactive_count", 0)) + 1
                st["awaiting_reply"] = True
                st["penalized_6h"] = False
                st["penalized_24h"] = False
                st["expectation"] = clamp(float(st.get("expectation", 0)) + 40.0)
                st["drives"]["attachment"] = clamp(st["drives"]["attachment"] + 3.0)
                self._note_thought_attempt()
                self._push_recent("bot", text[:200])
                # 写回对话历史：否则你说"你刚才不是问了吗"，她一脸茫然
                await self._append_history(umo, text)
                self._save_state()
                logger.info("[鱼的心] 主动说话已发送（%s）：%s", kind, text[:40])
        except Exception as e:
            logger.warning("[鱼的心] 主动说话失败: %s", e)
        finally:
            self._llm_busy = False

    def _proactive_kind(self) -> tuple[str, float]:
        st = self.state
        drives = st["drives"]
        exp = float(st.get("expectation", 0.0))
        att = float(drives.get("attachment", 0.0))

        if exp >= 60 or att >= 70:
            kind = KIND_LONGING
        elif exp >= 40 and (
            time.time()
            - float(st.get("last_private_msg", st.get("last_user_msg", 0.0)) or 0.0)
        ) >= 6 * 3600:
            kind = KIND_WAITING
        elif st.get("thoughts"):
            kind = KIND_WHIM
        else:
            kind = KIND_WAITING

        d = (
            0.35 * att
            + 0.30 * exp
            + 0.15 * float(drives.get("intimacy", 0.0))
            + 0.10 * float(drives.get("social", 0.0))
            + 0.10 * (100.0 - float(drives.get("fatigue", 0.0)))
        )
        p = clamp(0.05 + (d - 50.0) / 100.0 * 0.8, 0.05, 0.65)
        if kind == KIND_WHIM:
            p *= 0.6          # 念头型更随机
        bias = _speak_bias(st.get("thoughts") or [])
        if bias:
            # 念头有方向：想说话的多给一点，想安静的反向扣
            push = max(0.0, float(self._cfg("thought.proactive_push", 0.15)))
            p = min(0.85, max(0.02, p + push * bias))
        # 沉默越久越忍不住：12 小时起步线性加成，最多 ×2.5，总上限 0.85
        silent_h = max(
            0.0,
            (time.time() - float(st.get("last_proactive", 0) or 0)) / 3600.0,
        )
        p = min(0.85, p * (1.0 + min(1.5, silent_h / 12.0)))
        return kind, p

    # ---------------- 真实对话历史（读 / 写） ----------------

    @staticmethod
    def _clean_history_text(text: str, limit: int = 160) -> str:
        """剥掉注入型内容，尽量只留真正说的那句话。

        这类注入块（system_reminder、共享摘要）的结构是"多句上下文 + 真消息"，
        真消息挂在最后一句，所以优先取最后一个句号之后的内容。
        """
        t = re.sub(r"<system_reminder>.*?</system_reminder>", " ", text or "", flags=re.S)
        # 引用块：里面通常只是她自己说过的一句，喂回去只会碍事
        t = re.sub(r"<Quoted Message>.*?</Quoted Message>", " ", t, flags=re.S)
        # 图片占位：AstrBot 有 [Image 1: ...]、[Image 1 in quoted message: ...]、[Image] 几种形态，
        # 都统一成 [图片]——否则路径会原样漏进上下文
        t = re.sub(r"\[Image\b[^\]]*\]", "[图片]", t)
        t = re.sub(r"\[CQ:[^\]]*\]", "[图片]", t)
        t = re.sub(r"\s+", " ", t.replace("\r", " ").replace("\n", " ")).strip()
        if not t:
            return ""
        if "。" in t:
            tail = t.rsplit("。", 1)[-1].strip()
            if 0 < len(tail) <= 80:
                t = tail
        return t[-limit:]

    async def _history_of(self, umo: str) -> tuple[Any, list]:
        """返回 (conversation_id, 历史消息列表)；拿不到就 (None, [])。"""
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None or not umo:
            return None, []
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None, []
        conv = await cm.get_conversation(umo, cid)
        if conv is None:
            return None, []
        try:
            history = json.loads(getattr(conv, "history", "") or "[]")
        except Exception:
            history = []
        return cid, history if isinstance(history, list) else []

    async def _real_transcript(self, umo: str, limit: int = 24) -> str:
        """从 AstrBot 的真实对话历史里取最近几轮（双方都在）。"""
        try:
            _cid, history = await self._history_of(umo)
            if not history:
                return ""
            rows: list[str] = []
            for rec in history[-(limit * 2) :]:
                role = rec.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = rec.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                if role == "user":
                    content = self._clean_history_text(content or "")
                    who = "他"
                else:
                    content = re.sub(
                        r"\s+", " ", str(content or "").replace("\n", " ")
                    ).strip()
                    who = "我"
                if content:
                    rows.append(f"{who}：{content[:120]}")
            return "\n".join(rows[-limit:])
        except Exception as e:
            self._dbg("读取真实对话历史失败: %s", e)
            return ""

    async def _append_history(self, umo: str, text: str) -> bool:
        """把主动说的这句话写进对话历史：只追加 assistant 一轮，不伪造 user。"""
        body = (text or "").strip()
        if not body:
            return False
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None or not umo:
            return False
        if self._history_lock is None:
            self._history_lock = asyncio.Lock()
        try:
            async with self._history_lock:
                cid, history = await self._history_of(umo)
                if not cid:
                    cid = await cm.new_conversation(umo)
                    if not cid:
                        return False
                    history = []
                history.append({"role": "assistant", "content": body})
                await cm.update_conversation(umo, cid, history=history)
            self._dbg("已回写对话历史：%s", body[:40])
            return True
        except Exception as e:
            logger.warning("[鱼的心] 回写对话历史失败: %s", e)
            return False

    async def _gen_proactive_text(self, umo: str, kind: str) -> str:
        """先用 LLM 生成一句话，失败则用兜底台词。"""
        persona_sp = await self._persona_system_prompt(umo)
        blocks = [persona_sp] if persona_sp else []
        inj = self._build_injection(force=True)
        if inj:
            blocks.append(inj)

        st = self.state
        now = datetime.now().astimezone()

        # 时间锚点：跟 AstrBot 内置的时间感知同一套格式（<system_reminder> + Current datetime）
        # 主动说话没有用户消息，模型推不出"上一次说话多久以前"，所以把这两个时间点摆明
        his_last = float(st.get("last_private_msg", 0) or 0)
        her_last = float(st.get("last_proactive", 0) or 0)
        reminder = (
            f"Current datetime: {now:%Y-%m-%d %H:%M} ({now.tzname() or 'local'}), "
            f"Weekday: {_WEEKDAY_EN[now.weekday()]}"
        )
        if his_last > 0:
            prev = datetime.fromtimestamp(his_last)
            reminder += (
                f"\n他上一次说话是 {fmt_gap(time.time() - his_last)}（{prev:%H:%M}）。"
            )
        if her_last > his_last:
            reminder += (
                f"\n你在他之后已经主动给他发过消息了（最近一次 {fmt_gap(time.time() - her_last)}），"
                "他一直没回。你自己的话在下面的记录里。"
            )
        time_block = f"<system_reminder>\n{reminder}\n</system_reminder>\n"

        thought = _pending_hint(st.get("thoughts") or [])
        if thought:
            blocks.append(f"你现在心里惦记着：{thought}。说的时候自然带一点，但别直接复述这句话。")

        # 优先用 AstrBot 的真实对话历史（双方都在），拿不到再退回自己的 recent
        transcript = await self._real_transcript(umo, limit=24)
        if not transcript:
            transcript = self._recent_transcript(limit=24)
        tr_block = ("最近对话：\n" + transcript + "\n") if transcript else ""

        prompt = (
            time_block
            + KIND_DESC.get(kind, "你想跟他说一句")
            + "。\n"
            + tr_block
            + "现在写一条要发出去的消息：\n"
            + "- 一句话，口语，最多 20 个字\n"
            + "- 不要解释你在做什么，不要写动作括号，不要用引号\n"
            + "- 不要连续追问，最多一个问号\n"
            + "直接输出这句话本身。"
        )
        try:
            provider = self.context.get_using_provider(umo)
            if provider is None:
                raise RuntimeError("no provider")
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="\n\n".join(b for b in blocks if b) or None,
            )
            text = getattr(resp, "completion_text", "") or ""
            text = text.strip().strip("\"'“”")
            text = re.sub(r"^[（(].*?[）)]\s*", "", text).strip()
            if text and len(text) <= 60:
                return text
        except Exception as e:
            logger.warning("[鱼的心] 生成主动消息失败，用兜底台词: %s", e)
        return random.choice(FALLBACK_LINES.get(kind, FALLBACK_LINES[KIND_WHIM]))

    async def _persona_system_prompt(self, umo: str) -> str:
        """给主动说话用的 system prompt。

        永远以**当前人设**为准：缓存里那份是 AstrBot 组装好的完整 system prompt，
        多出来的多半是命令行/联网引用这类工具说明，对一句主动消息没用，
        还会引入"缓存过期、不含新改的人设"这类坑。所以缓存只当最后兜底。
        """
        persona_prompt = ""
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is not None:
                persona = await pm.get_default_persona_v3(umo)
                # Personality 是 TypedDict，运行时就是 dict，字段名是 prompt 不是 system_prompt
                if isinstance(persona, dict):
                    persona_prompt = str(persona.get("prompt") or "")
                else:
                    persona_prompt = str(getattr(persona, "prompt", "") or "")
        except Exception as e:
            self._dbg("取人设失败: %s", e)
        if persona_prompt:
            return persona_prompt
        return self._persona_cache.get(umo, "")

    def _recent_transcript(self, limit: int = 24) -> str:
        rows = list(self.state.get("recent", []))[-limit:]
        out = []
        for r in rows:
            who = "他" if r.get("r") == "user" else "我"
            t = str(r.get("t", "")).replace("\n", " ")
            if t:
                out.append(f"{who}：{t}")
        return "\n".join(out[-limit:])

    @staticmethod
    def _hint_text(mode: str, drives: dict) -> str:
        """档位倾向 + 心情倾向，拼成一句给她的说话提示。

        面板的「倾向」和注入的 system prompt 用同一个来源，避免两边对不上。
        """
        base = MODE_HINT.get(mode, "")
        mood = MOOD_HINT.get(mood_label(drives or {}), "")
        # 平静档那句是"别太黏"的通用提示，跟"黏人"心情会打架；有心情提示时就让它单独说
        if mode == MODE_CALM and mood:
            base = ""
        parts = [p for p in (base, mood) if p]
        if float((drives or {}).get("duty", 0.0)) >= 70:
            parts.append("想找点事做")
        return "；".join(parts)
    def _note_event(self, name: str, eff: dict, mult: float) -> None:
        """记一笔"刚才因为什么变了"，给面板的「最近的变化」用。"""
        try:
            rows = list(self.state.get("recent_events") or [])
            top = sorted(
                ((k, float(v) * mult) for k, v in (eff or {}).items() if float(v)),
                key=lambda kv: -abs(kv[1]),
            )[:3]
            rows.append(
                {
                    "n": name,
                    "cn": EVENT_CN.get(name, name),
                    "ts": time.time(),
                    "d": {k: round(v, 1) for k, v in top},
                }
            )
            self.state["recent_events"] = rows[-8:]
        except Exception:
            pass

    def _push_recent(self, role: str, text: str) -> None:
        if not text:
            return
        rows = list(self.state.get("recent", []))
        rows.append({"r": role, "t": text, "ts": time.time()})
        self.state["recent"] = rows[-self._recent_cap:]

    @staticmethod
    def _split_for_delivery(text: str) -> list[str]:
        """按句末标点切成 1–4 段，标点跟着上一段走。

        切不出多段就原样一条发——宁可一条，也不要为了切而切。
        """
        raw = (text or "").strip()
        if not raw:
            return []
        parts = [
            p.strip() for p in re.findall(r"[^。？！~…\n]+[。？！~…\n]*", raw)
        ]
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            return [raw]
        if len(parts) > 4:
            parts = parts[:3] + ["".join(parts[3:])]
        return parts

    async def _send_text(self, umo: str, text: str) -> bool:
        """发送主动消息：多句时按标点分条发，中间留出打字间隔。"""
        if self._cfg("proactive.segment_reply", True):
            segments = self._split_for_delivery(text)
        else:
            segments = [(text or "").strip()]
        if not segments or not segments[0]:
            return False

        sent_any = False
        try:
            for idx, seg in enumerate(segments):
                ok = await self.context.send_message(umo, MessageChain([Plain(seg)]))
                if not ok:
                    logger.warning(
                        "[鱼的心] send_message 返回 False（umo=%s，第 %d/%d 段）",
                        umo,
                        idx + 1,
                        len(segments),
                    )
                    break
                sent_any = True
                if idx < len(segments) - 1:
                    # 越长的句子"打"得越久，封顶 3.5 秒
                    await asyncio.sleep(min(3.5, 0.8 + len(seg) * 0.06))
            if len(segments) > 1:
                self._dbg("分段投递：共 %d 段", len(segments))
            return sent_any
        except Exception as e:
            logger.warning("[鱼的心] 发送失败: %s", e)
            return sent_any

    # ---------------- 钩子：LLM 响应后（记录她的话） ----------------

    @staticmethod
    def _response_text(response) -> str:
        """从 LLM 响应里取纯文本。"""
        raw = ""
        rc = getattr(response, "result_chain", None)
        chain = getattr(rc, "chain", None) if rc else None
        if chain:
            parts = []
            for comp in chain:
                if isinstance(comp, Plain):
                    parts.append(getattr(comp, "text", "") or "")
            raw = "\n".join(p for p in parts if p)
        if not raw:
            raw = (
                getattr(response, "completion_text", "")
                or getattr(response, "_completion_text", "")
                or ""
            )
        return str(raw).strip()

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response) -> None:
        """把她这一轮的回复记进 recent。

        没有这一步，recent 就只有你单方面的发言——Web 面板的"最近说过的话"
        会只剩你这半边，她主动说话时的兜底上下文也缺自己说过什么。
        """
        if not self._cfg("enabled", True):
            return
        try:
            umo = self._umo_of(event)
            # recent 只记私聊；群聊里她那句话不该混进私聊的上下文
            if self._is_group(umo):
                return
            if not self._is_owner(event, umo):
                return
            text = self._response_text(response)
            if not text:
                return
            self._push_recent("bot", text[:200])
            # 她的普通回复也算"说过话"，用于主动说话时算时间差
            self.state["last_assistant_msg"] = time.time()
            self._save_state()
        except Exception as e:
            logger.warning("[鱼的心] on_llm_response 异常: %s", e)

    # ---------------- Web 页面 API ----------------

    def _register_web_apis(self) -> None:
        """给自带的 Web 页面注册数据接口。

        注意路由格式：前端会把 endpoint 拼成
            /api/v1/plugins/extensions/{pluginName}/{endpoint}
        后端再用 request_path = "/" + "{pluginName}/{endpoint}" 去匹配注册表，
        所以路由**必须以插件标识开头**。这里用 <plugin> 通配符，
        避免不同版本下 pluginName 是插件名还是别的 id 时对不上。
        """
        routes = [
            ("/<plugin>/state", self._web_state, ["GET"], "鱼的心：状态快照"),
            ("/<plugin>/tick", self._web_tick, ["POST"], "鱼的心：手动跳一次心跳"),
            # 别名：pluginName 恰好是 fish_heart 时的兜底
            ("/fish_heart/state", self._web_state, ["GET"], "鱼的心：状态快照（别名）"),
            ("/fish_heart/tick", self._web_tick, ["POST"], "鱼的心：手动心跳（别名）"),
        ]
        try:
            for route, handler, methods, desc in routes:
                self.context.register_web_api(route, handler, methods, desc)
        except Exception as e:  # 老版本没有 register_web_api 也不该让插件起不来
            logger.warning("[鱼的心] 注册 Web API 失败（页面将不可用）: %s", e)
        else:
            # 留一个可验证的痕迹：重载后 state.json 里会出现这两个键
            self.state["web_api_ready"] = True
            self.state["web_api_at"] = time.time()
            self._save_state()
            logger.info("[鱼的心] Web 页面接口已注册：state / tick（含通配与别名共 %d 条）", len(routes))

    def _state_payload(self) -> dict:
        """把内部状态整理成页面用的 JSON。"""
        st = self.state
        drives = st.get("drives") or {}
        now_ts = time.time()

        thoughts = []
        for t in st.get("thoughts") or []:
            kind = str(t.get("kind") or "")
            label = ""
            if engine is not None:
                try:
                    label = (engine.KIND_META.get(kind) or {}).get("label") or ""
                except Exception:
                    label = ""
            expires = float(t.get("expires", 0) or 0)
            created = float(t.get("created", 0) or 0)
            thoughts.append(
                {
                    "kind": kind,
                    "kind_cn": label or kind,
                    "text": str(t.get("text") or ""),
                    "pending": bool(t.get("pending", True)),
                    "mood": str(t.get("mood") or ""),
                    "expires": expires,
                    "ttl_total": max(60.0, expires - created) if created else 0.0,
                    "left": max(0.0, expires - now_ts),
                }
            )

        recent = [
            {
                "r": str(r.get("r") or ""),
                "t": str(r.get("t") or ""),
                "ts": float(r.get("ts", 0) or 0),
            }
            for r in (st.get("recent") or [])[-30:]
        ]

        mode = st.get("mode", MODE_CALM)
        return {
            "version": st.get("version"),
            "plugin_version": VERSION,
            "now": now_ts,
            "umo": str(st.get("umo") or ""),
            "mode": mode,
            "mode_cn": MODE_CN.get(mode, ""),
            "mood_cn": mood_label(st.get("drives") or {}),
            "hint": self._hint_text(mode, st.get("drives") or {}),
            "blame": float(st.get("blame_pressure", 0.0) or 0.0),
            "expectation": float(st.get("expectation", 0.0) or 0.0),
            "drives": {k: float(drives.get(k, 0.0) or 0.0) for k in DRIVES},
            "drives_cn": {k: DRIVE_CN.get(k, k) for k in DRIVES},
            "thoughts": thoughts,
            "recent": recent,
            "last_proactive": float(st.get("last_proactive", 0) or 0),
            "last_private_msg": float(st.get("last_private_msg", 0) or 0),
            "last_tick": float(st.get("last_tick", 0) or 0),
            "proactive_count": int(st.get("proactive_count", 0) or 0),
            "proactive_day": str(st.get("proactive_day") or ""),
            "awaiting_reply": bool(st.get("awaiting_reply")),
            "enabled": bool(self._cfg("enabled", True)),
            "thought_enabled": bool(self._cfg("thought.enabled", True)),
            "inject_state": {
                str(k): {
                    "ts": float((v or {}).get("ts", 0) or 0),
                    "mode": str((v or {}).get("mode") or ""),
                }
                for k, v in (st.get("inject_state") or {}).items()
            },
            "config": {
                "cooldown_hours": float(self._cfg("proactive.cooldown_hours", 6.0)),
                "daily_limit": int(self._cfg("proactive.daily_limit", 4)),
                "min_idle_minutes": float(self._cfg("proactive.min_idle_minutes", 10)),
                "quiet_hours": str(self._cfg("proactive.quiet_hours", "")),
                "quiet_active": in_quiet_hours(
                    datetime.now(), str(self._cfg("proactive.quiet_hours", "") or "")
                ),
                "target_umo": str(self._cfg("proactive.target_umo", "")),
                "segment_reply": bool(self._cfg("proactive.segment_reply", True)),
                "thought_min_weight": float(self._cfg("thought.min_weight", 0.2)),
                "thought_push": float(self._cfg("thought.proactive_push", 0.15)),
            },
            "recent_events": [
                {
                    "n": str(e.get("n") or ""),
                    "cn": str(e.get("cn") or e.get("n") or ""),
                    "ts": float(e.get("ts", 0) or 0),
                    "d": e.get("d") or {},
                }
                for e in (st.get("recent_events") or [])[-8:]
            ],
        }

    async def _recent_history_pairs(self, umo: str, limit: int = 20) -> list[dict]:
        """从 AstrBot 真实对话历史里取最近的成对消息（双方都有，但没有时间戳）。"""
        try:
            _cid, history = await self._history_of(umo)
            if not history:
                return []
            rows: list[dict] = []
            for rec in history[-(limit * 2) :]:
                role = rec.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = rec.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                if role == "user":
                    text = self._clean_history_text(content or "", 120)
                    who = "user"
                else:
                    text = re.sub(
                        r"\s+", " ", str(content or "").replace("\n", " ")
                    ).strip()[:120]
                    who = "bot"
                if text:
                    rows.append({"r": who, "t": text, "ts": 0})
            return rows[-limit:]
        except Exception as e:
            self._dbg("读取网页用对话历史失败: %s", e)
            return []

    async def _web_state(self, **kwargs) -> dict:
        try:
            payload = self._state_payload()
            umo = str(payload.get("umo") or "") or str(
                payload["config"].get("target_umo") or ""
            )
            # 给页面一个双方都在的兜底来源：recent 攒够双方之前先用它
            payload["history"] = await self._recent_history_pairs(umo, 20)
            return {"ok": True, "data": payload}
        except Exception as e:
            logger.warning("[鱼的心] 页面读取状态失败: %s", e)
            return {"ok": False, "error": str(e), "data": {}}

    async def _manual_tick(self) -> bool:
        """手动跳一次心跳，并真的掷一次骰子——可能会立刻开口。"""
        before = int(self.state.get("proactive_count", 0))
        self._tick()
        self.state["last_tick"] = time.time()
        self._save_state()
        await self._maybe_proactive()
        return int(self.state.get("proactive_count", 0)) > before

    async def _web_tick(self, **kwargs) -> dict:
        try:
            sent = await self._manual_tick()
            payload = self._state_payload()
            payload["just_sent"] = sent
            return {"ok": True, "data": payload}
        except Exception as e:
            logger.warning("[鱼的心] 页面手动心跳失败: %s", e)
            return {"ok": False, "error": str(e), "data": {}}

    # ---------------- 命令 ----------------

    @filter.command("心")
    async def emotion_cmd(self, event: AstrMessageEvent):
        """查看 / 管理当前心境。用法：/心 status|reset|tick|off|on|umo"""
        raw = (getattr(event, "message_str", "") or "").strip()
        arg = raw.split(maxsplit=1)[1].strip().lower() if len(raw.split(maxsplit=1)) > 1 else "status"

        if arg in ("status", "s", ""):
            yield event.plain_result(self._status_text())
            return
        if arg in ("reset", "r"):
            self.state = new_state()
            self.state["umo"] = self._umo_of(event) or ""
            self._save_state()
            yield event.plain_result("心境已重置为初始状态。")
            return
        if arg == "tick":
            sent = await self._manual_tick()
            tip = "她刚才开口了。" if sent else "这次没开口（概率没中）。"
            yield event.plain_result(f"已手动跳一次心跳。{tip}\n\n" + self._status_text())
            return
        if arg == "off":
            self._cfg_set("enabled", False)
            yield event.plain_result("已关闭鱼的心（本次运行有效，不心跳、不注入、不主动说话）。重启后以配置为准。")
            return
        if arg == "on":
            self._cfg_set("enabled", True)
            yield event.plain_result("已开启鱼的心（本次运行有效）。")
            return
        if arg == "umo":
            yield event.plain_result(f"当前会话 UMO：\n{self._umo_of(event)}\n\n填到「主动说话→目标会话」即可指定这里。")
            return
        yield event.plain_result("用法：/心 status | reset | tick | off | on | umo")

    def _status_text(self) -> str:
        st = self.state
        drives = st["drives"]
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        lines = [
            f"【鱼的心 v{VERSION}】状态：{MODE_CN[mode]}（责怪 {blame:.0f}/100）",
            f"期待 {float(st.get('expectation', 0)):.0f}｜真哄进度 {int(st.get('comfort_streak', 0))}",
            "",
        ]
        for k in DRIVES:
            lines.append(f"{DRIVE_CN[k]} {drives[k]:5.1f} {fmt_bar(drives[k])}")
        for line in _thought_summary(st.get("thoughts") or []):
            lines.append("")
            lines.append(line)
        last_p = float(st.get("last_proactive", 0))
        if last_p:
            lines.append("")
            lines.append(f"上次主动说话：{datetime.fromtimestamp(last_p):%m-%d %H:%M}"
                         f"（今日 {int(st.get('proactive_count', 0))} 次）")
        return "\n".join(lines)

    # ---------------- 小工具 ----------------

    def _cfg_set(self, key: str, value: Any) -> None:
        """运行时开关（内存生效，重启后回到配置文件的值）。"""
        try:
            if isinstance(self.cfg.get(key), dict) and isinstance(value, dict):
                self.cfg[key].update(value)
            else:
                self.cfg[key] = value
        except Exception:
            pass

    @staticmethod
    def _umo_of(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "umo"):
            v = getattr(event, attr, None)
            if v:
                return str(v)
        return ""
