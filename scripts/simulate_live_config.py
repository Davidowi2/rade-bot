"""Replay a synthetic Telegram channel through the *live* configuration.

Every other simulation in this repo runs on test constants. This one reads the
numbers out of ``render.yaml`` - the blueprint the deployed service is built
from - so it cannot quietly drift from production. If the deployment's cap,
ceiling, contract size or quote age changes, this harness changes with it, and
a run is evidence about the bot that is actually trading.

It goes all the way to the broker: a message is delivered to
``process_once`` exactly as the listener delivers it, and anything that comes
back as a ``paper_intent`` is handed to the real ``ExecutionEngine`` over a
fake broker, which records whether an order was placed, at what size, and with
what stop.

    python scripts/simulate_live_config.py
    python scripts/simulate_live_config.py --json-out data/sim_live.json

Exit code 0 when every case matches its expectation, 1 otherwise.

What this proves: parsing, sizing, the guards, the pipeline's refusal branches,
and the engine's submission path all agree with each other under production
numbers.

What it does NOT prove: Telegram push delivery, the broker's own acceptance of
an order, or fills against a real book. Those need the live channel and the
demo account; no connection is opened here.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rade.execution.engine import ExecutionEngine  # noqa: E402
from rade.execution.guards import RiskLimits  # noqa: E402
from rade.observer.mirror_pipeline import (  # noqa: E402
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
    parse_message_signal,
    process_once,
)
from rade.observer.notify import RecordingNotifier  # noqa: E402
from tests.test_execution import FakeBroker  # noqa: E402

BLUEPRINT = ROOT / "render.yaml"


# ---------------------------------------------------------------------------
# the production numbers, read from the blueprint rather than retyped
# ---------------------------------------------------------------------------


def blueprint_env(path: Path = BLUEPRINT) -> dict[str, str]:
    """The service's env block, flattened. Secrets are irrelevant here."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for service in document.get("services", []):
        envs = service.get("envVars") or []
        if any(e.get("key") == "MIRROR_FIXED_LOTS" for e in envs):
            return {e["key"]: str(e.get("value", "")) for e in envs}
    raise SystemExit(f"no service with MIRROR_FIXED_LOTS in {path}")


ENV = blueprint_env()


def env_number(key: str) -> float:
    return float(ENV[key])


#: The deployed instrument. ``max_lot`` is the broker's, not ours.
INSTRUMENT = InstrumentSpec(
    "XAUUSD",
    env_number("MIRROR_CONTRACT_UNITS_PER_LOT") if "MIRROR_CONTRACT_UNITS_PER_LOT" in ENV else 100.0,
    0.01, 0.01, 40.0,
    units_confirmed=True,
)

#: The deployed sizing rule: a cap, a ceiling, and a quote age.
RISK = RiskConfig(
    equity=env_number("MIRROR_EQUITY"),
    risk_fraction=0.005,                       # unused while fixed_lots is set
    quote_max_age_seconds=env_number("MIRROR_QUOTE_MAX_AGE_S"),
    signal_max_age_seconds=env_number("MIRROR_MAX_SIGNAL_AGE_S"),
    fixed_lots=env_number("MIRROR_FIXED_LOTS"),
    max_trade_risk_usd=env_number("MIRROR_MAX_TRADE_RISK_USD"),
)

LIMITS = RiskLimits(
    max_open_positions=int(env_number("MIRROR_MAX_OPEN_POSITIONS")),
    daily_loss_fraction=env_number("MIRROR_DAILY_LOSS_FRACTION"),
    max_drawdown_fraction=env_number("MIRROR_MAX_DRAWDOWN_FRACTION"),
    max_spread=env_number("MIRROR_MAX_SPREAD"),
    max_slippage=env_number("MIRROR_MAX_SLIPPAGE"),
)

CHANNEL = "@XAUUSD_FOREX4"


# ---------------------------------------------------------------------------
# a signal, written the way the channel writes it
# ---------------------------------------------------------------------------


def sell_call(entry: float, stop: float, *targets: float) -> str:
    """``XAUUSD SELL NOW 3866 / Sl : 3876 / Tp : 3860 ...``"""
    def fmt(price: float) -> str:
        return str(int(price)) if float(price).is_integer() else str(price)

    body = [f"XAUUSD SELL NOW {fmt(entry)}", f"Sl : {fmt(stop)}"]
    body.extend(f"Tp : {fmt(t)}" for t in targets)
    return "\n".join(body)


