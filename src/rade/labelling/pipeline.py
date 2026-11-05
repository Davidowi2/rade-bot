"""Pipeline orchestration for Phase 2A."""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from .labeler import label_all
from .models import Label, Setup
from .recap_filter import is_recap
from .setup_extractor import extract_all


def labels_to_dataframe(labels: list[Label]) -> pd.DataFrame:
    rows = []
    for lb in labels:
        s = lb.setup
        rows.append(
            {
                "message_pk": s.message_pk,
                "posted_at": s.posted_at,
                "instrument": s.instrument,
                "direction": s.direction,
                "entry_type": s.entry_type,
                "entry_price": s.entry_price,
                "stop_loss": s.stop_loss,
                "take_profits": s.take_profits,
                "is_recap": lb.is_recap,
                "fill_status": lb.fill_status,
                "fill_price": lb.fill_price,
                "fill_ts": lb.fill_ts,
                "exit_type": lb.exit_type,
                "exit_price": lb.exit_price,
                "exit_ts": lb.exit_ts,
                "R": lb.R,
                "hours_held": lb.hours_held,
                "ambiguity": lb.ambiguity,
                "ambiguity_optimistic": lb.ambiguity_optimistic,
                "notes": lb.notes,
            }
        )
    return pd.DataFrame(rows)


def run_pipeline(raw_rows: Iterable, bars: pd.DataFrame) -> pd.DataFrame:
    setups = extract_all(raw_rows)
    labels = label_all(setups, bars)
    for lb in labels:
        lb.is_recap = is_recap(lb.setup, bars)
    return labels_to_dataframe(labels)
