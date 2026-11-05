# 06 — Shadow Study Spec (FROZEN)

## Purpose
Prospective, no-trade observation of new Telegram calls. Record each call and market data around it. Assess whether entries show repeatable follow-through after enough new data accumulates. Old results remain provisional.

## Status: FROZEN (do not modify during collection)

## Definitions

### Call types
- **NOW (market):** Entry at published price. Published price used as reference. Actual executable price and exact signal-time return are **unknown**. Reported as market movement from reference, not executable trade returns.
- **LIMIT:** Entry at published limit price. Record whether price **reached** the level (not "filled" — no broker order exists). Report first-touch time. Anchor primary measurements after the touch bar.

### Time resolution
- **Primary:** 1-minute bars from TradeLocker (verified available for XAUUSD).
- **Fallback:** 30-minute bars (if 1m unavailable for a period).

### Measurement horizons
- 1h, 2h, 4h, 8h, 15h. Measurement points, not trade duration claims.

### Forward return computation
- Anchor: first fully post-reference bar (reference bar excluded).
- For NOW: reference bar = bar containing posted_at. First post-reference bar = next bar.
- For LIMIT (price reached): reference bar = first bar where price touched entry level. First post-reference bar = next bar. Report touch bar separately.
- For LIMIT (not reached): record as `price_reached=False`. No forward returns computed.

### MFE/MAE
- Primary: exclude reference bar (first-touch bar for LIMIT, signal bar for NOW).
- Sensitivity: include reference bar.
- **Floored at zero:** MFE and MAE are magnitudes (≥ 0). If price moves only one way, the other is 0.
- Report separately, not as ratio.

### Normalization
- Primary: ATR-normalized (forward return / (entry_ref × d1_atr14_pct)).
- Secondary: R-normalized (forward return / published stop distance).

### Edge cases (frozen rules)

| Case | Rule |
|---|---|
| Edited posts | Record original post time and text. If edited, record edit time and new text. Use **original** post for entry timing. If edit changes entry level, flag `entry_changed=True` and exclude from primary analysis (report separately). **If a post is already edited before first collection (no prior version stored), flag `original_version_unknown=True` and exclude from primary analysis unless independently verified.** |
| Recaps | Flag `is_recap=True`. Exclude from entry analysis. Include in separate recap log if useful. |
| Unfilled LIMITs | Record as `price_reached=False`. Do not drop. Report reach rate and time-to-reach. Processor re-checks reach status on each run with new bars. |
| Ambiguous bars | Reference bar always flagged. Primary MFE/MAE excludes it. Fill-bar-inclusive reported as sensitivity. |
| False positives | Manual review only. Keyword screen suggests candidates. No auto-exclusion. Flag `review_status=pending` → `confirmed_signal` or `not_signal`. Only exclude `not_signal`. Retain decision. |
| NOW calls | `executable_price_unknown=True`. Analyze separately from LIMIT. |
| LIMIT calls | `price_reached` recorded. Analyze separately from NOW. |

### SL/TP touch columns
- Descriptive diagnostics only. Not success labels. Not pattern-selection targets.

### Pattern analysis rules
- Pre-specified pattern list (same as entry-quality study: direction, direction×hour, direction×volatility, direction×trend, direction×week-high, direction×risk, direction×weekday).
- No data-driven feature selection.
- Report effect sizes, sample counts, uncertainty. No arbitrary effect-size gates.
- No multiple-comparison correction claimed. State dependence caveats explicitly.
- Per-setup CIs are not independent (overlapping periods).

### Collection rules
- **Daily fetch with edit detection:** Each run fetches new messages (IDs > last collected) AND re-fetches the last 50 message IDs to detect edits. Deduplicates by message ID. For new messages, creates full row. For edits, updates only `edited_at`, `raw_text`, `original_text`, and `entry_changed` (compared entry fields). Preserves `matured`, `review_status`, and all computed forward returns. This prevents overwriting matured measurements or human review decisions.
- **Limitation:** Edits to messages older than 50 positions back will not be detected. For a complete edit history, a Telethon event listener would be needed. This is a known limitation.
- **Daily processing matures forward-return rows** with new bar data.
- **Weekly review** for signal adjudication and data quality. No performance-driven rule changes.
- Do not look at accumulating results during collection (performance kept hidden).
- Rules frozen. If a rule needs changing, start a new collection period.

### Sample assessment
- No fixed target. Estimate uncertainty from data.
- Assess after enough new data for meaningful CIs.
- Do not stop collection the first time a CI excludes zero.

### Storage
- Separate from old research outputs. Separate CSV files + separate state tracking.
- Preserve raw message snapshot and review decision.
- No appending to existing research files.

### Files
- `data/shadow_study.csv` — accumulated data (one row per call)
- `data/shadow_study_review_log.csv` — manual review decisions
- `data/shadow_state.json` — collection state (last message ID, last bar timestamp)
- `scripts/shadow_collect.py` — collection script
- `scripts/shadow_process.py` — maturity processing script
- `scripts/run_shadow_analysis.py` — pattern analysis (same as entry-quality, on new data only)

### No-trade constraint
- No trades placed. No orders. No position sizing.
- No exit rules designed or tested.
- No strategy code built.
- Old results remain provisional.