def buy_call(entry: float, stop: float, *targets: float) -> str:
    def fmt(price: float) -> str:
        return str(int(price)) if float(price).is_integer() else str(price)

    body = [f"XAUUSD BUY NOW {fmt(entry)}", f"Sl : {fmt(stop)}"]
    body.extend(f"Tp : {fmt(t)}" for t in targets)
    return "\n".join(body)


def quote(bid: float, ask: float | None = None,
          age_s: float = 0.0) -> MarketQuote:
    """A broker quote. ``age_s`` backdates it to test freshness."""
    return MarketQuote(bid, bid + 0.2 if ask is None else ask,
                       datetime.now(timezone.utc) - timedelta(seconds=age_s))


# ---------------------------------------------------------------------------
# one world: a ledger, a broker, and an engine, wired the way the service does
# ---------------------------------------------------------------------------


class World:
    """A private deployment. Fresh ledger, fresh broker, same configuration."""

    def __init__(self, tmp: Path, *, equity: float | None = None):
        tmp.mkdir(parents=True, exist_ok=True)
        self.ledger = SQLiteIntentLedger(tmp / "world.sqlite")
        self.broker = FakeBroker()
        if equity is not None:
            self.broker.account_equity = equity
        self.engine = ExecutionEngine(
            broker=self.broker,
            ledger=self.ledger,
            notifier=RecordingNotifier(),
            log=lambda line: None,
            fixed_lots=RISK.fixed_lots,
            max_trade_risk_usd=RISK.max_trade_risk_usd,
            limits=LIMITS,
        )
        self._next_id = 20_000

    def deliver(self, text: str, *, market: MarketQuote | None = None,
                revision: int = 0, message_id: int | None = None,
                quote_error: str | None = None,
                posted_at: datetime | None = None) -> tuple[object, dict]:
        """Deliver one message, then execute it if it survived the pipeline."""
        if message_id is None:
            self._next_id += 1
            message_id = self._next_id
        # One market, two readers. The pipeline is handed a quote and the
        # engine re-reads the broker later; if those disagreed the engine would
        # be judging a different market from the one the signal was accepted
        # on, which is a bug in the harness, not in the bot.
        if market is not None:
            self.broker.bid = float(market.bid)
            self.broker.ask = float(market.ask)
        posted = posted_at or datetime.now(timezone.utc)
        envelope = MessageEnvelope(
            channel=CHANNEL, message_id=message_id, revision=revision,
            posted_at=posted, received_at=posted, text=text,
            source="synthetic_channel",
        )
        result = process_once(envelope, ledger=self.ledger,
                              instrument=INSTRUMENT, risk=RISK,
                              quote=market, quote_error=quote_error)
        before = self.engine.guard_state().get("last_verdict")
        outcome = self.engine.execute(result, envelope)
        after = self.engine.guard_state().get("last_verdict")
        # A verdict that did not change did not run: the engine never got as
        # far as the guards. Leaving the old one in would let a stale "allowed"
        # answer a question about this message.
        if after is not None and after != before:
            outcome = dict(outcome)
            outcome["guard_reason"] = after.get("reason")
            outcome["guard_allowed"] = after.get("allowed")
        return result, outcome


def lots_of(result) -> float | None:
    intent = result.order_intent or {}
    size = intent.get("size_lots")
    return None if size is None else float(size)


def describe(result) -> str:
    return f"{result.status}: {result.reason}"


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

CASES: list[dict] = []


def case(name: str, *, expect: str, result, outcome: dict,
         expect_lots: float | None = None, expect_placed: bool | None = None,
         expect_guard: str | None = None, note: str = "") -> None:
    """Record one case and decide whether it behaved.

    ``expect_guard`` is a substring the *engine's* last guard verdict must
    contain. The risk guards live in the engine, not the pipeline, so a signal
    they refuse still reads as ``paper_intent`` - the refusal only shows up
    here, and a case that asserted on the pipeline status alone would pass
    while nothing was actually checked.
    """
    status_ok = result.status == expect
    placed = bool(outcome.get("executed"))
    lots = lots_of(result)

    problems = []
    if not status_ok:
        problems.append(f"expected status {expect!r}, got {result.status!r}")
    if expect_lots is not None and (lots is None or abs(lots - expect_lots) > 1e-9):
        problems.append(f"expected {expect_lots:g} lots, got {lots}")
    if expect_placed is not None and placed is not expect_placed:
        problems.append(f"expected placed={expect_placed}, got placed={placed}")
    if expect_guard is not None:
        if not outcome.get("guard_reason"):
            problems.append(f"expected a guard verdict mentioning "
                            f"{expect_guard!r}, but no guard ran")
        elif expect_guard.lower() not in outcome["guard_reason"].lower():
            problems.append(f"expected the guard to say {expect_guard!r}, "
                            f"it said {outcome['guard_reason']!r}")

    CASES.append({
        "case": name,
        "expected": expect,
        "status": result.status,
        "lots": lots,
        "order_placed": placed,
        "guard": outcome.get("guard_reason"),
        "verdict": "PASS" if not problems else "FAIL",
        "reason": result.reason,
        "note": note,
        "problems": problems,
    })


