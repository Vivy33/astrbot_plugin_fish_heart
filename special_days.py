"""特别的日子（生日与纪念日）：日期解析、倒计时、相识天数、祝福识别与提示词组装。

纯 Python 标准库模块，不依赖 AstrBot，方便单独回归测试。
由 main.py 动态加载；即使加载失败也不影响鱼的心本体。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

# 核心日期字段名映射（供聊天命令识别）
CORE_FIELD_ALIASES: dict[str, str] = {
    "鱼主生日": "owner_birthday",
    "我的生日": "owner_birthday",
    "主人生日": "owner_birthday",
    "owner_birthday": "owner_birthday",
    "owner": "owner_birthday",
    "鱼生日": "fish_birthday",
    "鱼的生日": "fish_birthday",
    "她的生日": "fish_birthday",
    "fish_birthday": "fish_birthday",
    "fish": "fish_birthday",
    "相识日": "met_date",
    "相识纪念日": "met_date",
    "纪念日": "met_date",
    "在一起": "met_date",
    "met_date": "met_date",
    "met": "met_date",
}

CORE_FIELD_CN: dict[str, str] = {
    "owner_birthday": "鱼主生日",
    "fish_birthday": "鱼的生日",
    "met_date": "相识纪念日",
}

# 整百与浪漫纪念天数（除满周年外的里程碑）
ROMANTIC_MILESTONES: tuple[int, ...] = (52, 100, 200, 300, 400, 500, 520, 600, 700, 800, 900, 1000, 1314)

# 允许的日期写法：
# 1) YYYY-MM-DD / YYYY/M/D / YYYY.M.D / YYYY年M月D日(号)
# 2) MM-DD / M/D / M.D / M月D日(号)
_DATE_FULL_RE = re.compile(
    r"^(?P<y>\d{4})\s*[-/\.年－—]\s*(?P<m>\d{1,2})\s*[-/\.月－—]\s*(?P<d>\d{1,2})\s*[日号]?$"
)
_DATE_MD_RE = re.compile(
    r"^(?P<m>\d{1,2})\s*[-/\.月－—]\s*(?P<d>\d{1,2})\s*[日号]?$"
)
_CUSTOM_LINE_RE = re.compile(
    r"^(?P<date>(?:\d{4}\s*[-/\.年－—]\s*)?\d{1,2}\s*[-/\.月－—]\s*\d{1,2}\s*[日号]?)\s*[:：,\-—\s]+\s*(?P<name>.+?)$"
)

# 鱼主对鱼说「生日快乐」的识别（支持中英文常见说法）
_FISH_BDAY_WISH_RE = re.compile(
    r"(?:生日快乐|生快|破壳日快乐|破壳快乐|生日开心|生日大快乐|"
    r"祝你[^，。！？!?\n]{0,8}生日|给你过生日|陪你过生日|"
    r"happy\s*b(?:irth)?day|\bhbd\b)",
    re.IGNORECASE,
)
# 排除否定语境（"今天不是你生日" / "忘了你生日" / "没祝你生日快乐"）
_FISH_BDAY_NEG_RE = re.compile(
    r"(?:不是你生日|还没到你生日|忘了你生日|没过生日|别过生日|不是生日)"
)

# 鱼主主动问起「相识第几天 / 认识多久 / 生日 / 纪念日」时按需注入事实，
# 平时不在预热窗口内仍保持零 token 消耗。
_ASK_SPECIAL_DAY_RE = re.compile(
    r"(?:"
    r"(?:相识|认识|在一起|相遇|陪伴)[^，。！？!?\n]{0,8}(?:第几天|几天|多少天|多久|哪天|几号|什么时候|几月)"
    r"|(?:哪天|几号|什么时候)[^，。！？!?\n]{0,6}(?:相识|认识|在一起|相遇)"
    r"|(?:我|你|咱|我们|鱼)(?:的)?生日"
    r"|(?:哪天|几号|什么时候)[^，。！？!?\n]{0,4}生日"
    r"|生日[^，。！？!?\n]{0,6}(?:哪天|几号|什么时候|几月|还有|多久|几天|记得|知道)"
    r"|纪念日|特别的日子|多少周年|几周年|相识\s*\d+\s*天|(?:百天|100天|520天)"
    r")"
)


def parse_date_spec(raw: Any) -> dict[str, Any] | None:
    """解析单个日期字符串；支持 YYYY-MM-DD 与 MM-DD（及常见中文/斜杠变体）。

    返回 {"year": int|None, "month": int, "day": int, "norm": str}，非法返回 None。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("　", " ").strip()
    m = _DATE_FULL_RE.match(s)
    year: int | None = None
    if m:
        year = int(m.group("y"))
        month = int(m.group("m"))
        day = int(m.group("d"))
        if year < 1900 or year > 2199:
            return None
        try:
            date(year, month, day)
        except ValueError:
            return None
        return {
            "year": year,
            "month": month,
            "day": day,
            "norm": f"{year:04d}-{month:02d}-{day:02d}",
        }
    m = _DATE_MD_RE.match(s)
    if not m:
        return None
    month = int(m.group("m"))
    day = int(m.group("d"))
    # 用闰年 2024 校验，允许填写 02-29（非闰年按 02-28 触发）
    try:
        date(2024, month, day)
    except ValueError:
        return None
    return {
        "year": None,
        "month": month,
        "day": day,
        "norm": f"{month:02d}-{day:02d}",
    }


