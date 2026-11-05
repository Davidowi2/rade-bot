"""Tests for the Telegram signal parser."""
from rade.ingestion.telegram.parser import parse_signal


def test_full_signal():
    text = "XAUUSD SELL NOW 2982\nSl : 2995\nTp : 2974\nTp : 2965"
    s = parse_signal(text)
    assert s is not None
    assert s["direction"] == "sell"
    assert s["entry_price"] == 2982.0
    assert s["stop_loss"] == 2995.0
    assert s["entry_type"] == "market"


def test_no_direction_returns_none():
    assert parse_signal("gold is going up today") is None


def test_result_message_skipped():
    # a "+70 pip" result line should not become a signal
    assert parse_signal("+70 pip") is None


def test_empty():
    assert parse_signal(None) is None
    assert parse_signal("") is None


def test_buy_limit():
    text = "XAUUSD BUY LIMIT 2950\nSl : 2930\nTp : 2980"
    s = parse_signal(text)
    assert s is not None
    assert s["direction"] == "buy"
    assert s["entry_type"] == "limit"
    assert s["entry_price"] == 2950.0
