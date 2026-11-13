"""The execution engine: turns a validated signal into a managed demo trade.

The engine is written against the :class:`rade.execution.broker.Broker`
contract, not against TradeLocker, so it can be driven by a double in tests
and only then by the demo account.

The rules it enforces, each of which exists because the opposite behaviour
would be invisible:

* **Never resubmit.** Before an order is sent, the engine asks the broker
  whether an order with this trade's correlation tag already exists. A
  restart, a retry, or a duplicate delivery therefore reconciles instead of
  placing a second order.
* **"Copied" is never said before the broker confirms.** A trade passes
  through submitted → accepted → filled, and only the last of those means the
  position exists.
* **The broker-side stop and final target are attached at entry**, so a
  monitoring outage cannot leave the position naked.
* **A stop only moves in the risk-reducing direction**, and every move is
  verified against the broker before it is recorded as applied.
* **An unconfirmed broker specification blocks the order.** No size is
  computed from a value nobody checked.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from rade.execution.broker import (
    Broker,
    BrokerError,
    BrokerPosition,
    BrokerRefused,
    BrokerSpec,
    OrderAck,
)
from rade.execution.guards import (
    GuardVerdict,
    RiskLimits,
    RiskSnapshot,
    RiskState,
    evaluate as evaluate_guards,
    utc_day,
)
from rade.execution.ladder import (
    STOP_NONE,
    ExitLeg,
    ExitPlan,
    LadderError,
    build_exit_plan,
    stop_move_is_risk_reducing,
    target_reached,
)
from rade.observer.mirror_pipeline import (
    STATUS_MANUAL_REVIEW,
    STATUS_PAPER_INTENT,
    MessageEnvelope,
    PipelineResult,
    as_utc,
    event_key,
    text_hash,
)

#: Trade states. ``open`` and ``partial`` are the ones a manager acts on.
STATE_VALIDATED = "validated"
STATE_NOT_COPIED = "not_copied"
STATE_BLOCKED = "blocked"
STATE_SUBMITTED = "submitted"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_OPEN = "open"
STATE_PARTIAL = "partial"
STATE_CLOSED = "closed"
STATE_REVIEW = "review"

#: States that still need managing or reconciling.
ACTIVE_STATES = (STATE_VALIDATED, STATE_SUBMITTED, STATE_ACCEPTED, STATE_OPEN, STATE_PARTIAL)

#: The correlation tag length the broker accepts. The documented limit is 31
#: characters; the SDK rejects anything longer.
MAX_STRATEGY_ID_LEN = 31


class ExecutionError(RuntimeError):
    """The engine could not carry out a step. The reason is recorded."""


class ExecutionLedger(Protocol):
    """Storage for trades and their state transitions."""

    def open_trade(self, trade: dict) -> str:
        ...

    def update_trade(self, trade: dict) -> bool:
        ...

    def get_trade(self, trade_id: str) -> dict | None:
        ...

    def list_trades(self, *, states: tuple[str, ...] | None = None) -> list[dict]:
        ...

    def record_transition(self, transition: dict) -> None:
        ...

    def transitions(self, trade_id: str) -> list[dict]:
        ...


def trade_id_for(event_key: str) -> str:
    """The correlation tag for a trade.

    It is derived from the event key, so it is the same on every run and on
    every host: a restart recomputes it without needing the database, and the
    broker's own order history can be searched for it.
    """
    digest = (event_key or "").strip().lower()
    if len(digest) < 24:
        digest = f"{digest:0<24}"
    tag = f"RADE-{digest[:24]}"
    return tag[:MAX_STRATEGY_ID_LEN]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ManagedTrade:
    """One trade, in memory, shaped like the ledger's row."""

    trade_id: str
    event_key: str
    channel: str
    message_id: int
    revision: int
    symbol: str
    side: str
    size: float
    entry: float
    stop: float
    targets: list[float]
    plan: ExitPlan
    state: str = STATE_VALIDATED
    legs_done: int = 0
    current_stop: float | None = None
    broker_order_id: int | None = None
    broker_position_id: int | None = None
    fill_price: float | None = None
    management_stopped: bool = False
    note: str = ""
    #: The hash of the message text this trade came from. Two message ids can
    #: carry identical text, and the hash is what makes that visible.
    text_sha256: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_row(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "event_key": self.event_key,
            "channel": self.channel,
            "message_id": int(self.message_id),
            "revision": int(self.revision),
            "symbol": self.symbol,
            "side": self.side,
            "size": float(self.size),
            "entry": float(self.entry),
            "stop": float(self.stop),
            "current_stop": self.current_stop,
            "targets": list(self.targets),
            "plan": {
                "policy": self.plan.policy,
                "final_target": self.plan.final_target,
                "ignored_targets": list(self.plan.ignored_targets),
                "lot_step": self.plan.lot_step,
                "min_lot": self.plan.min_lot,
                "legs": [
                    {
                        "label": leg.label,
                        "target": leg.target,
                        "fraction": leg.fraction,
                        "quantity": leg.quantity,
                        "managed": leg.managed,
                        "stop_after_kind": leg.stop_after_kind,
                        "stop_after_value": leg.stop_after_value,
                        "expected_remaining_after": leg.expected_remaining_after,
                    }
                    for leg in self.plan.legs
                ],
            },
            "state": self.state,
            "legs_done": int(self.legs_done),
            "broker_order_id": self.broker_order_id,
            "broker_position_id": self.broker_position_id,
            "fill_price": self.fill_price,
            "management_stopped": bool(self.management_stopped),
            "note": self.note,
            "text_sha256": self.text_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> "ManagedTrade | None":
        try:
            plan_blob = row.get("plan") or row.get("plan_json") or {}
            if isinstance(plan_blob, str):
                plan_blob = json.loads(plan_blob)
            legs = []
            for leg in plan_blob.get("legs", []):
                legs.append(ExitLeg(
                    label=leg["label"],
                    target=float(leg["target"]),
                    fraction=float(leg["fraction"]),
                    quantity=float(leg["quantity"]),
                    managed=bool(leg["managed"]),
                    stop_after_kind=leg.get("stop_after_kind", "none"),
                    stop_after_value=leg.get("stop_after_value"),
                    expected_remaining_after=float(leg.get("expected_remaining_after", 0.0)),
                ))
            plan = ExitPlan(
                policy=str(plan_blob.get("policy", "")),
                side=str(row["side"]),
                entry=float(row["entry"]),
                initial_stop=float(row["stop"]),
                final_target=float(plan_blob.get("final_target", 0.0)),
                total_size=float(row["size"]),
                lot_step=float(row.get("lot_step", 0.01)),
                min_lot=float(row.get("min_lot", 0.01)),
                legs=tuple(legs),
                ignored_targets=tuple(float(t) for t in plan_blob.get("ignored_targets", [])),
            )
            targets = row.get("targets") or []
            if isinstance(targets, str):
                targets = json.loads(targets)
            return cls(
                trade_id=str(row["trade_id"]),
                event_key=str(row["event_key"]),
                channel=str(row.get("channel", "")),
                message_id=int(row.get("message_id", 0) or 0),
                revision=int(row.get("revision", 0) or 0),
                symbol=str(row.get("symbol", "")),
                side=str(row["side"]),
                size=float(row["size"]),
                entry=float(row["entry"]),
                stop=float(row["stop"]),
                targets=[float(t) for t in targets],
                plan=plan,
                state=str(row.get("state", STATE_VALIDATED)),
                legs_done=int(row.get("legs_done", 0) or 0),
                current_stop=(float(row["current_stop"])
                              if row.get("current_stop") is not None else None),
                broker_order_id=(int(row["broker_order_id"])
                                 if row.get("broker_order_id") is not None else None),
                broker_position_id=(int(row["broker_position_id"])
                                    if row.get("broker_position_id") is not None else None),
                fill_price=(float(row["fill_price"])
                            if row.get("fill_price") is not None else None),
                management_stopped=bool(row.get("management_stopped")),
                note=str(row.get("note", "") or ""),
                text_sha256=str(row.get("text_sha256", "") or ""),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


class ExecutionEngine:
    """Submits, manages and reconciles demo trades."""

    def __init__(self, *, broker: Broker, ledger: ExecutionLedger,
                 notifier: Any = None,
                 log: Callable[[str], None] = print,
                 manage_every_s: float = 5.0,
                 fill_timeout_s: float = 300.0,
                 max_stop_attempts: int = 10,
                 limits: RiskLimits | None = None,
                 duplicate_window_s: float = 900.0):
        self.broker = broker
        self.ledger = ledger
        self.notifier = notifier
        self.log = log
        self.manage_every_s = float(manage_every_s)
        #: How long an accepted order may go without a position before the
        #: trade is sent to review instead of being managed as if it filled.
        self.fill_timeout_s = float(fill_timeout_s)
        #: How many times a stop move may be refused before a human is asked.
        #: A refusal caused by the price moving is transient and is retried;
        #: one that persists is not.
        self.max_stop_attempts = int(max_stop_attempts)
        #: The pre-trade limits. They gate new orders only; an open trade is
        #: always still managed, because abandoning open risk is worse than
        #: the loss the breaker is trying to cap.
        self.limits = limits or RiskLimits()
        #: The verdict of the most recent guard evaluation, for the status page.
        self._last_guard: GuardVerdict | None = None
        #: How far back to look for the same signal text posted again. The
        #: channel re-posts calls as separate messages, so the id-based
        #: uniqueness constraint cannot catch it. 0 disables the check.
        self.duplicate_window_s = float(duplicate_window_s)

    def guard_state(self) -> dict:
        """What the pre-trade guards are enforcing, and the last verdict.

        ``limits.describe()`` lists the checks that are switched off as well as
        the ones in force, so a disabled control cannot read as a passing one.
        """
        return {
            "limits": self.limits.describe(),
            "last_verdict": (self._last_guard.to_dict()
                             if self._last_guard is not None else None),
        }

    def pause(self, *, reason: str, changed_by: str) -> dict:
        """Throw the switch. Writes the shared row, not a local flag."""
        return self.ledger.set_paused(paused=True, reason=reason,
                                      changed_by=changed_by)

    def resume(self, *, reason: str, changed_by: str) -> dict:
        return self.ledger.set_paused(paused=False, reason=reason,
                                      changed_by=changed_by)

    def pause_state(self) -> dict:
        try:
            control = self.ledger.control()
        except Exception as exc:
            return {"paused": None, "reason": f"unreadable: {type(exc).__name__}",
                    "changed_at": None, "changed_by": ""}
        if control is None:
            return {"paused": False, "reason": "the pause switch has never been set",
                    "changed_at": None, "changed_by": ""}
        return control

    # -- helpers ----------------------------------------------------------

    def _fill_window_expired(self, trade: ManagedTrade) -> bool:
        try:
            created = datetime.fromisoformat(str(trade.created_at))
        except (TypeError, ValueError):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        return age > self.fill_timeout_s

    def _notify(self, trade_id: str, kind: str, text: str) -> None:
        """Send one lifecycle notification, recording the outcome."""
        if self.notifier is None:
            return
        try:
            from rade.observer.notify import notify_event

            notify_event(trade_id, kind, text, self.notifier, ledger=self.ledger)
        except Exception as exc:  # a notification failure must not stop trading
            self.log(f"notification failed for {trade_id}: {type(exc).__name__}")

    def _transition(self, trade: ManagedTrade, stage: str, status: str,
                    detail: str = "", *, payload: dict | None = None) -> None:
        """Record one state transition."""
        self.ledger.record_transition({
            "trade_id": trade.trade_id,
            "event_key": trade.event_key,
            "stage": stage,
            "status": status,
            "detail": detail,
            "broker_order_id": trade.broker_order_id,
            "broker_position_id": trade.broker_position_id,
            "message_id": trade.message_id,
            "revision": trade.revision,
            "payload": payload or {},
            "created_at": _now_iso(),
        })

    def _save(self, trade: ManagedTrade) -> None:
        trade.updated_at = _now_iso()
        self.ledger.update_trade(trade.to_row())

    def _blocked(self, trade: ManagedTrade, stage: str, reason: str) -> ManagedTrade:
        trade.state = STATE_BLOCKED
        trade.note = reason
        self._transition(trade, stage, STATE_BLOCKED, reason)
        self._save(trade)
        self._notify(trade.trade_id, "blocked", f"[BLOCKED] {reason}")
        return trade

    # -- entry ------------------------------------------------------------

    def execute(self, result: PipelineResult, message: MessageEnvelope) -> dict:
        """Act on a validated signal.

        Only a market call is ever sent. A LIMIT call is recorded as *not
        copied*, because the current policy excludes LIMIT orders.
        """
        trade_id = trade_id_for(result.event_key)

        if result.status != STATUS_PAPER_INTENT:
            # Blocked, skipped, or sent to a human. Nothing is sent to the
            # broker and nothing is recorded as a trade.
            return {"executed": False, "reason": f"signal status {result.status}"}

        intent = result.order_intent or {}
        signal = result.signal or {}
        entry_type = str(signal.get("entry_type") or intent.get("type") or "")
        if entry_type != "market":
            return self._not_copied(result, message, trade_id,
                                    "LIMIT orders excluded by current policy")

        # The channel re-posts the same call as a second message with its own
        # id, and the database's uniqueness constraint is keyed on the id, so
        # it cannot see that the two are one idea. Copying both would double
        # the risk on a single view of the market.
        repeated = self._same_signal_recently(message, trade_id)
        if repeated is not None:
            reason = (f"an identical signal was already copied as trade "
                      f"{repeated.get('trade_id')} (message "
                      f"{repeated.get('message_id')}); this is the same call "
                      "posted again, so it is not copied twice")
            return self._not_copied(result, message, trade_id, reason)

        spec = self.broker.spec()
        if not spec.verified:
            return self._blocked_new(result, message, trade_id, spec.unconfirmed_reason or
                                      "the broker specification is not verified")

        side = str(signal.get("side") or "")
        size = float(intent.get("size_lots") or 0.0)
        entry = float(signal.get("published_entry") or 0.0)
        stop = float(signal.get("stop_loss") or 0.0)
        targets = [float(t) for t in (signal.get("take_profits") or [])]

        try:
            plan = build_exit_plan(side=side, entry=entry, stop=stop, targets=targets,
                                    size=size, lot_step=spec.lot_step, min_lot=spec.min_lot)
        except LadderError as exc:
            return self._blocked_new(result, message, trade_id, str(exc))

        trade = ManagedTrade(
            trade_id=trade_id,
            event_key=result.event_key,
            channel=message.channel,
            message_id=int(message.message_id),
            revision=int(message.revision),
            symbol=spec.symbol,
            side=side,
            size=size,
            entry=entry,
            stop=stop,
            targets=targets,
            plan=plan,
            current_stop=stop,
            state=STATE_VALIDATED,
            text_sha256=text_hash(message.text),
        )
        outcome = self.ledger.open_trade(trade.to_row())
        if outcome != "inserted":
            # The trade is already known. Reconcile instead of resubmitting.
            existing = self.ledger.get_trade(trade_id)
            self.log(f"trade {trade_id} already exists; reconciling instead of resubmitting")
            if existing:
                trade = ManagedTrade.from_row(existing) or trade
            return self.reconcile_trade(trade)

        self._transition(trade, "validated", "validated",
                         f"ladder: {plan.description()}",
                         payload={"plan": trade.to_row()["plan"]})
        self._save(trade)

        # The per-trade cap says how much this signal may lose. It says nothing
        # about the day, the account, or whether the market has moved since the
        # channel posted. Those are asked here, immediately before the order,
        # against an equity read at this moment rather than at startup.
        blocked = self._pre_trade_check(trade)
        if blocked is not None:
            return blocked

        self._notify(trade.trade_id, "signal",
                     f"[SIGNAL] {side.upper()} {spec.symbol} market {size:g} lot; "
                     f"SL {stop:g}, final TP {plan.final_target:g}. "
                     f"Plan: {plan.description()}")

        return self.submit(trade)

    def _same_signal_recently(self, message: MessageEnvelope,
                              trade_id: str) -> dict | None:
        """A trade already opened from identical text, inside the window.

        Fails closed in the direction that matters: if the lookup itself
        fails, the signal is treated as a repeat and not copied, because the
        alternative is placing an order the guard could not check.
        """
        if self.duplicate_window_s <= 0:
            return None
        since = (datetime.now(timezone.utc)
                 - timedelta(seconds=self.duplicate_window_s)).isoformat()
        try:
            found = self.ledger.same_text_recently(
                text_hash(message.text), since=since, exclude_trade_id=trade_id)
        except Exception as exc:
            self.log(f"could not check for a repeated signal: "
                     f"{type(exc).__name__}: {exc}")
            return {"trade_id": "unknown", "message_id": "unknown",
                    "lookup_failed": True}
        return found

    def _pre_trade_check(self, trade: ManagedTrade) -> dict | None:
        """Run the account-level guards. Returns an outcome when blocked.

        Every read here fails closed. An equity that cannot be read, a risk
        state that cannot be persisted, or a pause switch that cannot be
        fetched all mean "do not place this order", with the reason recorded
        against the trade so the dashboard says why instead of going quiet.
        """
        try:
            quote = self.broker.quote()
        except Exception as exc:
            return self._guard_blocked(
                trade, f"the quote could not be read: {type(exc).__name__}: {exc}")

        try:
            equity = float(self.broker.equity())
        except Exception as exc:
            return self._guard_blocked(
                trade, "the account equity could not be read, so no size can be "
                       f"computed safely: {type(exc).__name__}: {exc}")

        try:
            stored = RiskState.from_dict(self.ledger.risk_state())
            state = stored or RiskState(day=utc_day(), day_start_equity=equity,
                                        peak_equity=equity)
            state = state.rolled(equity)
            self.ledger.save_risk_state(**state.to_dict())
        except Exception as exc:
            return self._guard_blocked(
                trade, "the daily-loss state could not be read or saved, so the "
                       f"breaker cannot be evaluated: {type(exc).__name__}: {exc}")

        try:
            control = self.ledger.control()
        except Exception as exc:
            return self._guard_blocked(
                trade, "the pause switch could not be read, so execution is "
                       f"treated as paused: {type(exc).__name__}: {exc}")

        paused: bool | None
        pause_reason = ""
        if control is None:
            # No row yet is a known absence, not an unknown: the switch has
            # never been thrown. It is still reported, not assumed silently.
            paused, pause_reason = False, "the pause switch has never been set"
        else:
            paused = bool(control.get("paused"))
            pause_reason = str(control.get("reason") or "")

        spread = float(quote.ask) - float(quote.bid)
        reference = float(quote.ask) if trade.side == "buy" else float(quote.bid)
        slippage = abs(reference - float(trade.entry)) if trade.entry else 0.0

        try:
            others = [row for row in self.ledger.list_trades(states=ACTIVE_STATES)
                      if row.get("trade_id") != trade.trade_id]
        except Exception as exc:
            return self._guard_blocked(
                trade, "the open-position count could not be read: "
                       f"{type(exc).__name__}: {exc}")

        snapshot = RiskSnapshot(
            equity=equity,
            day_start_equity=state.day_start_equity,
            peak_equity=state.peak_equity,
            open_positions=len(others),
            spread=spread,
            slippage=slippage,
            paused=paused,
            pause_reason=pause_reason,
        )
        verdict: GuardVerdict = evaluate_guards(self.limits, snapshot)
        self._last_guard = verdict
        if verdict.allowed:
            self._transition(trade, "risk", "guard_passed", verdict.reason,
                             payload={"snapshot": snapshot.__dict__,
                                      "checks": verdict.checks})
            self._save(trade)
            return None
        return self._guard_blocked(trade, verdict.reason, verdict=verdict,
                                   snapshot=snapshot)

    def _guard_blocked(self, trade: ManagedTrade, reason: str, *,
                       verdict: GuardVerdict | None = None,
                       snapshot: RiskSnapshot | None = None) -> dict:
        """Record a refusal by the account-level guards. Nothing is sent."""
        trade.state = STATE_BLOCKED
        trade.note = reason
        self._transition(trade, "risk", "guard_blocked", reason,
                         payload={"snapshot": getattr(snapshot, "__dict__", None),
                                  "checks": getattr(verdict, "checks", None)})
        self._save(trade)
        self._notify(trade.trade_id, "blocked", f"[BLOCKED] {reason}")
        return {"executed": False, "trade_id": trade.trade_id, "reason": reason,
                "state": STATE_BLOCKED, "guard": True}

    def _not_copied(self, result: PipelineResult, message: MessageEnvelope,
                    trade_id: str, reason: str) -> dict:
        trade = ManagedTrade(
            trade_id=trade_id,
            event_key=result.event_key,
            channel=message.channel,
            message_id=int(message.message_id),
            revision=int(message.revision),
            symbol=str((result.signal or {}).get("instrument", "")),
            side=str((result.signal or {}).get("side", "")),
            size=0.0,
            entry=float((result.signal or {}).get("published_entry") or 0.0),
            stop=float((result.signal or {}).get("stop_loss") or 0.0),
            targets=[float(t) for t in ((result.signal or {}).get("take_profits") or [])],
            plan=ExitPlan(policy="none", side="", entry=0.0, initial_stop=0.0,
                          final_target=0.0, total_size=0.0, lot_step=0.01, min_lot=0.01),
            state=STATE_NOT_COPIED,
            note=reason,
        )
        self.ledger.open_trade(trade.to_row())
        self._transition(trade, "policy", STATE_NOT_COPIED, reason)
        self._save(trade)
        self._notify(trade.trade_id, "not_copied", f"[NOT COPIED] {reason}")
        return {"executed": False, "trade_id": trade_id, "reason": reason,
                "state": STATE_NOT_COPIED}

    def _blocked_new(self, result: PipelineResult, message: MessageEnvelope,
                     trade_id: str, reason: str) -> dict:
        trade = ManagedTrade(
            trade_id=trade_id,
            event_key=result.event_key,
            channel=message.channel,
            message_id=int(message.message_id),
            revision=int(message.revision),
            symbol=str((result.signal or {}).get("instrument", "")),
            side=str((result.signal or {}).get("side", "")),
            size=0.0,
            entry=float((result.signal or {}).get("published_entry") or 0.0),
            stop=float((result.signal or {}).get("stop_loss") or 0.0),
            targets=[float(t) for t in ((result.signal or {}).get("take_profits") or [])],
            plan=ExitPlan(policy="none", side="", entry=0.0, initial_stop=0.0,
                          final_target=0.0, total_size=0.0, lot_step=0.01, min_lot=0.01),
            state=STATE_BLOCKED,
            note=reason,
        )
        self.ledger.open_trade(trade.to_row())
        self._transition(trade, "spec", STATE_BLOCKED, reason)
        self._save(trade)
        self._notify(trade.trade_id, "blocked", f"[BLOCKED] {reason}")
        return {"executed": False, "trade_id": trade_id, "reason": reason,
                "state": STATE_BLOCKED}

    def submit(self, trade: ManagedTrade) -> dict:
        """Send the entry order, unless the broker already knows about it."""
        existing = self._existing_order(trade.trade_id)
        if existing is not None:
            self.log(f"trade {trade.trade_id}: the broker already has order "
                     f"{existing.id}; not resubmitting")
            trade.broker_order_id = existing.id
            self._transition(trade, "reconcile", "already_submitted",
                             f"order {existing.id} already exists at the broker")
            self._save(trade)
            return self.reconcile_trade(trade)

        trade.state = STATE_SUBMITTED
        self._transition(trade, "submit", STATE_SUBMITTED,
                         f"market {trade.size:g} lot {trade.side} with SL "
                         f"{trade.stop:g} and TP {trade.plan.final_target:g}")
        self._save(trade)

        try:
            ack: OrderAck = self.broker.place_market(
                qty=trade.size,
                side=trade.side,
                stop_loss=trade.stop,
                take_profit=trade.plan.final_target,
                strategy_id=trade.trade_id,
            )
        except (BrokerRefused, BrokerError) as exc:
            trade.state = STATE_REJECTED
            trade.note = str(exc)
            self._transition(trade, "submit", STATE_REJECTED, str(exc))
            self._save(trade)
            self._notify(trade.trade_id, "rejected", f"[REJECTED] {exc}")
            return {"executed": False, "trade_id": trade.trade_id, "reason": str(exc),
                    "state": STATE_REJECTED}

        if not ack.accepted or ack.order_id is None:
            trade.state = STATE_REJECTED
            trade.note = ack.detail
            self._transition(trade, "submit", STATE_REJECTED, ack.detail,
                             payload={"request": ack.request})
            self._save(trade)
            self._notify(trade.trade_id, "rejected", f"[REJECTED] {ack.detail}")
            return {"executed": False, "trade_id": trade.trade_id, "reason": ack.detail,
                    "state": STATE_REJECTED}

        trade.broker_order_id = ack.order_id
        trade.state = STATE_ACCEPTED
        self._transition(trade, "accept", STATE_ACCEPTED,
                         f"the broker accepted order {ack.order_id}",
                         payload={"request": ack.request})
        self._save(trade)
        self._notify(trade.trade_id, "accepted",
                     f"[ACCEPTED] TradeLocker (demo) accepted order {ack.order_id}: "
                     f"{trade.side.upper()} {trade.symbol} {trade.size:g} lot, "
                     f"SL {trade.stop:g}, TP {trade.plan.final_target:g}. "
                     "Awaiting the fill confirmation.")
        return {"executed": True, "trade_id": trade.trade_id, "order_id": ack.order_id,
                "state": trade.state}

    def _existing_order(self, trade_id: str):
        """The broker's order for this trade, if it already has one."""
        try:
            orders = self.broker.orders(history=True)
        except BrokerError as exc:
            self.log(f"could not read the broker's orders: {exc}")
            return None
        for order in orders:
            if order.strategy_id == trade_id:
                return order
        return None

    def _position_for(self, trade_id: str) -> BrokerPosition | None:
        try:
            positions = self.broker.positions()
        except BrokerError as exc:
            self.log(f"could not read the broker's positions: {exc}")
            return None
        for position in positions:
            if position.strategy_id == trade_id:
                return position
        return None

    # -- fill confirmation -------------------------------------------------

    def confirm_fill(self, trade: ManagedTrade) -> dict:
        """Record the fill once the broker shows the position.

        The position, not the order, is the proof: an accepted order that
        never filled is not a trade.
        """
        position = self._position_for(trade.trade_id)
        if position is None:
            order = self._existing_order(trade.trade_id)
            if order is not None and order.is_filled and order.avg_price:
                trade.fill_price = float(order.avg_price)
                trade.broker_position_id = order.position_id
            else:
                return {"filled": False, "trade_id": trade.trade_id}
        else:
            trade.fill_price = float(position.avg_price) or trade.entry
            trade.broker_position_id = position.id

        trade.state = STATE_OPEN
        self._transition(trade, "fill", STATE_OPEN,
                         f"position {trade.broker_position_id} open at {trade.fill_price:g}",
                         payload={"position_id": trade.broker_position_id,
                                  "fill_price": trade.fill_price})
        self._save(trade)
        self._notify(trade.trade_id, "filled",
                     f"[FILLED] TradeLocker (demo) confirms position "
                     f"{trade.broker_position_id}: {trade.side.upper()} "
                     f"{trade.symbol} {trade.size:g} lot at {trade.fill_price:g}. "
                     f"Broker stop {trade.stop:g}, broker target "
                     f"{trade.plan.final_target:g}. Plan: {trade.plan.description()}")

        # The plan was built from the published entry, and the fill can be
        # worse than that. If the fill already sits at or beyond the first
        # target, the ladder as published cannot be walked: the "stop to TP1"
        # move would come out *behind* breakeven and would have to widen the
        # risk. That is not a reason to rewrite the policy, so the trade is
        # flagged and left protected by the stop and target at the broker.
        blocked = self._fill_beyond_first_target(trade)
        if blocked is not None:
            return blocked

        return {"filled": True, "trade_id": trade.trade_id,
                "position_id": trade.broker_position_id}

    def _fill_beyond_first_target(self, trade: ManagedTrade) -> dict | None:
        """Refuse a ladder whose first target the fill already passed."""
        if not trade.plan.legs or trade.fill_price is None:
            return None
        first = trade.plan.legs[0].target
        passed = (trade.fill_price >= first if trade.side == "buy"
                  else trade.fill_price <= first)
        if not passed:
            return None
        slipped = abs(trade.fill_price - (trade.entry or trade.fill_price))
        return self._stop_management(
            trade,
            f"the fill at {trade.fill_price:g} is already at or beyond the first "
            f"target {first:g} (published entry {trade.entry:g}, slippage "
            f"{slipped:g}); the published ladder cannot be walked as written, so "
            "automatic management did not start. The broker-side stop and target "
            "are still in place")

    # -- management --------------------------------------------------------

    def manage(self) -> dict:
        """Run one management pass over every open trade."""
        summary = {"considered": 0, "closed": 0, "partial": 0, "stopped": 0,
                   "pending_fill": 0, "errors": 0}
        for row in self.ledger.list_trades(states=(STATE_ACCEPTED, STATE_OPEN, STATE_PARTIAL)):
            trade = ManagedTrade.from_row(row)
            if trade is None:
                continue
            summary["considered"] += 1
            try:
                if trade.state == STATE_ACCEPTED:
                    filled = self.confirm_fill(trade)
                    if not filled.get("filled"):
                        summary["pending_fill"] += 1
                        continue
                    trade = ManagedTrade.from_row(self.ledger.get_trade(trade.trade_id)) or trade
                outcome = self.manage_trade(trade)
            except Exception as exc:
                summary["errors"] += 1
                self.log(f"management error for {trade.trade_id}: "
                         f"{type(exc).__name__}: {exc}")
                continue
            if outcome.get("closed"):
                summary["closed"] += 1
            elif outcome.get("partial"):
                summary["partial"] += 1
            elif outcome.get("stopped"):
                summary["stopped"] += 1
        return summary

    def manage_trade(self, trade: ManagedTrade) -> dict:
        """Advance one open trade by at most one ladder step."""
        if trade.management_stopped:
            return {"stopped": True, "trade_id": trade.trade_id}

        # A pause stops management as well as entries. An operator who pauses
        # is intervening by hand, and having this loop move stops underneath
        # them is the opposite of what they asked for. The position is still
        # protected by the stop and target attached at the broker.
        try:
            control = self.ledger.control()
        except Exception as exc:
            # Unlike an entry, management is risk-reducing work, so a control
            # read that fails does not stop it. The failure is logged instead.
            self.log(f"could not read the pause switch: {type(exc).__name__}: {exc}")
            control = None
        if control is not None and control.get("paused"):
            return {"paused": True, "trade_id": trade.trade_id,
                    "reason": control.get("reason") or "execution is paused"}

        # An accepted order that has not filled is not a position yet. Calling
        # it closed would lose the trade; it is confirmed instead, and only
        # abandoned once the fill window has passed.
        if trade.state == STATE_ACCEPTED:
            filled = self.confirm_fill(trade)
            if filled.get("stopped") or filled.get("closed"):
                return filled
            if not filled.get("filled"):
                if self._fill_window_expired(trade):
                    return self._stop_management(
                        trade,
                        "the order was accepted but no position appeared within "
                        f"{self.fill_timeout_s:g}s; the trade needs review")
                return {"waiting": True, "trade_id": trade.trade_id,
                        "reason": "awaiting the fill confirmation"}
            trade = ManagedTrade.from_row(self.ledger.get_trade(trade.trade_id)) or trade

        position = self._position_for(trade.trade_id)
        if position is None:
            # The position is gone. Which leg took it out matters: a position
            # that closed before any step ran was taken out by the broker's
            # own attached target or stop, not by this manager, and the
            # record must say so rather than implying the ladder completed.
            if trade.legs_done == 0:
                reason = ("the position closed at the broker before any ladder "
                          "step ran; the broker's attached target or stop took it")
            else:
                reason = (f"the position closed at the broker after "
                          f"{trade.legs_done} ladder step(s); the remainder was "
                          "taken by the stop or target attached at the broker, "
                          "not by this manager")
            return self._finish(trade, reason=reason)

        quote = self.broker.quote()

        # A leg's stop move can be refused - the price can move between the
        # close and the modification, which puts breakeven on the wrong side
        # of the market for a moment. Advancing the ladder past a stop that
        # was never applied would lose the move for good, so a pending stop is
        # retried before anything else happens. The position is still
        # protected in the meantime by the stop that is actually attached.
        pending = self._pending_stop_move(trade)
        if pending is not None:
            outcome = self._apply_stop(trade, pending, position, quote)
            if outcome.get("error"):
                return outcome
            return {"partial": True, "trade_id": trade.trade_id,
                    "stop_retried": pending.label}

        leg = self._next_leg(trade)
        if leg is None:
            return self._finish(trade, reason="the ladder has no further steps")

        if not target_reached(side=trade.side, target=leg.target,
                              bid=quote.bid, ask=quote.ask):
            return {"waiting": True, "trade_id": trade.trade_id,
                    "next_target": leg.target}

        # The target is reached. Before closing anything, the live position
        # must match what the plan expects.
        remaining = float(position.qty)
        if remaining + 1e-9 < leg.quantity:
            return self._stop_management(
                trade,
                f"the position holds {remaining:g} lots but the {leg.label} step "
                f"expects to close {leg.quantity:g}; the ladder no longer matches "
                "the broker, so automatic management stops for review")

        closed_qty = leg.quantity
        if not leg.managed:
            # The final target: close whatever is left.
            closed_qty = remaining

        if closed_qty + 1e-9 < trade.plan.min_lot:
            return self._stop_management(
                trade,
                f"closing {closed_qty:g} lots would be below the broker's "
                f"minimum of {trade.plan.min_lot:g}; automatic management stops "
                "for review")

        try:
            ok = self.broker.close_quantity(position_id=position.id, qty=closed_qty)
        except BrokerError as exc:
            self._transition(trade, leg.label, "close_failed", str(exc))
            self._save(trade)
            return {"error": True, "trade_id": trade.trade_id, "detail": str(exc)}
        if not ok:
            self._transition(trade, leg.label, "close_failed",
                             f"the broker refused to close {closed_qty:g} lots")
            self._save(trade)
            return {"error": True, "trade_id": trade.trade_id}

        # Verify the close actually happened.
        after = self._position_for(trade.trade_id)
        if after is not None and after.qty > remaining - closed_qty + 1e-6:
            self._transition(trade, leg.label, "close_unverified",
                             f"the position still shows {after.qty:g} lots after "
                             f"closing {closed_qty:g}")
            self._save(trade)
            return {"error": True, "trade_id": trade.trade_id}

        trade.legs_done = max(trade.legs_done, self._leg_index(trade, leg) + 1)
        trade.state = STATE_PARTIAL
        self._transition(trade, leg.label, "partial_closed",
                         f"closed {closed_qty:g} lots at {leg.target:g}",
                         payload={"closed_qty": closed_qty, "target": leg.target})
        self._save(trade)
        self._notify(trade.trade_id, "partial",
                     f"[{leg.label.upper()} HIT] Closed {closed_qty:g} lot(s) of "
                     f"{trade.symbol} at {leg.target:g} "
                     f"(position {position.id}). Remaining "
                     f"{max(after.qty, 0.0) if after else 0.0:g} lot(s).")

        if after is None:
            return self._finish(trade, reason="the position closed at the target")

        stop_result = self._apply_stop(trade, leg, after, quote)
        return {"partial": True, "trade_id": trade.trade_id,
                "closed_qty": closed_qty, **stop_result}

    def _apply_stop(self, trade: ManagedTrade, leg: ExitLeg,
                    position: BrokerPosition, quote) -> dict:
        """Move the stop after a leg, if the policy calls for it."""
        new_stop = leg.stop_after(fill_price=trade.fill_price or position.avg_price)
        if new_stop is None:
            # Nothing to move, but the final target must still be attached.
            return self._ensure_final_target(trade, position)
        if new_stop <= 0:
            return self._ensure_final_target(trade, position)

        old_stop = trade.current_stop
        ok, reason = stop_move_is_risk_reducing(
            side=trade.side, old_stop=old_stop, new_stop=new_stop, price=quote.bid)
        if not ok:
            # Two different failures wear the same shape and need different
            # answers. A move that would widen the risk means the plan itself
            # is wrong, and a human must look. A stop that is momentarily on
            # the wrong side of the market is transient: the price moved while
            # this pass was running, and the next pass will retry it.
            if "widen" in reason:
                return self._stop_management(
                    trade, f"the {leg.label} stop move was refused: {reason}")
            return self._defer_stop(trade, leg, reason)

        try:
            applied = self.broker.modify_position(position_id=position.id, stop_loss=new_stop)
        except BrokerError as exc:
            return self._defer_stop(trade, leg, str(exc))
        if not applied:
            return self._defer_stop(
                trade, leg, f"the broker refused the stop move to {new_stop:g}")

        verified, observation = self._verify_stop(trade, position, new_stop)
        if not verified:
            return self._defer_stop(
                trade, leg,
                f"the stop move to {new_stop:g} was not confirmed by the broker: "
                f"{observation}")

        trade.current_stop = new_stop
        self._transition(trade, leg.label, "stop_updated",
                         f"the stop moved to {new_stop:g}",
                         payload={"stop": new_stop, "previous_stop": old_stop})
        self._save(trade)
        self._notify(trade.trade_id, "stop",
                     f"[STOP MOVED] {leg.label.upper()} done: the broker-side stop "
                     f"moved to {new_stop:g} on position {position.id}.")
        return self._ensure_final_target(trade, position)

    def _defer_stop(self, trade: ManagedTrade, leg: ExitLeg, reason: str) -> dict:
        """Record a refused stop move and retry it on the next pass.

        The retry is bounded: a refusal that survives many passes is not a
        race with the price, and at that point a human is asked instead of the
        loop running forever.
        """
        attempts = sum(
            1 for item in self.ledger.transitions(trade.trade_id)
            if item.get("stage") == leg.label and item.get("status") == "stop_deferred"
        ) + 1
        if attempts > self.max_stop_attempts:
            return self._stop_management(
                trade,
                f"the {leg.label} stop could not be applied after {attempts - 1} "
                f"attempts ({reason}); the position is still protected by the "
                "stop that is attached")
        self._transition(trade, leg.label, "stop_deferred",
                         f"attempt {attempts}: {reason}")
        self._save(trade)
        return {"error": True, "retry": True, "trade_id": trade.trade_id,
                "reason": reason, "attempt": attempts}

    def _ensure_final_target(self, trade: ManagedTrade, position: BrokerPosition) -> dict:
        """Keep the final take profit attached.

        A reduction may drop the attached target. If the position is no longer
        carrying the final target, it is put back; the position must never be
        left with a stop but no target.
        """
        attached = self._attached_levels(position)
        if not attached.known:
            # The broker's open orders could not be read. The target may well
            # be attached, so nothing is written on a guess; the next pass
            # reads again, and the broker-side target keeps protecting the
            # position in the meantime.
            return {"ok": True, "trade_id": trade.trade_id, "levels_unknown": True}
        if attached.take_profit is None and trade.plan.final_target > 0:
            try:
                ok = self.broker.modify_position(position_id=position.id,
                                                 take_profit=trade.plan.final_target)
            except BrokerError as exc:
                self._transition(trade, "target", "target_failed", str(exc))
                self._save(trade)
                return {"error": True, "trade_id": trade.trade_id}
            if ok:
                self._transition(trade, "target", "target_restored",
                                 f"the final take profit {trade.plan.final_target:g} "
                                 "was re-attached")
                self._save(trade)
                return {"target_restored": True, "trade_id": trade.trade_id}
            return self._stop_management(
                trade,
                "the final take profit could not be re-attached; automatic "
                "management stops for review")
        return {"ok": True, "trade_id": trade.trade_id}

    def _attached_levels(self, position: BrokerPosition) -> tuple:
        """The stop and target actually attached to a position, as the broker sees them.

        The broker publishes a position's protective orders in two places and
        only one of them is live. The filled entry order carries ``stopLoss``
        and ``takeProfit`` as they were *at submission*; those fields never
        change, so reading them would confirm a stop move that never happened.
        The live levels are the two orders the position points at with
        ``stopLossId`` and ``takeProfitId``, and those are open (unexecuted)
        orders: a ``stop`` order whose level is ``stopPrice``, and a ``limit``
        order whose level is ``price``.

        Unreadable means unknown, never "still where it was": a failed read
        returns ``None`` for both, so a verification built on it fails closed.
        """
        try:
            orders = {order.id: order for order in self.broker.orders(history=False)}
        except BrokerError as exc:
            self.log(f"could not read the broker's open orders: {exc}")
            return _AttachedLevels(stop=None, take_profit=None, known=False)

        stop: float | None = None
        target: float | None = None
        if position.stop_loss_id and position.stop_loss_id in orders:
            order = orders[position.stop_loss_id]
            stop = order.stop_price if order.stop_price else order.price or None
        if position.take_profit_id and position.take_profit_id in orders:
            order = orders[position.take_profit_id]
            target = order.price if order.price else order.stop_price or None
        return _AttachedLevels(stop=stop, take_profit=target, known=True)

    def _verify_stop(self, trade: ManagedTrade, position: BrokerPosition,
                     expected: float) -> tuple[bool, str]:
        """Confirm the broker really carries the stop we asked for.

        Returns the verdict and, on failure, what the broker actually reports,
        so the ledger and the notification say why rather than only that.

        The position is passed in rather than re-read: its ids are what resolve
        the level, they cannot change while a modification is in flight, and
        every REST call here costs seconds during which the price keeps moving.
        """
        if self._position_for(trade.trade_id) is None:
            return False, "the position is no longer open"
        levels = self._attached_levels(position)
        if not levels.known:
            return False, "the broker's open orders could not be read"
        if levels.stop is None:
            return False, (f"the broker publishes no stop order for position "
                           f"{position.id} (stopLossId {position.stop_loss_id})")
        if abs(levels.stop - expected) > 1e-6:
            return False, (f"the broker reports a stop of {levels.stop:g}, not "
                           f"{expected:g}")
        return True, f"the broker reports a stop of {levels.stop:g}"

    def _next_leg(self, trade: ManagedTrade) -> ExitLeg | None:
        index = trade.legs_done
        if index < 0 or index >= len(trade.plan.legs):
            return None
        return trade.plan.legs[index]

    def _pending_stop_move(self, trade: ManagedTrade) -> ExitLeg | None:
        """The last completed leg whose stop move has not been applied yet.

        ``legs_done`` counts closes, not stop moves. The two can diverge when
        a modification is refused, and this is what notices.
        """
        if trade.legs_done <= 0 or trade.legs_done > len(trade.plan.legs):
            return None
        leg = trade.plan.legs[trade.legs_done - 1]
        if leg.stop_after_kind == STOP_NONE:
            return None
        expected = leg.stop_after(fill_price=trade.fill_price or trade.entry)
        if expected is None or expected <= 0:
            return None
        if trade.current_stop is not None and abs(trade.current_stop - expected) <= 1e-6:
            return None
        return leg

    def _leg_index(self, trade: ManagedTrade, leg: ExitLeg) -> int:
        for index, candidate in enumerate(trade.plan.legs):
            if candidate.label == leg.label:
                return index
        return trade.legs_done

    def _stop_management(self, trade: ManagedTrade, reason: str) -> dict:
        """Stop managing a trade automatically and ask for a human."""
        trade.management_stopped = True
        trade.state = STATE_REVIEW
        trade.note = reason
        self._transition(trade, "management", STATE_REVIEW, reason)
        self._save(trade)
        self._notify(trade.trade_id, "review",
                     f"[REVIEW] Automatic management stopped for trade "
                     f"{trade.trade_id}: {reason}. The broker-side stop and target "
                     "are still in place.")
        return {"stopped": True, "state": STATE_REVIEW, "trade_id": trade.trade_id,
                "reason": reason}

    def _finish(self, trade: ManagedTrade, *, reason: str) -> dict:
        trade.state = STATE_CLOSED
        trade.management_stopped = True
        trade.note = reason
        self._transition(trade, "close", STATE_CLOSED, reason)
        self._save(trade)
        self._notify(trade.trade_id, "closed",
                     f"[CLOSED] {trade.side.upper()} {trade.symbol} is closed: {reason}.")
        return {"closed": True, "state": STATE_CLOSED, "trade_id": trade.trade_id,
                "reason": reason}

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> dict:
        """Bring every unfinished trade back into agreement with the broker.

        Only trades this project owns are touched: the correlation tag is the
        ownership test, so a manual position in the same account is never
        managed.
        """
        summary = {"considered": 0, "open": 0, "closed": 0, "review": 0, "unknown": 0}
        for row in self.ledger.list_trades(states=ACTIVE_STATES):
            trade = ManagedTrade.from_row(row)
            if trade is None:
                continue
            summary["considered"] += 1
            try:
                outcome = self.reconcile_trade(trade)
            except Exception as exc:
                summary["unknown"] += 1
                self.log(f"reconciliation error for {trade.trade_id}: "
                         f"{type(exc).__name__}: {exc}")
                continue
            if outcome.get("state") == STATE_REVIEW:
                summary["review"] += 1
            elif outcome.get("state") == STATE_CLOSED:
                summary["closed"] += 1
            elif outcome.get("state") in {STATE_OPEN, STATE_PARTIAL}:
                summary["open"] += 1
            else:
                summary["unknown"] += 1
        # Trades already flagged for a human are not reconciled — reconciling
        # them would resume management, which is exactly what the flag forbids.
        # They are counted so a restart reports them instead of passing over
        # them in silence.
        summary["awaiting_review"] = len(self.ledger.list_trades(states=(STATE_REVIEW,)))
        return summary

    def reconcile_trade(self, trade: ManagedTrade) -> dict:
        """Reconcile one trade against the broker's orders and positions."""
        position = self._position_for(trade.trade_id)
        order = self._existing_order(trade.trade_id)

        if position is None:
            if order is not None and order.is_filled:
                trade.broker_order_id = order.id
                trade.broker_position_id = order.position_id
                trade.fill_price = float(order.avg_price) or trade.entry
                trade.state = STATE_CLOSED
                trade.management_stopped = True
                trade.note = "the position closed while this process was not running"
                self._transition(trade, "reconcile", STATE_CLOSED, trade.note,
                                 payload={"order_id": order.id,
                                          "avg_price": order.avg_price})
                self._save(trade)
                self._notify(trade.trade_id, "closed",
                             f"[CLOSED] {trade.side.upper()} {trade.symbol} was "
                             f"closed at the broker while this process was not "
                             f"running (average {trade.fill_price:g}).")
                return {"state": STATE_CLOSED, "trade_id": trade.trade_id}
            if order is None:
                # Nothing at the broker and nothing here: the order was never
                # accepted. It may be submitted once.
                trade.state = STATE_VALIDATED
                self._transition(trade, "reconcile", "not_found",
                                 "no order and no position at the broker")
                self._save(trade)
                return {"state": STATE_VALIDATED, "trade_id": trade.trade_id}
            # An order exists but is not filled and no position is open.
            trade.broker_order_id = order.id
            trade.state = STATE_ACCEPTED
            self._transition(trade, "reconcile", STATE_ACCEPTED,
                             f"order {order.id} is at the broker with status "
                             f"{order.status!r} and no position is open")
            self._save(trade)
            return {"state": STATE_ACCEPTED, "trade_id": trade.trade_id}

        trade.broker_position_id = position.id
        trade.fill_price = float(position.avg_price) or trade.fill_price or trade.entry

        # The ladder must still describe the position. If the broker's
        # attached target is not the final target of the plan, the ordering
        # cannot be established and automatic management stops.
        levels = self._attached_levels(position)
        expected_stop = trade.current_stop or trade.stop
        problems: list[str] = []
        if not levels.known:
            # Nothing about the attached levels can be confirmed, so the
            # ordering of the ladder cannot be established either. The broker
            # still holds the stop and the target; only our management stops.
            problems.append("the broker's open orders could not be read, so the "
                            "attached stop and target cannot be confirmed")
        elif levels.take_profit is None:
            problems.append("the position carries no take profit at the broker")
        elif abs(levels.take_profit - trade.plan.final_target) > 1e-6:
            problems.append(
                f"the broker's target {levels.take_profit:g} is not the plan's "
                f"final target {trade.plan.final_target:g}")
        if levels.known and levels.stop is None:
            problems.append("the position carries no stop loss at the broker")
        elif levels.known and abs(levels.stop - expected_stop) > 1e-6:
            problems.append(
                f"the broker's stop {levels.stop:g} is not the expected stop "
                f"{expected_stop:g}")
        if abs(position.qty - trade.plan.total_size) > 1e-6 and trade.legs_done == 0:
            problems.append(
                f"the position holds {position.qty:g} lots but the plan was "
                f"built for {trade.plan.total_size:g}")

        if problems:
            return self._stop_management(
                trade,
                "the position cannot be reconciled automatically: "
                + "; ".join(problems))

        trade.state = STATE_PARTIAL if trade.legs_done else STATE_OPEN
        self._transition(trade, "reconcile", trade.state,
                         f"the position {position.id} matches the plan",
                         payload={"qty": position.qty, "stop": levels.stop,
                                  "take_profit": levels.take_profit})
        self._save(trade)
        return {"state": trade.state, "trade_id": trade.trade_id}


@dataclass(frozen=True)
class _AttachedLevels:
    stop: float | None
    take_profit: float | None
    #: False when the broker could not be read at all. A level of ``None``
    #: then means "unknown", not "absent", and nothing may act on it.
    known: bool = True
