"""What the notification tells the operator actually happened.

Every message used to end with the constant sentence "paper only; no broker
order was sent" - printed whether or not an order had been placed, because the
notification was built and sent *before* execution ran. On a live account that
is the one line that must not lie, so these tests pin the three real cases.
"""
from __future__ import annotations

from rade.observer.mirror_pipeline import PipelineResult
from rade.observer.notify import build_notification


def result(status: str = "paper_intent", reason: str = "validated; simulation only"):
    return PipelineResult(
        event_key="k",
        status=status,
        reason=reason,
        signal={"instrument": "XAUUSD", "side": "buy", "entry_type": "market",
                "published_entry": 3850.0, "stop_loss": 3845.0,
                "take_profits": [3860.0]},
        order_intent={"size_lots": 0.02, "order_state": "pending",
                      "risk_budget": 10.0, "filled": False},
        notification_preview="",
    )


def test_a_process_with_no_executor_says_it_is_paper():
    text = build_notification(result(), execution=None)
    assert "paper mode" in text
    assert "no broker order was sent" in text


def test_a_placed_order_says_it_was_placed():
    text = build_notification(result(), execution={
        "executed": True, "trade_id": "abc123", "order_id": 98765,
    })
    assert "ORDER PLACED" in text
    assert "abc123" in text
    assert "98765" in text
    # The sentence that used to be unconditional must not appear.
    assert "paper only" not in text


def test_a_refused_order_says_why():
    text = build_notification(result(), execution={
        "executed": False,
        "reason": "1 positions are already open and the limit is 1",
    })
    assert "no broker order was sent" in text
    assert "limit is 1" in text


def test_an_execution_error_is_reported_as_such():
    text = build_notification(result(), execution={
        "executed": False, "error": "ConnectionError: session expired",
    })
    assert "no broker order was sent" in text
    assert "session expired" in text
