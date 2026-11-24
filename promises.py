"""小约定：纯本地 0-token 短期承诺捕捉、去重、兑现判断与温和提示。

定位为最多 3 条、默认 14 天自然淡掉的近期情感挂念，不与长期记忆插件冲突，
也不占用念头池槽位与念头冷却。
"""

from __future__ import annotations

import re
import time
from typing import Any

# 分句符（保留句末问号/感叹号附在每句末尾，防"周末看电影吗？今天真热"漏过问句拦截）
_CLAUSE_SPLIT_RE = re.compile(r"([。！？!?\n；;]+)")

# 问句/商量语境（不是承诺）
_QUESTION_RE = re.compile(
    r"(?:[?？]\s*$|(?:吗|嘛|好不好|行不行|可以吗|成不成|是不是|要不要|能不能)[?？\s]*$|你答应|答应我)"
)

# 否定/推脱/反话语境（不捕捉为承诺）
_PROMISE_NEG_RE = re.compile(
    r"(?:不敢答应|先不答应|不答应|没答应|算了|不去了|不去啦|没空|没时间|抽不出|"
    r"别指望|做不到|不买|不陪|不带|才不|不想|忘了吧|开玩笑|骗你的|逗你的)"
)

# 第三方转述语境
_REPORT_RE = re.compile(r"(?:他说|她说|别人说|他们说|朋友说|老师说|老板说)")

# 明确承诺句式（从强到弱）
_PROMISE_EXPLICIT_RE = re.compile(
    r"(?:我)?(?:答应你|向你保证|保证|跟你说好了|说好了|拉钩(?:上吊)?)"
    r"(?:[,，、\s]*(?:我一定会|我肯定会|我一定|我肯定|我会|我要|一定会|肯定会|一定|肯定|绝对|到时候|之后|以后))*"
    r"[,，、\s]*(?P<body>.{4,32})"
)
_PROMISE_WAIT_RE = re.compile(
    r"(?:等我|等这周|等下周|等周末|等明天|等放假|等考完|等下班|等忙完|等回来|等有空)"
    r"(?P<when>[^，。！？!?\n]{0,10}?)"
    r"(?:[,，、\s]*(?:一定|肯定|就|再|好好|一定好好))*"
    r"(?P<act>(?:陪你|带你去?|给你买|给你带|给你做|请你吃|请你喝|请你|送你|跟你一起|帮你).{2,22})"
)
_PROMISE_TIME_RE = re.compile(
    r"(?P<when>这周末|下周末|周末|明天|后天|下周|下次|改天|放假|考完试?)"
    r"(?:[,，、\s]*(?:我|一定|肯定|会|要|一定会))*"
    r"(?P<act>(?:陪你|带你去?|给你买|给你带|给你做|请你吃|请你喝|送你|一起去?).{2,22})"
)

# 只剥离明确的多字过渡前缀，绝不剥单个「会/要/就/再/的」（否则会把「会计」「就近」截坏）
_LEAD_TRIM_RE = re.compile(
    r"^(?:[,，、:：\s]+|(?:我一定会|我肯定会|我一定|我肯定|我会|我要|一定会|肯定会|一定|肯定|绝对|到时候))+"
)
_TAIL_TRIM_RE = re.compile(
    r"(?:[，,、~～!！。.\s]+|(?:呀|啊|哦|喔|啦|嘛|吧|呢|哈+|的啦|了哈|了呀|了哦|咯|喔))+?$"
)

# 提取核心实词时剔除的常见虚词/时间词
_STOP_WORDS: tuple[str, ...] = (
    "这周末", "下周末", "周末", "明天", "后天", "下周", "下次", "改天", "放假",
    "到时候", "以后", "之后", "一起", "好好", "一定", "肯定", "绝对",
    "给你", "陪你", "带你", "请你", "送你", "跟你", "帮你", "我们",
    "一下", "一次", "一个", "那个", "这个", "答应", "说好", "保证", "拉钩",
    "考完试", "考完", "下班", "忙完", "有空", "回来",
)
_CJK_OR_WORD_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]{2,}")

