"""鱼主与鱼的性别及代词适配：纯标准库单次扫描替换模块。

设计要点：
1. 源码内部统一沿用基准约定：「她」= 鱼，「他」= 鱼主。
2. 全部替换采用单次正则扫描（simultaneous replacement），彻底避免
   「男鱼 × 女鱼主」场景下先换「她→他」再换「他→她」造成的二次覆盖。
3. 精确保护常见非指代合成词（其他/他们/她们/他人/吉他/利他/排他等），
   且调用方仅对插件文案模板调用，绝不篡改用户聊天原文与自定义人设。
"""

from __future__ import annotations

import re
from typing import Any

VALID_GENDERS: tuple[str, ...] = ("female", "male", "neutral")

GENDER_PRONOUN: dict[str, str] = {
    "female": "她",
    "male": "他",
    "neutral": "TA",
}

GENDER_LABEL: dict[str, str] = {
    "female": "女",
    "male": "男",
    "neutral": "不指定",
}

GENDER_SYMBOL: dict[str, str] = {
    "female": "♀",
    "male": "♂",
    "neutral": "✦",
}

# 单次同时匹配基准文本中的指代词：
# - 跳过「其他/吉他/利他/排他」「他们/他人/他妈/他乡/他国/他处/他日/他年/他杀/他山」
# - 跳过「她们」
_PRONOUN_RE = re.compile(
    r"(?<!其)(?<!吉)(?<!利)(?<!排)他(?!们|人|妈|乡|国|处|日|年|杀|山)|她(?!们)"
)


def norm_gender(raw: Any, default: str = "female") -> str:
    """规范化性别配置值为 female / male / neutral。"""
    s = str(raw or "").strip().lower()
    if s in ("female", "f", "girl", "woman", "女", "女性", "她"):
        return "female"
    if s in ("male", "m", "boy", "man", "男", "男性", "他"):
        return "male"
    if s in ("neutral", "n", "none", "other", "ta", "中性", "不指定", "保密"):
        return "neutral"
    return default if default in VALID_GENDERS else "female"


def pronoun_of(gender: Any, default: str = "female") -> str:
    """性别 → 中文人称代词（她 / 他 / TA）。"""
    g = norm_gender(gender, default=default)
    return GENDER_PRONOUN[g]


def identity_info(
    fish_gender: Any = "female",
    owner_gender: Any = "male",
) -> dict[str, str]:
    """生成给 WebUI 与日志使用的标准化性别身份快照。"""
    fg = norm_gender(fish_gender, default="female")
    og = norm_gender(owner_gender, default="male")
    return {
        "fish_gender": fg,
        "owner_gender": og,
        "fish_pronoun": GENDER_PRONOUN[fg],
        "owner_pronoun": GENDER_PRONOUN[og],
        "fish_label": GENDER_LABEL[fg],
        "owner_label": GENDER_LABEL[og],
        "fish_symbol": GENDER_SYMBOL[fg],
        "owner_symbol": GENDER_SYMBOL[og],
    }


def _swap_pronouns(text: str, *, he_to: str, she_to: str) -> str:
    s = str(text or "")
    if not s or (he_to == "他" and she_to == "她"):
        return s

    def _repl(m: re.Match) -> str:
        tok = m.group(0)
        return he_to if tok == "他" else she_to

    return _PRONOUN_RE.sub(_repl, s)


def adapt_prompt(
    text: str,
    *,
    fish_gender: Any = "female",
    owner_gender: Any = "male",
) -> str:
    """适配面向 LLM 提示词与命令展示的文本（基准：她=鱼，他=鱼主）。"""
    fish_p = pronoun_of(fish_gender, default="female")
    owner_p = pronoun_of(owner_gender, default="male")
    return _swap_pronouns(text, he_to=owner_p, she_to=fish_p)


# 兼容别名：命令回复与提示词共用同一基准替换规则
adapt_user_text = adapt_prompt


def adapt_ui_text(
    text: str,
    *,
    fish_gender: Any = "female",
) -> str:
    """适配面向 WebUI 面板的展示文本（单次同时将基准「他→你」「她→鱼代词」）。

    这样即便设置为男鱼（她→他），「夸她一句」会变成「夸他一句」而不会
    再被二次替换成「夸你一句」，「想让他夸一句」则变成「想让你夸一句」。
    """
    fish_p = pronoun_of(fish_gender, default="female")
    return _swap_pronouns(text, he_to="你", she_to=fish_p)