def sweep_stop_distance(tmp: Path) -> None:
    """The channel's stop width, from tight to absurd, against a still market.

    The published entry is the market, so nothing but the stop distance and the
    lot arithmetic decide this. Anything that refuses here is refusing on
    size, not on timing.
    """
    for stop_distance in (3, 5, 8, 10, 12, 15, 18, 20, 25, 30, 32, 35, 40, 50):
        world = World(tmp / f"stop{stop_distance}")
        entry, stop = 3915.0, 3915.0 + stop_distance
        result, outcome = world.deliver(sell_call(entry, stop, 3905, 3900),
                                        market=quote(entry))
        # 0.02 is the cap; below a 15-point stop the ceiling would allow more
        # and the cap refuses to let it. Above it, the ceiling binds instead.
        if stop_distance <= 15:
            expect_lots = RISK.fixed_lots
        else:
            expect_lots = 0.01 if stop_distance <= 30 else None
        expect = "paper_intent" if stop_distance <= 30 else "blocked"
        case(f"stop {stop_distance:>2} points", expect=expect, result=result,
             outcome=outcome, expect_lots=expect_lots,
             expect_placed=expect == "paper_intent",
             note=f"${(expect_lots or 0) * stop_distance * 100:.2f} of risk")


def sweep_drift(tmp: Path) -> None:
    """The market has moved since the call was published.

    This is what the slippage guard used to refuse. The point of the sweep is
    that drift is now paid for in size, not in refusal - until the drift is so
    large that the stop no longer fits the ceiling at the smallest lot.

    The targets are deliberately far away (3885/3880). With the channel's usual
    10-point span the *first-target* gate fires at 10 points of favourable
    drift, which would hide what this sweep is measuring.
    """
    for drift in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0):
        world = World(tmp / f"drift{drift}")
        entry, stop = 3915.0, 3925.0          # a 10-point stop as published
        market = quote(entry - drift)          # price ran in the trade's favour
        result, outcome = world.deliver(sell_call(entry, stop, 3885, 3880),
                                        market=market)
        # The stop is a level, not a distance, so a lower market widens it.
        effective_stop = stop - (entry - drift)
        expect = "paper_intent" if effective_stop <= 30 else "blocked"
        case(f"price ran {drift:>4.1f} points in our favour", expect=expect,
             result=result, outcome=outcome,
             expect_placed=expect == "paper_intent",
             note=f"effective stop {effective_stop:.1f} points")

    # The other direction: the market ran against us, so the stop is nearer.
    # A tighter stop used to mean a bigger order; the cap is what stops that.
    for drift in (2.0, 5.0, 8.0):
        world = World(tmp / f"adverse{drift}")
        entry, stop = 3915.0, 3925.0
        result, outcome = world.deliver(sell_call(entry, stop, 3885, 3880),
                                        market=quote(entry + drift))
        case(f"price ran {drift:>4.1f} points against us", expect="paper_intent",
             result=result, outcome=outcome, expect_placed=True,
             expect_lots=RISK.fixed_lots,
             note=f"effective stop {stop - (entry + drift):.1f} points, "
                  "still capped at the configured size")


