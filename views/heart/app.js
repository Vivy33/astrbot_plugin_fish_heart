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
    calm: { cn: "平静", color: "#4fd1c5" },
    wronged: { cn: "委屈", color: "#f6ad55" },
    afraid: { cn: "害怕", color: "#a78bfa" },
    shut: { cn: "回避", color: "#f17a8a" },
  };

  const DRIVE_ORDER = [
    "attachment", "curiosity", "reflection", "duty", "social",
    "fatigue", "intimacy", "stress", "joy",
  ];

  const KIND_CN = {
    praise: "想被夸", miss: "想你", hug: "想抱抱", talk: "想找你说话",
    ask_day: "问你今天", task: "想找点事做", quiet: "想安静会儿",
    dwell: "想撒个娇",
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
      const v = clamp100(drives[k]) / 100;
      return `${cx + Math.cos(a) * R * v},${cy + Math.sin(a) * R * v}`;
    }).join(" ");
    root.appendChild(svg("polygon", { points: poly, class: "area" }));

    // 顶点
    DRIVE_ORDER.forEach((k, i) => {
      const a = angle(i);
      const v = clamp100(drives[k]) / 100;
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
        class: clamp100(drives[k]) >= 70 ? "label hot" : "label",
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
      const wrap = document.createElement("div");
      wrap.className = "ring";
      const dial = document.createElement("div");
      dial.className = "dial";
      const b = document.createElement("b");
      b.textContent = Math.round(clamp100(val));
      dial.appendChild(b);
      const cap = document.createElement("div");
      cap.className = "cap";
      cap.textContent = cn;
      wrap.append(dial, cap);
      box.appendChild(wrap);
      requestAnimationFrame(() => dial.style.setProperty("--p", clamp100(val)));
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
      const row = document.createElement("div");
      row.className = "meter";
      row.innerHTML = `<span class="name">${cn}</span><span class="bar"><i></i></span><span class="val">${Math.round(v)}</span>`;
      mbox.appendChild(row);
      requestAnimationFrame(() => { row.querySelector("i").style.width = `${v}%`; });
    });
  }

  function renderThoughts(list) {
    const ul = $("thoughtList");
    ul.replaceChildren();
    $("thoughtCount").textContent = `${list.length} 条`;
    $("thoughtEmpty").hidden = list.length > 0;

    const now = Date.now() / 1000;
    list.forEach((t) => {
      const li = document.createElement("li");
      li.className = "thought";

      const life = Math.max(1, Number(t.ttl_total) || 1);
      const remain = Math.max(0, Number(t.expires) - now);
      const pct = Math.min(100, (remain / life) * 100);
      const kindCn = toYou(t.kind_cn || KIND_CN[t.kind] || t.kind);
      const moodText = t.pending ? "还没被满足" : toYou(t.mood || "已经放下");

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
    const remain = last ? Math.max(0, last + cooldown - d.now) : 0;

    $("pCount").textContent = `${d.proactive_count}/${cfg.daily_limit ?? "?"}`;
    $("pCooldown").textContent = last ? (remain > 0 ? fmtSpan(remain) : "可开口") : "可开口";
    $("pLast").textContent = fmtAgo(last);
    $("pWaiting").textContent = d.awaiting_reply ? "是" : "否";

    $("proactiveHint").textContent = `${MODES[d.mode]?.cn || d.mode} · ${d.enabled ? "心跳中" : "已暂停"}`;
    $("moodBadge").textContent = d.mood_cn || "平常";

    const chips = [
      ["冷却", `${cfg.cooldown_hours}h`],
      ["每日上限", `${cfg.daily_limit}`],
      ["刚聊过不打扰", `${cfg.min_idle_minutes}min`],
      ["睡眠", cfg.quiet_hours || "不限"],
      ["分段发送", cfg.segment_reply ? "开" : "关"],
      ["念头", d.thought_enabled ? "开" : "关"],
      ["入池门槛", `${cfg.thought_min_weight ?? 0.2}`],
      ["念头加成", `+${cfg.thought_push ?? 0.15}`],
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
    if (!d.enabled) {
      readiness = "总开关已关闭，她不会主动开口。";
    } else if (cfg.quiet_active) {
      readiness = `现在是睡眠时段（${cfg.quiet_hours || "—"}），到点之后才会考虑开口。`;
    } else if (remain > 0) {
      readiness = `冷却中，还要等 ${fmtSpan(remain)}。`;
    } else if (d.proactive_count >= Number(cfg.daily_limit ?? 99)) {
      readiness = `今天的 ${cfg.daily_limit} 次已经用完了，明天再来。`;
    } else {
      readiness = "随时可能开口 —— 取决于她的心情，和那一次概率。";
    }
    $("readiness").textContent = readiness;
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

  function applyMode(mode) {
    const m = MODES[mode] || MODES.calm;
    document.documentElement.style.setProperty("--mode", m.color);
    document.documentElement.style.setProperty("--mode-soft", hexToSoft(m.color));
    const badge = $("modeBadge");
    badge.textContent = m.cn;
    badge.dataset.mode = mode;
  }

  function hexToSoft(hex) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, 0.14)`;
  }

  /* ---------------- 主流程 ---------------- */

  function render(d) {
    applyMode(d.mode);
    document.body.dataset.mode = d.mode;

    $("subtitle").textContent =
      `此刻${MODES[d.mode]?.cn || ""} · 期待 ${Math.round(d.expectation)} · 责怪 ${Math.round(d.blame)}`;
    $("tickHint").textContent = `心跳 ${fmtAgo(d.last_tick)}`;

    const hint = d.hint || "";
    $("tendency").hidden = !hint;
    $("tendencyText").textContent = hint;

    const drives = { ...d.drives, _cn: d.drives_cn || {} };
    drawRadar(drives);
    renderVitals(d);
    renderThoughts(d.thoughts || []);
    renderProactive(d);
    // recent 全是单边（历史遗留）时，改用 AstrBot 的真实对话历史
    const recent = d.recent || [];
    const hasBot = recent.some((m) => m.r === "bot");
    if (hasBot || !(d.history && d.history.length)) {
      renderRecent(recent.slice(-12), false);
    } else {
      renderRecent(d.history.slice(-12), true);
    }
    renderEvents(d.recent_events || []);
    renderInject(d.inject_state || {});

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
      const sent = !!(res && (res.just_sent || (res.data && res.data.just_sent)));
      toast(sent ? "她刚开口了，去看看消息" : "这次没开口（概率未命中）");
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