# 兑现判断：先拦截否定与将来推迟
_DONE_NEG_RE = re.compile(
    r"(?:还没|没有|没去|没买|没看|没吃|没喝|没做到|没来得及|没买到|没看到|没吃到|"
    r"忘买了|忘了买|差点忘了|不买了|不去了|不看了|还没开始|"
    r"下次再|明天再|改天再|等会再|一会儿再|以后再|过两天再|算了|去不了|买不到|看不了)"
)
# 兑现/完成态信号（去掉「回来了/下班了/到了」等宽泛生活状态词，防「我回来了，听说草莓蛋糕很贵」误判兑现）
_DONE_SIGNAL_RE = re.compile(
    r"(?:兑现|买到了|买回来|买好了|买给你了|买了|送你了|带回来了|做好了|"
    r"看完了|在看|正在看|去看了|吃到了|吃完了|喝上了|"
    r"来陪你|陪你了|陪你聊|做到了|办到了|完成了|搞定了|安排上了|已经.{1,10}了)"
)
_EXPLICIT_DONE_RE = re.compile(
    r"(?:答应你的.{0,6}兑现|小约定.{0,6}兑现|说好的.{0,6}兑现|约定兑现|说到做到)"
)


def _clean_body(raw: str) -> str:
    s = str(raw or "").strip().replace("　", " ")
    # 去掉尾巴的颜文字/标点/语气词（最多剥 3 轮）
    for _ in range(3):
        ns = _TAIL_TRIM_RE.sub("", s).strip()
        if ns == s:
            break
        s = ns
    s = re.sub(r"\s+", " ", s).strip()
    return s[:28]


def extract_keywords(body: str) -> list[str]:
    """从约定短句里抽 2 字以上核心实词，用于去重与兑现匹配。"""
    s = str(body or "").strip()
    if not s:
        return []
    for sw in _STOP_WORDS:
        s = s.replace(sw, " ")
    chunks = _CJK_OR_WORD_RE.findall(s)
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = tok.strip().lower()
        if len(t) < 2 or t in seen:
            return
        seen.add(t)
        out.append(t)

    for ch in chunks:
        if len(ch) < 2:
            continue
        _add(ch)
        # 4 字以上中文串（如"草莓蛋糕"、"看电影呀"）拆 2 字词片，提高口语命中率
        if re.fullmatch(r"[\u4e00-\u9fff]+", ch) and len(ch) >= 4:
            for i in range(0, len(ch) - 1):
                _add(ch[i : i + 2])
        elif re.fullmatch(r"[\u4e00-\u9fff]+", ch) and len(ch) == 3:
            _add(ch[1:])  # 如 "看电影" -> "电影", "吃火锅" -> "火锅"
            _add(ch[:2])
    return out[:8]


def _split_clauses(raw: str) -> list[str]:
    """按句末标点切分，并把标点留在各自句尾，方便准确识别每句是否为问句。"""
    parts = _CLAUSE_SPLIT_RE.split(str(raw or "").strip())
    clauses: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if _CLAUSE_SPLIT_RE.fullmatch(p):
            buf += p
            if buf.strip():
                clauses.append(buf.strip())
            buf = ""
        else:
            buf += p
    if buf.strip():
        clauses.append(buf.strip())
    return clauses


def extract_promise(text: str) -> str:
    """从鱼主私聊一句话里尝试提取明确的小约定短句；无明确承诺返回空串。"""
    raw = str(text or "").strip()
    if not raw or len(raw) < 5:
        return ""
    for clause in _split_clauses(raw):
        if len(clause) < 5 or len(clause) > 60:
            continue
        if _QUESTION_RE.search(clause):
            continue
        if _PROMISE_NEG_RE.search(clause):
            continue
        if _REPORT_RE.search(clause):
            continue

        body = ""
        m = _PROMISE_EXPLICIT_RE.search(clause)
        if m:
            body = _LEAD_TRIM_RE.sub("", m.group("body")).strip()
        if not body:
            m = _PROMISE_WAIT_RE.search(clause)
            if m:
                when = (m.group("when") or "").strip(" ，,、")
                act = (m.group("act") or "").strip()
                body = f"{when}{act}" if when else act
        if not body:
            m = _PROMISE_TIME_RE.search(clause)
            if m:
                when = (m.group("when") or "").strip()
                act = (m.group("act") or "").strip()
                body = f"{when}{act}"

        body = _clean_body(body)
        if len(body) < 4:
            continue
        # 必须至少有 1 个核心实词，挡住"我一定会的""给你好好弄"这类空洞话
        if not extract_keywords(body):
            continue
        return body
    return ""


