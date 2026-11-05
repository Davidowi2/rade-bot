# ADR-0002: v1 scope, asset, timeframe, hold cap

- Status: Accepted
- Date: 2026-11-02

## Decisions
- D1 Asset class v1: Gold/Forex via Tradelocker (demo first). Bybit/crypto deferred to v2.
- D2 Swing anchor timeframe: D1 (daily bars). Entry timing refined on lower TF if needed.
- D3 Max hold: 15 hours for BOTH the hedge probe observation window AND any real
  trade. No position may be held beyond 15h.
- D4 Paper/demo duration before live promotion: 1 week, BUT gated on
  ≥ 30 closed trades AND the full promotion checklist (08-execution.md), not
  on calendar time alone. Senior dev note: 1 week on D1 yields ~5 trades, so
  the trade-count gate will almost certainly extend this; recommend lead
  revisit if < 30 trades after 1 week.
- D5 Live promotion starts at 0.25x size for 2 weeks before full size.

## Tension flagged
D2 (D1 anchor) vs D3 (15h max hold): a D1 bar spans 24h, but max hold is 15h.
So entries are taken on a D1 bias but exited intraday. This is effectively
"swing bias, intraday hold" — feasible but unusual. Reconcile before Phase 3:
either (a) keep D1 bias + intraday exit (document as a rule), or (b) move
anchor to H4. Lead to confirm. For now: D1 bias, max 15h hold.

## Consequences
- Risk model, backtest, and execution all enforce 15h hard cap.
- Tradelocker demo creds (GOLD RAW / PLEXY) are the v1 broker path.
- Bybit keys remain unused in v1; do not wire them into v1 code.
