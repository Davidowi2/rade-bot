# 08 — Execution

## Adapters
- `TradelockerAdapter` (demo first) — XAUUSD (gold). v1 path. Use official
  `tradelocker` Python client (TLAPI). Base: https://demo.tradelocker.com.
- `BybitAdapter` (deferred to v2) — crypto.
- `MT5Adapter` (deferred to v2/v3) — MT5 gateway, per lead.

Both implement `BrokerAdapter`: connect, get_bars, place_order, cancel, modify,
get_positions, get_equity, flatten_all.

## Historical bars
Sourced from Tradelocker API via TLAPI.get_price_history (resolution "1D" for
D1 anchor; lower TFs only if needed for entry timing). No third-party data in v1.

## Notes
- /trade/* routes require accNum header; the Python client handles this.
- Account id comes from account switcher (#number) or all-accounts endpoint.

## Order types
- Market, limit, stop, stop-limit.
- Heavy use of limit orders for entries (per lead's style) and for probe
  observation. Market only for exits/kill switch.

## Safety
- Idempotent order submission (client order id).
- Reconcile local vs broker state every loop; alert on mismatch.
- All actions logged with broker response.

## Promotion gate (demo/paper → live)
1. Backtest passes thresholds.
2. Paper/demo runs ≥ 4 weeks, no unhandled errors.
3. Slippage within model.
4. Lead sign-off + key rotation.
5. Start live at 0.25x size for 2 weeks before full size.

## Open
- Exact Tradelocker API endpoints (already have demo creds).
- Whether Bybit is for crypto or gold via CFDs.