def is_similar_promise(a: str, b: str) -> bool:
    """判断两条约定是否说的是同一件事（去重用）。"""
    sa = str(a or "").strip().lower()
    sb = str(b or "").strip().lower()
    if not sa or not sb:
        return False
    if sa == sb or sa in sb or sb in sa:
        return True
    ka = set(extract_keywords(sa))
    kb = set(extract_keywords(sb))
    if not ka or not kb:
        return False
    inter = ka & kb
    if not inter:
        return False
    return (len(inter) / min(len(ka), len(kb))) >= 0.5


def prune_promises(
    rows: list[Any] | None,
    *,
    now_ts: float | None = None,
    expire_days: int = 14,
    max_active: int = 3,
) -> list[dict[str, Any]]:
    """清掉已过期或坏格式的约定，并截断到上限（保留最新的）。"""
    now = float(now_ts if now_ts is not None else time.time())
    ttl = max(1, int(expire_days or 14)) * 86400.0
    cap = max(1, min(10, int(max_active or 3)))
    clean: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        ts = float(row.get("ts", 0.0) or 0.0)
        if not text or ts <= 0:
            continue
        if now - ts > ttl:
            continue
        kws = row.get("kws")
        if not isinstance(kws, list) or not kws:
            kws = extract_keywords(text)
        clean.append(
            {
                "text": text[:30],
                "ts": ts,
                "src": str(row.get("src") or "auto"),
                "kws": [str(k) for k in kws if str(k)][:8],
            }
        )
    clean.sort(key=lambda r: float(r["ts"]))
    return clean[-cap:]


def match_completed_promise(
    text: str,
    rows: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """判断鱼主这句话是不是兑现了某条活跃的小约定。

    按分句判定：同一句里同时满足非否定/非推迟 + 含兑现信号词 + 命中核心实词
    （或仅剩 1 条且明确说兑现），避免跨句误拼导致误完成。
    """
    s = str(text or "").strip()
    active = [r for r in (rows or []) if isinstance(r, dict) and str(r.get("text") or "").strip()]
    if not s or not active:
        return None
    for clause in _split_clauses(s):
        if _DONE_NEG_RE.search(clause):
            continue
        if not _DONE_SIGNAL_RE.search(clause):
            continue
        low = clause.lower()
        for row in active:
            kws = row.get("kws") or extract_keywords(str(row.get("text") or ""))
            for kw in kws:
                if len(kw) >= 2 and kw.lower() in low:
                    return row
        if len(active) == 1 and _EXPLICIT_DONE_RE.search(clause):
            return active[0]
    return None


def build_promise_injection(
    rows: list[dict[str, Any]] | None,
    *,
    now_ts: float | None = None,
    mode: str = "calm",
    has_today_special: bool = False,
    min_age_hours: float = 18.0,
) -> tuple[str, str]:
    """生成温和的单行小约定提醒（仅在日常档、无当天特别日、且约定记下满 18 小时后）。"""
    if str(mode or "calm") != "calm" or has_today_special:
        return "", ""
    now = float(now_ts if now_ts is not None else time.time())
    need = max(0.0, float(min_age_hours)) * 3600.0
    due = [
        r for r in (rows or [])
        if isinstance(r, dict)
        and str(r.get("text") or "").strip()
        and (now - float(r.get("ts", 0.0) or 0.0)) >= need
    ]
    if not due:
        return "", ""
    # 挑最早记下的那一条轻轻提一下，绝不同时塞多条啰嗦
    due.sort(key=lambda r: float(r.get("ts", 0.0) or 0.0))
    picked = due[0]
    age_days = max(1, int((now - float(picked["ts"])) // 86400))
    age_str = "昨天" if age_days == 1 else f"{age_days} 天前"
    txt = str(picked["text"]).strip()
    sig = f"{txt[:12]}:{age_days}"
    line = (
        f"心里记着一个小约定：「{txt}」（{age_str}记下的）；"
        "聊到附近可以顺嘴轻轻提一句，如果他已经做完或正在做就顺势开心接住，别反复催。"
    )
    return sig, line
