# 05 — Hedge probe spec (draft)

## Purpose
Determine direction / control on a borderline setup WITHOUT taking a full
directional position first. A probe is a short, bounded observation.

## Concept
Place (in simulation, or as resting limit orders we observe) two opposite-side
limit orders around current price at a defined distance. Watch which side gets
touched first and how long price stays there. Aggregate into a direction score.

## Definition (v1, for Phase 2E measurement)

At candidate signal time `t`, with mid price `P` and 1H ATR `A`:

- buy limit  = `P − k·A`
- sell limit = `P + k·A`
- `k` default 0.5 (calibrate; 0.25 / 0.5 / 1.0 tested in 2E)
- both are **resting limit orders**, never market
- window: **15h hard cap** (same cap as a real trade)

## Sign convention (do not invert this)

- A **sell** limit resting *above* the market fills only when buyers cross the
  spread and lift the offer → **buyers are the aggressive side → bullish**.
- A **buy** limit resting *below* the market fills only when sellers hit the
  bid → **sellers are the aggressive side → bearish**.

So: touched **sell** side = bullish pressure; touched **buy** side = bearish
pressure.

## Probe parameters (defaults — tune)
- Distance from mid: ±k·ATR1H (k = 0.5 default).
- Size: minimal (probe size, not full trade size) — or zero in pure sim.
- Timeout: 15h max (matches "max cap").
- Observation cadence: every bar close (30m for labelling, H1 for probe ATR).

## Scoring
`score ∈ [−1, +1]`:
- `+1` — sell side touched first and price held above `P + k·A` for ≥ 60% of
  the window (clean buyer control).
- `0` — both sides touched, or neither touched, or hold time ambiguous → no
  directional information (and **no trade**).
- `−1` — buy side touched first with persistent hold below `P − k·A`.

Intermediate hold-time ratios map linearly. Ambiguous bars (both touched in the
same 30m bar) count as `0`, not as a directional read.

## Outcomes
- touched sell side first → bullish lean.
- touched buy side first → bearish lean.
- both touched → choppy / no edge → no trade.
- neither touched → no interest → no trade.

## Safety rules
- Probes are **simulated in Phase 2** — no live orders at all. Live limit-probe
  orders only after backtested edge is proven AND lead approves.
- A probe never leaves a net position (true by construction in simulation; a
  live version would flatten immediately on timeout or on both-sides fill).
- Probe output is a **feature / filter**, never a standalone trigger.
- Probe score may only *veto* or *size* a trade in Phase 3, never open one.

## Open
- Exact distance metric (ATR vs fixed pips) per instrument.
- Whether to run probe on every candidate or only borderline ones.
- How to weight probe score vs trader-signal score.