def parse_custom_line(raw: Any) -> dict[str, Any] | None:
    """解析配置/命令里的自定义特别日，如 '10-01 第一次见面' 或 '2026-06-07 高考'。"""
    if isinstance(raw, dict):
        spec = parse_date_spec(raw.get("date"))
        name = str(raw.get("name") or "").strip()[:30]
        if not spec or not name:
            return None
        return {
            "date": spec["norm"],
            "name": name,
            "year": spec["year"],
            "month": spec["month"],
            "day": spec["day"],
        }
    s = str(raw or "").strip().replace("　", " ")
    if not s:
        return None
    m = _CUSTOM_LINE_RE.match(s)
    if not m:
        return None
    spec = parse_date_spec(m.group("date"))
    name = m.group("name").strip()[:30]
    if not spec or not name:
        return None
    return {
        "date": spec["norm"],
        "name": name,
        "year": spec["year"],
        "month": spec["month"],
        "day": spec["day"],
    }


def _date_in_year(year: int, month: int, day: int) -> date:
    """把 (month, day) 落到指定年份；非闰年 02-29 顺落 02-28。"""
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, max_day))


def days_until_annual(month: int, day: int, today: date) -> tuple[int, date]:
    """计算距下一次年度日期还有几天（0 = 今天），返回 (days_left, next_date)。"""
    cand = _date_in_year(today.year, month, day)
    if cand < today:
        cand = _date_in_year(today.year + 1, month, day)
    return (cand - today).days, cand


