// Exercises the dashboard's render functions against a stub DOM.
//
// A blank page is the failure mode this guards: the script can parse, every
// request can be authorised, and the page still shows nothing because one
// render function threw on a real payload. The proxy tests never execute the
// page, and parsing it does not run it.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function makeDom() {
  const elements = {};
  const make = () => ({
    innerHTML: "", textContent: "", value: "",
    classList: { add() {}, remove() {}, contains: () => true },
    setAttribute() {}, addEventListener() {},
    querySelectorAll: () => [],
  });
  return {
    elements,
    document: {
      getElementById(id) { return elements[id] || (elements[id] = make()); },
      createElement() { return make(); },
      body: { appendChild() {} },
    },
  };
}

/// Loads the page script with a stub DOM and returns its render functions.
function load() {
  const { document, elements } = makeDom();
  const fetchStub = async () => ({ status: 401, ok: false, json: async () => ({}) });
  const expose = "\n;return { renderCards, renderBadges, renderFunnel, renderIntake,"
    + " renderGuards, renderBroker, renderTrades, renderIntents, renderEvents,"
    + " renderPause, badge, stateBadge, table, card, esc };";
  const factory = new Function("document", "fetch", "window", "setTimeout",
    script + expose);
  const api = factory(document, fetchStub, {}, () => {});
  return { api, elements };
}

const STATUS = {
  service: "rade-mirror", paper_only: true, broker_writes: false,
  account_scope: "none", exit_policy: "unresolved",
  uptime_s: 600, time: "2026-11-16T01:00:00+00:00",
  store: "SupabaseLedger",
  counts: { paper_intent: 3, blocked: 2, manual_review: 1, ignored: 64 },
  listener: { expected: true, known: true, healthy: true, phase: "listening",
              heartbeat_age_s: 12, reason: "listener is listening" },
  intake: {
    signal_max_age_seconds: 120, quote_max_age_seconds: 2,
    reconnect_seconds: 900, catchup_every_seconds: 15,
    delivery_interval_seconds: 15, delivery_within_age_limit: true,
    sizing: { mode: "fixed_lots", fixed_lots: 0.03, max_trade_risk_usd: 25,
              ready: true, problem: "", max_stop_distance: 8.333333333333334 },
    sizing_mode: "fixed_lots", max_stop_distance: 8.333333333333334,
    units_confirmed: true, intake_ready: true, intake_problem: "",
    fixed_lot_ready: true, fixed_lot_problem: "",
  },
  execution: { enabled: false, account_scope: "demo", paused: null,
               guards: null, broker: {} },
  config: { supabase: true, notifier: true, contract_units_confirmed: true },
};

test("every render function runs against a realistic payload", () => {
  const { api, elements } = load();
  assert.doesNotThrow(() => {
    api.renderCards(STATUS);
    api.renderBadges(STATUS);
    api.renderFunnel(STATUS);
    api.renderIntake(STATUS);
    api.renderGuards(STATUS);
    api.renderBroker(STATUS, []);
    api.renderTrades([]);
    api.renderTrades([{ trade_id: "RADE-x", state: "open", side: "buy",
      symbol: "XAUUSD", size_lots: 0.03, published_entry: 3900,
      fill_price: 3900.5, initial_stop: 3890, current_stop: 3910,
      final_target: 3930, policy: "hold_to_final", legs_done: 1, legs_total: 2,
      broker_order_id: 1, broker_position_id: 2, note: "", created_at: STATUS.time }]);
    api.renderIntents([{ received_at: STATUS.time, message_id: 1, revision: 0,
      signal_json: { side: "buy", instrument: "XAUUSD", published_entry: 3900,
        stop_loss: 3890, take_profits: [3910, 3930] },
      intent_json: { size_lots: 0.03, order_state: "would_fill_market",
        estimated_stop_risk_before_slippage_and_fees: 30 } }]);
    api.renderEvents([{ received_at: STATUS.time, message_id: 1, revision: 0,
      status: "blocked", reason: "the ceiling" }], "events");
    api.renderPause(STATUS);
  });
  assert.ok(elements.cards.innerHTML.length > 0, "the overview cards did not render");
  assert.ok(elements.funnel.innerHTML.length > 0, "the funnel did not render");
  assert.ok(elements.intake.innerHTML.includes("8.33"),
    "the intake panel must show the stop width the ceiling allows");
  assert.ok(elements.events.innerHTML.includes("blocked"),
    "the events table did not render the row status");
});

