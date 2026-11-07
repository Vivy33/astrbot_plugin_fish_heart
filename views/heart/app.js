/* 鱼的心 · WebUI 逻辑
   数据来自插件注册的 /fish_heart/state（经 dashboard bridge 代理） */

(() => {
  "use strict";

  let VIEW = null;

  /* endpoint 是"相对于插件"的后缀：
     前端会拼成 /api/v1/plugins/extensions/{pluginName}/{endpoint}，
     所以这里只能写 "state" / "tick"，不能写完整路径。 */
  const API_STATE = "state";
  const API_TICK = "tick";

  /* AstrBot 把桥接脚本注入在 </body> 之前，也就是本文件之后才执行，
     所以这里要等它出现，不能一上来就断定"没有桥接"。 */
  async function waitForBridge(timeout = 2500) {
    const started = Date.now();
    for (;;) {
      const v = window.AstrBotPluginView || window.AstrBotPluginPage || null;
      if (v) return v;
      if (Date.now() - started >= timeout) return null;
      await new Promise((r) => setTimeout(r, 40));
    }
  }

  const MODES = {
    calm: { cn: "日常", tag: "没在生气", color: "#4fd1c5" },
    wronged: { cn: "委屈", tag: "委屈", color: "#f6ad55" },
    angry: { cn: "生气", tag: "生气", color: "#ff6b6b" },
    afraid: { cn: "害怕", tag: "害怕", color: "#a78bfa" },
    shut: { cn: "回避", tag: "回避", color: "#f17a8a" },
  };

  const DRIVE_ORDER = [
    "attachment", "curiosity", "reflection", "duty", "social",
    "fatigue", "intimacy", "stress", "joy",
  ];

  const KIND_CN = {
    praise: "想被夸", miss: "想你", hug: "想抱抱", talk: "想找你说话",
    ask_day: "问你今天", task: "想找点事做", quiet: "想安静会儿",
    dwell: "等你回话", company: "想让你陪", deep: "走心",
  };

  /** 展示层统一把"他"换成"你"（避开"其他"这类词），引擎里的原文不动 */
  function toYou(s) {
    return String(s || "").replace(/(?<!其)他/g, "你");
  }

  const $ = (id) => document.getElementById(id);

  /* ---------------- 工具 ---------------- */

  const clamp100 = (v) => Math.max(0, Math.min(100, Number(v) || 0));

  function fmtClock(ts) {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function fmtAgo(ts) {
    if (!ts) return "从未";
    const diff = Date.now() / 1000 - Number(ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
    return fmtClock(ts);
  }

  function fmtSpan(sec) {
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} 分钟`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h} 小时 ${rm} 分` : `${h} 小时`;
  }

  function svg(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  /* ---------------- 雷达图 ---------------- */

  /** 该维度的"满格线"：依恋/亲密实际到不了 100，按 85 画满，
   *  否则那两条腿永远是短的、看着像坏了。其余维度仍是 100。 */
  function fillRatio(drives, k) {
    const max = Number((drives._max && drives._max[k]) || 100) || 100;
    return Math.min(1, clamp100(drives[k]) / max);
  }

  function drawRadar(drives) {
    const root = $("radar");
    root.replaceChildren();

    const cx = 170, cy = 170, R = 108;
    const n = DRIVE_ORDER.length;
    const angle = (i) => (-Math.PI / 2) + (i * 2 * Math.PI) / n;

    // 参考网格
    [0.25, 0.5, 0.75, 1].forEach((r) => {
      const pts = DRIVE_ORDER.map((_, i) => {
        const a = angle(i);
        return `${cx + Math.cos(a) * R * r},${cy + Math.sin(a) * R * r}`;
      });
      root.appendChild(svg("polygon", {
        points: pts.join(" "),
        class: "grid-line",
        opacity: r === 1 ? 1 : 0.55,
      }));
    });

    // 轴线
    DRIVE_ORDER.forEach((_, i) => {
      const a = angle(i);
      root.appendChild(svg("line", {
        x1: cx, y1: cy,
        x2: cx + Math.cos(a) * R,
        y2: cy + Math.sin(a) * R,
        class: "axis",
      }));
    });

    // 数据面
    const poly = DRIVE_ORDER.map((k, i) => {
      const a = angle(i);
      const v = fillRatio(drives, k);
      return `${cx + Math.cos(a) * R * v},${cy + Math.sin(a) * R * v}`;
    }).join(" ");
    root.appendChild(svg("polygon", { points: poly, class: "area" }));

    // 顶点
    DRIVE_ORDER.forEach((k, i) => {
      const a = angle(i);
      const v = fillRatio(drives, k);
      root.appendChild(svg("circle", {
        cx: cx + Math.cos(a) * R * v,
        cy: cy + Math.sin(a) * R * v,
        r: 3,
        class: "dot",
      }));
    });

    // 标签
    DRIVE_ORDER.forEach((k, i) => {
      const a = angle(i);
      const lx = cx + Math.cos(a) * (R + 26);
      const ly = cy + Math.sin(a) * (R + 26);
      const t = svg("text", {
        x: lx, y: ly,
        class: fillRatio(drives, k) >= 0.8 ? "label hot" : "label",
        "text-anchor": Math.abs(Math.cos(a)) < 0.2 ? "middle" : (Math.cos(a) > 0 ? "start" : "end"),
        "dominant-baseline": "middle",
      });
      t.textContent = (drives._cn && drives._cn[k]) || k;
      root.appendChild(t);
    });
  }

  /* ---------------- 卡片渲染 ---------------- */

  function renderVitals(d) {
    const rings = [
      { k: "attachment", cn: "依恋" },
      { k: "intimacy", cn: "亲密" },
      { k: "__exp", cn: "期待" },
      { k: "__blame", cn: "责怪" },
    ];
    const box = $("rings");
    box.replaceChildren();
    rings.forEach(({ k, cn }) => {
      const val = k === "__exp" ? d.expectation : k === "__blame" ? d.blame : d.drives[k];
      const dm = k in (d.display_max || {}) ? Number(d.display_max[k]) : 100;
      const pct = Math.min(100, (clamp100(val) / dm) * 100);
      // 里程碑刻度线：依恋/亲密在「黏人」门槛处标一道，满了才叫满格
      const tickVal = Number((d.display_tick && d.display_tick[k]) || 0);
      const wrap = document.createElement("div");
      wrap.className = "ring";
      wrap.title = dm === 100
        ? `${cn}：${Math.round(clamp100(val))}`
        : `${cn}：真实 ${Math.round(clamp100(val))}（按 ${dm} 归一成 ${Math.round(pct)}）`
          + (tickVal > 0 ? `｜刻度线 = ${tickVal}（黏人门槛）` : "");
      const dial = document.createElement("div");
      dial.className = "dial";
      if (tickVal > 0) {
        const tick = document.createElement("i");
        tick.className = "tick";
        dial.appendChild(tick);
        const tpct = Math.min(100, (tickVal / dm) * 100);
        requestAnimationFrame(() => tick.style.setProperty("--t", tpct));
      }
      const b = document.createElement("b");
      // 归一化：依恋/亲密按 display_max 映射到 0-100 显示
      b.textContent = Math.round(pct);
      dial.appendChild(b);
      const cap = document.createElement("div");
      cap.className = "cap";
      cap.textContent = cn;
      wrap.append(dial, cap);
      box.appendChild(wrap);
      requestAnimationFrame(() => dial.style.setProperty("--p", pct));
    });

    const meters = [
      { k: "stress", cn: "压力" },
      { k: "joy", cn: "喜悦" },
      { k: "social", cn: "社交" },
      { k: "fatigue", cn: "疲惫" },
      { k: "curiosity", cn: "好奇" },
      { k: "reflection", cn: "反刍" },
      { k: "duty", cn: "责任" },
    ];
    const mbox = $("meters");
    mbox.replaceChildren();
    meters.forEach(({ k, cn }) => {
      const v = clamp100(d.drives[k]);
      const dm = k in (d.display_max || {}) ? Number(d.display_max[k]) : 100;
      const pct = Math.min(100, (v / dm) * 100);
      const row = document.createElement("div");
      row.className = "meter";
      row.innerHTML = `<span class="name">${cn}</span><span class="bar"><i></i></span><span class="val">${Math.round(pct)}</span>`;
      row.title = dm === 100 ? `${cn}：${Math.round(v)}` : `${cn}：真实 ${Math.round(v)}（按 ${dm} 归一）`;
      mbox.appendChild(row);
      requestAnimationFrame(() => { row.querySelector("i").style.width = `${pct}%`; });
    });
  }

  /** 念头卡片顶部的"最近完成 / 最近放下"小条 */
  function renderLastEvent(id, data, label, tagText) {
    const el = $(id);
    if (!el) return;  // 旧页面缓存里可能还没有这一行
    const d = data || {};
    if (!d.mood) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    const kind = d.kind ? (KIND_CN[d.kind] || d.kind) : "";
    el.textContent = `${label}：${kind ? kind + " · " : ""}${toYou(d.mood)}（${fmtAgo(d.ts)}）`;
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = tagText;
    el.appendChild(tag);
  }

  /** 最近完成：2 小时内可能有多条，逐条列出 */
  function renderRewards(list) {
    const box = $("lastRewardList");
    if (!box) return;
    box.replaceChildren();
    (list || []).forEach((r) => {
      if (!r || !r.mood) return;
      const kind = r.kind ? (KIND_CN[r.kind] || r.kind) : "";
      const self = r.src === "self";
      const p = document.createElement("p");
      p.className = self ? "last-reward self" : "last-reward";
      p.textContent = `${self ? "自己缓过来" : "最近完成"}：${kind ? kind + " · " : ""}${toYou(r.mood)}（${fmtAgo(r.ts)}）`;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = self ? "自愈" : "回味中";
      p.appendChild(tag);
      box.appendChild(p);
    });
  }

  function renderThoughts(list, recentRewards, cooldownLeft, lastLetGo) {
    const ul = $("thoughtList");
    ul.replaceChildren();
    const cd = Number(cooldownLeft) || 0;
    $("thoughtCount").textContent =
      `${list.length} 条` + (cd > 0 ? ` · 下次可完成 ${fmtSpan(cd)}` : "");
    $("thoughtEmpty").hidden = list.length > 0;

    renderRewards(recentRewards);
    renderLastEvent("lastLetGo", lastLetGo, "最近放下", "放下");
    const lastCard = $("lastCard");
    if (lastCard) {
      const hasLast = !!((recentRewards && recentRewards.length)
        || (lastLetGo && lastLetGo.mood));
      lastCard.hidden = !hasLast;
    }

    const now = Date.now() / 1000;
    list.forEach((t) => {
      const li = document.createElement("li");
      li.className = "thought";
      if (!t.pending) li.classList.add("done");

      const life = Math.max(1, Number(t.ttl_total) || 1);
      const remain = Math.max(0, Number(t.expires) - now);
      const pct = Math.min(100, (remain / life) * 100);
      const kindCn = toYou(t.kind_cn || KIND_CN[t.kind] || t.kind);
      const how = toYou(t.how || "");
      const moodText = t.pending
        ? (how ? `还没被满足 · ${how}` : "还没被满足")
        : toYou(t.mood || "已经放下");

      li.innerHTML = `
        <div class="thought-top">
          <span class="thought-kind">${kindCn}</span>
          <span class="thought-text"></span>
        </div>
        <div class="thought-meta">
          <span>${moodText}</span>
          <span class="ttl"><i></i></span>
        </div>`;
      li.querySelector(".thought-text").textContent = toYou(t.text || "");
      li.querySelector(".ttl i").style.width = `${pct}%`;
      ul.appendChild(li);
    });
  }

  function renderProactive(d) {
    const cfg = d.config || {};
    const cooldown = Number(cfg.cooldown_hours || 0) * 3600;
    const last = Number(d.last_proactive || 0);
    const nowSec = Number(d.now) || Date.now() / 1000;
    const remain = last ? Math.max(0, last + cooldown - nowSec) : 0;
    const proactiveOn = !!d.enabled && cfg.proactive_enabled !== false;
    const limit = Math.max(0, Number(cfg.daily_limit ?? 0) || 0);
    const count = Number(d.proactive_count || 0);

    $("pCount").textContent = `${count}/${cfg.daily_limit ?? "?"}`;
    $("pCooldown").textContent = !proactiveOn
      ? "已关闭"
      : (d.sleep?.asleep
        ? "睡眠中"
        : (d.mode === "shut"
          ? "回避中"
          : (!limit
            ? "无额度"
            : (count >= limit
              ? "今日已满"
              : (last ? (remain > 0 ? fmtSpan(remain) : "可开口") : "可开口")))));
    $("pLast").textContent = fmtAgo(last);
    $("pWaiting").textContent = d.awaiting_reply ? "是" : "否";

    const mood = moodText(d);
    const tag = d.mode === "calm" && !d.mind_knot
      ? ""
      : ` · ${d.mode_tag_cn || MODES[d.mode]?.tag || ""}`;
    $("proactiveHint").textContent =
      `${mood}${tag} · ${!d.enabled ? "已暂停" : (!proactiveOn ? "主动说话已关" : (!limit ? "无主动额度" : (count >= limit ? "今日已满" : "心跳中")))}`;

    const chips = [
      ["冷却", `${cfg.cooldown_hours}h`],
      ["每日上限", `${cfg.daily_limit}`],
      ["刚聊过不打扰", `${cfg.min_idle_minutes}min`],
      ["作息", d.sleep?.enabled
        ? (d.sleep.asleep
          ? `睡着（${d.sleep.wake} 起）`
          : `${d.sleep.bedtime}-${d.sleep.wake}${d.sleep.stay_up ? " 熬夜" : ""}`)
        : "关"],
      ["群友模式", cfg.group_everyone
        ? `开 主×${cfg.group_owner_scale ?? 1} 群×${cfg.group_scale ?? 0.5} 负 群${cfg.group_negative_scale ?? 0.35}/主${cfg.group_owner_negative_scale ?? 0.5}`
        : "关"],
      ["念头", d.thought_enabled ? "开" : "关"],
      ["入池门槛", `${cfg.thought_min_weight ?? 0.2}`],
      ["念头加成", `+${cfg.thought_push ?? 0.15}`],
      ["回避不回", `私 ${d.silent_streak || 0} · 群 ${d.silent_streak_group || 0}/${cfg.silent_cap || 7}`],
      ["今日收益", `喜 ${Math.round(d.daily_gain?.joy || 0)}/${cfg.daily_cap?.joy || 0}`],
    ];
    const box = $("chips");
    box.replaceChildren();
    chips.forEach(([k, v]) => {
      const s = document.createElement("span");
      s.className = "chip";
      s.innerHTML = `${k} <b></b>`;
      s.querySelector("b").textContent = v;
      box.appendChild(s);
    });

    // 她"现在为什么安静 / 能不能开口"——把三个闸门直白讲出来
    let readiness;
    let readinessHidden = false; // 顶部四块已经写了同一件事时，这句不再重复
    if (!d.enabled) {
      readiness = "总开关已关闭，她不会主动开口。";
    } else if (d.mode === "shut") {
      const pct = Math.round(Number(cfg.silent_next_p ?? 0) * 100);
      const cap = Number(cfg.silent_cap || 7);
      readiness = cfg.shut_enabled === false
        ? "她在回避：主动说话被冻结，但「已读不回」已关闭，你说话她照常回。"
        : `她在回避：主动说话被冻结；私聊 ${d.silent_streak || 0}/${cap} 条（这一条回 ${pct}%）、群聊 ${d.silent_streak_group || 0}/${cap} 条不回。`;
    } else if (d.sleep?.asleep) {
      readiness = `她睡着了（${d.sleep.bedtime}-${d.sleep.wake}${d.sleep.stay_up ? "，今晚熬夜" : ""}），${d.sleep.wake} 之后才可能开口。`;
    } else if (!proactiveOn) {
      readiness = "主动说话已关闭；你说话她会回，但不会主动开口。";
    } else if (d.mode === "angry") {
      const who = topAngerTarget(d.anger_targets);
      const base = `她在生气${who ? `（对 ${who.label}）` : ""}：还会回你`;
      if (!limit) {
        readiness = `${base}；每日主动次数是 0，她不会主动开口；语气会冷，先道歉/说清楚比只哄有用。`;
      } else if (count >= limit) {
        readiness = `${base}；今天的 ${limit} 次主动已经用完了，但回复时仍会冷；先道歉/说清楚比只哄有用。`;
      } else if (remain > 0) {
        readiness = `${base}；主动说话冷却中，还要等 ${fmtSpan(remain)}；语气会冷，先道歉/说清楚比只哄有用。`;
      } else {
        readiness = `${base}，也可能主动开口；语气会冷，先道歉/说清楚比只哄有用。`;
      }
    } else if (!limit) {
      readiness = "每日主动次数是 0，她不会主动开口。";
    } else if (count >= limit) {
      readiness = `今天的 ${limit} 次已经用完了，明天再来。`;
    } else if (remain > 0) {
      // 「冷却剩余」那一块已经在倒计时了，这里再说一遍就是重复
      readiness = `冷却中，还要等 ${fmtSpan(remain)}。`;
      readinessHidden = true;
    } else {
      readiness = "随时可能开口 —— 取决于她的心情，和那一次概率。";
    }
    const readinessBox = $("readiness");
    readinessBox.textContent = readiness;
    readinessBox.hidden = readinessHidden;
  }

  /** 只在展示层过滤：去掉语气标记（&&soft&&）与媒体占位（[图片]），原始数据不动 */
  function stripMarks(s) {
    return String(s || "")
      .replace(/&&[^\s&]{1,16}&&/g, " ")
      .replace(/\[(?:图片|视频|语音|表情|Image[^\]]*|CQ:image[^\]]*)\]/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  /** 把「最近的变化」渲染成一行行：事件名 + 主要增量 + 时间 */
  const DIM_CN = {
    attachment: "依恋", curiosity: "好奇", reflection: "反刍", duty: "责任",
    social: "社交", fatigue: "疲惫", intimacy: "亲密", stress: "压力",
    joy: "喜悦", blame: "责怪", expectation: "期待",
  };

  function renderDesire(d) {
    const card = $("desireCard");
    const on = !!d.desire_awakened && Number.isFinite(Number(d.desire));
    card.hidden = !on;
    if (!on) return;

    const value = clamp100(d.desire);
    $("desireValue").textContent = Math.round(value);
    $("desireStage").textContent = d.desire_stage || "";
    $("desireFill").style.width = `${value}%`;
    const status = [];
    if (d.desire_adult_status) status.push(d.desire_adult_status);
    if (d.desire_bond_full === false) status.push("关系未满，正在退潮");
    if (d.desire_satiated) status.push("饱和中 · 收益减半");
    $("desireStatus").textContent = status.join(" · ");
    $("desireHint").textContent = "已解锁";

    const ul = $("desireLog");
    ul.replaceChildren();
    const rows = Array.isArray(d.desire_log) ? d.desire_log.slice(-5) : [];
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "还没有记录到涨落。";
      ul.appendChild(li);
      return;
    }
    [...rows].reverse().forEach((row) => {
      const li = document.createElement("li");
      const why = document.createElement("span");
      why.textContent = row.why || "变化";
      const delta = document.createElement("b");
      const n = Number(row.d || 0);
      delta.className = n >= 0 ? "up" : "down";
      delta.textContent = `${n >= 0 ? "+" : ""}${n.toFixed(1)} · ${fmtAgo(row.ts)}`;
      li.append(why, delta);
      ul.appendChild(li);
    });
  }

  function renderEvents(list) {
    const ul = $("eventList");
    ul.replaceChildren();
    $("eventEmpty").hidden = list.length > 0;
    [...list].reverse().forEach((e) => {
      const parts = Object.entries(e.d || {})
        .map(([k, v]) => `${DIM_CN[k] || k} ${v > 0 ? "+" : ""}${v}`)
        .join(" · ");
      const li = document.createElement("li");
      li.innerHTML = `<span class="ev-name"></span><span class="ev-delta"></span><span class="ev-time"></span>`;
      li.querySelector(".ev-name").textContent = e.cn || e.n || "";
      li.querySelector(".ev-delta").textContent = parts;
      li.querySelector(".ev-time").textContent = e.ts ? fmtAgo(e.ts) : "";
      ul.appendChild(li);
    });
  }
  function renderRecent(list, fromHistory) {
    const ol = $("recent");
    ol.replaceChildren();
    $("recentHint").textContent = fromHistory ? "来自对话历史" : "只记私聊";

    let shown = 0;
    list.forEach((m) => {
      const text = stripMarks(m.t);
      if (!text) return; // 纯图片 / 纯标记的一行不显示
      const li = document.createElement("li");
      li.dataset.who = m.r === "bot" ? "bot" : "user";
      // 对话历史里没有时间戳，用"此前"占位，避免显示 1970
      const when = m.ts ? fmtAgo(m.ts) : "此前";
      li.innerHTML = `<span class="who">${m.r === "bot" ? "她" : "你"} · ${when}</span><span class="what"></span>`;
      li.querySelector(".what").textContent = text;
      ol.appendChild(li);
      shown += 1;
    });
    $("recentEmpty").hidden = shown > 0;
  }

  function renderInject(map) {
    const ul = $("injectList");
    ul.replaceChildren();
    const entries = Object.entries(map || {});
    $("injectEmpty").hidden = entries.length > 0;
    entries
      .sort((a, b) => (b[1].ts || 0) - (a[1].ts || 0))
      .forEach(([umo, info]) => {
        const parts = String(umo).split(":");
        const label = parts.length >= 3 ? `${parts[1] === "GroupMessage" ? "群" : "私聊"} · ${parts[2]}` : umo;
        const li = document.createElement("li");
        li.innerHTML = `<span class="umo"></span><span class="when"></span>`;
        li.querySelector(".umo").textContent = label;
        li.querySelector(".when").textContent = fmtAgo(info.ts);
        ul.appendChild(li);
      });
  }

  function topAngerTarget(list) {
    return (Array.isArray(list) ? list : [])
      .filter((t) => t && t.label)
      .sort((a, b) => Number(b.blame || 0) - Number(a.blame || 0))[0] || null;
  }

  /** 主徽章文字：日常且没有明显心情时写“日常”，别写“平常/平静”绕人。 */
  function moodText(d) {
    const mood = d.mood_cn || "平常";
    if (mood === "平常" && d.mode === "calm") return "日常";
    return mood;
  }

  function applyMode(mode, knot, targets, modeTag) {
    const m = MODES[mode] || MODES.calm;
    document.documentElement.style.setProperty("--mode", m.color);
    document.documentElement.style.setProperty("--mode-soft", hexToSoft(m.color));
    // 第一枚 badge 显示“心情”（黏人/开心/累了…），颜色仍按责怪档走；
    // 第二枚 badge 显示“责怪档”（日常=没在生气/委屈/生气…）。
    // 心结：责怪被夹在 40，档位会永远停在「委屈」。不标出来，
    // 用户只会看到一个找不到出处的委屈徽章。
    const stuck = !!knot && mode === "wronged";
    const who = mode === "angry" ? topAngerTarget(targets) : null;
    const target = who ? `·对${String(who.label).slice(0, 8)}` : "";
    const tag = String(modeTag || m.tag || m.cn || "").replace(/·旧账$/, "");
    const tagText = stuck ? `${tag}·旧账` : `${tag}${target}`;
    const tagBadge = $("moodBadge");
    // 日常不显示“没在生气”第二枚：主徽章已经说清心情，下面再写一遍
    // 只会变成「黏人 · 没在生气 · 期待 0 · 责怪 0」。委屈/生气/害怕/回避
    // 才需要这枚副标签。
    tagBadge.style.display = (mode === "calm" && !stuck && !who) ? "none" : "";
    tagBadge.textContent = tagText;
    tagBadge.title = stuck
      ? "责怪已经停在 40（心结），不会再自己往下掉：哄/道歉，或 /心 calm 才能解"
      : who
        ? `她在生 ${who.label} 的气（责怪贡献 ${Math.round(who.blame || 0)}）；对其他人只是委屈/难受`
        : `责怪档：${m.cn}${mode === "calm" ? "（没在生气）" : ""}`;
    $("modeBadge").dataset.mode = mode;
  }

  /** 责怪那一行：数值 + 心结说明 + 最近一次责怪的原句（她"说不出来由"时就靠它）。
   *  返回空串表示这一行不该出现——和「倾向」合并在同一个虚线框里，不再单独占一块。 */
  function blameInfo(d) {
    const blame = Number(d.blame || 0);
    if (!(blame > 0)) return "";
    const floor = Number(d.blame_floor || 0);
    // 和 /心 status 用同一套拆法：40 是心结底，超出的才是新账。
    const parts = [floor > 0 && blame >= floor + 0.5
      ? `${Math.round(blame)}/100（心结 ${Math.round(floor)} + 另 ${Math.round(blame - floor)}）`
      : `${Math.round(blame)}/100`];
    const fade = Number(d.blame_fade_per_6h || 8);
    if (d.mind_knot) {
      parts.push("心结：不哄不会掉（/心 calm 可解）");
    } else if (d.next_fade_ts) {
      const left = Math.max(0, Number(d.next_fade_ts) - Number(d.now || 0));
      parts.push(left > 0 ? `${fmtSpan(left)}后 −${fade}` : `下一次心跳 −${fade}`);
    }
    const log = Array.isArray(d.blame_log) ? d.blame_log : [];
    const last = log[log.length - 1];
    if (last && last.t) {
      const text = String(last.t);
      let where = "私聊";
      if (last.g) {
        // 0.3.6 起记了"谁说的"：鱼主 / 群友昵称；老状态没有 o/w，就只写群里。
        if (last.o === false) where = last.w ? `群里·${last.w}` : "群里·群友";
        else if (last.o === true) where = "群里·鱼主";
        else where = "群里";
      }
      const shown = text.length > 24 ? `${text.slice(0, 24)}…` : text;
      parts.push(`最近：${fmtClock(last.ts)} ${where}「${shown}」`);
    }
    return parts.join(" · ");
  }

  /** 九维图例：把九个轴的名字和当前值列出来，避免"责任/责怪"这种误会 */
  function renderRadarLegend(d) {
    const box = $("radarLegend");
    box.replaceChildren();
    DRIVE_ORDER.forEach((k) => {
      const v = Number((d.drives || {})[k]) || 0;
      const dm = Number((d.display_max || {})[k]) || 100;
      const pct = Math.round(Math.min(100, (v / dm) * 100));
      const s = document.createElement("span");
      s.innerHTML = `<i></i><em></em> <b>${pct}</b>`;
      s.querySelector("em").textContent =
        (d.drives_cn && d.drives_cn[k]) || k;
      box.appendChild(s);
    });
  }

  function hexToSoft(hex) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, 0.14)`;
  }

  /* ---------------- 主流程 ---------------- */

  /* 单块渲染失败不该把整页拖黑：每张卡各自兜底，剩下的照常显示 */
  function guard(label, fn) {
    try {
      fn();
    } catch (e) {
      console.error(`[鱼的心] ${label} 渲染失败`, e);
    }
  }

  function render(d) {
    applyMode(d.mode, d.mind_knot, d.anger_targets, d.mode_tag_cn);
    document.body.dataset.mode = d.mode;

    $("modeBadge").textContent = moodText(d);
    $("modeBadge").title =
      `她的心情：${moodText(d)}；外圈颜色按责怪档：${MODES[d.mode]?.cn || d.mode}`;
    const ver = d.plugin_version ? `v${d.plugin_version} · ` : "";
    // 心情和责怪档已经在两枚徽章上；副标题只留数值，避免同一句里再说一遍。
    $("subtitle").textContent =
      `${ver}期待 ${Math.round(d.expectation)} · 责怪 ${Math.round(d.blame)}`;
    $("tickHint").textContent = `心跳 ${fmtAgo(d.last_tick)}`;

    const hint = d.hint || "";
    let blameText = "";
    guard("责怪行", () => { blameText = blameInfo(d); });
    $("tendencyRow").hidden = !hint;
    $("tendencyText").textContent = hint;
    $("blameLine").hidden = !blameText;
    $("blameText").textContent = blameText;
    $("tendency").hidden = !hint && !blameText;

    const drives = { ...d.drives, _cn: d.drives_cn || {}, _max: d.display_max || {} };
    guard("雷达", () => drawRadar(drives));
    guard("图例", () => renderRadarLegend(d));
    guard("此刻", () => renderVitals(d));
    guard("隐藏的欲", () => renderDesire(d));
    guard("念头", () => {
      renderThoughts(d.thoughts || [], d.recent_rewards || [],
        d.settle_cooldown_left, d.last_let_go);
    });
    guard("主动说话", () => renderProactive(d));
    // recent 全是单边（历史遗留）时，改用 AstrBot 的真实对话历史
    const recent = Array.isArray(d.recent) ? d.recent : [];
    const history = Array.isArray(d.history) ? d.history : [];
    const hasBot = recent.some((m) => m.r === "bot");
    guard("最近说过的话", () => {
      if (hasBot || !history.length) {
        renderRecent(recent.slice(-12), false);
      } else {
        renderRecent(history.slice(-12), true);
      }
    });
    guard("最近的变化", () => renderEvents(d.recent_events || []));
    guard("心境报到簿", () => renderInject(d.inject_state || {}));

    $("grid").hidden = false;
    $("errorCard").hidden = true;
  }

  function fail(msg) {
    $("errorText").textContent = msg;
    $("errorCard").hidden = false;
    $("grid").hidden = true;
  }

  async function load() {
    try {
      let data;
      if (!VIEW) VIEW = await waitForBridge();

      if (VIEW && typeof VIEW.apiGet === "function") {
        syncTheme(VIEW);
        data = await VIEW.apiGet(API_STATE);
      } else if (typeof window.__FISH_HEART_DEMO__ === "object") {
        data = window.__FISH_HEART_DEMO__; // 本地预览
      } else {
        throw new Error(
          "等不到 AstrBot 的页面桥接脚本（/api/plugin/page/bridge-sdk.js）。\n" +
          "通常是 AstrBot 版本不支持插件页面，或这个页面被直接打开、没有经过面板。",
        );
      }
      if (data && data.ok === false) {
        throw new Error(data.error || "读取状态失败");
      }
      const payload = data && data.data ? data.data : data;
      if (!payload || !payload.drives) throw new Error("返回数据里没有驱动数值");
      render(payload);
    } catch (e) {
      const msg = String(e && e.message ? e.message : e);
      if (msg.includes("未找到该路由") || /not found/i.test(msg)) {
        fail(
          "插件还没有注册页面接口。\n" +
          "请在插件管理里重载「鱼的心」——接口是在插件加载时注册的，只刷新页面不会生效。",
        );
        return;
      }
      fail(msg);
    }
  }

  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => { el.hidden = true; }, 320);
    }, 2200);
  }

  async function tick() {
    const btn = $("btnTick");
    btn.disabled = true;
    try {
      if (!VIEW) VIEW = await waitForBridge();
      if (!VIEW || typeof VIEW.apiPost !== "function") throw new Error("当前环境不支持调用");
      const res = await VIEW.apiPost(API_TICK, {});
      if (res && res.ok === false) {
        throw new Error(res.error || "心跳失败");
      }
      const sent = !!(res && (res.just_sent || (res.data && res.data.just_sent)));
      const cfg = (res && res.data && res.data.config) || {};
      const tickLimit = Number(cfg.daily_limit ?? 1);
      const tickCount = Number((res && res.data && res.data.proactive_count) || 0);
      if (res && res.data && res.data.enabled === false) {
        toast("总开关已关闭，这次只跳了心跳");
      } else if (cfg.proactive_enabled === false) {
        toast("主动说话已关闭，这次只跳了心跳");
      } else if (Number(cfg.daily_limit ?? 1) <= 0) {
        toast("每日主动次数为 0，这次只跳了心跳");
      } else if (tickLimit > 0 && tickCount >= tickLimit) {
        toast("今天的主动次数已经用完了，这次只跳了心跳");
      } else if (res && res.data && res.data.sleep?.asleep) {
        toast("她睡着了，这次只跳了心跳");
      } else if (res && res.data && res.data.mode === "shut") {
        toast("她在回避，主动说话被冻结，这次只跳了心跳");
      } else {
        toast(sent ? "她刚开口了，去看看消息" : "这次没开口（概率/冷却/目标会话格式，详情看日志）");
      }
      await load();
    } catch (e) {
      toast(`操作失败：${e.message || e}`);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------------- 事件绑定 ---------------- */

  $("btnRefresh").addEventListener("click", load);
  $("btnRetry").addEventListener("click", load);
  $("btnTick").addEventListener("click", tick);

  let themed = false;
  function syncTheme(view) {
    if (themed || !view || typeof view.onContext !== "function") return;
    themed = true;
    view.onContext((ctx) => {
      if (ctx && typeof ctx.isDark === "boolean") {
        document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
      }
    });
  }

  // 独立打开时的兜底主题（在面板里会被 onContext 覆盖）
  if (!document.documentElement.getAttribute("data-theme")) {
    document.documentElement.setAttribute(
      "data-theme",
      window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark",
    );
  }

  load();
  // 面板停留时每 60 秒自动刷新一次
  setInterval(() => { if (!$("grid").hidden) load(); }, 60000);
})();
