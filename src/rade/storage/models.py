"""SQLAlchemy ORM models for Rade Bot."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawTelegramMessage(Base):
    __tablename__ = "raw_telegram_messages"
    id = Column(Integer, primary_key=True)
    channel = Column(String(128), nullable=False)
    message_id = Column(Integer, nullable=False)
    posted_at = Column(DateTime(timezone=True), nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)
    deleted = Column(Integer, default=0)
    text = Column(Text, nullable=True)
    raw_json = Column(Text, nullable=True)
    ingested_at = Column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("channel", "message_id", name="uq_raw_msg"),)


class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, nullable=False)  # raw message PK
    instrument = Column(String(32), nullable=True)
    direction = Column(String(8), nullable=True)  # buy/sell
    entry_type = Column(String(16), nullable=True)  # market/limit
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Text, nullable=True)  # json list
    rationale = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    parsed_at = Column(DateTime(timezone=True), default=utcnow)
    parser_version = Column(String(32), nullable=True)


class Instrument(Base):
    __tablename__ = "instruments"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), nullable=False, unique=True)
    broker = Column(String(32), nullable=False)
    asset_class = Column(String(32), nullable=True)
    tick_size = Column(Float, nullable=True)
    contract_size = Column(Float, nullable=True)
    min_size = Column(Float, nullable=True)


class Bar(Base):
    __tablename__ = "bars"
    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=True)
    __table_args__ = (UniqueConstraint("instrument_id", "timeframe", "ts", name="uq_bar"),)


class Probe(Base):
    __tablename__ = "probes"
    id = Column(Integer, primary_key=True)
    instrument = Column(String(32), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    buy_limit = Column(Float, nullable=True)
    sell_limit = Column(Float, nullable=True)
    buy_filled_at = Column(DateTime(timezone=True), nullable=True)
    sell_filled_at = Column(DateTime(timezone=True), nullable=True)
    first_side = Column(String(8), nullable=True)
    hold_seconds = Column(Integer, nullable=True)
    outcome = Column(String(32), nullable=True)
    score = Column(Float, nullable=True)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    broker = Column(String(32), nullable=False)
    broker_order_id = Column(String(64), nullable=True)
    instrument = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)
    type = Column(String(16), nullable=False)
    price = Column(Float, nullable=True)
    size = Column(Float, nullable=True)
    status = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    filled_at = Column(DateTime(timezone=True), nullable=True)
    avg_fill = Column(Float, nullable=True)


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    instrument = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)
    size = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    opened_at = Column(DateTime(timezone=True), default=utcnow)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String(32), nullable=True)
    pnl = Column(Float, nullable=True)


class Feature(Base):
    __tablename__ = "features"
    id = Column(Integer, primary_key=True)
    instrument = Column(String(32), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    message_pk = Column(Integer, nullable=True, index=True)
    feature_name = Column(String(64), nullable=False)
    value = Column(Float, nullable=True)
    computed_at = Column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("instrument", "ts", "message_pk", "feature_name", name="uq_feature"),)


class LabeledSetup(Base):
    __tablename__ = "labeled_setups"
    id = Column(Integer, primary_key=True)
    message_pk = Column(Integer, nullable=False, index=True)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    instrument = Column(String(32), nullable=True)
    direction = Column(String(8), nullable=True)
    entry_type = Column(String(16), nullable=True)
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profits = Column(Text, nullable=True)  # json list
    is_recap = Column(Integer, default=0)
    fill_status = Column(String(16), nullable=True)
    fill_price = Column(Float, nullable=True)
    fill_ts = Column(DateTime(timezone=True), nullable=True)
    exit_type = Column(String(16), nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_ts = Column(DateTime(timezone=True), nullable=True)
    R = Column(Float, nullable=True)
    hours_held = Column(Float, nullable=True)
    ambiguity = Column(Integer, default=0)
    ambiguity_optimistic = Column(Integer, default=0)
    notes = Column(Text, nullable=True)
    labeled_at = Column(DateTime(timezone=True), default=utcnow)


class PnlDaily(Base):
    __tablename__ = "pnl_daily"
    date = Column(String(10), primary_key=True)
    equity_start = Column(Float, nullable=True)
    equity_end = Column(Float, nullable=True)
    realized = Column(Float, nullable=True)
    unrealized = Column(Float, nullable=True)
    max_dd = Column(Float, nullable=True)