def failure_modes(tmp: Path) -> None:
    """The things that have actually stopped a trade, one at a time."""

    # 1. the ordinary case, so every other case has a baseline
    world = World(tmp / "baseline")
    result, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915))
    case("a plain market call", expect="paper_intent", result=result,
         outcome=outcome, expect_lots=RISK.fixed_lots, expect_placed=True)

    # 2. the trade is announced, then the broker refuses the order
    world = World(tmp / "rejected")
    world.broker.reject_place = True
    result, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915))
    case("broker rejects the order", expect="paper_intent", result=result,
         outcome=outcome, expect_placed=False,
         note="the pipeline accepted; the broker said no")

    # 3. the broker takes the order but no position ever appears
    world = World(tmp / "nofill")
    world.broker.withhold_fill = True
    result, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915))
    case("order accepted, never fills", expect="paper_intent", result=result,
         outcome=outcome, expect_placed=True,
         note=f"left in state {outcome.get('state')!r} for the "
              f"{env_number('MIRROR_FILL_TIMEOUT_S'):.0f}s fill timeout, not lost")

    # 4. the book is crossed - bid above ask - which is what blocked 10:11
    world = World(tmp / "crossed")
    result, outcome = world.deliver(sell_call(3866, 3876, 3860, 3855),
                                    market=quote(3857.65, ask=3857.46))
    case("crossed book (bid above ask)", expect="blocked", result=result,
         outcome=outcome, expect_placed=False,
         note="still a real stopper; two HTTP reads, one fast move")

    # 5. the quote is older than the 2 s budget
    world = World(tmp / "stale")
    result, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915, age_s=30))
    case("quote 30 s old", expect="blocked", result=result, outcome=outcome,
         expect_placed=False)

    # 6. the quote provider raised - the broker returned an HTML page
    world = World(tmp / "quote_error")
    result, outcome = world.deliver(
        sell_call(3915, 3925, 3905, 3900), market=None,
        quote_error="BrokerError: HTTPError: Received response: '<!doctype html>'")
    case("quote provider raised", expect="blocked", result=result,
         outcome=outcome, expect_placed=False)

    # 7. the market already passed the first target but not the last. The
    # first-target gate is gone (2026-11-17): this copy happens, and the
    # budget is what bounds it. The stop is 18 points from the market, so
    # 0.02 lots would risk $36 and the size is fitted down to 0.01.
    world = World(tmp / "past_tp1")
    result, outcome = world.deliver(sell_call(3866, 3876, 3860, 3855),
                                    market=quote(3858))
    case("market already past TP1", expect="paper_intent", result=result,
         outcome=outcome, expect_lots=0.01, expect_placed=True,
         note="the gate is gone; the $30 ceiling sizes it to 0.01")

    # 8. the market is beyond the final target
    world = World(tmp / "past_final")
    result, outcome = world.deliver(sell_call(3866, 3876, 3860, 3855),
                                    market=quote(3850))
    case("market past the final target", expect="manual_review", result=result,
         outcome=outcome, expect_placed=False,
         note="no move left to copy; a human decides rather than the bot")

    # 9. the stop is on the wrong side of the entry
    world = World(tmp / "stop_wrong_side")
    result, outcome = world.deliver(sell_call(3915, 3905, 3900),
                                    market=quote(3915))
    case("stop above a sell entry", expect="manual_review", result=result,
         outcome=outcome, expect_placed=False,
         note="a malformed call goes to a human, it is not guessed at")

    # 10. the same message delivered twice
    world = World(tmp / "duplicate")
    first, _ = world.deliver(sell_call(3915, 3925, 3905, 3900),
                             market=quote(3915), message_id=777)
    second, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915), message_id=777)
    case("the same message twice", expect="duplicate", result=second,
         outcome=outcome, expect_placed=False,
         note=f"first delivery was {first.status}")

    # 11. the channel edits the call after posting it
    world = World(tmp / "edited")
    original = sell_call(3915, 3925, 3905, 3900)
    world.deliver(original, market=quote(3915), message_id=778)
    edited, outcome = world.deliver(sell_call(3915, 3925, 3905, 3895),
                                    market=quote(3915), message_id=778,
                                    revision=1)
    case("the channel edits the call", expect="manual_review", result=edited,
         outcome=outcome, expect_placed=False,
         note="an edit is never auto-applied")

    # 12. a second signal while a position is already open
    world = World(tmp / "one_at_a_time")
    world.deliver(sell_call(3915, 3925, 3905, 3900), market=quote(3915))
    second, outcome = world.deliver(sell_call(3900, 3910, 3890, 3885),
                                    market=quote(3900))
    case("a second signal while one is open", expect="paper_intent",
         result=second, outcome=outcome, expect_placed=False,
         expect_guard="already open",
         note="the pipeline accepted it; the engine's guard refused it")

    # 13. the daily loss breaker. The breaker measures against the day's
    # opening equity, which is written the first time a signal is evaluated -
    # so a fresh deployment has nothing to measure against and cannot trip it.
    # Seeding it here is what the live process does on its first signal.
    world = World(tmp / "daily_loss", equity=RISK.equity)
    world.deliver(sell_call(3915, 3925, 3905, 3900), market=quote(3915))
    world.broker.account_equity = RISK.equity * (1 - LIMITS.daily_loss_fraction)
    result, outcome = world.deliver(sell_call(3900, 3910, 3890, 3885),
                                    market=quote(3900))
    case("the day's loss limit is reached", expect="paper_intent", result=result,
         outcome=outcome, expect_placed=False,
         expect_guard="day's loss",
         note=f"${RISK.equity * LIMITS.daily_loss_fraction:.2f} down on the day")

    # 14. the drawdown floor, measured from the peak not the day
    world = World(tmp / "drawdown", equity=RISK.equity)
    world.deliver(sell_call(3915, 3925, 3905, 3900), market=quote(3915))
    world.broker.account_equity = RISK.equity * (1 - LIMITS.max_drawdown_fraction)
    result, outcome = world.deliver(sell_call(3900, 3910, 3890, 3885),
                                    market=quote(3900))
    case("the drawdown floor is reached", expect="paper_intent", result=result,
         outcome=outcome, expect_placed=False,
         expect_guard="drawdown floor",
         note=f"${RISK.equity * LIMITS.max_drawdown_fraction:.2f} below the peak")

    # 15. the pause switch
    world = World(tmp / "paused")
    world.engine.pause(reason="simulation", changed_by="harness")
    result, outcome = world.deliver(sell_call(3915, 3925, 3905, 3900),
                                    market=quote(3915))
    case("the pause switch is on", expect="paper_intent", result=result,
         outcome=outcome, expect_placed=False, expect_guard="paused",
         note="the pipeline has no opinion; the engine refuses to place")

    # 16. a LIMIT call, which policy excludes
    world = World(tmp / "limit")
    text = sell_call(3915, 3925, 3905, 3900).replace("NOW", "LIMIT")
    result, outcome = world.deliver(text, market=quote(3915))
    case("a LIMIT call", expect="paper_intent", result=result, outcome=outcome,
         expect_placed=False, note="recorded as not copied, never sent")

    # 17. a buy, so the direction logic is exercised too
    world = World(tmp / "buy")
    result, outcome = world.deliver(buy_call(3915, 3905, 3925, 3930),
                                    market=quote(3915, ask=3915.2))
    case("a plain buy call", expect="paper_intent", result=result,
         outcome=outcome, expect_lots=RISK.fixed_lots, expect_placed=True)

    # 18. an unparseable post
    world = World(tmp / "noise")
    result, outcome = world.deliver("Gold looking heavy into the NY open",
                                    market=quote(3915))
    case("a non-signal post", expect="ignored", result=result,
         outcome=outcome, expect_placed=False)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def render(cases: list[dict]) -> str:
    width = max(len(c["case"]) for c in cases)
    lines = [
        f"live config: cap {RISK.fixed_lots:g} lots | ceiling "
        f"${RISK.max_trade_risk_usd:.0f} | quote {RISK.quote_max_age_seconds:g}s | "
        f"contract {INSTRUMENT.contract_units_per_lot:g} oz/lot | "
        f"slippage {LIMITS.max_slippage:g}",
        "",
        f"{'case'.ljust(width)}  {'status':<14} {'lots':>5} {'order':>5}  verdict",
        "-" * (width + 42),
    ]
    for c in cases:
        lots = "-" if c["lots"] is None else f"{c['lots']:g}"
        placed = "yes" if c["order_placed"] else "no"
        lines.append(f"{c['case'].ljust(width)}  {c['status']:<14} "
                     f"{lots:>5} {placed:>5}  {c['verdict']}")
    failed = [c for c in cases if c["verdict"] != "PASS"]
    lines.append("")
    lines.append(f"{len(cases) - len(failed)}/{len(cases)} cases behaved as expected")
    for c in failed:
        lines.append(f"  FAIL {c['case']}: {'; '.join(c['problems'])}")
    return "\n".join(lines)


