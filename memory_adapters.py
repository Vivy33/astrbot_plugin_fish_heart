"""记忆插件适配层：纯 getattr 探测，不 import 任何插件。

职责只有两件：
1. 探测插件能力（getattr 链）、解析作用域；
2. 把召回结果映射成通用的 `memory_block.Entry`，排版交给 memory_block。

fish_heart 只认 `fish_heart_recall` / `fish_heart_remember` 协议；
本模块把常见记忆插件的能力包成这两个方法，让装了原版插件的用户也能开箱即用。

约定：
- 只用 getattr 链探测，不 import 目标插件的任何模块；
- 探测失败一律返回 None / False / 空串，不抛异常；
- 不缓存实例：插件可能重载，每次按当前对象重新探测；
- `recall` 返回**完整块**（`returns_formatted = True`），调用方不再包一层。

调用链（新增适配器时按这条线检查）：
  `_resolve_access`（可选，失败退回会话级 umo）
    → `MemoryAdapter.recall`
       → `_search`（按签名决定是否传 extra_scopes）
       → 过滤空正文 → `_pick_relevant`（门槛 + 同文去重）
       → `_to_entries`（映射成通用 Entry）
       → `memory_block.build`（2000 字排版 + 注入防护）
  任何一步失败都降级为空串，不阻塞主动消息。
"""

from __future__ import annotations

import inspect
import logging
import math
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

#: 条首最多列几个参与人
MAX_PARTICIPANTS = 4
#: 和 fish_heart 本体同一个 logger 命名空间，日志能对得上
logger = logging.getLogger("astrbot_plugin_fish_heart")

#: 同类失败只 warning 一次（之后只记 debug），避免刷屏又能被发现
_WARNED: set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    """同类问题每个进程只 warning 一次；之后由调用方记 debug。"""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(msg, *args)


def _load_block_module():
    """加载同目录的排版模块（不依赖 sys.path，插件目录不一定在里面）。"""
    try:
        import importlib.util as _ilu

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_block.py")
        spec = _ilu.spec_from_file_location(
            "astrbot_plugin_fish_heart_memory_block", path
        )
        module = _ilu.module_from_spec(spec)
        # dataclass 需要模块在 sys.modules 里才能解析注解
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)  # 别留半个模块在缓存里
            raise
        return module
    except Exception as e:
        logger.warning(
            "[鱼的心] 排版模块加载失败，本次进程内不再注入记忆"
            "（重载插件/重启后重试）: %s",
            e,
        )
        return None


