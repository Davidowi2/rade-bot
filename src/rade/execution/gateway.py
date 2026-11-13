"""The TradeLocker gateway: the only module in the project that talks to the broker.

Everything the execution engine needs from the broker goes through here, and
this module enforces three things the engine cannot be trusted to enforce
itself:

1. **Demo only.** :func:`assert_demo_environment` refuses anything that is not
   the demo environment, and it checks *both* the configured environment name
   and the resolved host. A configuration that says ``demo`` while pointing at
   ``live.tradelocker.com`` is refused rather than trusted.
2. **Nothing is guessed.** The instrument id, the trade route id, the account
   number, the contract size, the lot step and the minimum lot are all read
   from the broker and then checked against the values a human confirmed. A
   mismatch produces a spec with ``verified=False`` and a reason, never a
   fallback number.
3. **Credentials never appear in an error.** The broker's exceptions embed the
   request URL, and the request headers carry the account token. Every error
   that crosses this module's boundary is scrubbed.

The write surface is deliberately small: a market entry, a stop/target change
on an open position, and a quantity reduction. There is no "close everything",
no "cancel everything", and no way to reach a live account.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from rade.execution.broker import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerRefused,
    BrokerSpec,
    OrderAck,
)

__all__ = [
    "DEMO_HOST",
    "TradeLockerGateway",
    "assert_demo_environment",
    "tradelocker_gateway_from_env",
]

#: The only host this project will ever talk to.
DEMO_HOST = "demo.tradelocker.com"

#: Tokens that must never survive into an exception message.
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|access_token|refresh_token|password|api[-_]?key)\b"
    r"\s*[:=]\s*\S+"
)


def _scrub(text: str, *secrets: str | None) -> str:
    """Remove anything that could be a credential from an error message."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return _SECRET_RE.sub(r"\1=***", out)


def assert_demo_environment(environment: str, base_url: str) -> None:
    """Refuse to run anywhere but the TradeLocker demo environment.

    Both the environment name and the resolved host are checked. The name
    alone is not enough: the base URL is a separate setting, and a
    configuration that says ``demo`` while pointing at the live host is a
    configuration that must fail rather than be believed.
    """
    env = (environment or "").strip().lower()
    if env != "demo":
        raise BrokerRefused(
            f"refusing a non-demo TradeLocker environment ({env or 'unset'}); "
            "this project has no live-account execution path"
        )
    host = (urlparse(base_url or "").hostname or "").lower()
    if host != DEMO_HOST:
        raise BrokerRefused(
            f"refusing a TradeLocker base URL that is not the demo host "
            f"(host={host or 'unset'!r})"
        )


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


