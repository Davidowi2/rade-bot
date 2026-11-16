
"use strict";
let timer = null;
let me = null;

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"]/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
}

function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${date} ${time}`;
}

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

async function api(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { showLogin(); throw new Error("unauthorised"); }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

async function post(path, payload) {
  const res = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { showLogin(); throw new Error("unauthorised"); }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function flash(message) {
  const el = document.createElement("div");
  el.className = "flash";
  el.setAttribute("role", "status");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function showLogin() {
  if (timer) { clearInterval(timer); timer = null; }
  document.getElementById("app").classList.add("hidden");
  document.getElementById("login-view").classList.remove("hidden");
}

function showApp() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("who").textContent = me || "";
}

function badge(status) {
  const cls = status === "paper_intent" ? "ok"
    : (status === "blocked" || status === "conflict") ? "bad"
    : (status === "manual_review" || status === "stale") ? "warn"
    : status === "ignored" ? "" : "info";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function stateBadge(state) {
  const cls = (state === "open" || state === "partial") ? "ok"
    : (state === "blocked" || state === "rejected" || state === "review") ? "bad"
    : state === "closed" ? "" : "warn";
  return `<span class="badge ${cls}">${esc(state)}</span>`;
}

function table(rows, columns, clickable) {
  if (!rows.length) return '<div class="empty">Nothing here.</div>';
  const head = columns.map(c => `<th${c.num ? ' class="num"' : ""}>${esc(c.label)}</th>`).join("");
  const body = rows.map(r =>
    `<tr${clickable ? ' data-click="1"' : ""}>` +
    columns.map(c => `<td${c.num ? ' class="num"' : ""}>${c.render(r)}</td>`).join("") +
    "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function card(label, value, note, tone) {
  return `<div class="card ${tone || ""}">
    <div class="label">${esc(label)}</div>
    <div class="value">${value}</div>
    ${note ? `<div class="note">${note}</div>` : ""}
  </div>`;
}

function renderCards(status) {
  const listener = status.listener || {};
  const counts = status.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0);
  const exec = status.execution || {};
  const broker = exec.broker || {};

  const listenerValue = !listener.known ? "unknown"
    : listener.stale ? "stale" : (listener.phase || "unknown");
  const listenerTone = (listener.stale || !listener.known) ? "is-bad"
    : listener.phase === "listening" ? "is-ok" : "";

  const mode = status.paper_only ? "paper" : "executing";
  const modeTone = status.paper_only ? "" : "is-ok";
  const dest = broker.environment
    ? `${broker.environment} · ${esc(broker.account_number || "?")}`
    : "not resolved";

  document.getElementById("cards").innerHTML = [
    card("Mode", esc(mode), status.paper_only ? "no broker writes" : "orders enabled", modeTone),
    card("Listener", esc(listenerValue),
      listener.heartbeat_age_s != null
        ? `heartbeat ${esc(listener.heartbeat_age_s)}s ago`
        : (listener.reason || "no heartbeat file"), listenerTone),
    card("Events stored", `<span class="mono">${total}</span>`,
      Object.entries(counts).filter(([, v]) => typeof v === "number")
        .map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ") || "none yet"),
    card("Destination", `<span class="mono">${esc(dest)}</span>`,
      broker.verified ? "broker specification verified" : "execution disabled"),
    card("Uptime", `${Math.round((status.uptime_s || 0) / 60)}m`,
      "this process, not the listener"),
  ].join("");
}

function renderBadges(status) {
  const exec = status.execution || {};
  const broker = exec.broker || {};
  const items = [];
  if (exec.enabled) {
    items.push('<span class="badge ok">executing</span>');
    items.push(`<span class="badge ${broker.verified ? "ok" : "bad"}">broker ${broker.verified ? "verified" : "unverified"}</span>`);
    items.push(`<span class="badge">${esc(broker.symbol || "?")} · ${esc(broker.instrument_id)}</span>`);
  } else {
    items.push('<span class="badge ok">paper only</span>');
    items.push('<span class="badge">broker writes disabled</span>');
  }
  items.push(`<span class="badge ${status.exit_policy === "tp_ladder" ? "ok" : "warn"}">exit ${esc(status.exit_policy || "unresolved")}</span>`);
  if (exec.error) items.push('<span class="badge bad">execution error</span>');
  const paused = exec.paused;
  if (paused && paused.paused === true) items.push('<span class="badge bad">paused</span>');
  else if (paused && paused.paused === null) items.push('<span class="badge bad">pause state unreadable</span>');
  if (status.config && status.config.notifier === false) items.push('<span class="badge warn">notifications off</span>');
  document.getElementById("badges").innerHTML = items.join("");
}

function renderFunnel(status) {
  const c = status.counts || {};
  const total = Object.values(c).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0);
  const steps = [
    ["Stored", total, ""],
    ["Intents", c.paper_intent || 0, "ok"],
    ["Blocked", c.blocked || 0, "bad"],
    ["Review", (c.manual_review || 0) + (c.conflict || 0), "warn"],
    ["Stale", c.stale || 0, "warn"],
    ["Ignored", c.ignored || 0, ""],
  ];
  document.getElementById("funnel").innerHTML = '<div class="funnel">' +
    steps.map(([k, v, tone]) =>
      `<div class="step"><div class="k">${esc(k)}</div><div class="v ${tone}">${esc(v)}</div></div>`
    ).join("") + "</div>";
}

// A control that is switched off must read as off, never as passed.
function renderGuards(status) {
  const el = document.getElementById("guards");
  const exec = status.execution || {};
  const guards = exec.guards;
  if (!exec.enabled || !guards || !guards.limits) {
    el.innerHTML = '<div class="empty">No pre-trade controls are running: the execution path is not enabled.</div>';
    return;
  }
  const limits = guards.limits;
  const disabled = new Set(limits.disabled || []);
  const pct = v => `${(Number(v) * 100).toFixed(2)}%`;
  const rows = [
    { key: "max_open_positions", label: "Max open positions", value: limits.max_open_positions },
    { key: "daily_loss_fraction", label: "Daily loss limit", value: pct(limits.daily_loss_fraction) },
    { key: "max_drawdown_fraction", label: "Drawdown stop", value: pct(limits.max_drawdown_fraction) },
    { key: "max_spread", label: "Max spread", value: limits.max_spread },
    { key: "max_slippage", label: "Max slippage", value: limits.max_slippage },
  ];
  const last = guards.last_verdict;
  el.innerHTML = '<div class="grid">' +
    rows.map(r => card(r.label, `<span class="mono">${esc(r.value)}</span>`,
      disabled.has(r.key) ? "disabled by configuration" : "enforced",
      disabled.has(r.key) ? "is-bad" : "")).join("") +
    card("Last pre-trade verdict", last ? (last.allowed ? "passed" : "blocked") : "—",
      esc(last ? last.reason : "no order attempted yet"),
      last ? (last.allowed ? "is-ok" : "is-bad") : "") +
    "</div>";
}

// The intake panel is where a refusal that happens before sizing is visible.
function renderIntake(status) {
  const el = document.getElementById("intake");
  const intake = status.intake || {};
  const sizing = intake.sizing || {};
  const seconds = v => v === undefined ? "unknown"
    : Number(v) <= 0 ? "disabled" : `${Number(v)}s`;
  const money = v => v === undefined || v === null ? "not set" : `$${Number(v).toFixed(2)}`;

  let sizeCard;
  if (sizing.mode === "fixed_lots") {
    const stopNote = Number.isFinite(sizing.max_stop_distance)
      ? `allows a stop of ${Number(sizing.max_stop_distance).toFixed(2)} pts`
      : "stop width not computable";
    sizeCard = card("Position size", `<span class="mono">${esc(Number(sizing.fixed_lots))} lot</span>`,
      sizing.ready
        ? `fixed, or the signal is refused. Ceiling ${esc(money(sizing.max_trade_risk_usd))} · ${stopNote}.`
        : `NOT READY: ${esc(sizing.problem)}`,
      sizing.ready ? "" : "is-bad");
  } else if (sizing.blocks_execution) {
    sizeCard = card("Position size", "not configured", `NOT READY: ${esc(sizing.problem)}`, "is-bad");
  } else {
    sizeCard = card("Position size", "risk fraction", "no fixed lot; size is a fraction of equity");
  }

  const ready = intake.intake_ready;
  const readyTone = ready === true ? "is-ok" : "is-bad";
  const readyNote = ready === true
    ? "a signal can be sized"
    : (intake.intake_problem || "intake is blocked");

  el.innerHTML =
    card("Intake", ready === true ? "ready" : "blocked", esc(readyNote), readyTone) +
    sizeCard +
    card("Signal age limit", esc(seconds(intake.signal_max_age_seconds)),
      Number(intake.signal_max_age_seconds) <= 0
        ? "disabled by configuration"
        : "older NOW signals refused as stale") +
    card("Quote age limit", esc(seconds(intake.quote_max_age_seconds)),
      "the price must be this fresh at sizing time") +
    card("Delivery interval", esc(seconds(intake.delivery_interval_seconds)),
      intake.delivery_within_age_limit === false
        ? "outside the age limit: late signals refused, nothing copied"
        : "the poll that fetches a signal when no push arrives");
}

function renderPause(status) {
  const el = document.getElementById("pause");
  const exec = status.execution || {};
  if (!exec.enabled) {
    el.innerHTML = '<div class="empty">The pause switch controls the execution path, which is not enabled.</div>';
    return;
  }
  const paused = exec.paused || {};
  const isPaused = paused.paused === true;
  const unreadable = paused.paused === null;
  el.innerHTML = `<div class="toolbar">
    <span class="badge ${isPaused || unreadable ? "bad" : "ok"}">
      ${isPaused ? "paused" : unreadable ? "unreadable" : "running"}
    </span>
    <span class="sub">${esc(paused.reason || "")}</span>
    <input id="pause-reason" placeholder="reason (required to pause)" maxlength="200">
    <button id="pause-btn" ${isPaused || unreadable ? "disabled" : ""}>Pause</button>
    <button id="resume-btn" class="ghost" ${!isPaused ? "disabled" : ""}>Resume</button>
  </div>`;
  const send = async (path) => {
    const reason = (document.getElementById("pause-reason").value || "").trim();
    if (path === "/api/pause" && !reason) { flash("A reason is required to pause execution."); return; }
    try {
      await post(path, { reason, by: "dashboard" });
      await refresh();
    } catch (err) {
      flash(`Could not ${path === "/api/pause" ? "pause" : "resume"}: ${err.message}`);
    }
  };
  document.getElementById("pause-btn").addEventListener("click", () => send("/api/pause"));
  document.getElementById("resume-btn").addEventListener("click", () => send("/api/resume"));
}

function renderBroker(status, trades) {
  const exec = status.execution || {};
  const el = document.getElementById("broker");
  document.getElementById("broker-hint").textContent = exec.enabled
    ? "pinned destination account" : "execution disabled — nothing is written";
  if (!exec.enabled) {
    el.innerHTML = '<div class="empty">The execution path is not enabled. This service is the paper mirror: it sizes and records intents, and writes nothing to a broker.</div>';
    return;
  }
  const broker = exec.broker || {};
  const active = trades.find(t => t.state === "open" || t.state === "partial");
  if (!active) {
    const rows = [
      ["Environment", broker.environment || "?"],
      ["Account", broker.account_number || "?",
      ["Symbol", `${broker.symbol || "?"} · instrument ${broker.instrument_id ?? "?"}`],
      ["Contract", `${broker.contract_units_per_lot ?? "?"} units/lot · step ${broker.lot_step ?? "?"} · min ${broker.min_lot ?? "?"} · max ${broker.max_lot ?? "?"}`],
      ["Status", `${broker.symbol_status || "?"} · ${broker.verified ? "verified" : "unverified"}`],
    ];
    el.innerHTML = '<div class="kv">' + rows.map(pair =>
      `<div><div class="k">${esc(pair[0])}</div><div class="v">${esc(pair[1])}</div></div>`).join("") +
      '</div><div class="empty">No open position on the destination account.</div>';
    return;
  }
  el.innerHTML = '<div class="grid">' + [
    card("Position", `<span class="mono">${esc(active.broker_position_id)}</span>`, `order ${esc(active.broker_order_id)}`),
    card("Side / size", `${esc((active.side || "").toUpperCase())} <span class="mono">${esc(active.size_lots)}</span>`, esc(active.symbol)),
    card("Fill", `<span class="mono">${esc(active.fill_price)}</span>`, `published ${esc(active.published_entry)}`),
    card("Stop (broker)", `<span class="mono">${esc(active.current_stop)}</span>`, `initial ${esc(active.initial_stop)}`),
    card("Final target", `<span class="mono">${esc(active.final_target)}</span>`, `policy ${esc(active.policy)}`),
    card("Ladder", `<span class="mono">${esc(active.legs_done)} / ${esc(active.legs_total)}</span>`,
      active.management_stopped ? "management stopped" : "managing"),
  ].join("") + "</div>";
}

function renderTrades(rows) {
  const el = document.getElementById("trades");
  if (!rows.length) { el.innerHTML = '<div class="empty">No trades yet.</div>'; return; }
  el.innerHTML = table(rows, [
    { label: "Created", num: true, render: r => esc(shortTime(r.created_at)) },
    { label: "Trade", render: r => `<code>${esc(r.trade_id)}</code>` },
    { label: "Side", render: r => `${esc((r.side || "").toUpperCase())} ${esc(r.symbol || "")}` },
    { label: "State", render: r => stateBadge(r.state) },
    { label: "Size", num: true, render: r => esc(r.size_lots) },
    { label: "Entry", num: true, render: r => esc(r.fill_price || r.published_entry) },
    { label: "Stop", num: true, render: r => esc(r.current_stop || r.initial_stop) },
    { label: "Broker", num: true, render: r =>
        `pos ${esc(r.broker_position_id || "—")}<br><span class="sub">order ${esc(r.broker_order_id || "—")}</span>` },
    { label: "Note", render: r => `<span class="sub">${esc(r.note)}</span>` },
  ], true);
  Array.from(el.querySelectorAll("tbody tr")).forEach((tr, index) => {
    tr.addEventListener("click", () => showTradeDetail(rows[index].trade_id));
  });
}

async function showTradeDetail(tradeId) {
  const panel = document.getElementById("detail-panel");
  const el = document.getElementById("detail");
  panel.classList.remove("hidden");
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const body = await api(`/api/trades/${encodeURIComponent(tradeId)}`);
    const trade = body.trade || {};
    const transitions = body.transitions || [];
    el.innerHTML = table(transitions, [
      { label: "When", num: true, render: t => esc(shortTime(t.created_at)) },
      { label: "Stage", render: t => `<code>${esc(t.stage)}</code>` },
      { label: "Status", render: t => stateBadge(t.status) },
      { label: "Detail", render: t => `<span class="sub">${esc(t.detail)}</span>` },
      { label: "Broker", num: true, render: t =>
          `pos ${esc(t.broker_position_id || "—")} order ${esc(t.broker_order_id || "—")}` },
    ]) + `<div class="sub" style="padding:14px 18px">
      ${esc((trade.side || "").toUpperCase())} ${esc(trade.symbol || "")} ·
      policy ${esc(trade.policy || "—")} ·
      final target ${esc(trade.final_target)} ·
      ignored ${esc((trade.ignored_targets || []).join(", ") || "none")}
    </div>`;
  } catch (err) {
    el.innerHTML = `<div class="err" style="padding:16px 18px">${esc(err.message)}</div>`;
  }
}

function renderIntents(rows) {
  document.getElementById("intents").innerHTML = table(rows, [
    { label: "Received", num: true, render: r => `${esc(shortTime(r.received_at))} <span class="tiny">${esc(relTime(r.received_at))}</span>` },
    { label: "Message", num: true, render: r => `${esc(r.message_id)} rev ${esc(r.revision)}` },
    { label: "Signal", render: r => {
        const s = r.signal_json || {};
        return `${esc((s.side || "").toUpperCase())} ${esc(s.instrument || "")}<br>
                <span class="sub">entry ${esc(s.published_entry)} · SL ${esc(s.stop_loss)} · TP ${esc((s.take_profits || []).join(", "))}</span>`;
      } },
    { label: "Intent", render: r => {
        const i = r.intent_json || {};
        return `<span class="mono">${esc(i.size_lots)} lot</span> · ${esc(i.order_state)}<br>
                <span class="sub">risk $${esc(i.estimated_stop_risk_before_slippage_and_fees ?? i.risk_budget)}</span>`;
      } },
  ]);
}

function renderEvents(rows, target) {
  document.getElementById(target).innerHTML = table(rows, [
    { label: "Received", num: true, render: r => `${esc(shortTime(r.received_at))} <span class="tiny">${esc(relTime(r.received_at))}</span>` },
    { label: "Message", num: true, render: r => `${esc(r.message_id)} rev ${esc(r.revision)}` },
    { label: "Status", render: r => badge(r.status) },
    { label: "Reason", render: r => `<span class="sub">${esc(r.reason)}</span>` },
  ]);
}

async function refresh() {
  try {
    const status = await api("/api/status");
    document.getElementById("loading").classList.add("hidden");
    document.getElementById("dash").classList.remove("hidden");
    renderCards(status);
    renderBadges(status);
    renderFunnel(status);
    const [intents, review, events, trades] = await Promise.all([
      api("/api/intents?limit=25"),
      api("/api/review?limit=25"),
      api("/api/events?limit=40"),
      api("/api/trades?limit=25"),
    ]);
    const tradeRows = trades.trades || [];
    renderBroker(status, tradeRows);
    renderIntake(status);
    renderGuards(status);
    renderPause(status);
    renderTrades(tradeRows);
    renderIntents(intents.events || []);
    renderEvents(review.events || [], "review");
    renderEvents(events.events || [], "events");
    document.getElementById("clock").textContent =
      `service time ${status.time ? new Date(status.time).toLocaleString() : "—"}`;
    document.getElementById("foot").innerHTML =
      `<span><span class="dot ${status.listener && status.listener.healthy ? "ok" : "bad"}"></span>listener</span>` +
      `<span>account ${esc(status.account_scope || "none")}</span>` +
      `<span>exit ${esc(status.exit_policy || "unresolved")}</span>` +
      `<span>store ${esc(status.store || "?")}</span>` +
      `<span>refresh 30s</span>`;
  } catch (err) {
    if (err.message !== "unauthorised") flash(`Cannot reach the service: ${err.message}`);
  }
}

function startRefresh() {
  if (timer) clearInterval(timer);
  refresh();
  timer = setInterval(refresh, 30000);
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("login-btn");
  const err = document.getElementById("login-err");
  err.textContent = "";
  btn.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("email").value,
        password: document.getElementById("password").value,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.textContent = body.error || "Sign-in failed.";
      btn.disabled = false;
      return;
    }
    me = body.email || "";
    document.getElementById("password").value = "";
    showApp();
    startRefresh();
  } catch (exc) {
    err.textContent = `Cannot reach the service: ${exc.message}`;
    btn.disabled = false;
  }
});

document.getElementById("signout").addEventListener("click", async () => {
  try { await post("/api/logout"); } catch (_err) { /* the cookie is cleared anyway */ }
  me = null;
  showLogin();
});

document.getElementById("detail-close").addEventListener("click", () => {
  document.getElementById("detail-panel").classList.add("hidden");
});

// Who is already signed in? A 401 here is the normal first visit.
(async () => {
  try {
    const who = await api("/api/me");
    me = who.email || "";
    showApp();
    startRefresh();
  } catch (_err) {
    showLogin();
  }
})();