test("the live mode reads as live, not as demo", () => {
    // A funded account sits on the broker's demo host; the host is a fact, not
    // a mode. The page must say live when orders are being placed.
    const live = {...STATUS, paper_only: false, broker_writes: true,
                  live_trading: true, exit_policy: "hold_to_final",
                  execution: {enabled: true, error: null, paused: {paused: false},
                              guards: null,
                              broker: {verified: true, symbol: "XAUUSD",
                                       instrument_id: 4231, account_number: 1}}};
    const { api, elements } = load();
    api.renderCards(live);
    api.renderBadges(live);
    assert.ok(elements.cards.innerHTML.includes(">live</div>"),
      "the mode card must read live when the executor is running");
    assert.ok(!elements.cards.innerHTML.includes(">demo<"),
      "the mode card must not describe the host as a mode");
    assert.ok(elements.badges.innerHTML.includes("live · orders placed"));
    assert.ok(elements.badges.innerHTML.includes("XAUUSD"));
});

test("the guards panel shows the risk room the account has left", () => {
  // Without this the drawdown limit is a bare percentage and the dashboard
  // cannot answer "how much can I still lose".
  const withRoom = {...STATUS,
    execution: {enabled: true, error: null, paused: {paused: false},
                guards: {limits: {max_open_positions: 1, daily_loss_fraction: 0.01,
                                   max_drawdown_fraction: 0.0335, max_spread: 1,
                                   max_slippage: 2, disabled: []},
                         preflight: {room: {equity: 4862.73, peak_equity: 4862.73,
                                            day_start_equity: 4862.73, stored: false,
                                            drawdown_floor: 4699.83,
                                            drawdown_room: 162.9,
                                            daily_floor: 4814.1,
                                            daily_room: 48.63}},
                         last_verdict: null},
                broker: {}}};
  const { api, elements } = load();
  api.renderGuards(withRoom);
  assert.ok(elements.guards.innerHTML.includes("Risk room"),
    "the risk room card is missing");
  assert.ok(elements.guards.innerHTML.includes("162.90"),
    "the drawdown room must be shown in dollars");
  assert.ok(elements.guards.innerHTML.includes("4699.83"),
    "the drawdown floor must be shown so the room means something");
  assert.ok(elements.guards.innerHTML.includes("48.63"),
    "the daily breaker room must be shown too");

  // No room reported -> no card, not an empty one.
  const noRoom = {...withRoom,
    execution: {...withRoom.execution,
                guards: {limits: withRoom.execution.guards.limits,
                          preflight: {}, last_verdict: null}}};
  api.renderGuards(noRoom);
  assert.ok(!elements.guards.innerHTML.includes("Risk room"),
    "a risk room card appeared with no room data");
});

test("the event tables show the signal body, not just the reason", () => {
  // The review queue is read by a human. A reason with no body cannot be
  // judged, so the text has to reach the page.
  const { api, elements } = load();
  api.renderEvents([{ received_at: STATUS.time, message_id: 9, revision: 0,
    status: "manual_review", reason: "edited before first sighting",
    raw_text: "XAUUSD BUY NOW 3915\nSl : 3905\nTp : 3920" }], "review");
  assert.ok(elements.review.innerHTML.includes("XAUUSD BUY NOW 3915"),
    "the signal body did not reach the table");
  assert.ok(elements.review.innerHTML.includes("Signal text"));

  api.renderEvents([{ received_at: STATUS.time, message_id: 9, revision: 0,
    status: "ignored", reason: "not a signal" }], "review");
  assert.ok(elements.review.innerHTML.includes("no body stored"),
    "an event with no body should say so, not render an empty cell");
});

test("the helpers escape and classify", () => {
  const { api } = load();
  assert.ok(api.esc("<script>").includes("&lt;"), "esc must escape HTML");
  assert.ok(api.badge("blocked").includes("badge bad"));
  assert.ok(api.badge("paper_intent").includes("badge ok"));
  assert.ok(api.stateBadge("open").includes("badge ok"));
  assert.ok(api.stateBadge("review").includes("badge bad"));
  assert.ok(api.table([], [{ label: "x", render: () => "y" }]).includes("Nothing here"));
});