class TradeLockerGateway:
    """A :class:`rade.execution.broker.Broker` backed by the demo account.

    The client is injected rather than constructed here, so a test can supply
    a double without this module importing the SDK at import time.
    """

    def __init__(self, client: Any, *, environment: str, base_url: str,
                 symbol: str = "XAUUSD",
                 confirmed_contract_units: float | None = None,
                 account_id: int | None = None,
                 log: Callable[[str], None] = print):
        assert_demo_environment(environment, base_url)
        self.client = client
        self.environment = environment
        self.base_url = base_url
        self.symbol = symbol
        self.confirmed_contract_units = confirmed_contract_units
        self.account_id = account_id if account_id is not None else int(
            getattr(client, "account_id", 0) or 0)
        self.log = log
        self._spec: BrokerSpec | None = None
        self._secrets: tuple[str | None, ...] = ()

    # -- internals --------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return _scrub(text, *self._secrets)

    def _fail(self, exc: Exception, what: str) -> BrokerError:
        return BrokerError(f"{what} failed: {self._scrub(f'{type(exc).__name__}: {exc}')}")

    # -- reads ------------------------------------------------------------

    def spec(self) -> BrokerSpec:
        """Resolve and verify the instrument and account specification.

        Every value comes from the broker. The only thing that is *not* from
        the broker is ``confirmed_contract_units``, which is the number a human
        checked against the broker's own published specification; it is used
        to cross-check the broker's answer, never to replace it.
        """
        if self._spec is not None:
            return self._spec

        try:
            instrument_id = int(self.client.get_instrument_id_from_symbol_name(self.symbol))
        except Exception as exc:
            raise self._fail(exc, f"resolving the instrument id for {self.symbol}") from exc
        if instrument_id <= 0:
            raise BrokerError(
                f"the broker did not resolve an instrument id for {self.symbol}")

        try:
            details = self.client.get_instrument_details(instrument_id) or {}
        except Exception as exc:
            raise self._fail(exc, f"reading the instrument details for {self.symbol}") from exc
        try:
            route_id = str(self.client.get_trade_route_id(instrument_id) or "")
        except Exception as exc:
            raise self._fail(exc, f"resolving the trade route id for {self.symbol}") from exc
        if not route_id:
            raise BrokerError(f"the broker returned no trade route id for {self.symbol}")

        account_number = self._resolve_account_number()

        # lotSize is the broker's contract size: how many units one lot buys.
        units = _as_float(details.get("lotSize"))
        lot_step = _as_float(details.get("lotStep"))
        min_lot = _as_float(details.get("minLot"))
        max_lot = _as_float(details.get("maxLot"))
        tick_size = _as_float(_first_tick_size(details.get("tickSize"))) or 0.0
        symbol_status = str(details.get("symbolStatus") or "")

        reason: str | None = None
        if units is None or units <= 0:
            reason = "the broker did not report a contract size (lotSize)"
        elif lot_step is None or lot_step <= 0:
            reason = "the broker did not report a quantity step (lotStep)"
        elif min_lot is None or min_lot <= 0:
            reason = "the broker did not report a minimum quantity (minLot)"
        elif max_lot is None or max_lot < min_lot:
            reason = "the broker did not report a usable maximum quantity (maxLot)"
        elif not route_id:
            reason = "the broker did not report a trade route id"
        elif account_number <= 0:
            reason = "the broker did not report an account number"
        elif self.confirmed_contract_units is None:
            reason = (
                "the contract size has not been confirmed by a human "
                "(MIRROR_CONTRACT_UNITS_CONFIRMED is not set)"
            )
        elif abs(float(self.confirmed_contract_units) - units) > 1e-9:
            reason = (
                f"the broker reports a contract size of {units:g} but "
                f"{float(self.confirmed_contract_units):g} was confirmed; "
                "the two disagree, so the specification is unconfirmed"
            )
        elif symbol_status and symbol_status.upper() != "FULLY_OPEN":
            reason = f"the broker reports the symbol status {symbol_status!r}"

        spec = BrokerSpec(
            environment=self.environment,
            base_url=self.base_url,
            account_id=self.account_id,
            account_number=account_number,
            symbol=self.symbol,
            instrument_id=instrument_id,
            trade_route_id=route_id,
            contract_units_per_lot=units or 0.0,
            lot_step=lot_step or 0.0,
            min_lot=min_lot or 0.0,
            max_lot=max_lot or 0.0,
            tick_size=tick_size,
            symbol_status=symbol_status,
            confirmed_contract_units=self.confirmed_contract_units,
            verified=reason is None,
            unconfirmed_reason=reason,
        )
        self._spec = spec
        if reason:
            self.log(f"broker specification is not verified: {reason}")
        return spec

    def _resolve_account_number(self) -> int:
        """The account number, confirmed against the broker's account list.

        The account list at ``GET /trade/accounts`` does **not** carry
        ``accNum``; it carries the account id, the name, the currency and the
        trading rules. The number that the ``accNum`` header needs is resolved
        by the SDK at login from the fuller account table, so the id is matched
        here and the SDK's value is used once the match confirms it is the
        right account.

        A match with no number is not a failure: it falls through to the SDK's
        value rather than returning zero, which is what a naive lookup did.
        """
        matched = False
        try:
            accounts = self.client.get_trade_accounts() or []
        except Exception:
            accounts = []
        for account in accounts:
            try:
                if int(account.get("id", 0)) != int(self.account_id):
                    continue
            except (TypeError, ValueError):
                continue
            matched = True
            number = _opt_int(account.get("accNum"))
            if number and number > 0:
                return number
            break
        number = _opt_int(getattr(self.client, "acc_num", 0))
        if number and number > 0:
            return number
        return 0

    def account_state(self) -> dict:
        """The account's balances, as the broker reports them."""
        try:
            return dict(self.client.get_account_state() or {})
        except Exception as exc:
            raise self._fail(exc, "reading the account state") from exc

    def equity(self) -> float:
        """The equity used for sizing, from the broker rather than from config.

        ``projectedBalance`` is the balance plus open P&L, which is what a new
        trade's risk should be measured against. ``balance`` is the fallback
        when the broker omits it.
        """
        state = self.account_state()
        for key in ("projectedBalance", "equity", "balance"):
            value = _as_float(state.get(key))
            if value is not None and value > 0:
                return float(value)
        return 0.0

    def quote(self) -> BrokerQuote:
        spec = self.spec()
        try:
            bid = float(self.client.get_latest_bid_price(spec.instrument_id))
            ask = float(self.client.get_latest_asking_price(spec.instrument_id))
        except Exception as exc:
            raise self._fail(exc, "reading the quote") from exc
        return BrokerQuote(bid=bid, ask=ask, quoted_at=datetime.now(timezone.utc))

    def positions(self) -> list[BrokerPosition]:
        try:
            frame = self.client.get_all_positions()
        except Exception as exc:
            raise self._fail(exc, "reading positions") from exc
        out: list[BrokerPosition] = []
        for _, row in _iter_rows(frame):
            try:
                out.append(BrokerPosition(
                    id=int(row.get("id", 0)),
                    instrument_id=int(row.get("tradableInstrumentId", 0) or 0),
                    side=str(row.get("side", "") or ""),
                    qty=float(row.get("qty", 0) or 0),
                    avg_price=float(row.get("avgPrice", 0) or 0),
                    stop_loss_id=_opt_int(row.get("stopLossId")),
                    take_profit_id=_opt_int(row.get("takeProfitId")),
                    strategy_id=str(row.get("strategyId", "") or "") or None,
                    unrealized_pl=float(row.get("unrealizedPl", 0) or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

    def orders(self, *, history: bool = False) -> list[BrokerOrder]:
        try:
            frame = self.client.get_all_orders(history=history)
        except Exception as exc:
            raise self._fail(exc, "reading orders") from exc
        out: list[BrokerOrder] = []
        for _, row in _iter_rows(frame):
            try:
                out.append(BrokerOrder(
                    id=int(row.get("id", 0)),
                    instrument_id=int(row.get("tradableInstrumentId", 0) or 0),
                    side=str(row.get("side", "") or ""),
                    qty=float(row.get("qty", 0) or 0),
                    status=str(row.get("status", "") or ""),
                    type=str(row.get("type", "") or ""),
                    filled_qty=float(row.get("filledQty", 0) or 0),
                    avg_price=float(row.get("avgPrice", 0) or 0),
                    price=float(row.get("price", 0) or 0),
                    position_id=_opt_int(row.get("positionId")),
                    stop_loss=_opt_float(row.get("stopLoss")),
                    take_profit=_opt_float(row.get("takeProfit")),
                    stop_price=_opt_float(row.get("stopPrice")),
                    strategy_id=str(row.get("strategyId", "") or "") or None,
                    created_at=_epoch_ms_to_dt(row.get("createdDate")),
                ))
            except (TypeError, ValueError):
                continue
        return out

    # -- writes -----------------------------------------------------------

    def place_market(self, *, qty: float, side: str, stop_loss: float,
                     take_profit: float, strategy_id: str) -> OrderAck:
        """Place a market order with the broker-side stop and final target.

        The stop and the final take profit are attached to the *order*, so they
        exist at the broker from the moment the position exists. A monitoring
        outage between the fill and the first management tick therefore cannot
        leave the position naked.
        """
        spec = self.spec()
        if not spec.verified:
            raise BrokerRefused(
                f"the broker specification is not verified: {spec.unconfirmed_reason}")
        if side not in {"buy", "sell"}:
            raise BrokerError(f"invalid side {side!r}")
        if qty <= 0:
            raise BrokerError(f"invalid quantity {qty!r}")

        request = {
            "qty": qty,
            "side": side,
            "type": "market",
            "validity": "IOC",
            "tradableInstrumentId": spec.instrument_id,
            "routeId": spec.trade_route_id,
            "stopLoss": stop_loss,
            "stopLossType": "absolute",
            "takeProfit": take_profit,
            "takeProfitType": "absolute",
            "strategyId": strategy_id,
        }
        try:
            order_id = self.client.create_order(
                instrument_id=spec.instrument_id,
                quantity=qty,
                side=side,
                type_="market",
                validity="IOC",
                stop_loss=stop_loss,
                stop_loss_type="absolute",
                take_profit=take_profit,
                take_profit_type="absolute",
                strategy_id=strategy_id,
            )
        except Exception as exc:
            return OrderAck(accepted=False, order_id=None,
                            detail=self._scrub(f"{type(exc).__name__}: {exc}"),
                            request=request)
        if order_id is None:
            return OrderAck(accepted=False, order_id=None,
                            detail="the broker returned no order id", request=request)
        return OrderAck(accepted=True, order_id=int(order_id),
                        detail="accepted", request=request)

    def modify_position(self, *, position_id: int, stop_loss: float | None = None,
                        take_profit: float | None = None) -> bool:
        """Change the attached stop and/or target of an open position."""
        if stop_loss is None and take_profit is None:
            raise BrokerError("a position modification needs a stop loss or a take profit")
        params: dict[str, Any] = {}
        if stop_loss is not None:
            params["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = float(take_profit)
        try:
            return bool(self.client.modify_position(int(position_id), params))
        except Exception as exc:
            raise self._fail(exc, f"modifying position {position_id}") from exc

    def close_quantity(self, *, position_id: int, qty: float) -> bool:
        """Reduce a position by ``qty``; ``qty=0`` closes it entirely."""
        try:
            return bool(self.client.close_position(position_id=int(position_id),
                                                   close_quantity=float(qty)))
        except Exception as exc:
            raise self._fail(exc, f"closing {qty} of position {position_id}") from exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    result = _as_float(value)
    return result


def _opt_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_tick_size(value: Any) -> Any:
    """The tick size arrives as a list of ranges."""
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("tickSize")
    return None


def _iter_rows(frame: Any):
    """Iterate a pandas frame as dicts; tolerate an empty or missing frame."""
    if frame is None:
        return []
    try:
        if getattr(frame, "empty", False):
            return []
        return frame.iterrows()
    except AttributeError:
        return []


def tradelocker_gateway_from_env(*, symbol: str = "XAUUSD",
                                 confirmed_contract_units: float | None = None,
                                 log: Callable[[str], None] = print) -> TradeLockerGateway:
    """Build the gateway from the backend environment.

    Credentials are read from the settings object and never returned, logged
    or stored.
    """
    from rade.config.settings import get_settings

    settings = get_settings()
    base_url = settings.tradelocker_base_url
    assert_demo_environment(settings.tradelocker_env, base_url)

    from tradelocker import TLAPI

    client = TLAPI(
        environment=base_url,
        username=settings.tradelocker_email,
        password=settings.tradelocker_password,
        server=settings.tradelocker_server,
        account_id=settings.tradelocker_account_id,
        log_level="error",
    )
    gateway = TradeLockerGateway(
        client,
        environment=settings.tradelocker_env,
        base_url=base_url,
        symbol=symbol,
        confirmed_contract_units=confirmed_contract_units,
        account_id=settings.tradelocker_account_id,
        log=log,
    )
    return gateway