memory_block = _load_block_module()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容 dict 与对象属性：适配层不假设对方返回什么形态。"""
    if obj is None:
        return default
    try:
        if isinstance(obj, Mapping):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        # 对方的 __getattr__/get 可能抛任意异常，探测失败一律当没有
        return default


def _as_text(value: Any, default: str = "") -> str:
    """转字符串的容错版：None/对象的 __str__ 抛异常都返回 default。"""
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _entry_body(item: Any) -> str:
    """正文：优先 persona_summary（第一人称叙事），没有就退 content。

    换行会被压平成空格——每条必须是「条首 + 一行正文」，
    否则按行截断时会把一条记忆切成两半。
    """
    meta = _field(item, "metadata") or {}
    body = _as_text(_field(meta, "persona_summary")).strip()
    if not body:
        body = _as_text(_field(item, "content")).strip()
    if not body:
        return ""
    return re.sub(r"\s*\n\s*", " ", body).strip()


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """读对象属性的容错版：属性 getter 抛任意异常都当默认值。"""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _as_float(value: Any, default=None):
    """宽松转 float：非数字、bool、溢出、NaN/Inf 都返回 default。

    Python 里 `float(10**400)` 会抛 OverflowError（JSON 的整数不设上限），
    记忆元数据/分数来自外部插件，不能假设它们在 float 范围内。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        num = float(value)
    except (OverflowError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _to_importance(value: Any):
    """把原始 importance 映射成 float：数字（含 bool，兼容旧行为）才转，溢出/非有限/其它类型 → None。"""
    if not isinstance(value, (int, float)):
        return None
    try:
        num = float(value)
    except (OverflowError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _score_of(item: Any):
    """取召回分数（LM 用 final_score；兼容 score）。拿不到就返回 None。"""
    for attr in ("final_score", "score", "relevance"):
        value = _as_float(_field(item, attr))
        if value is not None:
            return value
    return None


def _body_of(item: Any, bodies: dict | None = None) -> str:
    """取正文：优先用调用方预算好的映射，避免同一条重复解析。"""
    if bodies is not None:
        body = bodies.get(id(item))
        if isinstance(body, str):
            return body
    return _entry_body(item)


def _pick_relevant(results: list, min_score: float,
                   bodies: dict | None = None) -> list:
    """过滤低相关 + 去掉正文完全相同的重复条目。

    - 分数拿不到的条目按「通过」处理（不能因为对方没给分就丢掉记忆）
    - 全部低于门槛时，保底保留分数最高的一条（有记忆就至少注入一条）
    - `bodies` 可选：{id(结果对象): 正文}，避免重复调用 `_entry_body`
    """
    threshold = 0.0
    try:
        threshold = float(min_score or 0.0)
    except (TypeError, ValueError, OverflowError):
        threshold = 0.0
    if not results:
        return []

    kept: list = []
    for item in results:
        score = _score_of(item)
        if score is None or score >= threshold:
            kept.append(item)

    if not kept:
        # 保底：全都不达标，就留分数最高的那一条
        best = results[0]
        best_score = _score_of(best)
        for item in results[1:]:
            score = _score_of(item)
            if score is not None and (best_score is None or score > best_score):
                best, best_score = item, score
        kept = [best]

    # 正文完全相同的直接丢弃（LM 自己也有 MMR/合并，这里只兜底）
    seen: set[str] = set()
    unique: list = []
    for item in kept:
        key = re.sub(r"\s+", "", _body_of(item, bodies))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _to_entries(results: list, bodies: dict | None = None):
    """把记忆插件的结果映射成通用 `Entry`（这是适配层唯一需要懂对方字段的地方）。

    `bodies` 可选：{id(结果对象): 正文}；传入后不再重复调用 `_entry_body`。

    最小契约：**只要把正文映射到 `text` 就能用**，其余字段可选——
    缺时间/重要性/话题只是条首不显示；缺 `score` 时相关度门槛对该插件失效
    （全部当通过）、超预算压缩退化成平均水位；不会报错，只是注入块质量下降。

    对接新插件时额外注意两处类型：
    - 时间用 epoch 秒最稳（数字串、datetime 也能识别，毫秒/微秒会自动换算）；
      ISO 字符串、占位符或超出 2000–2100 的值会被当成"没有时间"，条首不显示时间、
      也不会出现 1970（换插件时最好在适配层转成 epoch）；
    - 重要性必须是 0–1 的数字（"0.8" 这种字符串/越界值同样只是不显示）。
    """
    out = []
    for item in results:
        try:
            meta = _field(item, "metadata") or {}
            importance = _field(meta, "importance")
            topics = _field(meta, "topics")
            if isinstance(topics, str):
                topics = [topics]
            elif not isinstance(topics, (list, tuple, set)):
                topics = []
            out.append(
                memory_block.Entry(
                    text=_body_of(item, bodies),
                    time=_field(meta, "create_time"),
                    importance=_to_importance(importance),
                    topics=[
                        t for t in (_as_text(x).strip() for x in topics) if t
                    ],
                    participants=_participants(meta),
                    score=_score_of(item),
                )
            )
        except Exception:
            # 单条映射失败只丢这一条，不影响同批其它记忆
            continue
    return out


def _participants(meta: Any) -> list[str]:
    """参与人：用平台 ID，不用昵称（昵称会重名/改名，ID 才能区分）。

    - 优先 `participant_identities[].sender_id`
    - 退回 `participants`，但只接受纯数字（那是 ID；文本一律当昵称忽略）
    - 跳过 bot 自己（`is_bot=True`）
    """
    ids: list[str] = []
    identities = _field(meta, "participant_identities")
    if isinstance(identities, list):
        for identity in identities:
            if identity is None:
                continue
            if _field(identity, "is_bot"):
                continue
            sender_id = _as_text(_field(identity, "sender_id")).strip()
            if sender_id:
                ids.append(sender_id)
    if not ids:
        people = _field(meta, "participants")
        if isinstance(people, (list, tuple, set)):
            for person in people:
                value = _as_text(person).strip()
                if value.isdigit():
                    ids.append(value)
    unique: list[str] = []
    for sender_id in ids:
        if sender_id not in unique:
            unique.append(sender_id)
    return unique[:MAX_PARTICIPANTS]


def _loaded_access_resolver():
    """找 LM 的 `resolve_event_memory_access`（作用域/人格解析）。

    只在**已经加载**的模块里按前缀找现成函数，不写 import 语句；
    找不到就返回 None，调用方退回会话级作用域。
    """
    for name, module in list(sys.modules.items()):
        if not name.startswith("astrbot_plugin_livingmemory"):
            continue
        fn = getattr(module, "resolve_event_memory_access", None)
        if callable(fn):
            return fn
    return None


class _EventShim:
    """用 umo 造一个最小 event，喂给 LM 的作用域解析。

    LM 取值走 `_event_value(event, "get_xxx", "attr")`：先找方法、再退回属性、
    异常吞掉。所以这里只需要提供几个方法，不需要真的 AstrMessageEvent。
    """

    def __init__(
        self,
        umo: str,
        platform: str,
        group_id: str | None = None,
        sender_id: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self.sender_name = ""
        self._platform = platform or "unknown"
        self._group = group_id
        self._sender = sender_id

    def get_platform_name(self) -> str:
        return self._platform

    def get_group_id(self) -> str | None:
        return self._group

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self.sender_name

    def get_message_type(self) -> str:
        return "GroupMessage" if self._group else "FriendMessage"


def _chain(obj: Any, *attrs: str):
    """安全地按属性链取值，任一段缺失就返回 None。"""
    for name in attrs:
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


def _is_group(umo: str) -> bool:
    return "GroupMessage" in (umo or "")


def _platform_of(ctx: Any, umo: str) -> str:
    """把 UMO 的平台前缀换成平台名（aiocqhttp 等）；拿不到就退回前缀。"""
    pid = (umo or "").split(":", 1)[0] or "unknown"
    manager = _chain(ctx, "platform_manager")
    getter = getattr(manager, "get_insts", None) if manager is not None else None
    if callable(getter):
        try:
            for inst in getter() or []:
                meta = inst.meta() if hasattr(inst, "meta") else None
                if getattr(meta, "id", "") == pid:
                    return str(getattr(meta, "name", "") or pid)
        except Exception:
            pass
    return pid


async def _resolve_access(plugin: Any, ctx: Any, umo: str, owner_id: str = ""):
    """用 umo 造替身，调 LM 自己的 `resolve_event_memory_access()`。

    拿不到（模块没加载 / 字段不匹配 / 抛异常）就返回 None，调用方退回默认行为
    （`session_id=umo, persona_id=None`）。
    """
    resolver = _loaded_access_resolver()
    if resolver is None:
        return None
    config = getattr(plugin, "config_manager", None)
    lm_ctx = getattr(plugin, "context", None)
    if config is None or lm_ctx is None:
        return None
    platform = _platform_of(ctx, umo)
    group_id = umo.rsplit(":", 1)[-1] if _is_group(umo) else None
    if not _is_group(umo):
        sender = owner_id or umo.rsplit(":", 1)[-1]
    else:
        if not owner_id:
            # 群里主动开口的"说话人"必须是鱼主；没配 owner 时硬造一个空身份，
            # LM 的 user 模式会把作用域拼成 `livingmemory:user:platform:` 这种怪值。
            # 宁可不解析，让调用方退回会话级 umo（最严格）。
            return None
        sender = owner_id
    shim = _EventShim(umo, platform, group_id, sender)
    try:
        return await resolver(config, lm_ctx, shim)
    except Exception as e:
        _warn_once(
            "access",
            "[鱼的心] 记忆作用域解析失败（已退回会话级 umo，同类只提示一次）: %s",
            e,
        )
        return None


class MemoryAdapter:

    name = ""
    plugin_names: tuple[str, ...] = ()
    #: recall 返回的文本是否已经自带标题/脚注（True 时调用方不再包一层）
    returns_formatted = False

    def matches(self, plugin: Any) -> bool:
        raise NotImplementedError

    async def recall(
        self, plugin: Any, ctx: Any, umo: str, query: str, limit: int,
        owner_id: str = "", min_score: float = 0.0,
        max_entries: int | None = None,
    ) -> str:
        raise NotImplementedError

    async def remember(
        self, plugin: Any, ctx: Any, umo: str, text: str, ts: float | None,
    ) -> bool:
        raise NotImplementedError


class LivingMemoryAdapter(MemoryAdapter):
    """LivingMemory（`astrbot_plugin_livingmemory`）2.x。

    - 召回：`initializer.memory_engine.search_memories(query, k, session_id, persona_id)`
    - 写回：`initializer.conversation_manager.add_message(...)`
    """

    name = "LivingMemory"
    plugin_names = ("astrbot_plugin_livingmemory",)
    returns_formatted = True

    @staticmethod
    def _engine(plugin: Any):
        return _chain(plugin, "initializer", "memory_engine") or _chain(
            plugin, "memory_engine"
        )

    @staticmethod
    def _conversations(plugin: Any):
        return _chain(plugin, "initializer", "conversation_manager") or _chain(
            plugin, "conversation_manager"
        )

    def matches(self, plugin: Any) -> bool:
        engine = self._engine(plugin)
        conversations = self._conversations(plugin)
        return callable(getattr(engine, "search_memories", None)) and callable(
            getattr(conversations, "add_message", None)
        )

    async def recall(
        self,
        plugin: Any,
        ctx: Any,
        umo: str,
        query: str,
        limit: int,
        owner_id: str = "",
        min_score: float = 0.0,
        max_entries: int | None = None,
    ) -> str:
        """召回并拼块。任何异常都吞掉返回空串——绝不阻塞主动消息发出。

        `limit` 是召回候选数；`max_entries` 是注入条数上限（None = 跟随 limit）。
        """
        try:
            return await self._recall(
                plugin, ctx, umo, query, limit, owner_id, min_score, max_entries
            )
        except Exception as e:
            _warn_once(
                "recall",
                "[鱼的心] 记忆召回失败（已降级为不注入，同类只提示一次）: %s",
                e,
            )
            logger.debug("[鱼的心] 记忆召回失败（已降级为不注入）: %s", e)
            return ""

    async def _recall(
        self,
        plugin: Any,
        ctx: Any,
        umo: str,
        query: str,
        limit: int,
        owner_id: str,
        min_score: float,
        max_entries: int | None,
    ) -> str:
        engine = self._engine(plugin)
        if engine is None or not query:
            return ""
        access = await _resolve_access(plugin, ctx, umo, owner_id)
        allowed = _safe_attr(access, "allowed", True) if access is not None else True
        if access is not None and not allowed:
            # 尊重 LM 自己的白名单/黑名单：它不让这段记忆参与，我们就不注。
            # 这条用 info：用户开了访问控制却发现"回复有记忆、主动没有"时能查到原因。
            logger.info(
                "[鱼的心] 记忆插件 %s 的访问控制不允许该会话（%s），本次主动不注入记忆",
                self.name,
                umo,
            )
            return ""
        scope = (_safe_attr(access, "scope", None) or umo) if access else umo
        persona = _safe_attr(access, "recall_persona_id", None) if access else None
        extra_raw = _safe_attr(access, "extra_read_scopes", []) if access else []
        try:
            extras = list(extra_raw or [])
        except Exception:
            extras = []
        results = await self._search(engine, query, limit, scope, persona, extras)
        bodies: dict = {}
        kept: list = []
        for item in results or []:
            body = _entry_body(item)
            if body:
                bodies[id(item)] = body
                kept.append(item)
        results = kept
        if not results:
            logger.debug("[鱼的心] 记忆召回为空（scope=%s）", scope)
            return ""
        scores = [s for s in (_score_of(r) for r in results) if s is not None]
        picked = _pick_relevant(results, min_score, bodies)
        threshold = _as_float(min_score, 0.0)
        logger.debug(
            "[鱼的心] 记忆召回：候选 %d 条（分数 %s~%s），过门槛 %d 条（门槛 %.2f）",
            len(results),
            f"{min(scores):.2f}" if scores else "?",
            f"{max(scores):.2f}" if scores else "?",
            len(picked),
            threshold,
        )
        if scores and max(scores) < threshold:
            # 整批都不够相关：这是唯一会"保底注入一条"的情况，debug 留痕即可
            logger.debug(
                "[鱼的心] %d 条记忆全部低于相关度门槛（最高 %.2f < %.2f），保底注入最高分那条",
                len(results),
                max(scores),
                threshold,
            )
        if not picked:
            return ""
        if memory_block is None:
            return ""
        # 映射成通用 Entry，排版交给 memory_block（水位法分配 + 抽句截断）
        cap = max_entries if max_entries is not None else limit
        block = memory_block.build(
            _to_entries(picked, bodies),
            query,
            with_people=_is_group(umo),
            max_entries=max(1, min(int(cap), memory_block.MAX_ENTRIES)),
        )
        logger.debug(
            "[鱼的心] 记忆注入：%d 条 → %d 字", len(picked), len(block)
        )
        return block

    @staticmethod
    async def _search(engine: Any, query: str, limit: int, scope: str,
                      persona: Any, extras: list[str]) -> list:
        """调 search_memories；按签名决定要不要带 `extra_scopes`。

        不用「TypeError 就重试」——那会把 search 内部真正的 TypeError 也吞掉，
        变成一次白跑的双倍检索（还会吃掉超时预算）。
        """
        kwargs = {
            "query": query,
            "k": limit,
            "session_id": scope or None,
            "persona_id": persona,
        }
        params = None
        try:
            params = inspect.signature(engine.search_memories).parameters
        except (TypeError, ValueError):
            params = None
        accepts_extras = bool(extras) and (
            params is None
            or "extra_scopes" in params
            or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        )
        if accepts_extras:
            if params is not None:
                return await engine.search_memories(extra_scopes=extras, **kwargs)
            # 拿不到签名（C 实现/装饰器）：只在这种情况兜底重试一次
            try:
                return await engine.search_memories(extra_scopes=extras, **kwargs)
            except TypeError:
                pass
        return await engine.search_memories(**kwargs)

    async def remember(
        self,
        plugin: Any,
        ctx: Any,
        umo: str,
        text: str,
        ts: float | None,
    ) -> bool:
        """写回一条记忆。任何异常都吞掉返回 False——写回失败不影响发送。"""
        try:
            return await self._remember(plugin, ctx, umo, text, ts)
        except Exception as e:
            _warn_once(
                "remember",
                "[鱼的心] 记忆写回失败（已忽略，同类只提示一次）: %s",
                e,
            )
            logger.debug("[鱼的心] 记忆写回失败（已忽略）: %s", e)
            return False

    async def _remember(
        self,
        plugin: Any,
        ctx: Any,
        umo: str,
        text: str,
        ts: float | None,
    ) -> bool:
        conversations = self._conversations(plugin)
        text = (text or "").strip()
        if conversations is None or not umo or not text:
            return False
        platform = _platform_of(ctx, umo)
        sender_id, sender_name = await self._bot_identity(conversations, umo, platform)
        # 注意：这里刻意用原始 UMO，而不是 access.scope。
        # LM 的总结流程是「按 UMO 取会话窗口 → 再按当时的 scope 落记忆」，
        # 如果把会话行写进 livingmemory:global / user:xxx 这类 scope，
        # get_summary_window(umo) 就再也读不到它，主动消息会永远进不了记忆。
        kwargs = {
            "session_id": umo,
            "role": "assistant",
            "content": text,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "group_id": umo if _is_group(umo) else None,
            "platform": platform,
            "is_bot_message": True,
            "timestamp": ts if ts else None,
        }
        # 对方版本可能少几个参数（或将来多几个）：按签名过滤，别让一个
        # 无关参数把整条写回打死。
        try:
            params = inspect.signature(conversations.add_message).parameters
        except (TypeError, ValueError):
            params = None
        if params is not None and not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        await conversations.add_message(**kwargs)
        return True

    @staticmethod
    async def _bot_identity(conversations: Any, umo: str, platform: str) -> tuple[str, str]:
        """从最近的历史里找 bot 自己发过的行，复用它的 sender_id / sender_name。"""
        getter = getattr(conversations, "get_messages", None)
        if callable(getter):
            try:
                rows = await getter(umo, limit=20)
                for row in reversed(rows or []):
                    if getattr(row, "role", "") != "assistant":
                        continue
                    sender_id = str(getattr(row, "sender_id", "") or "")
                    if sender_id and sender_id != umo and ":" not in sender_id:
                        name = str(getattr(row, "sender_name", "") or sender_id)
                        return sender_id, name
            except Exception:
                pass
        return f"bot:{platform}", "Bot"


ADAPTERS: tuple[MemoryAdapter, ...] = (LivingMemoryAdapter(),)


def find_for_plugin(plugin: Any):
    """返回第一个匹配的适配器；没有就返回 None。"""
    for adapter in ADAPTERS:
        try:
            if adapter.matches(plugin):
                return adapter
        except Exception:
            continue
    return None


def candidate_names() -> list[str]:
    """所有适配器支持的插件名，用于自动识别。"""
    names: list[str] = []
    for adapter in ADAPTERS:
        for name in adapter.plugin_names:
            if name not in names:
                names.append(name)
    return names