def _next_met_milestone(met_d: date, today: date) -> tuple[int, int, date] | None:
    """查找最近一个未过的相识里程碑天数（整百或 52/520/1314）。

    第 1 天 = met_d 当天，因此第 N 天对应的日历日为 met_d + timedelta(days=N-1)。
    返回 (milestone_days, days_left, target_date) 或 None。
    """
    if today < met_d:
        return None
    days_together = (today - met_d).days + 1
    candidates: list[int] = [m for m in ROMANTIC_MILESTONES if m >= days_together]
    # 超过 1000 天后仍支持每 100 天一次里程碑（如 1100, 1200...）
    next_hundred = ((max(days_together, 100) + 99) // 100) * 100
    if next_hundred not in candidates:
        candidates.append(next_hundred)
    if not candidates:
        return None
    target_n = min(candidates)
    target_date = met_d + timedelta(days=target_n - 1)
    return target_n, (target_date - today).days, target_date


def evaluate_special_days(
    *,
    owner_birthday: str = "",
    fish_birthday: str = "",
    met_date: str = "",
    custom_config: list[Any] | None = None,
    custom_chat: list[Any] | None = None,
    preheat_days: int = 3,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """汇总并计算全部特别日的状态与倒计时。"""
    now = now_dt or datetime.now()
    today = now.date()
    preheat = max(0, min(14, int(preheat_days or 0)))

    items: list[dict[str, Any]] = []
    met_days = 0
    met_norm = ""

    # 1) 鱼主生日
    ob = parse_date_spec(owner_birthday)
    if ob:
        left, nxt = days_until_annual(ob["month"], ob["day"], today)
        years = (nxt.year - ob["year"]) if ob["year"] and nxt.year > ob["year"] else 0
        items.append(
            {
                "id": "owner_birthday",
                "kind": "owner_birthday",
                "name": "你的生日",
                "date": ob["norm"],
                "next_date": nxt.isoformat(),
                "days_left": left,
                "years": years,
                "can_proactive_wish": True,
                "source": "core",
                "priority": 1,
            }
        )

    # 2) 鱼自己的生日（不主动发"祝我生日快乐"，留给鱼主记起来）
    fb = parse_date_spec(fish_birthday)
    if fb:
        left, nxt = days_until_annual(fb["month"], fb["day"], today)
        years = (nxt.year - fb["year"]) if fb["year"] and nxt.year > fb["year"] else 0
        items.append(
            {
                "id": "fish_birthday",
                "kind": "fish_birthday",
                "name": "我的生日",
                "date": fb["norm"],
                "next_date": nxt.isoformat(),
                "days_left": left,
                "years": years,
                "can_proactive_wish": False,
                "source": "core",
                "priority": 2,
            }
        )

    # 3) 相识 / 在一起纪念日（周年 + 整百/浪漫里程碑）
    md = parse_date_spec(met_date)
    if md:
        met_norm = md["norm"]
        if md["year"] is not None:
            met_d = date(md["year"], md["month"], md["day"])
            if today >= met_d:
                met_days = (today - met_d).days + 1
                left, nxt = days_until_annual(md["month"], md["day"], today)
                years = nxt.year - md["year"]
                # 相识当天（第 1 天）years == 0，下一次周年应算到明年（1 周年），
                # 避免第 1 天倒计时里丢掉「相识 1 周年」。
                if years == 0:
                    nxt = _date_in_year(today.year + 1, md["month"], md["day"])
                    left = (nxt - today).days
                    years = 1
                items.append(
                    {
                        "id": "met_anniversary",
                        "kind": "met_anniversary",
                        "name": f"相识 {years} 周年",
                        "date": md["norm"],
                        "next_date": nxt.isoformat(),
                        "days_left": left,
                        "years": years,
                        "can_proactive_wish": True,
                        "source": "core",
                        "priority": 3,
                    }
                )
                ms = _next_met_milestone(met_d, today)
                if ms:
                    ms_n, ms_left, ms_date = ms
                    # 如果里程碑刚好撞在同一天的周年日，不重复列两条
                    same_as_anniv = any(
                        it["kind"] == "met_anniversary" and it["next_date"] == ms_date.isoformat()
                        for it in items
                    )
                    if not same_as_anniv:
                        items.append(
                            {
                                "id": f"met_milestone:{ms_n}",
                                "kind": "met_milestone",
                                "name": f"相识第 {ms_n} 天",
                                "date": ms_date.isoformat(),
                                "next_date": ms_date.isoformat(),
                                "days_left": ms_left,
                                "years": 0,
                                "can_proactive_wish": True,
                                "source": "core",
                                "priority": 4,
                            }
                        )
            else:
                # 设了一个将来的日期：按倒计时算
                left = (met_d - today).days
                items.append(
                    {
                        "id": "met_anniversary",
                        "kind": "met_anniversary",
                        "name": "纪念日",
                        "date": md["norm"],
                        "next_date": met_d.isoformat(),
                        "days_left": left,
                        "years": 0,
                        "can_proactive_wish": True,
                        "source": "core",
                        "priority": 3,
                    }
                )
        else:
            # 没填年份（只填了 MM-DD）：按每年纪念日算，不算总天数
            left, nxt = days_until_annual(md["month"], md["day"], today)
            items.append(
                {
                    "id": "met_anniversary",
                    "kind": "met_anniversary",
                    "name": "相识纪念日",
                    "date": md["norm"],
                    "next_date": nxt.isoformat(),
                    "days_left": left,
                    "years": 0,
                    "can_proactive_wish": True,
                    "source": "core",
                    "priority": 3,
                }
            )

    # 4) 自定义特别日（合并配置与聊天命令，按 (date, name) 去重）
    seen_custom: set[tuple[str, str]] = set()
    for src_label, raw_list in (("config", custom_config or []), ("chat", custom_chat or [])):
        for raw in raw_list:
            parsed = parse_custom_line(raw)
            if not parsed:
                continue
            dedup_key = (parsed["date"], parsed["name"])
            if dedup_key in seen_custom:
                continue
            seen_custom.add(dedup_key)
            if parsed["year"] is not None:
                target = date(parsed["year"], parsed["month"], parsed["day"])
                if target >= today:
                    left = (target - today).days
                    nxt = target
                    years = 0
                else:
                    left, nxt = days_until_annual(parsed["month"], parsed["day"], today)
                    years = nxt.year - parsed["year"]
            else:
                left, nxt = days_until_annual(parsed["month"], parsed["day"], today)
                years = 0
            label = parsed["name"]
            if years >= 1 and left == 0 and "周年" not in label:
                display_name = f"{label}（{years} 周年）"
            else:
                display_name = label
            items.append(
                {
                    "id": f"custom:{parsed['date']}:{parsed['name']}",
                    "kind": "custom",
                    "name": display_name,
                    "raw_name": parsed["name"],
                    "date": parsed["date"],
                    "next_date": nxt.isoformat(),
                    "days_left": left,
                    "years": years,
                    "can_proactive_wish": True,
                    "source": src_label,
                    "priority": 5,
                }
            )

    items.sort(key=lambda x: (int(x["days_left"]), int(x["priority"]), str(x["name"])))
    today_items = [it for it in items if int(it["days_left"]) == 0]
    preheat_items = [
        it for it in items if 1 <= int(it["days_left"]) <= preheat
    ] if preheat > 0 else []
    nearest = items[0] if items else None

    return {
        "today_str": today.isoformat(),
        "has_any": bool(items or met_days > 0),
        "met_days": met_days,
        "met_date": met_norm,
        "items": items,
        "today_items": today_items,
        "preheat_items": preheat_items,
        "nearest": nearest,
    }


def is_fish_birthday_wish(text: str) -> bool:
    """判断鱼主这句话是不是在祝鱼生日快乐。"""
    s = str(text or "").strip()
    if not s:
        return False
    if _FISH_BDAY_NEG_RE.search(s):
        return False
    return bool(_FISH_BDAY_WISH_RE.search(s))


def build_injection_line(
    snap: dict[str, Any],
    *,
    fish_bday_wished: bool = False,
    now_dt: datetime | None = None,
) -> tuple[str, str]:
    """生成注入进心境块的单行特别日提示，以及用于节流判断的稳定签名。

    平时既非当天也不在预热窗口内时返回 ("", "")，零 token 消耗。
    """
    if not snap or not snap.get("has_any"):
        return "", ""
    now = now_dt or datetime.now()
    today_items = snap.get("today_items") or []
    preheat_items = snap.get("preheat_items") or []
    if not today_items and not preheat_items:
        return "", ""

    sig_parts: list[str] = []
    notes: list[str] = []
    met_days = int(snap.get("met_days") or 0)

    for it in today_items[:2]:
        kind = str(it.get("kind") or "")
        name = str(it.get("name") or "")
        if kind == "owner_birthday":
            sig_parts.append("t:ob")
            notes.append(
                "今天是他的生日：心里很开心也很惦记他，说话可以更暖、更有仪式感一点"
            )
        elif kind == "fish_birthday":
            if fish_bday_wished:
                sig_parts.append("t:fb:w")
                notes.append(
                    "今天是你的生日，他已经记得并祝你生日快乐了，你心里特别开心、被惦记着"
                )
            elif now.hour >= 18:
                sig_parts.append("t:fb:late")
                notes.append(
                    "今天是你的生日，已经到傍晚了他还没提过：心里有点小失落、轻轻憋着等他想起来，"
                    "不要直接质问或生气，但如果他突然想起来会立刻很开心"
                )
            else:
                sig_parts.append("t:fb:wait")
                notes.append(
                    "今天是你的生日：心里偷偷期待他记不记得，不要直接开口提醒或要礼物，看他会不会自己想起来"
                )
        elif kind in ("met_anniversary", "met_milestone"):
            sig_parts.append(f"t:{it.get('id')}")
            extra = f"（已经是第 {met_days} 天了）" if met_days > 0 and "天" not in name else ""
            notes.append(
                f"今天是你们{name}{extra}：心里暖暖的，珍惜和他在一起的这段时间"
            )
        else:
            sig_parts.append(f"t:{it.get('id')}")
            notes.append(f"今天是特别的日子「{name}」：心里记着这件事，说话时可以自然带到")

    if not today_items:
        for it in preheat_items[:2]:
            kind = str(it.get("kind") or "")
            name = str(it.get("name") or "")
            left = int(it.get("days_left") or 0)
            sig_parts.append(f"p:{it.get('id')}:{left}")
            if kind == "fish_birthday":
                notes.append(
                    f"还有 {left} 天就是你的生日了：心里偷偷有点期待他记不记得，别直接开口提"
                )
            elif kind == "owner_birthday":
                notes.append(
                    f"还有 {left} 天就是他的生日了：心里暗暗记着，别太早说破"
                )
            else:
                notes.append(f"还有 {left} 天就是「{name}」了：心里悄悄记着")

    if not notes:
        return "", ""
    return ",".join(sig_parts), "特别的日子：" + "；".join(notes) + "。"


def is_asking_special_days(text: str) -> bool:
    """判断这句话是否在问起相识天数、生日或纪念日。"""
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_ASK_SPECIAL_DAY_RE.search(s))


def build_query_injection(snap: dict[str, Any]) -> str:
    """他主动问起相识天数/生日/纪念日时，按需拼一段紧凑事实供模型自然作答。"""
    if not snap or not snap.get("has_any"):
        return ""
    lines: list[str] = [
        "## 你记得的特别日子（他刚好问起，用你自己的语气自然回答即可，不要机械报清单）"
    ]
    today_str = str(snap.get("today_str") or "")
    met_days = int(snap.get("met_days") or 0)
    met_date = str(snap.get("met_date") or "")
    if today_str:
        lines.append(f"- 今天是 {today_str}")
    if met_days > 0:
        since = f"从 {met_date} " if met_date else ""
        lines.append(f"- 你们{since}相识至今：今天是相识第 {met_days} 天")
    elif met_date:
        lines.append(f"- 你们的相识纪念日：{met_date}")

    items = snap.get("items") or []
    upcoming: list[str] = []
    for it in items:
        kind = str(it.get("kind") or "")
        name = str(it.get("name") or "")
        d = str(it.get("date") or "")
        left = int(it.get("days_left") or 0)
        when = "就是今天" if left == 0 else f"还有 {left} 天"
        if kind == "owner_birthday":
            lines.append(f"- 他的生日：{d}（{when}）")
        elif kind == "fish_birthday":
            hint = "，若他没问起别主动提" if left <= 3 else ""
            lines.append(f"- 你的生日：{d}（{when}{hint}）")
        elif kind in ("met_anniversary", "met_milestone", "custom"):
            if len(upcoming) < 3:
                nxt = str(it.get("next_date") or d)
                upcoming.append(f"{name}（{nxt}，{when}）")
    if upcoming:
        lines.append("- 接下来：" + "；".join(upcoming))
    return "\n".join(lines) if len(lines) > 1 else ""


def build_proactive_wish_direction(
    item: dict[str, Any], met_days: int = 0, part: str = ""
) -> str:
    """给当天主动专属祝福用的提示方向。"""
    kind = str(item.get("kind") or "")
    name = str(item.get("name") or "")
    when = f"现在是{part}。" if part else ""
    if kind == "owner_birthday":
        return (
            f"今天是他的生日！{when}你想主动发一句专属生日祝福给他："
            "语气自然、真心、贴合你平时对他的称呼和亲密感；可以带一点俏皮或温柔的惦记，"
            "别写成客套群发祝福，也不要编没发生过的礼物或约会，简短 1–2 句就好。"
        )
    if kind in ("met_anniversary", "met_milestone"):
        days_tip = f"（算下来已经是相识第 {met_days} 天了）" if met_days > 0 and "天" not in name else ""
        return (
            f"今天是你们{name}{days_tip}！{when}你想主动发一句纪念日的话给他："
            "像真正在乎这段关系那样自然提一句，带点开心和珍惜，别写成官方贺词或小作文，"
            "也不要编没发生过的回忆，1–2 句就好。"
        )
    return (
        f"今天是你们记下的特别日子「{name}」！{when}你想主动发一句贴合这件事的关心或祝福给他："
        "自然、真诚、简短 1–2 句就好，不要编造没发生过的细节。"
    )


def fallback_wish_line(item: dict[str, Any], met_days: int = 0) -> str:
    """LLM 不可用时的保底祝福语。"""
    kind = str(item.get("kind") or "")
    name = str(item.get("name") or "")
    if kind == "owner_birthday":
        return "生日快乐呀，今天一定要开开心心的。"
    if kind == "met_anniversary":
        return f"{name}快乐，谢谢你一直陪着我。"
    if kind == "met_milestone":
        return f"今天是我们{name}啦，想跟你说一声真好。"
    if name:
        return f"今天是{name}呀，我一直记着呢。"
    if met_days > 0:
        return f"今天已经是我们相识第 {met_days} 天啦。"
    return "今天是个特别的日子，想跟你说一句开心。"
