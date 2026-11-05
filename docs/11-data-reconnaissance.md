# 11 — Data reconnaissance (Phase 2 design inputs)

All numbers below come from real runs against the live Tradelocker demo API and
the live Telegram channel (2026-11-02). Scripts: scratch probes
`design_probe1..13.py`, `design_analysis.py`.

## 1. Inventory

| Asset | Count | Coverage |
|---|---|---|
| Raw channel messages | 15,027 | 2023-06-04 → 2026-11-02 |
| Parsed signals | 2,396 | 2025-04-09 → 2026-11-02 |
| Clean setups (`XAUUSD BUY/SELL NOW\|LIMIT <px>`) | 2,382 (99.4%) | — |
| TP-hit result messages | 2,951 | — |
| D1 bars | 2,771 | 2016-11-06 → 2026-11-02 |
| 1H bars | ~11,000 | 2025-01 → 2026-11-02 (chunked) |
| 30m bars | 18,861 | 2025-04-01 → 2026-11-02 (**full signal history**) |
| 15m bars | 37,678 | 2025-04-01 → 2026-11-02 |

Signal volume ramp: 33/mo (Apr 2025) → 204/mo (Oct 2026).
No signals before 2025-04-09 — the channel only started posting signals then.

## 2. Instrument microstructure (Tradelocker demo, Plexy)

- `XAUUSD` — EQUITY_CFD, exchange Plexy, market data Metals, bar source BID
- tickSize 0.01 · tickCost 0.0 · **observed spread 0.25**
- lotSize 100 (1 lot = 100 oz) · minLot 0.01 · maxLot 50 · lotStep 0.01
- leverage 31.25 · margin type `fx_cfd` · session id 12691
- Demo account 2509174 (GOLD RAW), balance 100,992.92

## 3. The single most important finding: his entry is a LIMIT level, not a market price

- Only **6.9%** of `NOW` setups have their stated entry within $1 of the next
  bar open. Median deviation **$8.72**, p90 $26.
- But **90.2%** of stated entries match *some* bar close within $1 somewhere in
  `[-120d, +24h]` → the level is real, it is just reached later.
- Under a limit-fill model (touch within 24h): **2,013 / 2,382 setups resolve**
  (84.6% fill), 361 never fill. Fill delay: median 0.65h, p90 10.3h.
- ⇒ **We must model his entry as a resting limit order, not a market fill.**
  This is also why he prefers limit orders, per the lead.

Sign test to run in Phase 2: sell entries should sit *above* spot and buy
entries *below* spot. If confirmed, deviation is order-type geometry, not feed
error. (Median `entry − bar_open` = −$3.67, p10 −$21, p90 +$14 → noise, no
systematic basis drift, so a $0.25 spread difference between our feed and his
does not explain the gaps.)

## 4. His setup geometry

| Metric | Value |
|---|---|
| Risk per trade (entry → SL), USD | median 9, p90 14 |
| TP levels per setup | median 3 (2:795, 3:1261, 4:186) |
| TP distance in R | median 1.50, p10 0.62, p90 4.57 |
| Direction mix (live setups) | BUY 1,333 / SELL 570 |
| Direction mix (all parsed) | BUY 1,726 / SELL 670 |
| Days with both BUY and SELL | 175 / 446 |
| Setups per day | avg 5.3, max 17 |
| Posting hours (UTC) | 22–01 (late NY / rollover) and 10–14 (London–NY) |
| Weekday mix | Mon 483, Tue 496, Wed 503, Thu 441, Fri 311, Sun 162 |

His risk is *small in dollar terms* ($9 ≈ 0.22% of a 4,100 gold price) and he
de-risks through a TP ladder, not a single target.

## 5. Outcome quality of his own setups (our baseline)

Limit-fill model, 30m bars, 15h max hold, 0.25 spread charged on fill:

- Resolved 2,013 → **TP-first 57.3%**, SL-first 42.7%, timeout 1
- R: median **+0.59**, mean **+0.18**, p10 −1.02, p90 +1.49
- BUY: n=1,427, TP 57.5%, median R +0.61
- SELL: n=586, TP 56.7%, median R +0.54
- Median resolution: 0h (same 30m bar), p75 0.5h, p90 1h

**Read this honestly:** his edge on his own brackets is thin — median positive
R, but mean R only +0.18 before any extra costs. Phase 2's success criterion
must therefore be *beat the copy baseline*, not *reproduce his P&L*.

## 6. Labeling hazards (must be designed around, not discovered later)

1. **Intra-bar ambiguity — 17.9%.** In 360 of 2,010 resolved trades both SL and
   a TP are touched inside the same 30m bar; **350 of those 360 are in the fill
   bar itself**. Fix: pessimistic labelling (SL wins ties) as the base case,
   with an optimistic bound reported alongside.
2. **Recap posts — 477 setups (20%).** Entry level only reachable *before* the
   post → historical re-posts, not actionable. Must be excluded from training.
3. **Incomplete channel reporting — 51%.** Of 2,382 setup episodes, 883 report a
   TP hit, 284 report only a pip figure, **1,215 report nothing** within 48h.
   ⇒ Never use the channel's own result messages as ground truth. Outcomes must
   be derived from market data; channel results are only a cross-check.
4. **Pip unit = $0.10.** Calibrated: 1,572/2,919 TP-hit reports match the
   $0.10/pip unit within 8%, 1,489 within 1 pip; the $1.00 unit matches 1.
5. **Feed difference.** Our feed ≠ his feed. Keep all distance features
   normalized (ATR or % of price), never absolute dollars.

## 7. Consequences for the Phase 2 design

- 30m bars cover the entire signal history — use 30m as the labelling and
  execution-resolution timeframe; D1 for swing bias/structure; 1H for the probe.
- Tradelocker price history caps at 30,000 rows per request → all history
  fetches must be chunked by explicit `start_timestamp`/`end_timestamp`.
- Labels must come from the market, not from the channel's commentary.
- The trader's own setups are the *benchmark*, and they are mediocre → the
  probe and the learned decision rule have to carry the edge.