def replay_archive(db: Path, *, limit: int | None = None) -> dict:
    """Every archived message in the real channel, priced at its own entry.

    The synthetic cases above cover the shapes I thought of. This covers the
    ones I did not: 15,000 real posts, with the market assumed to sit exactly
    on the published entry. That assumption is optimistic - in reality the
    market has moved - but it is the right one for isolating the question
    "would this call be copied at all", because it removes timing from the
    answer and leaves parsing, geometry and sizing.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT message_id, posted_at, text FROM raw_telegram_messages "
        "ORDER BY message_id" + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    con.close()

    tally: dict[str, int] = {}
    reasons: dict[str, int] = {}
    lots: dict[float, int] = {}

    for message_id, posted_at, text in rows:
        posted = datetime.fromisoformat(posted_at)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        envelope = MessageEnvelope(
            channel=CHANNEL, message_id=int(message_id), revision=0,
            posted_at=posted, received_at=posted, text=text or "",
            source="archived_channel",
        )
        # The market sits on the published entry, with a typical 0.2 spread.
        parsed = parse_message_signal(text or "")
        if parsed and parsed.get("entry_price"):
            entry = float(parsed["entry_price"])
            market = MarketQuote(entry, entry + 0.2, posted)
        else:
            market = MarketQuote(3900.0, 3900.2, posted)

        outcome = evaluate_message(envelope, instrument=INSTRUMENT, risk=RISK,
                                   quote=market)
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
        if outcome.status != "paper_intent":
            # Group by the first clause so 8,000 variants of one sentence do
            # not each get their own row.
            head = outcome.reason.split(";")[0].split("(")[0].strip()[:70]
            reasons[head] = reasons.get(head, 0) + 1
        else:
            size = lots_of(outcome)
            lots[size] = lots.get(size, 0) + 1

    total = sum(tally.values())
    return {
        "messages": total,
        "statuses": dict(sorted(tally.items(), key=lambda kv: -kv[1])),
        "refusal_reasons": dict(Counter(reasons).most_common(12)),
        "sizes": {str(k): v for k, v in sorted(lots.items(), key=lambda kv: -kv[1])},
        "copied_share": round(tally.get("paper_intent", 0) / total, 4) if total else 0.0,
    }


def render_archive(report: dict) -> str:
    lines = [f"archived channel replay: {report['messages']} messages, "
             f"{report['copied_share']:.1%} would be copied", ""]
    for status, count in report["statuses"].items():
        lines.append(f"  {status:<14} {count:>6}")
    if report["refusal_reasons"]:
        lines.append("\n  why the rest were not copied")
        for reason, count in report["refusal_reasons"].items():
            lines.append(f"    {count:>6}  {reason}")
    if report["sizes"]:
        lines.append("\n  sizes among the copied")
        for size, count in report["sizes"].items():
            lines.append(f"    {count:>6}  {size} lots")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--verbose", action="store_true",
                    help="print each case's refusal reason")
    ap.add_argument("--archive", default=None,
                    help="replay every message in this research database "
                         "(raw_telegram_messages) instead of the synthetic cases")
    ap.add_argument("--archive-limit", type=int, default=None)
    args = ap.parse_args()

    if args.archive:
        report = replay_archive(Path(args.archive), limit=args.archive_limit)
        print(render_archive(report))
        if args.json_out:
            out = Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\nwrote {out}")
        return 0

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        sweep_stop_distance(tmp)
        sweep_drift(tmp)
        failure_modes(tmp)

    print(render(CASES))
    if args.verbose:
        print("\nreasons")
        for c in CASES:
            print(f"  {c['case']}: {c['reason']}")

    if args.json_out:
        payload = {
            "mode": "simulation",
            "config": {
                "fixed_lots": RISK.fixed_lots,
                "max_trade_risk_usd": RISK.max_trade_risk_usd,
                "quote_max_age_s": RISK.quote_max_age_seconds,
                "signal_max_age_s": RISK.signal_max_age_seconds,
                "equity": RISK.equity,
                "contract_units_per_lot": INSTRUMENT.contract_units_per_lot,
                "lot_step": INSTRUMENT.lot_step,
                "min_lot": INSTRUMENT.min_lot,
                "limits": LIMITS.describe(),
                "source": "render.yaml (the deployed blueprint)",
            },
            "cases": CASES,
            "summary": {
                "total": len(CASES),
                "passed": sum(1 for c in CASES if c["verdict"] == "PASS"),
                "placed": sum(1 for c in CASES if c["order_placed"]),
            },
        }
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")

    return 0 if all(c["verdict"] == "PASS" for c in CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
