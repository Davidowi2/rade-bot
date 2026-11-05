"""Database engine/session helpers."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rade.config.settings import get_settings


def db_url() -> str:
    s = get_settings()
    db_path = s.data_dir / "rade.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def make_engine():
    return create_engine(db_url(), echo=False, future=True)


def make_session_factory():
    return sessionmaker(bind=make_engine(), expire_on_commit=False)
