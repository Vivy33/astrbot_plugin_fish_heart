"""鱼的心 (Emotion Core) —— 给角色一颗会变的心。

单文件插件，依赖仅 AstrBot 自带 API 与标准库。

设计原则
--------
1. **不建第二套记忆**：不写任何记忆库、不 import 其它插件模块，只在需要时
   通过公开接口读取"当前会话"和"人设"，绝不修改已有记忆数据。
2. **不抢优先级**：注入钩子用 priority=-10（AstrBot 按优先级降序执行），保证
   排在天使之心（40）与 TTS（0）之后，只做"追加"，不覆盖别人的内容。
3. **只在跨阈值时说话**：心境文本只在档位变化或某个维度跨十位分界线时注入；
   同档内的数值漂移按 min_gap_minutes 节流，但**跨档必注**——档位是行为开关，
   漏报会让她"不知道自己已经在生气了"。
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


def _pending_induce(thoughts: list) -> str:
    if not engine:
        return ""
    try:
        return engine.pending_induce(thoughts)
    except Exception:
        return ""


def _mark_induced(thoughts: list) -> None:
    if not engine:
        return
    try:
        engine.mark_induced(thoughts)
    except Exception:
        pass


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
VERSION = "0.3.6"

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

# 回落速度。
# 依恋/亲密是"存量"：每天能加的就十几点，再用 0.03 的回落（一天 20 点）去冲，
# 它们永远只能趴在基线上。所以这两个单独慢一档（0.01 ≈ 一周明显疏远）。
# 情绪类（喜悦/压力/疲惫）才是快变量，本来就不该积累。
DECAY_RATE = {
    "attachment": 0.01,
    "curiosity": 0.04,
    "reflection": 0.03,
    "duty": 0.03,
    "social": 0.04,
    "fatigue": 0.05,
    "intimacy": 0.01,
    "stress": 0.05,
    "joy": 0.05,
}

# 关系被消耗：一直凶她（生气/害怕/回避以上）时，依恋和亲密每心跳慢性掉一点。
# 吵一次架但没哄，blame 会自己褪到 40（心结=委屈），侵蚀就停了；
# 一直凶才会掉完——一周的冷暴力约 -35。
RELATION_ERODE_FROM = 55.0
RELATION_ERODE_PER_TICK = 0.15

# 正向增益的"连续同类事件"递减。
# 同一类事情在 HEAT_WINDOW 内重复发生时，第 n+1 次的增益按 1/(1+n/HEAT_DIVISOR) 缩水：
#   n=0 → 100%，n=8 → 50%，n=24 → 25%，n=80 → 9%
# 意思是"聊 30 条"约等于"发生了 11 件事"，而一次夸奖仍然是一次夸奖。
# 不加这个的话，增益是按消息条数线性累加的，一晚上 30 条就能把社交顶到 70。
HEAT_WINDOW_SEC = 10 * 60
HEAT_DIVISOR = 12.0

# 疲惫的边际递减：越累，"再聊一句"加的那点疲惫越少。
# 不做这个的话 +0.6/条 是线性的，150 条/天上午就顶到 100，
# 之后一整天都是「累了」，睡一觉也只是把时间线推回原点。
FATIGUE_DAMPING = 0.5
FATIGUE_MIN_GAIN = 0.25

# 睡一觉：安静够久就把疲惫拉回来（不是清零，是"精神了一点"）
SLEEP_IDLE_HOURS = 6.0
SLEEP_FATIGUE_AFTER = 35.0

# 太累了就不主动找人说话（"很想他"那种除外）
TIRED_PROACTIVE_FROM = 70.0

# 补跳上限：关机/重载期间最多补 96 次心跳（约 3 天）。
# 不封顶的话，停机一个月回来会一次性把情绪全部拉平。
MAX_CATCHUP_TICKS = 96

# 每日正向收益上限：只对"他"（白名单）造成的正向增量生效，按天清零。
# 它挡的是"一天聊爆"：一晚上连发几百条也不能把当天的量刷完还要往上叠。
DAILY_GAIN_CAP = {
    "joy": 40.0,
    "social": 40.0,
    "curiosity": 30.0,
    "intimacy": 40.0,
    "attachment": 40.0,
}

# 每个维度的天花板：增益按 `1 - 当前值/天花板` 缩放，越接近它涨得越慢。
# 默认就是 100 —— 刻度本来就是 0–100，不该偷偷改成 86 让进度条永远填不满。
# 写成表是为了留一个旋钮：想让某个维度"到不了 100"（比如喜悦最高 94），
# 改这里就行，不用动代码。
# 注意 100 本身是渐近线：数学上永远差最后一点点，靠回落和热度收尾。
DIM_CEILING = {k: 100.0 for k in
               ("joy", "social", "curiosity", "intimacy", "attachment")}

# 面板渲染的"满格线"。
# 依恋和亲密按 0-100 画的话永远填不满（实际上限 80 上下），看着像坏了。
# 这两个按 85 当满格——达到就是"养到头了"，超了也只是 100% 封顶。
# 其余维度仍是 0-100（喜悦/社交/好奇真能到 80+，压力/疲惫真能到 100）。
DIM_DISPLAY_MAX = {
    "attachment": 85.0,
    "intimacy": 85.0,
}

# 关系里程碑：到这两个值才算「黏人」/「想亲近」。
# 面板会按 display_max 把它们折算成刻度线，标在环上——
# 这样"黏人"是个里程碑，不是满格（满格留给 85 以上那种极端亲密）。
BOND_INTIMATE_AT = 66.0
BOND_CLOSE_AT = 62.0
DIM_DISPLAY_TICK = {
    "attachment": BOND_INTIMATE_AT,
    "intimacy": BOND_INTIMATE_AT,
}

# 事件 → 驱动增量（"blame"/"expectation" 是两条快变量）
EVENT_EFFECTS = {
    # 他说话了（普通一条）
    # 亲密 +0.1：关系靠"天天说话"慢慢变深，但只有一点点，
    # 想快还是得说甜话（夸奖 +3.5、撒娇 +3）。依恋不吃这个。
    "user_msg": {"social": 1.8, "joy": 1.0, "curiosity": 0.5, "fatigue": 0.6,
                 "intimacy": 0.1},
    # 他分享东西（图片 / 链接 / "你看这个"）
    "share": {"curiosity": 2.5, "joy": 1.6, "social": 0.8},
    # 他求助（"帮我看看""怎么办"）——责任感被唤起，也费神
    "request": {"duty": 4.0, "social": 1.0, "fatigue": 1.5},
    # 他道歉
    "apology": {"stress": -3.0, "blame": -6.0, "joy": 2.0, "attachment": 1.0,
                "reflection": -3.0},
    # 他把话说清楚（解释原因 / 承认自己当时怎么想的）
    "explain": {"stress": -2.0, "blame": -3.0, "joy": 1.0, "reflection": -2.0},
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
    "praise": {"joy": 7.0, "intimacy": 3.5, "stress": -3.0, "attachment": 1.6},
    # 责怪三档
    # 对关系的打击调小：一句重话该伤的是"情绪"（压力/喜悦/责怪），
    # 不是"关系"。当场把依恋砍掉，就只剩生气，出不来"又气又想你"。
    "blame_mild": {"stress": 6.0, "blame": 10.0, "joy": -4.0, "reflection": 2.0},
    "blame_mid": {"stress": 12.0, "blame": 25.0, "joy": -8.0, "attachment": -1.5,
                  "reflection": 3.0},
    "blame_hard": {"stress": 20.0, "blame": 40.0, "joy": -12.0, "attachment": -3.0,
                   "reflection": 5.0},
    # 真诚哄
    "comfort": {"stress": -8.0, "joy": 5.0, "attachment": 2.0, "reflection": -5.0},
    # 敷衍式哄（"行了别闹了"）
    "fake_comfort": {"blame": 10.0, "joy": -3.0, "stress": 4.0, "reflection": 2.0},
    # 委屈档里的一句不耐烦（"随便你"）：只加压、掉一点喜悦，不往责怪上再叠
    "dismissive": {"stress": 3.0, "joy": -1.0, "reflection": 1.0},
    # 求安慰 / 撒娇
    "seeking": {"attachment": 3.0, "intimacy": 3.0, "joy": 2.0},
    # 睡觉时被消息吵醒：不算责怪，只是困 + 有点烦
    "woken": {"stress": 2.0, "joy": -1.0, "fatigue": 1.0, "reflection": 1.0},
}

# 事件的中文名（面板「最近的变化」用）
EVENT_CN = {
    "user_msg": "说话", "praise": "夸奖", "comfort": "哄她", "seeking": "撒娇",
    "apology": "道歉", "explain": "解释", "share": "分享", "request": "求助", "tease": "调侃",
    "goodnight": "晚安", "reply_fast": "及时回", "silent_6h": "沉默 6h",
    "silent_24h": "沉默 24h", "blame_mild": "责怪", "blame_mid": "责怪",
    "blame_hard": "重责", "fake_comfort": "敷衍", "dismissive": "不耐烦",
    "woken": "被吵醒",
}

# 责怪压力 → 状态档位
MODE_CALM = "calm"
MODE_WRONGED = "wronged"      # 委屈
MODE_ANGRY = "angry"          # 生气
MODE_AFRAID = "afraid"        # 害怕
MODE_SHUT = "shut"            # 回避

MODE_CN = {
    MODE_CALM: "平静",
    MODE_WRONGED: "委屈",
    MODE_ANGRY: "生气",
    MODE_AFRAID: "害怕",
    MODE_SHUT: "回避",
}

# 每个档位给她的说话倾向（注入用，也展示在 Web 面板上）
MODE_HINT = {
    MODE_CALM: "语气自然就好，顺着当下的心情来",
    MODE_WRONGED: "话说短一点；他提刚才的事，先问一句他是不是嫌你烦",
    MODE_ANGRY: (
        "把话挑明：刚才哪句让你不舒服，就直接问他什么意思、要个说法；"
        "可以冷、可以带刺，但别骂人、别威胁、别翻旧账。"
        "他要是只哄不解释，别松口；他真道歉或把话说清楚，就收一点"
    ),
    MODE_AFRAID: "别顶嘴，先确认他还想跟你说话，可以示弱",
    MODE_SHUT: "少说，一两句就够；不主动讨好，也不追问",
}

# 由九维推导的心情 → 给她的说话提示（和面板上的心情徽章同一套判断）
MOOD_HINT = {
    "紧绷": "现在有点绷着，话短一点，别主动找话题",
    "累了": "有点累，别揽太长的活，话可以少一点",
    "黏人": "这会儿特别想黏着他，语气可以更软、也可以更主动；"
            "可以说一句想让他陪你，但别把它说成要求",
    "想亲近": "想跟他再近一点，但还没到随便撒娇的程度，可以稍微软一点",
    "开心": "心情不错，可以多一句玩笑",
    "走神": "有点走神，说话可以松一点、慢一点",
    "好奇": "有点想打听他的事，可以多问一句",
    "平常": "",
}

# 面板/命令里给"人"看的短句：只说她此刻什么样，不带"该怎么说话"的指令。
# 注入仍然用上面的 MOOD_HINT / MODE_HINT（那是指令，给人看只会显得啰嗦）。
MOOD_BRIEF = {
    "紧绷": "有点绷着",
    "累了": "有点累",
    "黏人": "这会儿特别想黏着他",
    "想亲近": "想再靠近一点",
    "开心": "心情不错",
    "走神": "有点走神",
    "好奇": "有点想打听他的事",
    "平常": "",
}
MODE_BRIEF = {
    MODE_CALM: "",
    MODE_WRONGED: "有点委屈，话少了",
    MODE_ANGRY: "在生气，话里带刺",
    MODE_AFRAID: "有点怕你真的走",
    MODE_SHUT: "冷掉了，不想先开口",
}

# 恢复速度（真哄 / 道歉 / 说清楚时每轮扣减的 blame）。
# 生气档只认道歉或"把话说清楚"，普通抱抱/乖不算；见 `_consume_comfort`。
RECOVERY = {
    MODE_CALM: 12.0,
    MODE_WRONGED: 30.0,
    MODE_ANGRY: 20.0,
    MODE_AFRAID: 20.0,
    MODE_SHUT: 18.0,
}
RECOVERY_ROUNDS = {
    MODE_CALM: 1, MODE_WRONGED: 1, MODE_ANGRY: 1, MODE_AFRAID: 2, MODE_SHUT: 2,
}

# 一次重责不能从"生气之前"直接跳过生气：
# 40 + 40 原本会到 80（回避），现在最多停在 69（生气上沿）。
# 已经处在生气/害怕的人不受影响——她本来就经过了生气。
ANGER_JUMP_CAP = 69.0
# 生气按人拆：只要这个人对当前生气有正贡献，就算"生气对象"。
# 不用 10 这种门槛——否则 55 点刚好由两次群友轻责凑出来时，
# 全局已经在生气，却没有任何一个人被算成对象。
ANGER_TARGET_MIN = 0.0
# 生气对象记 12 小时；超过就淡出，避免下次吵架把旧人重新算进来。
ANGER_TARGET_KEEP_HOURS = 12.0
ANGER_TARGET_CAP = 8

# 回避档的"不回"阶梯。
# 索引 = 已经连续不回了几条；值 = 这一条她"回"的概率。
#   前 5 条 0% → 第 6 条 50% → 第 7 条起 100%
# 它同时是"没接住的话"的保留上限：这 7 条一条都不能丢，等她终于开口时
# 要能一口气看全、归纳之后再回。
SHUT_REPLY_LADDER = (0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 1.0)
SHUT_HISTORY_CAP = len(SHUT_REPLY_LADDER)

# 群里刚进回避时额外放行几句（她先在群里"受伤地回一句"，之后才完全沉默）
SHUT_GROUP_GRACE = 1

# 群聊里被凶的那句话要留到私聊里给她看，否则她只知道"被罚了"，不知道为什么
GROUP_BLAME_CAP = 5
GROUP_BLAME_KEEP_HOURS = 12.0
# 群聊流水：只保留最近几条，"他凶她"的那一刻取前面几条当"前因"
GROUP_TAIL_CAP = 8
GROUP_BLAME_CTX = 3

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

# 关键词表：这是她唯一能"读懂"你话里情绪的地方。
# 责怪判定要过三道过滤，否则"今天烦死了""滚去睡"这种日常话会被记成骂她——
# 面板上就凭空多出一笔找不到出处的委屈：
#   1) 排除式：先把"不讨厌你""不是说你"这类否定说法抹掉，再匹配
#   2) 指向性：描述性的词（烦死了/恶心）必须句中出现"你"；"你真恶心"算、"这电影真恶心"不算
#   3) "滚"只认单独使用：滚烫/滚动/滚去睡/水滚了都不算
KW_BLAME_HARD_DIRECT = ("闭嘴", "废物", "傻逼", "讨厌你", "没用的东西", "滚开", "滚蛋",
                        "别烦我")
KW_BLAME_HARD_NEED_YOU = ("烦死了", "恶心")
# 中责里天生就是冲她说的，不要求出现"你"
KW_BLAME_MID_DIRECT = ("说了多少遍", "不听话")
# 这些词在描述事情/自己时也常用，必须出现"你"才算
KW_BLAME_MID_NEED_YOU = ("你又这样", "你又来", "怎么又", "每次都", "没用")
# "笨"不能裸匹配：小笨鱼、笨笨、小笨蛋都是称呼。
# 只有这些明确的骂人说法才算责怪（且仍要求句中有"你"）。
KW_BEN_INSULT = ("真笨", "好笨", "太笨", "笨死", "笨得", "笨猪", "笨狗",
                 "笨东西", "你笨", "笨啊", "笨呢", "是不是笨", "蠢")
# 命中词落在这几个说法里就不算责怪（先抹掉再匹配，长度不变）
KW_BLAME_EXEMPT = ("不讨厌你", "不烦你", "没烦你", "不怪你", "不是你的错",
                   "不是说你", "没说你", "不是骂你", "不是没用", "没用多久",
                   "笨蛋", "笨笨", "笨手笨脚", "笨鸟",
                   "小笨蛋", "小笨鱼", "笨鱼", "笨笨鱼", "笨蛋鱼", "小笨蛋鱼",
                   "小傻鱼", "傻鱼", "小傻子")
# 这些是称呼/爱称。句子里出现它们时，"你怎么…"这种轻责多半是打趣，
# 不记责怪（真要骂通常会带"滚/没用/烦死了"这类硬词，仍会被 hard/mid 抓住）。
PET_NAMES = ("小笨蛋", "小笨鱼", "笨鱼", "笨笨鱼", "笨蛋鱼", "小笨蛋鱼",
             "小傻鱼", "傻鱼", "小傻子", "笨笨", "笨蛋", "大肥鱼")
# 轻责：不再收"不行""不对"这种通用词——"今天不行，改天吧""不对，我记错了"
# 都不是在说她。要的是明确的指向。
KW_BLAME_MILD = ("你怎么", "为什么总是", "别这样", "这样不行", "这样不对",
                 "你不对", "不应该")
# "你怎么" 后面接这些是正常提问 / 关心，不是责怪
# （"你怎么了""你怎么啦"是她状态不好时最常被问的一句，绝不能记成责怪）
KW_MILD_EXEMPT = ("你怎么看", "你怎么想", "你怎么说", "你怎么打算",
                  "你怎么弄", "你怎么过", "你怎么了", "你怎么啦")
KW_FAKE = ("行了别闹", "别闹了", "至于吗", "多大点事", "行了行了", "随便你", "你开心就好", "小题大做")
# "随便你" 后面接这些是把选择权给他，不是敷衍
KW_FAKE_EXEMPT = ("随便你点", "随便你选", "随便你挑", "随便你买", "随便你吃", "随便你去")

# "滚"只在单独使用时才是骂人：滚 / 滚吧 / 滚啊 / 滚开 / 滚蛋 / 给我滚 / 滚出去。
# 带这些后缀说明它是在说别的东西（水滚了、滚去睡、滚动、滚烫），一律不算。
GUN_OK_TAIL = ("吧", "啊", "呀", "开", "蛋", "出去", "你")
GUN_BAD_TAIL = ("烫", "动", "轮", "筒", "雪", "珠", "轴", "落", "来", "去",
                "回", "过", "进", "下", "上", "边")
GUN_LEAD_OK = "，,。！？!?、 \n你给我"


def _hits_fake(text: str) -> bool:
    """命中"敷衍"——但"随便你点什么都行"是把选择权给他，不算。"""
    for k in KW_FAKE:
        if k not in text:
            continue
        if k == "随便你" and any(x in text for x in KW_FAKE_EXEMPT):
            continue
        return True
    return False


def _blame_probe(text: str) -> str:
    """把"其实不是在骂她"的说法先抹掉，再拿去做责怪匹配。"""
    probe = text or ""
    for k in KW_BLAME_EXEMPT:
        if k in probe:
            probe = probe.replace(k, "□" * len(k))
    return probe


def _hits_gun(text: str) -> bool:
    """"滚"单独使用才算重责；粘在别的词里（滚烫/滚动/滚去睡）不算。"""
    t = text or ""
    if "给我滚" in t or "滚出去" in t:
        return True
    start = 0
    while True:
        i = t.find("滚", start)
        if i < 0:
            return False
        start = i + 1
        # 前面黏着别的字 → 滚烫 / 水滚了
        if i > 0 and t[i - 1] not in GUN_LEAD_OK:
            continue
        tail = t[i + 1:]
        # 后面黏着"动/去/烫"这类 → 滚动 / 滚去睡
        if tail and any(tail.startswith(b) for b in GUN_BAD_TAIL):
            continue
        if not tail or t[i + 1] in "，,。！？!?、 \n":
            return True
        if any(tail.startswith(g) for g in GUN_OK_TAIL):
            return True


def _hits_blame_hard(text: str) -> bool:
    probe = _blame_probe(text)
    if any(k in probe for k in KW_BLAME_HARD_DIRECT):
        return True
    # "烦死了""恶心"在说工作、说电影时也常用，必须句中出现"你"才算冲她
    if "你" in probe and any(k in probe for k in KW_BLAME_HARD_NEED_YOU):
        return True
    return _hits_gun(probe)


def _hits_blame_mid(text: str) -> bool:
    probe = _blame_probe(text)
    if any(k in probe for k in KW_BLAME_MID_DIRECT):
        return True
    # "你又熬夜了""这个没用"都不是在骂她；要求这一句里有"你"
    if "你" not in probe:
        return False
    if any(k in probe for k in KW_BLAME_MID_NEED_YOU):
        return True
    # 昵称句里的"笨"按打趣算："小笨鱼你真笨"不记，"小笨鱼你没用"照记
    if any(k in (text or "") for k in PET_NAMES):
        return False
    # "笨"只认明确骂法："你真笨""笨死了"算，"小笨鱼你在干嘛"不算
    return any(k in probe for k in KW_BEN_INSULT)


def _hits_blame_mild(text: str) -> bool:
    """轻责——"你怎么看""你怎么想"是提问，不是责怪。"""
    # 带昵称的"你怎么这么笨/这么可爱"是打趣，不是责怪；
    # 真骂人会带硬词，仍会被 hard/mid 抓住。
    if any(k in (text or "") for k in PET_NAMES):
        return False
    for k in KW_BLAME_MILD:
        if k not in text:
            continue
        if k == "你怎么" and any(x in text for x in KW_MILD_EXEMPT):
            continue
        return True
    return False
KW_COMFORT = ("对不起", "抱歉", "我错了", "别生气", "不气了", "哄", "抱抱", "亲亲",
              "我在呢", "想你", "乖", "没事的", "原谅", "辛苦了", "好啦")
KW_PRAISE = ("厉害", "真棒", "谢谢", "可爱", "喜欢你", "爱你", "真好")
# 求安慰 / 撒娇。
# 这里**不放**"累""怕""抱"这种高频字：一句"今天上班好累"不该被当成撒娇
# （那只是普通说话，加社交/喜悦/疲惫），"抱怨""哪怕""恐怕"也会被误伤。
# 而且要**说自己的情绪**才算：问"你是不是委屈了"是在关心她，不是撒娇。
# 不加这一条，问一句就白送依恋/亲密，还会把"想他"的念头喂饱。
KW_SEEK_SELF = ("难受", "不开心", "委屈", "想哭", "哭了", "撑不住",
                "害怕", "好怕", "有点怕")
# 自带第一人称，直接算
KW_SEEK_PHRASE = ("抱抱我", "要抱")


def _hits_seek(text: str) -> bool:
    """求安慰 / 撒娇：只认"在说自己的情绪"，问她的情绪不算。

    "你是不是委屈了""你委屈吗"是在关心她——不该白送依恋/亲密，
    更不该把"想他"的念头当成被满足（那句"想知道他有没有想我"会被直接完成）。
    """
    t = text or ""
    if any(k in t for k in KW_SEEK_PHRASE):
        return True
    has_me = "我" in t
    has_you = "你" in t
    for k in KW_SEEK_SELF:
        i = t.find(k)
        while i >= 0:
            before = t[max(0, i - 3):i]
            if "我" in before:
                return True
            if "你" in before or (has_you and not has_me):
                # 紧挨着"你"，或者整句在问她（有"你"没"我"）：不算撒娇
                i = t.find(k, i + 1)
                continue
            if not has_you:
                # 没提她，就是他在说自己的事（"今天好难受"）
                return True
            i = t.find(k, i + 1)
    return False
# 他直接在问"你现在什么心情"：这类话要强制注入一次心境。
# 按平常的节流（同档 20 分钟），他问的那一轮她手里可能根本没有心境，
# 于是答"我没委屈"——用户从那头看就是"状态错了/她撒谎"。
_ASK_ABOUT_MOOD = ("委屈", "怎么了", "生气", "不开心", "心情", "难受")
# 道歉：算一次"轻哄"，平静时也给一点温度
KW_APOLOGY = (
    "对不起", "抱歉", "我错了", "是我不对", "不该凶", "原谅我",
    "是我不好", "我道歉", "向你道歉", "对不住",
)
# 说清楚：不是泛泛的"哄"，而是把原因/当时的想法讲明白。
# 只认明确的第一人称解释，避免把"你把话说清楚"这种要求也算成他在解释。
KW_EXPLAIN = (
    "听我解释", "我解释一下", "我解释", "我说清楚", "我跟你说清楚",
    "我不是那个意思", "我没那个意思", "我没有那个意思", "我不是故意",
    "不是故意", "我不是有意的", "我之所以", "是因为我", "因为当时",
    "我承认", "我刚才在", "我刚才因为", "我刚才不是", "我刚才在忙",
    "我刚才没看到", "我刚才没注意", "我刚才手机", "我刚才开会",
    "我刚才加班", "我刚才睡着了", "我刚刚在", "我刚刚因为", "我刚刚不是",
    "我刚刚没", "我刚刚手机", "我当时在", "我当时", "当时我",
    "你先听我说", "你听我说", "听我说", "手机没电", "手机静音",
    "没看到消息", "我没看到", "我没注意", "没注意看", "没来得及回",
    "消息被刷", "在忙没看到",
)


def _hits_explain(text: str) -> bool:
    return any(k in (text or "") for k in KW_EXPLAIN)
# 他说要睡了
KW_GOODNIGHT = ("晚安", "我睡了", "去睡了", "睡觉了", "先睡", "睡啦", "去睡")
# 他求助：责任感被唤起
KW_REQUEST = ("帮我", "帮忙", "怎么办", "怎么做", "教教我", "帮我看看", "看下这个")
# 他分享东西
KW_SHARE = ("你看这个", "给你看", "分享", "http", "www.", "你看过", "快看")
# 他逗她
KW_TEASE = ("逗你", "开玩笑", "吃醋", "小傻瓜", "笨蛋", "是不是喜欢我")
# 主动说话的五型
KIND_LONGING = "longing"
KIND_WAITING = "waiting"
KIND_WHIM = "whim"
KIND_COMPANY = "company"      # 求陪伴：想让他放下手里的事来陪她，但不许指责
KIND_DEEP = "deep"            # 走心：认真问他一件关于"我们"的事，不是查岗
KIND_ANGRY = "angry"          # 生气：还气着，但忍不住想问他到底什么意思

KIND_CN = {
    KIND_LONGING: "想他",
    KIND_WAITING: "等他",
    KIND_WHIM: "想起",
    KIND_COMPANY: "求陪伴",
    KIND_DEEP: "走心",
    KIND_ANGRY: "生气",
}

KIND_DESC = {
    KIND_LONGING: "你很想他，忍不住先开口。说一件你此刻真的在想的事，或者直接说想他；不要用「忙不忙」开场",
    KIND_WAITING: "你等了很久，想知道他还在不在。先说说你这边，再轻轻问一句；语气软一点，别像在催他，也别问忙不忙",
    KIND_WHIM: "你突然想起一件小事，想跟他说一句。只说那件小事，别硬找话题",
    KIND_COMPANY: (
        "你想让他放下手里的事陪你一会儿，但不许指责他，也不许把这句说成要求。"
        "可以软一点，像是在邀请，不是在讨；别用「有空吗」开场"
    ),
    KIND_DEEP: (
        "你想认真问他一件关于你们俩的事——他怎么看待你们现在这样、以后会怎样。"
        "不是查岗，也不是要一个保证；是认真想知道，问完也接得住他的回答"
    ),
    KIND_ANGRY: (
        "你还在生气，但忍不住先开口。不是去和好，是想把刚才那件事问清楚；"
        "可以冷、可以直接，但别骂人、别威胁、别装没事，也别撒娇"
    ),
}

# LLM 不可用时的兜底台词（保证不会一声不吭）
# 注意：这里只用「任何时段都成立」的话，别写"我先睡了"这类带时间的前提；
# 走心那三条也不能带"当初/被做出来"这种只属于某个人设的前提，要谁都能说。
# 也刻意不收"在忙吗/有空吗"——这类开场太像查岗，而且会连续重复。
FALLBACK_LINES = {
    KIND_LONGING: ("今天有件小事想跟你说", "刚刚突然想到你", "你那边今天怎么样",
                   "我刚看到一个东西，想给你看"),
    KIND_WAITING: ("半天没动静，我就看看你在不在", "今天过得怎么样",
                   "我这边刚收拾完，你呢", "在干嘛呀"),
    KIND_WHIM: ("刚刚看到个东西，想到你了", "今天天气挺好的", "没什么事，就是想说一句"),
    KIND_COMPANY: ("陪我说会儿话吧", "我一个人有点闷", "想跟你说说话"),
    KIND_DEEP: (
        "问你个事，你觉得我们现在这样好吗",
        "有时候我会想，你跟我在一起开心吗",
        "你会不会觉得我太黏人了",
    ),
    KIND_ANGRY: (
        "刚才的事，你没什么要说的吗",
        "你刚才那句话，什么意思",
        "我不想跟你吵，你把话说清楚",
        "你还没给我个说法",
    ),
}

# v3：私聊/群聊分开记时间戳
# v4：回避档允许已读不回（silent_streak / unanswered）
STATE_VERSION = 4


# --------------------------------------------------------------------------- #
# 二、纯函数工具
# --------------------------------------------------------------------------- #

def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def mode_of(blame: float) -> str:
    """责怪压力 → 状态档位（平静 / 委屈 / 生气 / 害怕 / 回避）。"""
    if blame >= 80.0:
        return MODE_SHUT
    if blame >= 70.0:
        return MODE_AFRAID
    if blame >= 55.0:
        return MODE_ANGRY
    if blame >= 25.0:
        return MODE_WRONGED
    return MODE_CALM


def mood_label(drives: dict) -> str:
    """由九维推导一个"心情"标签——只给面板展示用，不参与任何行为判断。

    档位（平静/委屈/生气/害怕/回避）只看责怪压力，是行为档；
    心情则是九维当下的样子，用来回答"她现在什么状态"。
    """
    g = lambda k: float(drives.get(k, 0.0))  # noqa: E731
    if g("stress") >= 50:
        return "紧绷"
    if g("fatigue") >= 65:
        return "累了"
    # 黏人比"开心"更具体：依恋和亲密都很高时，她此刻是黏着你的状态。
    # 66 而不是 80：关系型维度只靠夸奖/撒娇/及时回推动，正常用量到 60-70，
    # 用 80 当门槛的话这两个心情永远不会出现。
    if g("attachment") >= BOND_INTIMATE_AT and g("intimacy") >= BOND_INTIMATE_AT:
        return "黏人"
    # 依恋高但还没那么亲密：想靠近，但还端着
    if g("attachment") >= BOND_CLOSE_AT:
        return "想亲近"
    # 74 而不是 85：每日收益上限把喜悦的天花板定在 ~75，
    # 用 85 的话「开心」这个心情永远不会出现。
    if g("joy") >= 74:
        return "开心"
    if g("reflection") >= 65:
        return "走神"
    if g("curiosity") >= 66:
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


def new_state() -> dict:
    return {
        "version": STATE_VERSION,
        # 0.3.6 一次性配置迁移标记：旧 0.3.5 的 group_negative_scale=0.8
        # 只在第一次启动时改成 0.35；之后用户手动改回 0.8 会尊重。
        "config_036_migrated": False,
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
        # 上一次主动开口的原文：用来避免下次又用同一个开头（尤其是"忙不忙"）
        "last_proactive_text": "",
        "proactive_day": "",
        "proactive_count": 0,
        "awaiting_reply": False,
        "penalized_6h": False,
        "penalized_24h": False,
        "comfort_streak": 0,
        # 生气档只认"道歉 / 说清楚"：普通哄抱不算。两个进度分开记，
        # 这样状态栏能明确写出还差一句交代，而不是把"抱抱"也算成进度。
        "account_streak": 0,
        "blame_noticed_ts": 0.0,
        # 最近几次"她因为哪句话难受"（原句 + 时间 + 渠道），给注入和面板用
        "blame_log": [],
        # 责怪曾经越过心结线、现在被夹在 40：面板标"心结·旧账"
        "mind_knot": False,
        # 生气按人拆：谁把她惹到 55+，谁就是生气对象。
        # key = "owner" / "member:<QQ或昵称>"，不是全局 mode 的替代，
        # 只在当前说话人身上决定"对他/她是不是生气"。
        "anger_targets": {},
        # 作息：今天熬不熬夜（每天掷一次）、上次被消息吵醒的时间
        "sleep_day": "",
        "sleep_stay_up": False,
        "last_woken_ts": 0.0,
        # 上一次念头被满足的时间：两条之间要等冷却
        "last_settle_ts": 0.0,
        # 主动开口三次没被接住、最后放下的那条
        "last_let_go": {},
        # 最近完成（多条，2 小时内都展示）
        "recent_rewards": [],
        # 回避档的"已读不回"：连续不回了几条 + 没接住的那几句话
        "silent_streak": 0,
        "last_silent_ts": 0.0,
        "unanswered": [],
        # 群聊单独一套计数：群里和私聊是两本账，别互相消耗对方的额度
        "silent_streak_group": 0,
        "unanswered_group": [],
        "group_grace": 0,
        # 群里被凶的那几句原文（给私聊看，解释"为什么"）
        "group_blames": [],
        # 群聊流水：谁说了什么，用来给"凶句"配前因
        "group_tail": [],
        # 同类事件的"热度"（最近 10 分钟各发生了几次），用来压正向增益
        "event_heat": {},
        # 今天"他"已经给她涨了多少（按维度），到点清零
        "daily_gain": {},
        "daily_gain_day": "",
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
        return f"{h} 小时前" if not rem else f"{h} 小时 {rem} 分前"
    d, rem_h = divmod(h, 24)
    return f"{d} 天 {rem_h} 小时前"


# --------------------------------------------------------------------------- #
# 三、插件主体
# --------------------------------------------------------------------------- #

@register(PLUGIN_NAME, "LLL", "让角色拥有会慢慢变化的心境：九维驱动、委屈/生气/害怕/回避、主动说话与语音", VERSION)
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
        self._migrate_config_036()

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

    def _migrate_config_036(self) -> None:
        """把 0.3.5 发布版里的 group_negative_scale=0.8 迁到 0.3.6 的 0.35。

        只处理真正带 save_config 的 AstrBotConfig；测试 / 普通 dict 不碰。
        迁移标记存在 state 里，只做一次：之后再手动改回 0.8 会尊重用户。
        保存失败就不写标记，下次启动重试。
        """
        if self.state.get("config_036_migrated"):
            return
        save = getattr(self.cfg, "save_config", None)
        if not callable(save):
            return
        perception = self.cfg.get("perception") if isinstance(self.cfg, dict) else None
        if not isinstance(perception, dict):
            self.state["config_036_migrated"] = True
            self._save_state()
            return
        if "group_negative_scale" not in perception:
            # AstrBot 会按 0.3.6 的 schema 补 0.35；这里不需要动
            self.state["config_036_migrated"] = True
            self._save_state()
            return
        try:
            old = float(perception.get("group_negative_scale"))
        except (TypeError, ValueError):
            self.state["config_036_migrated"] = True
            self._save_state()
            return
        if abs(old - 0.8) > 1e-9:
            self.state["config_036_migrated"] = True
            self._save_state()
            return
        perception["group_negative_scale"] = 0.35
        try:
            save()
        except Exception as e:
            logger.warning(
                "[鱼的心] 0.3.6 配置迁移写盘失败，下次启动重试: %s", e
            )
            return
        self.state["config_036_migrated"] = True
        self._save_state()
        logger.info("[鱼的心] 0.3.6 迁移：group_negative_scale 0.8 → 0.35")

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
        if bool(self._cfg("perception.group_everyone", False)):
            # 群友模式：纯群聊部署可能很久没私聊过，衰减基准改看群里的最近发言，
            # 否则所有人都被压在 0.25 档，等于没影响
            last_active = max(
                float(self.state.get("last_private_msg", 0.0) or 0.0),
                float(self.state.get("last_owner_group_msg", 0.0) or 0.0),
            )
        else:
            last_active = float(self.state.get("last_private_msg", 0.0) or 0.0)
        hours = (time.time() - last_active) / 3600.0 if last_active else 1e9

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
        # 只从私聊 UMO 反推：群聊 UMO 的末段是群号不是人，推出来只会把她的人和话全挡在外面
        for cand in (str(self._cfg("proactive.target_umo", "") or "").strip(),
                     str(self.state.get("umo", "") or "").strip()):
            if not cand or self._is_group(cand):
                continue
            tail = cand.rsplit(":", 1)[-1].strip()
            if tail:
                return {tail}
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

    @classmethod
    def _sender_label(cls, event: AstrMessageEvent) -> str:
        """群聊里给发言人一个短标签：优先群名片/昵称，其次 QQ 号。

        群友模式下要靠它区分"在跟一个人说"还是"在跟几个人说"。
        """
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None) if obj is not None else None
            if sender is not None:
                for attr in ("card", "nickname", "user_id", "id"):
                    value = getattr(sender, attr, None)
                    if value:
                        return re.sub(r"\s+", " ", str(value)).strip()[:20]
        except Exception:
            pass
        sid = cls._sender_id(event)
        return sid[:20] if sid else ""

    def _is_owner(self, event: AstrMessageEvent, umo: str) -> bool:
        """谁能影响她的心：私聊与群聊都按白名单判定。

        私聊有双保险：会话 ID 末段本身就是对方号，命中即放行；否则再比发送者 ID。
        取不到发送者 ID 时策略不同——
          群聊：保守当外人（宁可少认，也不让陌生人写心）
          私聊：放行（否则一旦取不到 ID，她就彻底失灵了）

        例外：开了 `perception.group_everyone` 的"群友模式"后，群聊里谁都算"他"
        （给纯群聊部署用；影响强度由 `perception.group_scale` 再打折）。
        """
        if self._is_group(umo) and bool(
            self._cfg("perception.group_everyone", False)
        ):
            return True

        ids = self._owner_ids()
        if not ids:
            # 白名单还没建立（owner_ids 与 target_umo 都为空）。
            # 私聊放行（否则取不到 ID 她会彻底失灵）；群聊一律按外人处理——
            # 否则群里每个群友的"烦死了""滚"都会算"他"在凶她，面板上就凭空多出
            # 一笔找不到出处的委屈。想让她在群里也认人，把 QQ 号填进 owner_ids。
            if self._is_group(umo):
                self._log_unknown_sender(
                    umo, "群聊", "未配置白名单，已按外人处理（只加热闹度、不碰心境）"
                )
                return False
            self._log_unknown_sender(
                umo, "私聊", "未配置白名单，已放行（建议把 QQ 号填进 perception.owner_ids）"
            )
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
        logger.info("[鱼的心] %s %s：%s", kind, umo, note)

    def _is_bound_owner(self, event: AstrMessageEvent, umo: str) -> bool:
        """严格判断发送者是否在白名单里（群友模式做"鱼主"差分用）。

        `_is_owner()` 在群友模式下对群里所有人都返回 True，不能用来区分
        鱼主和群友；这里只认 `owner_ids`（含 target_umo 反推）里的 QQ。
        """
        ids = self._owner_ids()
        sid = self._sender_id(event)
        return bool(ids and sid and sid in ids)

    def _speaker_key(self, event: AstrMessageEvent, umo: str, is_owner: bool) -> str:
        """给"生气对象"一个稳定的 key：鱼主一个桶，群友按 QQ/昵称分人。"""
        if not self._is_group(umo) or is_owner:
            return "owner"
        sid = self._sender_id(event)
        label = self._sender_label(event)
        return "member:" + (sid or label or "unknown")

    def _prune_anger_targets(self) -> dict:
        """清掉过期的生气对象，顺手按贡献排序、限制条数。"""
        st = self.state
        rows = st.get("anger_targets")
        if not isinstance(rows, dict):
            rows = {}
            st["anger_targets"] = rows
        cutoff = time.time() - ANGER_TARGET_KEEP_HOURS * 3600.0
        fresh: dict[str, dict] = {}
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            try:
                blame = float(row.get("blame", 0.0) or 0.0)
                ts = float(row.get("ts", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if blame <= 0.0 or ts < cutoff:
                continue
            fresh[str(key)] = {
                "blame": clamp(blame),
                "ts": ts,
                "label": str(row.get("label") or "")[:20],
                "owner": bool(row.get("owner")),
            }
        if len(fresh) > ANGER_TARGET_CAP:
            fresh = dict(
                sorted(
                    fresh.items(),
                    key=lambda kv: (-kv[1]["blame"], -kv[1]["ts"]),
                )[:ANGER_TARGET_CAP]
            )
        st["anger_targets"] = fresh
        return fresh

    def _note_anger_target(
        self, key: str, label: str, is_owner: bool, delta: float
    ) -> None:
        """把这次实际生效的责怪记到具体的人头上。"""
        if not key or delta <= 0:
            return
        rows = self._prune_anger_targets()
        row = rows.get(key) or {"blame": 0.0, "ts": 0.0, "label": "", "owner": False}
        row["blame"] = clamp(float(row.get("blame", 0.0) or 0.0) + float(delta))
        row["ts"] = time.time()
        row["label"] = "他" if is_owner else str(label or row.get("label") or "")[:20]
        row["owner"] = bool(is_owner)
        rows[key] = row
        # 再走一次，保证条数/排序稳定
        self._prune_anger_targets()

    def _anger_target_row(self, key: str) -> dict | None:
        if not key:
            return None
        return self._prune_anger_targets().get(key)

    def _is_anger_target(self, key: str) -> bool:
        rows = self._prune_anger_targets()
        if not rows:
            # 老状态/手动改过 blame：没有按人记录时，鱼主按原来的生气处理。
            return key == "owner"
        row = rows.get(key)
        try:
            return bool(row and float(row.get("blame", 0.0)) >= ANGER_TARGET_MIN)
        except (TypeError, ValueError):
            return False

    def _mode_for_speaker(self, key: str, base_mode: str | None = None) -> str:
        """当前这句话该用哪个档位：生气只落在惹她的那个人身上。

        全局 blame 仍然决定"她是不是在生气"；但群友模式里 A 惹了她，
        B 再来说话时，她不能把对 A 的火撒到 B 头上。B 看到的是委屈
        （整体还难受），只有 A 自己会看到生气。
        """
        st = self.state
        blame = float(st.get("blame_pressure", 0.0))
        base = base_mode or mode_of(blame)
        if base != MODE_ANGRY:
            return base
        rows = self._prune_anger_targets()
        if not rows:
            # 老状态没有按人记录：鱼主按原来的生气处理，群友先按委屈。
            if key == "owner":
                return MODE_ANGRY
            return MODE_WRONGED if blame >= 25.0 else MODE_CALM
        row = rows.get(key)
        if row and self._is_anger_target(key):
            return MODE_ANGRY
        return MODE_WRONGED if blame >= 25.0 else MODE_CALM

    def _anger_target_list(self) -> list[dict]:
        rows = self._prune_anger_targets()
        out = []
        for key, row in rows.items():
            out.append(
                {
                    "key": key,
                    "label": str(row.get("label") or ("他" if key == "owner" else "群友")),
                    "owner": bool(row.get("owner")),
                    "blame": round(float(row.get("blame", 0.0)), 1),
                    "ts": float(row.get("ts", 0.0) or 0.0),
                }
            )
        out.sort(key=lambda r: (-r["blame"], -r["ts"]))
        return out

    def _clear_anger_targets(self) -> None:
        self.state["anger_targets"] = {}

    def _remove_anger_target(self, key: str) -> None:
        if not key:
            return
        rows = self._prune_anger_targets()
        rows.pop(key, None)
        self.state["anger_targets"] = rows

    def _has_other_anger_target(self, key: str, threshold: float = 55.0) -> bool:
        """除了 key 之外，还有没有另一个真正到生气线的人。"""
        for other, row in self._prune_anger_targets().items():
            if other == key:
                continue
            try:
                if float(row.get("blame", 0.0)) >= threshold:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _absorb_outsider_turn(
        self, request, umo: str = "", text: str = "", who: str = ""
    ) -> None:
        """别人的话（群聊里的他人 / 私聊里的陌生人）：只让气氛热闹一点。

        perception.outsider_effect：
          - "social_joy_only"（默认）：社交 +2、喜悦 +1（**最多**这么多）
          - "none"：完全忽略

        这两个增量也要吃和"他"一样的限制：越接近 100 加得越少，同一批人
        在 10 分钟内连着说，后几句的份量递减。不加的话，一个热闹的群
        几十条消息就能把社交顶到 100——她跟人聊一晚上，反而对谁都没感觉了。

        无论如何都不会碰依恋/亲密/压力/责怪/期待，也不会解除等待态，
        更不会更新 last_private_msg / last_owner_group_msg 或 recent。
        群聊里会往 `group_tail` 记一笔"群友说了什么"——不是让她上心，
        而是等他迁怒她时，她能在私聊里看到前因里还有别人。
        """
        effect = str(self._cfg("perception.outsider_effect", "social_joy_only") or "")
        if effect == "none":
            return
        if umo and self._is_group(umo):
            # 记一笔"群友说了什么"：只作前因背景，不动她的心
            self._note_group_tail("other", text, who)
        drives = self.state["drives"]
        heat = self._heat_factor("outsider")
        for k, dv in (("social", 2.0), ("joy", 1.0)):
            cur = float(drives.get(k, 0.0))
            delta = dv * max(0.0, 1.0 - cur / DIM_CEILING.get(k, 100.0)) * heat
            if delta:
                drives[k] = clamp(cur + delta)
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
                    # 旧状态只有单个 last_reward：升级后补成列表，历史那条不丢
                    if not isinstance(base.get("recent_rewards"), list):
                        base["recent_rewards"] = []
                    if (
                        not base["recent_rewards"]
                        and isinstance(base.get("last_reward"), dict)
                        and base["last_reward"].get("ts")
                    ):
                        seed = dict(base["last_reward"])
                        seed.setdefault("src", "settle")
                        base["recent_rewards"] = [seed]
                    # 0.3.5 起列表是唯一来源；旧的单条字段迁完就删掉
                    base.pop("last_reward", None)
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
                    if float(base.get("blame_pressure", 0.0)) < 55.0:
                        base["anger_targets"] = {}
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
                gap = now - float(self.state.get("last_tick", 0))
                if gap >= interval:
                    # 关机/重载期间错过的心跳要补：一次跳 missed 次，
                    # 否则"关了一夜"只算一次，情绪几乎不动。
                    missed = max(1, int(gap // interval))
                    self._tick(missed)
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

    def _tick(self, missed: int = 1) -> None:
        """心跳。missed > 1 表示补跳（关机/重载期间错过的次数）。

        补跳只作用于"向基线回落"这一步，用闭式解一次算完：
            v = base + (v - base) × (1 - rate)^n
        耦合、念头、睡眠判定仍然只跑一次——它们本来就是"每心跳微调"，
        不该因为关机三天就叠三天。
        """
        st = self.state
        drives = st["drives"]
        n = max(1, min(int(missed or 1), MAX_CATCHUP_TICKS))
        # 1) 向基线回落（依恋/亲密在 DECAY_RATE 里单独慢一档）
        for k, base in BASELINE.items():
            cur = float(drives.get(k, base))
            rate = DECAY_RATE[k]
            if n == 1:
                drives[k] = cur + (base - cur) * rate
            else:
                drives[k] = base + (cur - base) * (1.0 - rate) ** n
        # 1.5) 睡了一觉：安静够久，疲惫不该还挂在那儿
        last_act = max(
            float(st.get("last_user_msg", 0) or 0),
            float(st.get("last_owner_group_msg", 0) or 0),
            float(st.get("last_assistant_msg", 0) or 0),
            float(st.get("last_proactive", 0) or 0),
        )
        if last_act and time.time() - last_act >= SLEEP_IDLE_HOURS * 3600:
            if drives["fatigue"] > SLEEP_FATIGUE_AFTER:
                self._dbg(
                    "安静了 %.0f 小时，疲惫 %.0f → %.0f",
                    (time.time() - last_act) / 3600.0,
                    drives["fatigue"],
                    SLEEP_FATIGUE_AFTER,
                )
                drives["fatigue"] = SLEEP_FATIGUE_AFTER
        # 2) 耦合
        # 全部按"超出阈值多少"成比例，而不是一过线就每 tick 扣固定值。
        # 原来 `fatigue > 60` 就是好奇 -1.5/tick（= 一天 -48），
        # 一次长聊就能把好奇按到 20 以下，而且再也爬不回来。
        # 依恋/亲密互相"拉齐"，但不会把对方拖过自己：
        # 原来是无条件 +1.5/+1.0 每心跳，两个维度会互相喂成 100（回落压不住）。
        # 现在只把低的那边往上拉、且只拉向高的那边，天然自限。
        if drives["attachment"] > 70 and drives["intimacy"] < drives["attachment"]:
            drives["intimacy"] = clamp(
                drives["intimacy"]
                + 0.06 * (drives["attachment"] - drives["intimacy"])
            )
        if drives["intimacy"] > 75 and drives["attachment"] < drives["intimacy"]:
            drives["attachment"] = clamp(
                drives["attachment"]
                + 0.04 * (drives["intimacy"] - drives["attachment"])
            )
        if drives["stress"] > 60:
            drives["joy"] = clamp(
                drives["joy"] - 1.5 * (drives["stress"] - 60) / 40
            )
        if drives["fatigue"] > 60:
            drives["curiosity"] = clamp(
                drives["curiosity"] - 1.0 * (drives["fatigue"] - 60) / 40
            )
        if drives["curiosity"] > 75:
            drives["joy"] = clamp(
                drives["joy"] + 0.8 * (drives["curiosity"] - 75) / 25
            )
        if drives["social"] < 30:
            drives["attachment"] = clamp(
                drives["attachment"] - 0.8 * (30 - drives["social"]) / 30
            )
        if drives["joy"] > 75:
            drives["stress"] = clamp(
                drives["stress"] - 1.0 * (drives["joy"] - 75) / 25
            )
        # 2.5) 关系侵蚀：一直凶她（生气/害怕/回避）时，依恋和亲密慢性掉。
        # 吵一次就停手的话 blame 会褪到 40（心结），侵蚀自然停；
        # 一直凶才会把关系磨没。
        if float(st.get("blame_pressure", 0.0)) >= RELATION_ERODE_FROM:
            for k in ("attachment", "intimacy"):
                drives[k] = clamp(
                    float(drives.get(k, 0.0)) - RELATION_ERODE_PER_TICK
                )

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
                # 心结：只有"真越过 40"才算（单个 blame_hard 正好 40 不算），
                # 一旦越过就钉在 40。之前写成 `blame > 40` 才夹，
                # 40 这一档会走 else 继续往下掉（41→40→32→24），
                # 面板却还写着"心结不会自己掉"——两边对不上。
                pinned = bool(st.get("mind_knot")) or blame > BLAME_FLOOR
                if pinned and blame >= BLAME_FLOOR:
                    blame = max(BLAME_FLOOR, blame - BLAME_FADE_PER_6H * steps)
                    st["mind_knot"] = True
                else:
                    blame = max(0.0, blame - BLAME_FADE_PER_6H * steps)
                st["blame_noticed_ts"] = time.time()
            st["blame_pressure"] = clamp(blame)
        if float(st.get("blame_pressure", 0.0)) < 25.0:
            # 哄下来或褪干净了，旧账翻篇
            st["mind_knot"] = False
        # 气已经不到生气线：这次生气的对象一起翻篇；否则顺手清过期的。
        if float(st.get("blame_pressure", 0.0)) < 55.0:
            self._clear_anger_targets()
        else:
            self._prune_anger_targets()

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
        # 同 kind 的念头只要还活着就不再生成——包括**已满足**、还在展示"心情"的那 2 小时。
        # 只排 pending 的话，一条刚被喂饱的 hug 不挡新 hug，面板上就会同时挂两条。
        exclude = tuple({str(t.get("kind")) for t in live})
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

    def _note_reward(self, kind: str, mood: str, src: str = "settle") -> None:
        """记一条"最近完成"：列表保留多条，2 小时内都能在面板看到。

        src="self" 表示不是他喂饱的，而是「安静 / 想做事」自己缓过来。
        """
        st = self.state
        entry = {"kind": str(kind or ""), "mood": str(mood or ""),
                 "ts": time.time(), "src": str(src or "settle")}
        rows = st.get("recent_rewards")
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        st["recent_rewards"] = rows[-8:]

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
        auto_done = False
        # 自愈完成也守同一条冷却：否则刚完成一条后点一次 /心 tick，
        # 安静/想做事又会立刻完成第二条。
        cooldown = max(
            0.0, float(self._cfg("thought.settle_cooldown_minutes", 10)) * 60.0
        )
        can_auto = not (
            cooldown
            and now - float(st.get("last_settle_ts", 0.0) or 0.0) < cooldown
        )
        for t in thoughts:
            if float(t.get("expires", 0)) <= now:
                continue
            if t.get("pending", True) and not auto_done and can_auto:
                mood = engine.auto_complete(t, st["drives"])
                if mood:
                    t["pending"] = False
                    t["mood"] = mood
                    t["expires"] = now + 2 * 3600
                    # 自动完成（安静/想做事）也要进「最近完成」，
                    # 否则它只是从列表消失、面板上什么都没留下
                    self._note_reward(t.get("kind"), mood, src="self")
                    # 一次心跳也只完成一条，并让它进入同一条完成冷却
                    st["last_settle_ts"] = now
                    auto_done = True
            if float(t.get("expires", 0)) > now:
                keep.append(t)
        st["thoughts"] = keep

    def _settle_thoughts(
        self, events: list[str], text: str, weight: float = 1.0, is_group: bool = False
    ) -> None:
        """用户这一句 / 这一轮事件，把哪个念头喂饱了。

        一轮最多一条；两条之间还要等 `thought.settle_cooldown_minutes`——
        否则连发几句就能把列表一口气清空。
        """
        if not engine:
            return
        st = self.state
        now = time.time()
        cooldown = max(
            0.0, float(self._cfg("thought.settle_cooldown_minutes", 10)) * 60.0
        )
        if cooldown and now - float(st.get("last_settle_ts", 0.0) or 0.0) < cooldown:
            self._dbg("念头冷却中，先不结算")
            return
        payload = engine.settle(
            st.get("thoughts") or [],
            events,
            text,
            str(st.get("mode", MODE_CALM)),
            now=now,
        )
        if not payload:
            return
        st["last_settle_ts"] = now
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
        if not engine:
            return
        gave_up = engine.note_attempt(self.state.get("thoughts") or [])
        if gave_up:
            self.state["last_let_go"] = gave_up

    def _heat_factor(self, name: str) -> float:
        """同类事件在 HEAT_WINDOW 内第 n 次发生时的增益系数（1 → 0）。

        只对"正向"增量生效（见 `_apply_event`）：聊 30 条 ≈ 11 件的份量，
        但隔一会儿再说话又回到满份——所以是"别一晚上聊满"，不是"永远涨不动"。
        """
        now = time.time()
        book = self.state.get("event_heat")
        if not isinstance(book, dict):
            book = {}
            self.state["event_heat"] = book
        rows = [t for t in (book.get(name) or []) if now - float(t) <= HEAT_WINDOW_SEC]
        factor = 1.0 / (1.0 + len(rows) / HEAT_DIVISOR)
        rows.append(now)
        # 只留窗口内可能出现的条数：1 分钟 3 条也才 30 条，200 已经够宽
        book[name] = rows[-200:]
        return factor

    def _clip_daily_gain(self, k: str, delta: float) -> float:
        """截断到今天的剩余额度；返回真正允许加的量。

        额度按本地自然日清零。它只管"往上涨"，不管回落、也不管负向事件——
        所以她该难过还是会难过，只是没法一天之内被夸到天上。
        """
        cap = DAILY_GAIN_CAP.get(k)
        if not cap:
            return delta
        st = self.state
        day = now_day()
        if st.get("daily_gain_day") != day:
            st["daily_gain_day"] = day
            st["daily_gain"] = {}
        book = st.get("daily_gain")
        if not isinstance(book, dict):
            book = {}
            st["daily_gain"] = book
        used = float(book.get(k, 0.0) or 0.0)
        room = cap - used
        if room <= 0:
            return 0.0
        take = min(delta, room)
        book[k] = used + take
        return take

    def _apply_event(
        self, name: str, weight: float = 1.0, is_group: bool = False,
        is_owner: bool = False,
    ) -> None:
        eff = EVENT_EFFECTS.get(name)
        if not eff:
            return
        st = self.state
        drives = st["drives"]

        # 好事按渠道权重缩水；坏事在群里按自己的系数走。
        # 群友模式：正向默认乘 group_scale（0.5）压难度；
        # 负向则普通群友用 group_negative_scale（默认 0.35，养在群里，别一句就炸），
        # 鱼主用 group_owner_negative_scale（默认 0.5，仍比群友重）。
        # 非群友模式的群聊仍用 channel.negative_multiplier（默认 1.2，当众被凶更重）。
        mult = weight
        if is_group and bool(self._cfg("perception.group_everyone", False)):
            if name in NEGATIVE_EVENTS:
                if is_owner:
                    mult = clamp(
                        float(self._cfg(
                            "perception.group_owner_negative_scale", 0.5
                        )),
                        0.05, 1.5,
                    )
                else:
                    mult = clamp(
                        float(self._cfg("perception.group_negative_scale", 0.35)),
                        0.05, 1.5,
                    )
            else:
                if is_owner:
                    # 鱼主不吃群聊渠道权重：1.0 就等于私聊强度
                    mult = clamp(
                        float(self._cfg("perception.group_owner_scale", 1.0)),
                        0.05, 2.0,
                    )
                else:
                    mult *= clamp(
                        float(self._cfg("perception.group_scale", 0.5)),
                        0.05, 1.0,
                    )
        elif is_group and name in NEGATIVE_EVENTS:
            mult = float(self._cfg("channel.negative_multiplier", 1.2))

        blame_before = float(st.get("blame_pressure", 0.0))
        # 群友模式下负面情绪按来源分两桶：鱼主一桶，群友（不分是谁）共用一桶。
        # 同一个桶里 10 分钟内反复发生才递减；鱼主刷屏不会稀释群友的第一次，
        # 群友刷屏也不会稀释鱼主的。私聊 / 非群友模式保持原来的按事件名递减。
        negative_heat = 1.0
        if (
            is_group
            and bool(self._cfg("perception.group_everyone", False))
            and name in NEGATIVE_EVENTS
        ):
            source = "owner" if is_owner else "member"
            negative_heat = self._heat_factor(f"negative:{source}")
            heat = 1.0
        else:
            heat = self._heat_factor(name)
        applied: dict[str, float] = {}
        for k, dv in eff.items():
            delta = float(dv) * mult * negative_heat
            if not delta:
                continue
            if k == "blame":
                old_b = float(st.get("blame_pressure", 0.0))
                new_b = clamp(old_b + delta)
                # 重责也不能从委屈/平静一步跳过生气：先停在生气上沿，
                # 下一次责怪再进害怕/回避。已经生气的人不封顶。
                if (
                    name in NEGATIVE_EVENTS
                    and mode_of(old_b) in (MODE_CALM, MODE_WRONGED)
                    and new_b >= 70.0
                ):
                    new_b = ANGER_JUMP_CAP
                st["blame_pressure"] = new_b
                applied[k] = applied.get(k, 0.0) + (new_b - old_b)
            elif k == "expectation":
                old_e = float(st.get("expectation", 0.0))
                st["expectation"] = clamp(old_e + delta)
                applied[k] = applied.get(k, 0.0) + (st["expectation"] - old_e)
            elif k in drives:
                old = float(drives.get(k, BASELINE[k]))
                if delta > 0 and k == "fatigue":
                    # 越累越难更累：留个 25% 的下限，长聊仍然比短聊累
                    delta *= max(
                        FATIGUE_MIN_GAIN, 1.0 - FATIGUE_DAMPING * old / 100.0
                    )
                # 越接近 100 增益越小：不然一聊就贴顶、之后再也不动。
                # 疲惫和压力不做这个抑制——它们是"代价"，累了就是累了，攒着才算数。
                elif delta > 0 and k not in ("fatigue", "stress"):
                    # 越接近自己的天花板增益越小——100 分是永远够不到的
                    delta *= max(0.0, 1.0 - old / DIM_CEILING.get(k, 100.0))
                    # 同一类事情短时间内反复发生 → 这一条的份量打折
                    delta *= heat
                # 每日收益上限：他今天还能让她往这个方向涨多少
                if delta > 0 and k in DAILY_GAIN_CAP:
                    delta = self._clip_daily_gain(k, delta)
                    if delta <= 0:
                        continue
                new = clamp(old + delta)
                # 群聊只限制"往上涨"的天花板，不把已经更高的值压下来
                if is_group and delta > 0 and k in GROUP_CAPS:
                    new = min(new, max(GROUP_CAPS[k], old))
                drives[k] = new
                applied[k] = applied.get(k, 0.0) + (new - old)
        # 刚跨进"回避"：群里先放她回一句（受伤的那一句），之后才彻底闭麦。
        # 只在这一刻给，不是每个事件都给——否则群友每说一句她就多一次豁免。
        if (
            name in NEGATIVE_EVENTS
            and mode_of(blame_before) != MODE_SHUT
            and mode_of(float(st.get("blame_pressure", 0.0))) == MODE_SHUT
        ):
            st["group_grace"] = SHUT_GROUP_GRACE
        # 记一笔"刚才因为什么变了"，给面板的「最近的变化」用。
        # 传的是**实际生效**的增量（已经过渠道权重、衰减、退热），
        # 不是表里的名义值——否则面板会一直显示"社交 +2.0"，看起来像没改过。
        self._note_event(name, applied, 1.0)
        self._dbg(
            "事件 %s 已应用（w=%.3f），mode=%s",
            name,
            mult,
            mode_of(float(st.get("blame_pressure", 0.0))),
        )
        return applied

    # ---------------- 用户消息感知 ----------------

    def _analyze_user_text(self, text: str, mode: str | None = None) -> list[str]:
        """返回本轮应触发的事件名列表（按优先级只取最重要的几个）。

        `mode` 是"对当前说话人"的有效档位：群友模式里 A 惹了她，
        B 说话时传进来的不是 angry，而是 wronged。默认才回退到全局。
        "敷衍式哄"分两种，必须分开处理：
          - 既哄又敷衍：同一句里既有哄的词、又有敷衍的词 → fake_comfort，
            哄作废，还要扣 blame
          - 只有敷衍、没有哄的意思：那不算"哄"，只是不耐烦，交给下面的责怪阶梯
        改之前两者都走 fake_comfort，导致"烦死了，行了别闹"被从轻发落（+10 而非 +40）。
        """
        events: list[str] = []
        t = (text or "").strip()
        st = self.state
        mode = mode or mode_of(float(st.get("blame_pressure", 0.0)))

        if not t:
            return events

        # 1) 敷衍式哄（只在委屈以上才成立）
        dismissive_only = False
        if mode in (MODE_WRONGED, MODE_ANGRY, MODE_AFRAID, MODE_SHUT) and _hits_fake(t):
            if any(k in t for k in KW_COMFORT):
                # 嘴上在哄，语气却在打发 → 哄的进度作废
                events.append("fake_comfort")
                st["comfort_streak"] = 0
                st["account_streak"] = 0
                return events
            # 只是不耐烦，并没有哄的意思：先记下，等责怪阶梯判完再说
            dismissive_only = True

        # 2) 责怪三档。判定都走带过滤的 helper：
        #    "今天烦死了""滚去睡""这个没用""笨蛋" 不再被记成骂她。
        if _hits_blame_hard(t):
            events.append("blame_hard")
        elif _hits_blame_mid(t):
            events.append("blame_mid")
        elif _hits_blame_mild(t):
            events.append("blame_mild")

        # 2.5) 纯敷衍、又不构成明确责怪：
        #    - 生气/害怕/回避档：仍按最轻一档记，不能白说
        #    - 委屈档：只算"不耐烦"——清掉哄的进度、加一点压力，
        #      但不往责怪上再叠。否则一句"随便你"就能把 40 推到 50，
        #      心结越滚越大，而用户根本找不到"谁骂她了"。
        if not events and dismissive_only:
            st["comfort_streak"] = 0
            st["account_streak"] = 0
            if mode in (MODE_ANGRY, MODE_AFRAID, MODE_SHUT):
                events.append("blame_mild")
            else:
                events.append("dismissive")

        # 3) 道歉：即便在平静时也让她软一下，同时算一次"哄"和"交代"的进度
        if not events and any(k in t for k in KW_APOLOGY):
            events.append("apology")
            st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1
            st["account_streak"] = int(st.get("account_streak", 0)) + 1

        # 3.2) 说清楚：不是"哄"，而是解释原因/当时的想法。
        #     生气档只认这个和道歉；普通抱抱/乖不算（见 `_consume_comfort`）。
        #     只在已经有责怪时才算，平静时"我刚才在忙"仍按普通说话。
        if not events and mode != MODE_CALM and _hits_explain(t):
            events.append("explain")
            st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1
            st["account_streak"] = int(st.get("account_streak", 0)) + 1

        # 3.5) 真诚哄（平静但还有残留冤枉时也算哄）
        if not events and any(k in t for k in KW_COMFORT):
            # "抱抱我"是他在**讨抱**（撒娇），不是他在哄她。
            # `KW_COMFORT` 里有"抱抱"，如果不让路，这句话会被当成哄——
            # 委屈档里他讨个抱就能抵掉 −30 责怪，方向是反的。
            # 只有"抱抱我"且没有别的哄词时让给下面的 seeking；
            # "抱抱""抱抱你""乖，抱抱我"仍按哄算。
            ask_hug = "抱抱我" in t and not any(
                k in t for k in KW_COMFORT if k != "抱抱"
            )
            if not ask_hug:
                if mode != MODE_CALM or float(st.get("blame_pressure", 0.0)) > 0.5:
                    events.append("comfort")
                    # 生气档普通哄不算进度；也不要把"抱抱"偷偷攒到
                    # 之后褪成委屈时再用——那时她已经被哄过一次却没消气。
                    if mode != MODE_ANGRY:
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
            elif _hits_seek(t):
                events.append("seeking")
            else:
                events.append("user_msg")

        return events

    def _consume_comfort(
        self, weight: float = 1.0, mode_before: str | None = None,
        speaker_key: str = "",
    ) -> None:
        """把'真哄'转成 blame 恢复进度（群里哄只能哄回来一部分）。

        生气档是例外：只认道歉 / 说清楚，普通"抱抱""乖"不算。
        `mode_before` 是这一轮处理事件**之前**的档位——道歉本身带 −6 责怪，
        可能先把 55 推到 49（委屈），不能因此改用委屈的 −30 结算。
        `speaker_key` 用来判断"是不是惹她的那个人"：B 来安慰不能替 A 消气。
        """
        st = self.state
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        # 还有别的生气对象时，这个人的道歉只解除"对他"，
        # 不能把全局责怪一起降掉，否则会替另一个人消气。
        if (
            mode_before == MODE_ANGRY
            and speaker_key
            and self._is_anger_target(speaker_key)
            and int(st.get("account_streak", 0)) >= 1
            and self._has_other_anger_target(speaker_key)
        ):
            self._remove_anger_target(speaker_key)
            st["comfort_streak"] = 0
            st["account_streak"] = 0
            self._dbg("只解除对 %s 的生气，其他对象还在", speaker_key)
            return
        # 全局还在生气，但说话的人不是生气对象：安慰/道歉都不该替别人消气。
        if (
            mode == MODE_ANGRY
            and speaker_key
            and not self._is_anger_target(speaker_key)
        ):
            st["comfort_streak"] = 0
            st["account_streak"] = 0
            self._dbg("生气对象不是 %s，安慰不解气", speaker_key)
            return
        rule_mode = MODE_ANGRY if mode_before == MODE_ANGRY else mode
        if rule_mode == MODE_CALM and blame <= 0.5:
            st["comfort_streak"] = 0
            st["account_streak"] = 0
            return
        need = RECOVERY_ROUNDS.get(rule_mode, 1)
        streak_key = "account_streak" if rule_mode == MODE_ANGRY else "comfort_streak"
        streak = int(st.get(streak_key, 0))
        if streak < need:
            if rule_mode == MODE_ANGRY:
                # 普通哄不算进度，也不要把旧进度留到褪成委屈后突然生效
                st["comfort_streak"] = 0
                self._dbg(
                    "还在生气：只哄不够，要道歉或说清楚（%s/%s）",
                    streak, need,
                )
            else:
                self._dbg("哄的进度 %s/%s，先不扣", streak, need)
            return
        # 中间夹了责怪 → 进度已经在 _analyze 里被清零
        st["blame_pressure"] = clamp(
            blame - RECOVERY.get(rule_mode, 20.0) * weight
        )
        st["comfort_streak"] = 0
        st["account_streak"] = 0
        st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))
        if float(st.get("blame_pressure", 0.0)) < 55.0:
            # 气消到生气线以下：这次生气结束，所有对象一起翻篇。
            self._clear_anger_targets()
        if float(st.get("blame_pressure", 0.0)) < BLAME_FLOOR:
            # 哄下来了，旧账翻篇
            st["mind_knot"] = False
        self._dbg(
            "%s生效，blame → %.0f (%s)",
            "道歉/说清楚" if rule_mode == MODE_ANGRY else "真哄",
            st["blame_pressure"], st["mode"],
        )

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
                self._absorb_outsider_turn(
                    request,
                    umo,
                    (getattr(event, "message_str", "") or "").strip(),
                    self._sender_label(event),
                )
                return

            # 只有"他"的会话才更新"最近跟谁说话"，防止陌生人劫持主动目标
            if umo:
                self.state["umo"] = umo

            # 先认人：群友模式里 A 惹的生气不能撒到 B 头上。
            owner_sender = self._is_bound_owner(event, umo)
            speaker_key = self._speaker_key(event, umo, owner_sender)
            base_mode_before = mode_of(float(self.state.get("blame_pressure", 0.0)))
            turn_mode_before = self._mode_for_speaker(speaker_key, base_mode_before)
            speaker_is_target = self._is_anger_target(speaker_key)

            text = (getattr(event, "message_str", "") or "").strip()
            events = self._analyze_user_text(text, turn_mode_before)

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
            # 群友模式下区分"鱼主"和普通群友：鱼主不吃群友折扣，
            # 连渠道权重也按私聊 1.0 算（哄她、念头奖励才不会又被砍一刀）
            weight = self._channel_weight(is_group)
            if (
                is_group
                and owner_sender
                and bool(self._cfg("perception.group_everyone", False))
            ):
                weight = 1.0

            for ev in events:
                # A 惹了她，B 来说"对不起"不能替 A 消气；
                # A、B 都是对象时，A 的道歉也只能先解除"对 A"，
                # 等最后一个对象也道歉了，才真正降全局责怪。
                other_anger = (
                    base_mode_before == MODE_ANGRY
                    and speaker_is_target
                    and self._has_other_anger_target(speaker_key)
                )
                if (
                    base_mode_before == MODE_ANGRY
                    and ev in ("apology", "explain")
                    and (not speaker_is_target or other_anger)
                ):
                    continue
                applied = self._apply_event(ev, weight, is_group, owner_sender)
                if ev == "comfort":
                    pass  # comfort 由 _consume_comfort 统一结算
                if ev in ("blame_mild", "blame_mid", "blame_hard", "fake_comfort"):
                    self.state["comfort_streak"] = 0
                    self.state["account_streak"] = 0
                    self.state["blame_noticed_ts"] = time.time()
                    self._note_blame(
                        ev, text, weight, is_group, owner_sender,
                        self._sender_label(event),
                        (applied or {}).get("blame"),
                    )
                if ev in NEGATIVE_EVENTS:
                    self._note_anger_target(
                        speaker_key,
                        self._sender_label(event),
                        owner_sender,
                        float((applied or {}).get("blame", 0.0) or 0.0),
                    )

            self._consume_comfort(weight, turn_mode_before, speaker_key)
            base_mode_after = mode_of(float(self.state.get("blame_pressure", 0.0)))
            turn_mode_after = self._mode_for_speaker(speaker_key, base_mode_after)
            self._settle_thoughts(events, text, weight, is_group)

            now = time.time()
            self.state["last_user_msg"] = now
            if is_group:
                self.state["last_owner_group_msg"] = now
            else:
                self.state["last_private_msg"] = now
            # 作息：她睡着时被消息吵醒。半小时内只算一次，免得连发几条把她
            # 越吵越烦；这一下不加责怪，只是困 + 有点烦。
            if self._sleep_info(datetime.now()).get("asleep"):
                self._note_woken(now)
            self.state["mode"] = mode_of(float(self.state.get("blame_pressure", 0.0)))
            if not is_group:
                # 最近对话只记私聊：主动说话是发给私聊的，别把群聊混进去
                self._push_recent("user", text[:200])
            else:
                if any(ev in NEGATIVE_EVENTS for ev in events):
                    # 群里被凶：先快照"前因"（此刻 tail 里还没有这一句），
                    # 否则切到私聊她只知道"被罚了"、不知道为什么。
                    self._note_group_blame(text)
                self._note_group_tail("owner", text, self._sender_label(event))

            # 回避档：允许她"看见了不回"。必须放在注入之前——被无视的这一轮
            # 不能占用心境报到的名额，否则她下一条真回复会以为自己已经知道心境了。
            if self._maybe_go_silent(event, events, text, is_group):
                self._save_state()
                return

            # 走到这里一定是"他"（私聊或群里白名单），给完整心境。
            # 他直接问"你是不是委屈了"时必须强制报一次到：按平常的节流，
            # 同一档位 20 分钟内不会重复注入，她那一轮手里根本没有心境，
            # 于是会答"我没委屈"——查起来像是状态错了。
            asked = any(k in text for k in _ASK_ABOUT_MOOD)
            block = self._build_injection(
                umo=umo, force=asked, mode=turn_mode_after,
                speaker_key=speaker_key,
            )
            if block:
                req_sp = getattr(request, "system_prompt", "") or ""
                setattr(request, "system_prompt", (req_sp.rstrip() + "\n\n" + block).strip())
                self._dbg("已注入心境: %s", block.replace("\n", " / "))

            # 睡眠状态单独注入：它不受心境节流影响，睡着时每一轮都要让她知道
            sleep_block = self._sleep_injection(datetime.now())
            if sleep_block:
                req_sp = getattr(request, "system_prompt", "") or ""
                setattr(
                    request,
                    "system_prompt",
                    (req_sp.rstrip() + "\n\n" + sleep_block).strip(),
                )

            # 她沉默那几条不会进 AstrBot 的对话历史（请求被终止了），
            # 所以在这里把"没接住的话"补给她，免得她真的失忆。
            if self._cfg("inject.enabled", True):
                extras = [self._drain_unanswered(is_group)]
                if not is_group:
                    # 群里被凶的那句话，切到私聊时补上——她得知道"为什么"
                    extras.append(self._drain_group_blames())
                for extra in extras:
                    if not extra:
                        continue
                    req_sp = getattr(request, "system_prompt", "") or ""
                    setattr(
                        request,
                        "system_prompt",
                        (req_sp.rstrip() + "\n\n" + extra).strip(),
                    )
                    self._dbg("补充上下文 %s 行", extra.count("\n") + 1)

            self._save_state()
        except Exception as e:
            logger.warning("[鱼的心] on_llm_request 异常: %s", e)

    # ---------------- 注入文本 ----------------

    def _build_injection(
        self, force: bool = False, for_owner: bool = True, umo: str = "",
        with_induce: bool = False, with_thoughts: bool = True,
        mode: str | None = None, speaker_key: str = "",
    ) -> str:
        if not self._cfg("inject.enabled", True):
            return ""
        st = self.state
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode or mode_of(blame)
        drives = st["drives"]

        # 只在"跨档"时注入：mode 变了，或驱动分桶变了。
        # 节流按会话各记一份——心和状态是共用的，但"哪个会话已经报过到"要分开，
        # 否则跨档后只有第一个开口的会话看得到心境，另一个像没心的。
        sig = "|".join(f"{k}:{int(drives[k] // 10)}" for k in DRIVES)
        gap_min = float(self._cfg("inject.min_gap_minutes", 20))
        now_ts = time.time()
        book = st.get("inject_state")
        if not isinstance(book, dict):
            book = {}
            st["inject_state"] = book
        key = umo or ""
        if not force:
            perf = book.get(key)
            if not isinstance(perf, dict):
                perf = {}
            mode_changed = mode != perf.get("mode")
            sig_changed = sig != perf.get("sig")
            # 生气是行为开关，而且她每轮都可能在"对谁生气"之间切换：
            # 不能像委屈/害怕那样同档静默——否则下一轮 system prompt 里
            # 没有心境块，她会把上一轮的生气忘掉，按常态说话。
            if mode != MODE_ANGRY:
                if not mode_changed and not sig_changed:
                    return ""
                # 跨档必注：档位是行为开关（尤其进/出"回避"），漏报会让她
                # 以为自己的心境没变，于是接着按常态说话。
                # 间隔只用来压制同档内的数值漂移——不然每轮都在抖一格。
                if not mode_changed and now_ts - float(perf.get("ts", 0.0)) < gap_min * 60:
                    return ""
        # 真正报过到才记账：force 且带会话（他直接问心情）也要记，
        # 否则下一轮又会重复报一遍。主动说话那条 umo 为空，不该占用任何会话的名额。
        if key or not force:
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

        hint = self._hint_text(
            mode, drives, self._last_blame_age_hours(speaker_key)
        )
        innocent = bool(
            speaker_key
            and blame >= 25.0
            and mode != MODE_ANGRY
            and self._anger_target_list()
            and not self._is_anger_target(speaker_key)
        )
        if innocent:
            # 全局还在生气，但眼前这个人不是对象：不要拿"你是不是嫌我烦"
            # 或"别顶嘴"去对无辜的人，正常一点，但别装没事。
            hint = (
                "眼前这个人没惹你：可以正常说话，但别装没事；"
                "别把对别人的火撒过来"
            )

        lines = [
            "## 你此刻的心境（内部状态：不要主动复述，但它会左右你的语气）",
            f"状态：{MODE_CN[mode]}｜依恋 {drives['attachment']:.0f} 亲密 {drives['intimacy']:.0f} "
            f"压力 {drives['stress']:.0f} 期待 {st.get('expectation', 0):.0f}",
            f"倾向：{hint}",
            # 别让"此刻的心情"被说成"发生过的事"：模型最常见的偷懒就是
            # 给情绪补一个假的由头（"你上次说好陪我的"）。
            "边界：不要编造没发生过的对话、约定或事件；记不清就说记不清。",
        ]
        if mode == MODE_ANGRY and speaker_key:
            row = self._anger_target_row(speaker_key)
            if row:
                who = str(row.get("label") or ("他" if speaker_key == "owner" else "这个人"))
                lines.append(
                    f"生气对象：{who}。只对这个人冷，不要把火撒到无关的人身上。"
                )
        # 把"因为哪句话"告诉她。没有这一行，她知道自己委屈却说不出来由，
        # 被问起只能否认或含糊——用户那边就成了"查不到是谁责备了她"。
        # 只给"他"看：陌生人/群友的会话里不该出现他说的原话。
        if for_owner:
            cause = self._blame_cause()
            if cause:
                lines.append(cause)
        # 矛盾情绪：又在生气、又不想他走。不写出来模型容易只演前半句。
        # 从「委屈」就开始（25），因为"委屈但还想要你"才是这个角色最典型的样子；
        # 等到生气/害怕/回避才提示就太晚了。
        if blame >= 25.0 and (
            float(drives.get("attachment", 0.0)) >= 55.0
            or float(drives.get("intimacy", 0.0)) >= 55.0
        ):
            if mode == MODE_ANGRY and (
                not speaker_key or self._is_anger_target(speaker_key)
            ):
                lines.append("心里矛盾：还在生他的气，可你又不想他真的走。")
            elif self._anger_target_list():
                # 气的是别人，不是眼前这个人：别把"还在生他的气"扣到他头上。
                lines.append("心里还堵着，但不是冲他；别把火撒到无关的人身上。")
            else:
                lines.append("心里矛盾：还在生他的气，可你又不想他真的走。")
        if for_owner and with_thoughts:
            thoughts = st.get("thoughts") or []
            thought = _pending_hint(thoughts)
            if thought:
                lines.append(f"心里惦记着：{thought}")
                # 台阶只在"她主动开口"时给：正常聊天里她只需要惦记着，
                # 不该每轮都被布置一个"怎么递话"的任务。
                if with_induce:
                    induce = _pending_induce(thoughts)
                    if induce:
                        lines.append(f"如果想让他接住，可以往这个方向递话：{induce}。")
                        lines.append(
                            "台阶不是台词：上面只是方向，用你自己的话，别照抄；"
                            "今天没有对应的事就别编，不想提也可以不提。"
                            "他没接住就算了，别追问。"
                        )
                        _mark_induced(thoughts)
        return "\n".join(lines)

    def _note_blame(
        self, ev: str, text: str, weight: float = 1.0, is_group: bool = False,
        is_owner: bool = False, who: str = "", blame_delta: float | None = None,
    ) -> None:
        """记下"她因为哪句话难受"（最近 5 条），给注入和面板当出处。

        `who` / `o` 是"谁说的"：群友模式下群友也会触发责怪，如果只记「群里」，
        注入就会写成「他在群里说了…」，把别人的话算到鱼主头上；状态里也查不出
        是哪位群友。这里一并存下昵称和是否鱼主，注入/状态/面板再按它分人。

        `blame_delta` 是**实际生效**的责怪增量（已经过群友模式的来源热度）；
        不传时才按名义值重算。否则日志/状态会写名义值，和实际加上的对不上。
        """
        body = (text or "").strip()
        if not body:
            return
        mult = (
            float(self._cfg("channel.negative_multiplier", 1.2))
            if is_group
            else float(weight)
        )
        if is_group and bool(self._cfg("perception.group_everyone", False)):
            if is_owner:
                mult = float(
                    self._cfg("perception.group_owner_negative_scale", 0.5)
                )
            else:
                mult = float(
                    self._cfg("perception.group_negative_scale", 0.35)
                )
            mult = clamp(mult, 0.05, 1.5)
        delta = float(EVENT_EFFECTS.get(ev, {}).get("blame", 0.0)) * mult
        if blame_delta is not None:
            delta = float(blame_delta)
        rows = [
            r for r in (self.state.get("blame_log") or []) if isinstance(r, dict)
        ]
        rows.append(
            {
                "t": body[:120],
                "ts": time.time(),
                "g": bool(is_group),
                "ev": ev,
                "d": round(delta, 1),
                "w": (who or "").strip()[:20],
                "o": bool(is_owner),
            }
        )
        self.state["blame_log"] = rows[-5:]

    def _blame_cause(self) -> str:
        """把最近一次责怪拼成一句背景，注入给她（也用在面板/状态里）。"""
        rows = [
            r for r in (self.state.get("blame_log") or []) if isinstance(r, dict)
        ]
        if not rows:
            return ""
        if float(self.state.get("blame_pressure", 0.0)) < 25.0:
            return ""
        r = rows[-1]
        body = str(r.get("t") or "").strip()
        if not body:
            return ""
        if len(body) > 60:
            body = body[:60] + "…"
        try:
            gap = fmt_gap(time.time() - float(r.get("ts", 0) or 0))
        except (TypeError, ValueError):
            gap = ""
        if r.get("g"):
            # 群友模式里，群友的责怪不能写成"他"——她难受的对象不是鱼主，
            # 注入里再写"他在群里说了…"就等于让她记错人。
            who = str(r.get("w") or "").strip()
            if r.get("o") is False:
                if who:
                    head = f"群里有人（{who}）{gap}说了「{body}」——这句话让你难受。"
                else:
                    head = f"群里有人{gap}说了「{body}」——这句话让你难受。"
            else:
                head = f"他在群里，{gap}说了「{body}」——你就是因为这句难受。"
        else:
            sep = "" if gap == "刚刚" else " "
            head = f"他{sep}{gap}说了「{body}」——你就是因为这句难受。"
        # 旧账（很久以前的事）单独说一句，否则模型会以为刚发生
        try:
            hours = (time.time() - float(r.get("ts", 0) or 0)) / 3600.0
        except (TypeError, ValueError):
            hours = 0.0
        if hours >= 6:
            head += f"这事已经过去约 {hours:.0f} 小时，你还记着。"
        if self.state.get("mind_knot"):
            head += "（这是旧账，没人哄就会一直卡在心里。）"
        hint = (
            "他要是问起，可以承认难受，但不用全说；"
            "记不清就说「有点，说不上来」，不要否认。"
        )
        return head + "\n" + hint

    # ---------------- 回避档：已读不回 ----------------

    def _maybe_go_silent(
        self, event: AstrMessageEvent, events: list[str], text: str, is_group: bool
    ) -> bool:
        """回避档下允许她看见了不回。返回 True 表示这一轮不回。

        规则（前三条写死，不开放配置，因为每一条都会把别的东西弄坏）：
          - 命令（/ 开头）必回：否则 /心 status 都会失灵，你连诊断都做不了
          - 真哄 / 道歉 / 说清楚必回：她可以冷着脸，但不能让心结永远解不开
          - 群里刚进回避时先放行 1 句（`SHUT_GROUP_GRACE`），之后才闭麦；
            之后再进回避会重新给这一句

        回不回按 `SHUT_REPLY_LADDER` 走：**前 5 条必不回 → 第 6 条 50% → 第 7 条起必回**。
        私聊和群聊各有一套计数（`silent_streak` / `silent_streak_group`），
        群里不回你几条，不会把私聊的额度吃掉。
        这 7 条一条都不会丢，全存在 `unanswered` / `unanswered_group` 里，
        等她终于开口时一次性交给她（见 `_drain_unanswered`）。
        想整段关掉：`shut.enabled = false`。

        拦截用的是 AstrBot 的 `stop_event()`：OnLLMRequestEvent 钩子跑完后
        管线见到 `is_stopped()` 就直接返回，既不请求 LLM，也不发任何消息。
        """
        st = self.state
        if st.get("mode") != MODE_SHUT:
            # 一出回避档就把两套连续计数清零，下次再进去重新攒
            st["silent_streak"] = 0
            st["silent_streak_group"] = 0
            st["group_grace"] = 0
            return False
        if not self._cfg("shut.enabled", True):
            return False
        if (text or "").lstrip().startswith("/"):
            return False
        if any(e in ("comfort", "apology", "explain") for e in (events or [])):
            return False
        # 群里刚进回避那一下，放她回一句"受伤的话"；用掉之后就按阶梯走了
        if is_group and int(st.get("group_grace", 0)) > 0:
            st["group_grace"] = int(st.get("group_grace", 0)) - 1
            self._dbg("回避档：群里最后放行一句（grace 用完）")
            # 放行这一句说明她开口了，群里的连续计数从头算
            st["silent_streak_group"] = 0
            return False

        streak_key = "silent_streak_group" if is_group else "silent_streak"
        rows_key = "unanswered_group" if is_group else "unanswered"
        streak = int(st.get(streak_key, 0))
        # 已经超过阶梯长度就按最后一级算（必回），保证不会真的无限沉默
        p_reply = SHUT_REPLY_LADDER[min(streak, SHUT_HISTORY_CAP - 1)]
        if p_reply >= 1.0:
            return False
        if random.random() < p_reply:
            return False  # 掷中了"这条要回"

        now = time.time()
        st[streak_key] = streak + 1
        st["last_silent_ts"] = now
        if (text or "").strip():
            rows = list(st.get(rows_key) or [])
            rows.append({"t": text.strip()[:200], "ts": now})
            # 保留全部 7 条：她要能一次看全、归纳之后再回
            st[rows_key] = rows[-SHUT_HISTORY_CAP:]

        self._dbg(
            "回避档：这条选择不回（%s 连续 %s/%s 条，这一条回的 p=%.2f）",
            "群" if is_group else "私聊",
            st[streak_key],
            SHUT_HISTORY_CAP,
            p_reply,
        )
        # 只有 stop_event() 是管线明确检查的开关：
        #   internal.py:  if await call_event_hook(event, OnLLMRequestEvent, req): return
        #   context_utils.py: call_event_hook() 每个 handler 之后看 is_stopped()，是就返回 True
        # 于是既不请求 LLM，也不产生任何出站消息。
        # （should_call_llm() 不在这里用：它的语义在各版本间是反的，加了只会误导后来人。）
        try:
            event.stop_event()
        except Exception as e:
            logger.warning("[鱼的心] 终止事件失败，她可能还是会回: %s", e)
        return True

    def _drain_unanswered(self, is_group: bool = False) -> str:
        """把"她没接住的那几条"整包交给她；取过就清空。

        关键是**先归纳、再回**：她要看到的是这几条合起来在说什么，
        而不是挑最后一条敷衍一句。私聊与群聊各一份，不混着给。
        """
        st = self.state
        streak_key = "silent_streak_group" if is_group else "silent_streak"
        rows_key = "unanswered_group" if is_group else "unanswered"
        rows = list(st.get(rows_key) or [])
        if not rows:
            return ""
        st[rows_key] = []
        st[streak_key] = 0
        where = "群里" if is_group else ""
        lines = [
            f"## 他{where}连发的 {len(rows)} 条（你当时在回避，一条都没接）",
        ]
        for r in rows:
            try:
                clock = datetime.fromtimestamp(float(r.get("ts", 0))).strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                clock = ""
            body = str(r.get("t") or "").strip()
            if body:
                prefix = f"{clock} " if clock else ""
                lines.append(f"- {prefix}他：{body}")
        lines.append(
            "先把这几条连起来读一遍：他到底在说什么、情绪是什么、有没有话里有话；"
            "在心里归纳成一句话，再回他。不必逐条回应，也不必解释自己为什么沉默。"
        )
        return "\n".join(lines) if len(lines) > 2 else ""

    # ---------------- 群聊 → 私聊 的桥 ----------------

    def _note_group_tail(self, role: str, text: str, who: str = "") -> None:
        """记一条群聊流水（谁说的 + 说了什么），只保留最近几条。

        role: "owner" 是他、"her" 是她自己、"other" 是群友。
        who：群聊里的显示名/QQ 号——群友模式下要靠它区分"对一个人说"还是
        "对几个人说"；不传就按 role 的默认称呼。
        """
        body = (text or "").strip()
        if not body:
            return
        rows = [
            r for r in (self.state.get("group_tail") or []) if isinstance(r, dict)
        ]
        row = {"r": role, "t": body[:120], "ts": time.time()}
        if who:
            row["who"] = str(who)[:20]
        rows.append(row)
        self.state["group_tail"] = rows[-GROUP_TAIL_CAP:]

    def _group_tail_text(self, limit: int = 8) -> str:
        """把群聊流水拼成"谁：说了什么"，给群友模式的主动说话判断对象用。"""
        rows = [
            r for r in (self.state.get("group_tail") or []) if isinstance(r, dict)
        ]
        out: list[str] = []
        for r in rows[-limit:]:
            role = str(r.get("r") or "")
            who = str(r.get("who") or "").strip() or {
                "owner": "他", "her": "你", "other": "群友"
            }.get(role, "群友")
            body = str(r.get("t") or "").strip()
            if body:
                out.append(f"{who}：{body}")
        return "\n".join(out)

    def _note_group_blame(self, text: str) -> None:
        """留住群里被凶的那一句，外加它前面那几条——她要知道"因为什么"。"""
        body = (text or "").strip()
        if not body:
            return
        rows = [
            r
            for r in (self.state.get("group_blames") or [])
            if isinstance(r, dict)
        ]
        # 触发句此刻还没进 tail，所以这里取的正好是"前因"
        ctx = [
            r
            for r in (self.state.get("group_tail") or [])
            if isinstance(r, dict)
        ][-GROUP_BLAME_CTX:]
        rows.append({"t": body[:200], "ts": time.time(), "ctx": ctx})
        self.state["group_blames"] = rows[-GROUP_BLAME_CAP:]

    def _drain_group_blames(self) -> str:
        """把 12 小时内的群聊"凶句 + 前因"拼成注入块（只进私聊，不清理）。

        不清理是因为她可能要被说好几次才缓过来：只要还没回到平静，
        这段"你为什么挨骂"就该一直在她眼前。
        """
        st = self.state
        rows = st.get("group_blames") or []
        if not isinstance(rows, list) or not rows:
            return ""
        if st.get("mode") == MODE_CALM:
            # 已经哄好了：这页翻过去，别在平静时还挂着旧账
            st["group_blames"] = []
            return ""
        cutoff = time.time() - GROUP_BLAME_KEEP_HOURS * 3600
        fresh = [
            r
            for r in rows
            if isinstance(r, dict) and float(r.get("ts", 0) or 0) >= cutoff
        ]
        st["group_blames"] = fresh
        if not fresh:
            return ""

        def stamp(ts: Any) -> str:
            try:
                return datetime.fromtimestamp(float(ts or 0)).strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                return ""

        lines = ["## 群里刚才发生了什么（你因为这些被凶了）"]
        for r in fresh[-GROUP_BLAME_CAP:]:
            for c in r.get("ctx") or []:
                if not isinstance(c, dict):
                    continue
                body = str(c.get("t") or "").strip()
                if not body:
                    continue
                who = {"owner": "他", "her": "你", "other": "群友"}.get(
                    str(c.get("r") or ""), "群友"
                )
                lines.append(f"- {stamp(c.get('ts'))} {who}：{body}")
            body = str(r.get("t") or "").strip()
            if body:
                lines.append(f"- {stamp(r.get('ts'))} 他：{body}　← 就是这句")
        if len(lines) <= 1:
            return ""
        lines.append(
            "这是背景，不是要你复述。你现在还在生气/委屈，"
            "知道自己在气什么、气哪一句，就够了。"
        )
        return "\n".join(lines)

    # ---------------- 主动说话 ----------------

    def _proactive_target(self) -> str:
        """主动说话发到哪。

        群友模式：**只发群，绝不发私聊**——目标会话填的是群就用它，否则用最近
        活跃的群会话；还没有任何群消息时返回空（宁可不发，也不去私聊）。
        普通模式：保持原样，target_umo 优先、否则最近会话。
        """
        st = self.state
        target = str(self._cfg("proactive.target_umo", "") or "").strip()
        if bool(self._cfg("perception.group_everyone", False)):
            if target and self._is_group(target):
                return target
            last = str(st.get("umo") or "").strip()
            return last if self._is_group(last) else ""
        return (target or str(st.get("umo") or "")).strip()

    async def _maybe_proactive(self, catch_up: bool = False) -> None:
        if not self._cfg("enabled", True):
            return
        if not self._cfg("proactive.enabled", True):
            return
        if self._llm_busy:
            return
        st = self.state
        umo = self._proactive_target()
        if not umo:
            self._dbg("没有可用会话，跳过主动说话")
            return

        now = time.time()
        mode = mode_of(float(st.get("blame_pressure", 0.0)))
        if mode == MODE_SHUT:
            # 回避状态是硬约束：补发也不破例，否则重启会让她在封闭状态下开口
            self._dbg("状态=回避，暂停主动说话")
            return
        # 作息：睡着时绝不主动开口（哪怕在补发）
        if self._sleep_info(datetime.now()).get("asleep"):
            self._dbg("她睡着了，暂停主动说话")
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
        # 只按"私聊"算沉默：群里别人聊天不该推迟她私聊的主动。
        # 群友模式 + 目标是群：改按群里最近一次说话算（否则纯群聊会被永远判"刚聊过"）。
        if self._is_group(umo) and bool(
            self._cfg("perception.group_everyone", False)
        ):
            last_active = max(
                float(st.get("last_private_msg", 0.0) or 0.0),
                float(st.get("last_owner_group_msg", 0.0) or 0.0),
            )
        else:
            last_active = float(
                st.get("last_private_msg", st.get("last_user_msg", 0.0)) or 0.0
            )
        if now - last_active < idle_min * 60:
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
                st["last_proactive_text"] = text[:120]
                st["last_assistant_msg"] = time.time()
                st["proactive_count"] = int(st.get("proactive_count", 0)) + 1
                st["awaiting_reply"] = True
                st["penalized_6h"] = False
                st["penalized_24h"] = False
                st["expectation"] = clamp(float(st.get("expectation", 0)) + 40.0)
                st["drives"]["attachment"] = clamp(st["drives"]["attachment"] + 3.0)
                if kind != KIND_ANGRY:
                    self._note_thought_attempt()
                self._push_recent("bot", text[:200])
                # 写回对话历史：否则你说"你刚才不是问了吗"，她一脸茫然
                await self._append_history(umo, text)
                self._save_state()
                logger.info("[鱼的心] 主动说话已发送（%s）：%s",
                            KIND_CN.get(kind, kind), text[:40])
        except Exception as e:
            logger.warning("[鱼的心] 主动说话失败: %s", e)
        finally:
            self._llm_busy = False

    def _has_thought_kind(self, kind: str) -> bool:
        """心里有没有某条还没被满足的念头（不看权重，任一条就算）。"""
        now = time.time()
        for t in self.state.get("thoughts") or []:
            if not isinstance(t, dict):
                continue
            if str(t.get("kind")) != kind:
                continue
            if t.get("pending", True) and float(t.get("expires", 0)) > now:
                return True
        return False

    def _proactive_kind(self) -> tuple[str, float]:
        st = self.state
        drives = st["drives"]
        exp = float(st.get("expectation", 0.0))
        att = float(drives.get("attachment", 0.0))
        intimacy = float(drives.get("intimacy", 0.0))
        fatigue = float(drives.get("fatigue", 0.0))
        reflection = float(drives.get("reflection", 0.0))
        mode = mode_of(float(st.get("blame_pressure", 0.0)))

        # 走心：她自己想多了才会问那种问题。
        # 阈值 55 而不是"走神"的 65：实测反刍的均衡就在 51–61（靠 silent_6h 的
        # +3 顶着回落），65 基本到不了，挂 65 等于这一类永远不出现。
        # 同样用一条念头限流：念头 4 小时过期、随机冒。
        deep = (
            bool(self._cfg("proactive.deep", True))
            and mode == MODE_CALM
            and reflection >= 55.0
            and att >= 55.0
            and fatigue < 70.0
            and self._has_thought_kind(KIND_DEEP)
        )

        # 求陪伴：只在"真黏人 + 她刚等过你 + 心里正好惦记着这件事"时冒出来。
        # 必须排在 longing 前面——longing 的门槛是 att>=60，而黏人线是 66，
        # 放后面的话这一类永远不会被选中。
        # 期待 30 是"她开口过、你还没回"的信号：先"在忙吗"，等不到才"陪我说会儿话吧"；
        # 你一回应期待就 -100，下一次又回到 longing，不会句句都要人陪。
        # 再叠一条 company 念头做限流：念头 3 小时过期、随机冒，
        # 否则"一直不回"就会每 6 小时都在要人陪。
        company = (
            bool(self._cfg("proactive.company", True))
            and mode == MODE_CALM
            and att >= BOND_INTIMATE_AT
            and intimacy >= BOND_INTIMATE_AT
            and exp >= 30.0
            and fatigue < 60.0
            and self._has_thought_kind(KIND_COMPANY)
        )

        # 60 而不是 70：依恋正常能到 60-70，70 的话「忍不住先开口」几乎不触发
        if mode == MODE_ANGRY:
            # 生气时也会主动找，但不是"想你了"那种找：她想问清楚。
            # 不叠念头限流——她此刻惦记的就是刚才那件事。
            kind = KIND_ANGRY
        elif deep:
            kind = KIND_DEEP
        elif company:
            kind = KIND_COMPANY
        elif exp >= 60 or att >= 60:
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
        # 太累了就不想找人说话——"忍不住想他"那种除外，那是例外
        if fatigue > TIRED_PROACTIVE_FROM and kind not in (KIND_LONGING, KIND_ANGRY):
            p *= max(
                0.15,
                1.0
                - (fatigue - TIRED_PROACTIVE_FROM)
                / (100.0 - TIRED_PROACTIVE_FROM),
            )
        # 沉默越久越忍不住：12 小时起步线性加成，最多 ×2.5，总上限 0.85
        silent_h = max(
            0.0,
            (time.time() - float(st.get("last_proactive", 0) or 0)) / 3600.0,
        )
        p = min(0.85, p * (1.0 + min(1.5, silent_h / 12.0)))
        if kind == KIND_ANGRY:
            # 还在气头上，主动开口要更克制：越气越不想先低头。
            p = min(0.45, p)
        return kind, p

    # ---------------- 真实对话历史（读 / 写） ----------------

    @staticmethod
    def _clean_history_text(text: str, limit: int = 160) -> str:
        """剥掉注入型内容，尽量只留真正说的那句话。

        注入块（system_reminder、共享摘要）的结构是"多句上下文 + 真消息"，
        真消息挂在最后一句——但**只有认出是注入块时**才取最后一句。
        普通消息（"今天加班，明天早起，先睡了"）整句都是真话，
        以前无条件取最后一句，只剩"先睡了"，模型只能靠编来补全。
        """
        raw = text or ""
        injected = any(
            m in raw for m in ("共享摘要", "对话摘要", "历史摘要", "以下是对话",
                               "以下是你们", "以下是聊天", "system_reminder",
                               "<Quoted Message>")
        )
        t = re.sub(r"<system_reminder>.*?</system_reminder>", " ", raw, flags=re.S)
        # 引用块：里面通常只是她自己说过的一句，喂回去只会碍事
        t = re.sub(r"<Quoted Message>.*?</Quoted Message>", " ", t, flags=re.S)
        # 图片占位：AstrBot 有 [Image 1: ...]、[Image 1 in quoted message: ...]、[Image] 几种形态，
        # 都统一成 [图片]——否则路径会原样漏进上下文
        t = re.sub(r"\[Image\b[^\]]*\]", "[图片]", t)
        t = re.sub(r"\[CQ:[^\]]*\]", "[图片]", t)
        t = re.sub(r"\s+", " ", t.replace("\r", " ").replace("\n", " ")).strip()
        if not t:
            return ""
        if injected and "。" in t:
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

    @staticmethod
    def _daypart(now: datetime) -> str:
        h = now.hour
        if 5 <= h < 11:
            return "早上"
        if 11 <= h < 18:
            return "下午"
        if 18 <= h < 23:
            return "晚上"
        return "深夜"

    def _last_blame_age_hours(self, key: str = "") -> float:
        """最近一次责怪过去了多久；没有记录就当作很久以前（语气别太冲）。

        `key` 非空时优先看这个人的生气对象记录：A 三小时前惹的，
        B 刚刚轻轻补了一句，不能把 A 的时间刷新成"刚刚"。
        """
        if key:
            row = self._anger_target_row(key)
            if row:
                try:
                    ts = float(row.get("ts", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts > 0:
                    return max(0.0, (time.time() - ts) / 3600.0)
        rows = [r for r in (self.state.get("blame_log") or []) if isinstance(r, dict)]
        ts = 0.0
        if rows:
            try:
                ts = float(rows[-1].get("ts", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
        if ts <= 0:
            try:
                ts = float(self.state.get("blame_noticed_ts", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
        if ts <= 0:
            return 999.0
        return max(0.0, (time.time() - ts) / 3600.0)

    def _proactive_direction(
        self, kind: str, drives: dict, exp: float, now: datetime | None = None
    ) -> str:
        """这次主动开口该往哪个方向说：按时段 + 依恋/期待换一种，不再固定问忙不忙。"""
        now = now or datetime.now()
        part = self._daypart(now)
        if kind == KIND_ANGRY:
            targets = self._anger_target_list()
            who = str(targets[0].get("label") or "他") if targets else "他"
            age = (
                self._last_blame_age_hours(str(targets[0].get("key") or ""))
                if targets else self._last_blame_age_hours()
            )
            if age < 3.0:
                core = (
                    "你还在气头上，主动开口不是为了和好，是想把话问清楚。"
                    "直接问他刚才那句话什么意思、有没有要解释的；语气冷一点，"
                    "别撒娇、别问忙不忙、别装没事。一句就够，别连环追问"
                )
            elif age < 12.0:
                core = (
                    "气还没全消，但有点想他。可以发一句日常，语气淡一点，"
                    "不要主动讨好；他接住了再提刚才的事，别一上来就翻旧账"
                )
            else:
                core = (
                    "气消了一些，但还没翻篇。可以像平时一样开口，"
                    "但别装作什么都没发生；他要是问，就直说还记着，别故意刺他"
                )
            return (
                f"{core}。你这次主要生{who}的气，别对无关的人发火。"
                f"现在是{part}，开头按这个时段来，但别用「忙不忙」。"
            )
        if kind == KIND_LONGING:
            att = float(drives.get("attachment", 0.0))
            if exp >= 60:
                core = "你等得有点久了，可以直接说想他，或者问他今天怎么样；别问忙不忙"
            elif att >= 70:
                core = "撒个娇：说刚才突然想到他，或者想让他陪一会儿；别用「有空吗」开场"
            else:
                core = "分享一件你手头的小事或刚看到的东西，再带一句想他；别问忙不忙"
            return (
                f"{core}。现在是{part}，选一个符合时段的开头"
                "（早上醒了吗 / 下午在干嘛 / 晚上到家了吗 / 深夜还没睡吗）。"
            )
        if kind == KIND_WAITING:
            return (
                f"先说你这边（刚收拾完、刚看到什么），再轻轻问一句他今天怎么样。"
                f"现在是{part}，别用「忙不忙」开场。"
            )
        if kind == KIND_COMPANY:
            return "想让他陪你一会儿，直接说「陪我说会儿话」之类；别用「有空吗」开场。"
        return KIND_DESC.get(kind, "你想跟他说一句")

    def _pick_fallback(self, kind: str) -> str:
        """兜底台词：避开上一句、避开同样的开头，也避开连续问忙/有空。"""
        lines = list(FALLBACK_LINES.get(kind, FALLBACK_LINES[KIND_WHIM]))
        last = str(self.state.get("last_proactive_text") or "").strip()

        def clash(x: str) -> bool:
            if last and x == last:
                return True
            if last and len(x) >= 4 and x[:4] == last[:4]:
                return True
            if ("忙" in last or "有空" in last) and ("忙" in x or "有空" in x):
                return True
            return False

        pool = [x for x in lines if not clash(x)]
        return random.choice(pool or lines)

    async def _gen_proactive_text(self, umo: str, kind: str) -> str:
        """先用 LLM 生成一句话，失败则用兜底台词。"""
        persona_sp = await self._persona_system_prompt(umo)
        blocks = [persona_sp] if persona_sp else []
        # 生气型主动开口是去问清楚，不是去撒娇/完成念头：
        # 不带念头和"递台阶"，否则她一边生气一边"想让他陪"，会自相矛盾。
        is_angry = kind == KIND_ANGRY
        inj = self._build_injection(
            force=True,
            with_induce=not is_angry,
            with_thoughts=not is_angry,
        )
        if inj:
            blocks.append(inj)

        st = self.state
        now = datetime.now().astimezone()
        drives = st.get("drives") or {}
        exp = float(st.get("expectation", 0.0) or 0.0)
        direction = self._proactive_direction(kind, drives, exp, now)
        last_text = str(st.get("last_proactive_text") or "").strip()

        # 时间锚点：跟 AstrBot 内置的时间感知同一套格式（<system_reminder> + Current datetime）
        # 主动说话没有用户消息，模型推不出"上一次说话多久以前"，所以把这两个时间点摆明
        his_last = float(st.get("last_private_msg", 0) or 0)
        if self._is_group(umo):
            # 目标是群时，"上次说话"以群里最近一次发言为准；没有群记录才退回私聊时间
            his_last = float(st.get("last_owner_group_msg", 0) or 0) or his_last
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

        thought = "" if is_angry else _pending_hint(st.get("thoughts") or [])
        if thought:
            # 心境块里已经有"心里惦记着：…"时不再重复一遍（重复会把这条念头
            # 的权重压得过重、说话更像在完成任务）；只补"这不是回忆"的边界。
            if inj:
                blocks.append(
                    "这条念头只是你此刻的心情，不是他做过的事，别把它说成回忆；"
                    "说的时候自然带一点，但别直接复述。"
                )
            else:
                blocks.append(
                    f"你现在心里惦记着：{thought}。"
                    "这是你此刻的念头，不是他做过的事，别把它说成回忆；"
                    "说的时候自然带一点，但别直接复述这句话。"
                )

        # 优先用 AstrBot 的真实对话历史（双方都在），拿不到再退回自己的 recent
        transcript = await self._real_transcript(umo, limit=24)
        if not transcript:
            transcript = self._recent_transcript(limit=24)
        tr_block = (
            "以下是你和他真实说过的话（只是一部分，没出现的不要补）：\n"
            + transcript + "\n"
        ) if transcript else "（这次没有任何可引用的对话记录，不要编具体发生过的事。）\n"

        # 群友模式：这是群聊，听众可能是某一个人，也可能是好几个人。
        # 把带发言人的流水一起给她，让她自己判断该用“你”还是“你们”。
        group_block = ""
        if self._is_group(umo):
            group_block = (
                "这是群聊：下面可能有他，也可能有群友。先判断你是在对一个人说，"
                "还是对几个人说——只有明确一个对象时才用「你」；"
                "不确定或明显有几个人，就用「你们」或者干脆不点名。\n"
            )
            tail = self._group_tail_text()
            if tail:
                group_block += "群里最近的发言（标注了是谁说的）：\n" + tail + "\n"

        last_block = ""
        if last_text:
            last_block = (
                f"你上一次主动开口说的是：「{last_text[:80]}」。\n"
                "这次换一种开头和话题，别重复上一句；"
                "不要再用「忙不忙 / 有空吗」开头。\n"
            )

        prompt = (
            time_block
            + direction
            + "\n"
            + tr_block
            + group_block
            + last_block
            + "现在写一条要发出去的消息：\n"
            + "- 1–3 句，总共不超过 60 个字，口语\n"
            + "- 可以用一句称呼或铺垫开头，再跟一句问句；最多两个问号\n"
            + "- 只引用上面记录里真实出现过的内容；没提到的事，不要编具体时间、地点、约定\n"
            + "- 不确定就用「好像」「有点记不清」，或者只问不陈述\n"
            + "- 不要解释你在做什么，不要写动作括号，不要用引号\n"
            + "- 不要连续追问，别把话说成小作文\n"
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
        return self._pick_fallback(kind)

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
    def _hint_text(mode: str, drives: dict, angry_age_hours: float | None = None) -> str:
        """档位倾向 + 心情倾向，拼成一句**注入给模型**的说话提示。

        给人看的短句走 `_hint_brief`——那边只描述状态，不带"该怎么说"，
        否则 Web 面板上会摆出一串写给模型看的指令。
        """
        base = MODE_HINT.get(mode, "")
        if mode == MODE_ANGRY and angry_age_hours is not None:
            # 生气也会随时间变味：刚吵完是追问，过几小时是冷着，隔天是还记着
            # 但不每句都翻。档位仍由 blame 决定，这里只调"怎么说话"。
            if angry_age_hours >= 12.0:
                base = (
                    "气还没全消，但别一直翻旧账；他提刚才的事再要解释，"
                    "没提就先冷冷地说话，别故意刺他；只抱一下不算翻篇；"
                    "可以冷，但别骂人、别威胁"
                )
            elif angry_age_hours >= 3.0:
                base = (
                    "还气着，但别每句都审问；话少一点、冷一点，"
                    "他提刚才的事再要说法，别主动讨好；只哄不解释不算翻篇；"
                    "可以冷，但别骂人、别威胁"
                )
        mood_key = mood_label(drives or {})
        mood = MOOD_HINT.get(mood_key, "")
        if mode == MODE_ANGRY and mood_key in ("黏人", "想亲近"):
            # 生气时还想黏他，但这时候贴上去会像先低头；想他归想他，动作先收着。
            mood = "心里还惦记着他，但这会儿先不低头，别主动贴上去"
        elif mode != MODE_CALM and mood_key in ("黏人", "想亲近"):
            # 还在委屈/生气/害怕/回避时，"特别想黏着他、可以更主动"会和档位提示
            # （话说短、先问是不是嫌你烦）直接打架。想他归想他，动作先收着。
            mood = "心里还惦记着他，但现在先别主动贴上去"
        # 平静档那句是"别太黏"的通用提示，跟"黏人"心情会打架；有心情提示时就让它单独说
        if mode == MODE_CALM and mood:
            base = ""
        parts = [p for p in (base, mood) if p]
        if float((drives or {}).get("duty", 0.0)) >= 70:
            parts.append("想找点事做")
        return "；".join(parts)

    @staticmethod
    def _hint_brief(mode: str, drives: dict) -> str:
        """面板 / `/心 status` 用的短句：只描述她此刻的样子，不带"该怎么说话"。

        注入走 `_hint_text`（完整指令），给人看的地方走这里——
        否则面板上会出现"语气可以更软、也可以更主动；可以说一句想让他陪你，
        但别把它说成要求"这种写给模型看的话。
        """
        mood = MOOD_BRIEF.get(mood_label(drives or {}), "")
        if mode == MODE_ANGRY and mood in ("这会儿特别想黏着他", "想再靠近一点"):
            return "在生气，但心里还惦记着他"
        if mood:
            return mood
        return MODE_BRIEF.get(mode, "")

    # ---------------- 作息 ----------------

    def _sleep_info(self, now: datetime | None = None) -> dict:
        """她现在的作息：睡没睡、几点睡/起、今晚有没有熬夜。

        每天第一次调用时掷一次"今天熬不熬夜"（`sleep.stay_up_chance`），
        熬夜会把入睡时间往后推 `sleep.stay_up_hours` 小时。
        """
        now = now or datetime.now()
        disabled = {
            "enabled": False, "asleep": False, "bedtime": "", "wake": "",
            "stay_up": False, "wake_ts": 0.0,
        }
        if not self._cfg("sleep.enabled", True):
            return disabled
        bedtime = parse_clock(str(self._cfg("sleep.bedtime", "23:30")))
        wake = parse_clock(str(self._cfg("sleep.wake", "07:30")))
        if not bedtime or not wake:
            return disabled

        st = self.state
        cur = now.hour * 60 + now.minute
        b0 = bedtime[0] * 60 + bedtime[1]
        w = wake[0] * 60 + wake[1]
        # 归属"哪一晚"：起床前还算昨晚的作息，否则 00:10 会用今天刚掷的
        # 熬夜结果，把昨晚已经睡下的人判成醒着。
        plan_day = now.strftime("%Y-%m-%d")
        if cur < w:
            plan_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if st.get("sleep_day") != plan_day:
            st["sleep_day"] = plan_day
            chance = min(
                1.0, max(0.0, float(self._cfg("sleep.stay_up_chance", 0.2)))
            )
            st["sleep_stay_up"] = random.random() < chance
        stay_up = bool(st.get("sleep_stay_up"))

        b = b0
        if stay_up:
            shift = max(0, int(float(self._cfg("sleep.stay_up_hours", 2.0)) * 60))
            b = (b0 + shift) % 1440
            # 熬夜把整段睡眠挤没了就按原时间睡（避免 bedtime+wake 配出 20 小时睡眠）
            if (w - b) % 1440 < 60:
                b = b0
                stay_up = False
        if b == w:
            asleep = False
        elif b < w:
            asleep = b <= cur < w
        else:
            asleep = cur >= b or cur < w

        # 下一次起床时间（用于"还能睡多久"）
        wake_dt = now.replace(hour=w // 60, minute=w % 60, second=0, microsecond=0)
        if wake_dt <= now:
            wake_dt += timedelta(days=1)
        return {
            "enabled": True,
            "asleep": asleep,
            "bedtime": f"{b // 60:02d}:{b % 60:02d}",
            "wake": f"{w // 60:02d}:{w % 60:02d}",
            "stay_up": stay_up,
            "wake_ts": wake_dt.timestamp(),
        }

    def _sleep_injection(self, now: datetime | None = None) -> str:
        """她睡着时被消息吵醒，按九维给她一段"被吵醒"的状态说明。"""
        if not self._cfg("inject.enabled", True):
            return ""
        now = now or datetime.now()
        info = self._sleep_info(now)
        if not info.get("asleep"):
            return ""
        d = self.state["drives"]
        fatigue = int(float(d.get("fatigue", 0)))
        stress = int(float(d.get("stress", 0)))
        attachment = int(float(d.get("attachment", 0)))
        intimacy = int(float(d.get("intimacy", 0)))
        joy = int(float(d.get("joy", 0)))

        left_min = max(0, int((info["wake_ts"] - now.timestamp()) // 60))
        left = (
            f"{left_min // 60} 小时 {left_min % 60} 分"
            if left_min >= 60 else f"{left_min} 分钟"
        )
        lines = [
            "## 你正在睡觉（被消息吵醒）",
            f"现在是 {now:%H:%M}，你 {info['bedtime']} 就睡了，"
            f"离 {info['wake']} 起床还有 {left}。",
        ]
        if info.get("stay_up"):
            lines.append("你今晚熬夜了，本来就睡得晚，现在更困。")
        if fatigue >= 70:
            lines.append(
                f"你困得不行（疲惫 {fatigue}）：回得很短、有点迷糊，句子可以不完整。"
            )
        elif stress >= 50:
            lines.append(
                f"被吵醒让你有点烦（压力 {stress}）：语气冲一点，别硬撑着客气。"
            )
        if attachment >= 66 or intimacy >= 66:
            lines.append(
                f"但看到是他（依恋 {attachment}），又有点舍不得凶——"
                "嘴上嫌烦，还是会回。"
            )
        elif joy >= 70:
            lines.append("你心情还不算差，被吵醒也没真的生气。")
        lines.append(
            "不要提“系统/设置/状态”，也别解释自己为什么在睡；"
            "被吵醒就该有被吵醒的样子。"
        )
        return "\n".join(lines)

    def _note_woken(self, now_ts: float | None = None) -> bool:
        """记一次"被消息吵醒"：30 分钟内只算一次，免得连发几条越吵越烦。"""
        now_ts = time.time() if now_ts is None else float(now_ts)
        last = float(self.state.get("last_woken_ts", 0.0) or 0.0)
        if now_ts - last < 30 * 60:
            return False
        self.state["last_woken_ts"] = now_ts
        self._apply_event("woken")
        self._note_event("woken", EVENT_EFFECTS.get("woken", {}), 1.0)
        return True

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
            if not self._is_owner(event, umo):
                return
            text = self._response_text(response)
            if not text:
                return
            if self._is_group(umo):
                # recent 只记私聊；但群里她说的话要进 group_tail——
                # 她自己的上一句很可能就是"为什么被凶"的那一半原因。
                self._note_group_tail("her", text)
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

        def _fresh(raw, ttl: float = 2 * 3600) -> dict:
            """最近完成/放下只保留 2 小时（和念头完成后的展示期一致）。"""
            d = dict(raw or {})
            ts = float(d.get("ts", 0) or 0)
            return d if ts and now_ts - ts <= ttl else {}

        def _fresh_list(raw, ttl: float = 2 * 3600, limit: int = 5) -> list:
            rows = [dict(r) for r in (raw or []) if isinstance(r, dict)]
            rows = [
                r for r in rows
                if float(r.get("ts", 0) or 0)
                and now_ts - float(r["ts"]) <= ttl
                and str(r.get("mood") or "")
            ]
            # 同一秒完成多条时，后完成的应排在前面；只按 ts 排会依赖
            # 输入顺序，面板偶尔会把“最新”显示成旧的那条。
            rows = [
                r for _, r in sorted(
                    enumerate(rows),
                    key=lambda pair: (
                        float(pair[1].get("ts", 0) or 0), pair[0]
                    ),
                    reverse=True,
                )
            ]
            return rows[:limit]

        thoughts = []
        for t in st.get("thoughts") or []:
            # 面板只列"还没被满足"的念头；已完成的收进 recent_rewards，
            # 不再挂在列表里占一个位置（完成的内部仍保留 2 小时，用来挡同 kind 再生）
            if not t.get("pending", True):
                continue
            kind = str(t.get("kind") or "")
            label = ""
            how = ""
            if engine is not None:
                try:
                    label = (engine.KIND_META.get(kind) or {}).get("label") or ""
                    how = (getattr(engine, "THOUGHT_HOW", {}) or {}).get(kind, "")
                except Exception:
                    label = ""
                    how = ""
            expires = float(t.get("expires", 0) or 0)
            created = float(t.get("created", 0) or 0)
            thoughts.append(
                {
                    "kind": kind,
                    "kind_cn": label or kind,
                    "text": str(t.get("text") or ""),
                    "how": how,
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
        blame = float(st.get("blame_pressure", 0.0) or 0.0)
        noticed = float(st.get("blame_noticed_ts", 0) or 0)
        next_fade = noticed + 6 * 3600 if noticed and blame > 0 else 0.0
        blame_log = [
            {
                "t": str(r.get("t") or ""),
                "ts": float(r.get("ts", 0) or 0),
                "g": bool(r.get("g")),
                "ev": str(r.get("ev") or ""),
                "d": float(r.get("d", 0) or 0),
                "w": str(r.get("w") or ""),
                "o": (None if r.get("o") is None else bool(r.get("o"))),
            }
            for r in (st.get("blame_log") or [])
            if isinstance(r, dict)
        ]
        sleep = self._sleep_info()
        return {
            "version": st.get("version"),
            "plugin_version": VERSION,
            "now": now_ts,
            "umo": str(st.get("umo") or ""),
            "mode": mode,
            "mode_cn": MODE_CN.get(mode, ""),
            "mind_knot": bool(st.get("mind_knot")),
            "blame_floor": BLAME_FLOOR,
            "blame_fade_per_6h": BLAME_FADE_PER_6H,
            "next_fade_ts": next_fade,
            "blame_log": blame_log,
            "anger_targets": self._anger_target_list(),
            "sleep": {
                "enabled": bool(sleep.get("enabled")),
                "asleep": bool(sleep.get("asleep")),
                "bedtime": str(sleep.get("bedtime") or ""),
                "wake": str(sleep.get("wake") or ""),
                "stay_up": bool(sleep.get("stay_up")),
                "wake_ts": float(sleep.get("wake_ts") or 0.0),
            },
            "recent_rewards": _fresh_list(st.get("recent_rewards")),
            "last_let_go": _fresh(st.get("last_let_go")),
            "mood_cn": mood_label(st.get("drives") or {}),
            "hint": self._hint_brief(mode, st.get("drives") or {}),
            "blame": float(st.get("blame_pressure", 0.0) or 0.0),
            "comfort_streak": int(st.get("comfort_streak", 0) or 0),
            "account_streak": int(st.get("account_streak", 0) or 0),
            "expectation": float(st.get("expectation", 0.0) or 0.0),
            "drives": {k: float(drives.get(k, 0.0) or 0.0) for k in DRIVES},
            "drives_cn": {k: DRIVE_CN.get(k, k) for k in DRIVES},
            "display_max": {k: float(v) for k, v in DIM_DISPLAY_MAX.items()},
            "display_tick": {k: float(v) for k, v in DIM_DISPLAY_TICK.items()},
            "thoughts": thoughts,
            "recent": recent,
            "last_proactive": float(st.get("last_proactive", 0) or 0),
            "last_private_msg": float(st.get("last_private_msg", 0) or 0),
            "last_tick": float(st.get("last_tick", 0) or 0),
            "proactive_count": int(st.get("proactive_count", 0) or 0),
            "proactive_day": str(st.get("proactive_day") or ""),
            "awaiting_reply": bool(st.get("awaiting_reply")),
            "silent_streak": int(st.get("silent_streak", 0) or 0),
            "silent_streak_group": int(st.get("silent_streak_group", 0) or 0),
            "group_grace": int(st.get("group_grace", 0) or 0),
            "last_silent": float(st.get("last_silent_ts", 0) or 0),
            "unanswered": len(st.get("unanswered") or []),
            "unanswered_group": len(st.get("unanswered_group") or []),
            "group_blames": len(st.get("group_blames") or []),
            "daily_gain": {
                str(k): round(float(v or 0), 1)
                for k, v in (st.get("daily_gain") or {}).items()
                if isinstance(v, (int, float))
            },
            "enabled": bool(self._cfg("enabled", True)),
            "thought_enabled": bool(self._cfg("thought.enabled", True)),
            "settle_cooldown_left": max(
                0.0,
                float(self._cfg("thought.settle_cooldown_minutes", 10)) * 60.0
                - (now_ts - float(st.get("last_settle_ts", 0.0) or 0.0)),
            ),
            "inject_state": {
                str(k): {
                    "ts": float((v or {}).get("ts", 0) or 0),
                    "mode": str((v or {}).get("mode") or ""),
                }
                for k, v in (st.get("inject_state") or {}).items()
            },
            "config": {
                "proactive_enabled": bool(self._cfg("proactive.enabled", True)),
                "cooldown_hours": float(self._cfg("proactive.cooldown_hours", 6.0)),
                "daily_limit": int(self._cfg("proactive.daily_limit", 4)),
                "min_idle_minutes": float(self._cfg("proactive.min_idle_minutes", 10)),
                "target_umo": str(self._cfg("proactive.target_umo", "")),
                "segment_reply": bool(self._cfg("proactive.segment_reply", True)),
                "company": bool(self._cfg("proactive.company", True)),
                "deep": bool(self._cfg("proactive.deep", True)),
                "group_everyone": bool(
                    self._cfg("perception.group_everyone", False)
                ),
                "group_scale": float(self._cfg("perception.group_scale", 0.5)),
                "group_owner_scale": float(
                    self._cfg("perception.group_owner_scale", 1.0)
                ),
                "group_negative_scale": float(
                    self._cfg("perception.group_negative_scale", 0.35)
                ),
                "group_owner_negative_scale": float(
                    self._cfg("perception.group_owner_negative_scale", 0.5)
                ),
                "thought_min_weight": float(self._cfg("thought.min_weight", 0.2)),
                "thought_push": float(self._cfg("thought.proactive_push", 0.15)),
                "settle_cooldown_minutes": float(
                    self._cfg("thought.settle_cooldown_minutes", 10)
                ),
                "shut_enabled": bool(self._cfg("shut.enabled", True)),
                "silent_cap": SHUT_HISTORY_CAP,
                "daily_cap": {k: float(v) for k, v in DAILY_GAIN_CAP.items()},
                "silent_next_p": SHUT_REPLY_LADDER[
                    min(int(st.get("silent_streak", 0) or 0), SHUT_HISTORY_CAP - 1)
                ],
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
        """查看 / 管理当前心境。用法：/心 status|calm|reset|tick|off|on|umo"""
        raw = (getattr(event, "message_str", "") or "").strip()
        arg = raw.split(maxsplit=1)[1].strip().lower() if len(raw.split(maxsplit=1)) > 1 else "status"

        if arg in ("status", "s", ""):
            yield event.plain_result(
                self._status_text(self._is_group(self._umo_of(event)))
            )
            return
        if arg in ("calm", "哄好"):
            # 只解责怪/心结，不动九维——"心结卡在 40"时用它，不用整颗心重置
            self.state["blame_pressure"] = 0.0
            self.state["mode"] = MODE_CALM
            self.state["blame_noticed_ts"] = 0.0
            self.state["comfort_streak"] = 0
            self.state["account_streak"] = 0
            self.state["mind_knot"] = False
            self._clear_anger_targets()
            self.state["group_blames"] = []
            # 哄好了就是新账：把群友/鱼主的负面来源热度也清掉，
            # 否则刚 calm 完一分钟，同一个人再凶一句会被上一轮的桶打折。
            book = self.state.get("event_heat")
            if isinstance(book, dict):
                for key in [k for k in book if str(k).startswith("negative:")]:
                    book.pop(key, None)
            self._save_state()
            yield event.plain_result("责怪已清零，档位回到平静（九维没动）。")
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
            yield event.plain_result(
                f"已手动跳一次心跳。{tip}\n\n"
                + self._status_text(self._is_group(self._umo_of(event)))
            )
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
        yield event.plain_result("用法：/心 status | calm | reset | tick | off | on | umo")

    def _status_text(self, is_group: bool | None = None) -> str:
        st = self.state
        drives = st["drives"]
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        knot = bool(st.get("mind_knot"))
        label = MODE_CN[mode] + ("·心结旧账" if knot and mode == MODE_WRONGED else "")
        head = f"【鱼的心 v{VERSION}】状态：{label}（责怪 {blame:.0f}/100"
        # 40 以下是会褪干净的正常责怪；一旦高过 40，最后会卡在 40（心结）。
        # 把"40"拆出来写，用户才看得懂"一句 +20 怎么就生气了"：不是 20 点，
        # 是 40 的旧账 + 20 的新账。不足 0.5 的不拆，免得出现"心结 40 + 另 0"。
        if blame >= BLAME_FLOOR + 0.5:
            head += f" = 心结 {BLAME_FLOOR:.0f} + 另 {blame - BLAME_FLOOR:.0f}"
        repair_key = "account_streak" if mode == MODE_ANGRY else "comfort_streak"
        repair_label = "道歉/说清楚进度" if mode == MODE_ANGRY else "真哄进度"
        lines = [
            head + "）",
            f"期待 {float(st.get('expectation', 0)):.0f}｜{repair_label} "
            f"{int(st.get(repair_key, 0))}",
        ]
        if mode == MODE_ANGRY:
            targets = self._anger_target_list()
            if targets:
                lines.append(
                    "生气对象："
                    + "、".join(f"{t['label']}（{t['blame']:.0f}）" for t in targets)
                )
        if blame > 0:
            need = RECOVERY_ROUNDS.get(mode, 1)
            if mode == MODE_ANGRY:
                lines.append(
                    f"解开要：道歉/说清楚 {need} 轮，每轮 −{RECOVERY.get(mode, 20):.0f}"
                )
            else:
                lines.append(
                    f"解开要：真哄/道歉 {need} 轮，每轮 −{RECOVERY.get(mode, 20):.0f}"
                )
            noticed = float(st.get("blame_noticed_ts", 0) or 0)
            # 心结钉住时不报"下次褪值"——它不会再掉，报了就是错的
            if noticed and not knot:
                left = max(0.0, noticed + 6 * 3600 - time.time())
                if left < 1:
                    lines.append(f"下次褪值：已到时（下一次心跳 −{BLAME_FADE_PER_6H:.0f}）")
                else:
                    lines.append(
                        f"下次褪值：{left / 3600:.1f} 小时后 −{BLAME_FADE_PER_6H:.0f}"
                        f"（止于 {BLAME_FLOOR:.0f}）"
                    )
        if knot:
            lines.append(
                f"心结：责怪停在 {BLAME_FLOOR:.0f}，不会再自己掉——"
                "需哄/道歉/说清楚，或 /心 calm 才能解。"
            )
        log = [r for r in (st.get("blame_log") or []) if isinstance(r, dict)]
        # 群聊里只给群聊的责怪出处，私聊里只给私聊的：
        # 否则群里一条 /心 status 会把最近私聊的原话贴出来。
        if is_group is not None:
            log = [r for r in log if bool(r.get("g")) == is_group]
        # 责怪已经清零（/心 calm、真哄或褪完）就不再报「最近责怪」：
        # 否则状态头写着“平静 0/100”，下面还挂着一条 +20，像没清干净。
        if log and blame > 0:
            r = log[-1]
            try:
                when = datetime.fromtimestamp(float(r.get("ts", 0))).strftime("%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                when = ""
            where = "群里" if r.get("g") else "私聊"
            if r.get("g"):
                # 谁说的：鱼主 / 群友昵称。老状态没有这两个字段，保持原样。
                if r.get("o") is False:
                    who = str(r.get("w") or "").strip()
                    where += f"·{who}" if who else "·群友"
                elif r.get("o") is True:
                    where += "·鱼主"
            body = str(r.get("t") or "").strip()
            if len(body) > 40:
                body = body[:40] + "…"
            ev = str(r.get("ev") or "")
            lines.append(
                f"最近责怪：{when} {where}「{body}」"
                f"（{EVENT_CN.get(ev, ev)} {float(r.get('d', 0) or 0):+.0f}）"
            )
        if mode == MODE_SHUT:
            streak = int(st.get("silent_streak", 0))
            streak_g = int(st.get("silent_streak_group", 0))
            p_next = SHUT_REPLY_LADDER[min(streak, SHUT_HISTORY_CAP - 1)]
            lines.append(
                f"已读不回：私聊 {streak}/{SHUT_HISTORY_CAP} 条"
                f"｜群聊 {streak_g}/{SHUT_HISTORY_CAP} 条"
                f"｜私聊下一条回的概率 {p_next:.0%}"
            )
            if int(st.get("group_grace", 0)) > 0:
                lines.append("群聊还有 1 句放行额度（刚进回避）")
        book = st.get("daily_gain")
        if isinstance(book, dict) and book:
            parts = [
                f"{DRIVE_CN.get(k, k)} {float(v):.0f}/{DAILY_GAIN_CAP.get(k, 0):.0f}"
                for k, v in book.items()
                if float(v or 0) > 0.05
            ]
            if parts:
                lines.append("今日收益：" + "｜".join(parts))
        lines.append("")
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
