"""The two-call quote read, and the crossed book it can invent.

The broker exposes bid and ask separately, so they are two requests a few
milliseconds apart. On a moving market that gap can produce bid > ask at no
single instant, and the pipeline refuses a crossed book as invalid data - which
blocks a perfectly good signal. `read_book` re-reads once; these tests pin that
behaviour, and pin that a genuinely inverted feed is still refused downstream.
"""
from __future__ import annotations

from rade.execution.broker import BrokerQuote, quote_problem, read_book
from datetime import datetime, timezone


def reader(values):
    """A bid/ask reader that yields from ``values`` and counts its calls."""
    queue = list(values)
    calls = {"n": 0}

    def read() -> float:
        calls["n"] += 1
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return read, calls


def test_a_normal_book_is_read_once():
    bid, bid_calls = reader([3850.1])
    ask, ask_calls = reader([3850.4])
    b, a = read_book(bid, ask)
    assert (b, a) == (3850.1, 3850.4)
    assert bid_calls["n"] == 1 and ask_calls["n"] == 1


def test_a_crossed_read_is_retried_and_the_second_one_is_used():
    bid, _ = reader([3851.0, 3850.2])
    ask, _ = reader([3850.4, 3850.5])
    b, a = read_book(bid, ask)
    assert (b, a) == (3850.2, 3850.5)
    assert a >= b


def test_a_genuinely_inverted_feed_is_returned_for_the_caller_to_refuse():
    bid, _ = reader([3851.0])
    ask, _ = reader([3850.4])
    b, a = read_book(bid, ask)
    # read_book does not hide it: the quote still fails validation.
    quote = BrokerQuote(bid=b, ask=a, quoted_at=datetime.now(timezone.utc))
    assert quote_problem(quote) is not None


def test_attempts_of_one_disables_the_retry():
    bid, _ = reader([3851.0, 3850.2])
    ask, _ = reader([3850.4, 3850.5])
    b, a = read_book(bid, ask, attempts=1)
    assert (b, a) == (3851.0, 3850.4)
