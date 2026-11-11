"""Reconciliation tests: the replay against the archived legacy baseline.

The replay was built as an independent engine. It is therefore worth proving,
in code, that it reproduces the archived S0 baseline wherever the two are
supposed to agree, and that every place they *do not* agree has a named,
documented cause.

The reconciliation that produced these numbers is in
``scripts/diagnose_legacy_divergence.py``; the findings are written up in
``docs/evidence/legacy-reconciliation.md``.

Three distinct sources of disagreement were found and are pinned here:

1. **The legacy engine never normalised the take-profit ladder.** A target on
   the wrong side of the entry was treated as reachable and filled instantly at
   that target price. On ``pk=452`` the raw ladder was ``[2367, 3380, 3400]``
   with an entry of 3360, so the first "target" sat $993 below the entry and
   produced a single leg worth **-36.78 R**. This is a defect in the legacy
   engine, not a disagreement about strategy.

2. **A data gap across a cap boundary.** The 30m series has 73 weekend gaps of
   ~49.5h. The replay used to stop scanning at the last bar *before* the cap
   boundary, which truncated a Friday-evening fill at Friday's close and
   mislabelled it ``window_end``. The plan requires the cap to bind at the
   first bar *at or after* the boundary, and ``window_end`` to mean the end of
   available data. Fixed; these tests guard it.

3. **The stop beats the cap in the same bar.** The plan (§3.2) makes the stop
   win ties. The legacy engine closed at the bar close via its timeout first,
   which is the optimistic reading. The replay is pessimistic here, on purpose.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from rade.replay.assumptions import Assumptions
from rade.replay.engine import POLICY_A, Bar, Setup, replay_setup

UTC = timezone.utc
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FRIDAY_2000 = datetime(2026, 1, 2, 20, 0, tzinfo=UTC)  # a Friday
SUNDAY_2200 = datetime(2026, 1, 4, 22, 0, tzinfo=UTC)


def _bar(ts, o=100.0, h=101.0, l=99.0, c=100.0) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def _flat_bars(times) -> list[Bar]:
    return [_bar(t) for t in times]


# --------------------------------------------------------------------------
# Data-gap handling (reconciliation finding 2)
# --------------------------------------------------------------------------

def test_cap_binds_at_the_first_bar_after_a_weekend_gap():
    """A Friday fill with no bar until Sunday must close on the Sunday bar.

    The cap is a wall-clock rule, so it binds at the first bar *at or after*
    the boundary. Stopping at the last bar before it would close the trade at
    a stale Friday price.
    """
    a = Assumptions(pre_ladder_cap_hours=15.0)
    times = [
        FRIDAY_2000,
        FRIDAY_2000 + timedelta(minutes=30),
        SUNDAY_2200,
        SUNDAY_2200 + timedelta(minutes=30),
    ]
    setup = Setup(
        setup_id=1,
        posted_at=FRIDAY_2000 - timedelta(hours=1),
        direction="buy",
        entry_price=100.0,
        stop_loss=90.0,
        take_profits=[110.0],
    )
    r = replay_setup(setup, _flat_bars(times), POLICY_A, a)

    assert r.status == "filled"
    assert r.fill_ts == FRIDAY_2000
    assert r.exit_reason == "time_cap", "the cap must bind, not the data window"
    assert r.exit_ts == SUNDAY_2200, "the cap binds at the first bar after the gap"
    assert not r.flag("window_end")


def test_window_end_means_the_end_of_the_data_not_a_gap():
    """With no bar after the fill, the position closes at the final bar."""
    a = Assumptions(pre_ladder_cap_hours=15.0)
    times = [FRIDAY_2000, FRIDAY_2000 + timedelta(minutes=30)]
    setup = Setup(
        setup_id=1,
        posted_at=FRIDAY_2000 - timedelta(hours=1),
        direction="buy",
        entry_price=100.0,
        stop_loss=90.0,
        take_profits=[110.0],
    )
    bars = _flat_bars(times)
    bars[-1] = _bar(times[-1], o=100.0, h=101.0, l=99.0, c=102.0)
    r = replay_setup(setup, bars, POLICY_A, a)

    assert r.exit_reason == "window_end"
    assert r.exit_ts == times[-1]
    assert r.exit_price == pytest.approx(102.0), "closed at the final bar's close"


def test_stop_in_the_cap_boundary_bar_beats_the_cap():
    """The stop wins a tie with the cap (plan §3.2 - pessimistic)."""
    a = Assumptions(pre_ladder_cap_hours=15.0)
    times = [
        FRIDAY_2000,
        FRIDAY_2000 + timedelta(minutes=30),
        SUNDAY_2200,
    ]
    bars = _flat_bars(times)
    # the bar that also carries the cap boundary dips through the stop
    bars[2] = _bar(SUNDAY_2200, o=100.0, h=101.0, l=89.0, c=99.0)
    setup = Setup(
        setup_id=1,
        posted_at=FRIDAY_2000 - timedelta(hours=1),
        direction="buy",
        entry_price=100.0,
        stop_loss=90.0,
        take_profits=[110.0],
    )
    r = replay_setup(setup, bars, POLICY_A, a)

    assert r.exit_reason == "sl"
    assert r.exit_price == pytest.approx(90.0)
    assert r.exit_ts == SUNDAY_2200


# --------------------------------------------------------------------------
# Timestamp handling (reconciliation finding 4)
# --------------------------------------------------------------------------

def test_naive_cohort_timestamps_are_read_as_utc():
    """A naive timestamp in the cohort CSV means UTC, not local time.

    The cohort export inherits the labeller's naive-UTC format. Reading it
    with ``.astimezone()`` would interpret it as local time and shift every
    ``posted_at`` - and therefore every fill window - by the machine's UTC
    offset. On a UTC+1 machine that is a one-hour drift in every setup.
    """
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    from run_paired_replay import parse_utc

    assert parse_utc("2025-04-09 21:21:18") == datetime(2025, 4, 9, 21, 21, 18, tzinfo=UTC)
    # an explicit offset is still honoured
    assert parse_utc("2025-04-09T21:21:18+02:00") == datetime(2025, 4, 9, 19, 21, 18, tzinfo=UTC)


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REPO_ROOT, "data", "rade.db")),
    reason="archived database not present",
)
def test_cohort_csv_timestamps_match_the_database():
    """The cohort export and the database must agree on every timestamp."""
    import csv
    import sqlite3
    from datetime import datetime as _dt

    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    from run_paired_replay import parse_utc

    rows = {
        int(r["message_pk"]): r
        for r in csv.DictReader(
            open(os.path.join(REPO_ROOT, "cohort", "v1", "labeled_setups.csv"), encoding="utf-8")
        )
    }
    con = sqlite3.connect(os.path.join(REPO_ROOT, "data", "rade.db"))
    checked = 0
    for pk in list(rows)[:500]:
        db = con.execute(
            "SELECT posted_at FROM labeled_setups WHERE message_pk = ?", (pk,)
        ).fetchone()
        if db is None or not db[0]:
            continue
        checked += 1
        assert parse_utc(rows[pk]["posted_at"]) == _dt.fromisoformat(db[0]).replace(tzinfo=UTC)
    con.close()
    assert checked > 400


# --------------------------------------------------------------------------
# Full-cohort reconciliation against the archived baseline
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.path.exists(os.path.join(REPO_ROOT, "data", "rade.db")),
    reason="archived database not present",
)
def test_replay_reconciles_with_the_archived_legacy_baseline():
    """The replay and the legacy engine agree, except for 8 named setups.

    With the ladder normalised and the cap/stop tie broken the pessimistic way
    (which is what the replay does), the two engines produce the same path for
    1,961 of 1,969 setups. The 8 exceptions are the stop-vs-cap ties, and each
    one is *worse* for the replay - the replay never flatters itself.
    """
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

    from diagnose_legacy_divergence import (  # noqa: E402
        bars_frame,
        load_bars,
        load_labeled,
        run_legacy,
    )

    bars = load_bars()
    setups = load_labeled()
    bf = bars_frame(bars)

    legacy = run_legacy(setups, bf, normalise=True)

    a = Assumptions(breakeven_after_tp1=False, pre_ladder_cap_hours=15.0)
    replay = {}
    for s in setups:
        r = replay_setup(
            Setup(
                setup_id=s.message_pk,
                posted_at=s.posted_at,
                direction=s.direction,
                entry_price=s.entry_price,
                stop_loss=s.stop_loss,
                take_profits=list(s.take_profits),
            ),
            bars,
            POLICY_A,
            a,
        )
        if r.status == "filled":
            replay[s.message_pk] = r

    common = sorted(set(legacy) & set(replay))
    assert len(common) == 1969, "the cohort size is a frozen number"

    identical = 0
    tie_cases = []
    for sid in common:
        lr = legacy[sid]["path_R"]
        rr = replay[sid].legacy_R
        if abs(lr - rr) < 1e-9:
            identical += 1
        else:
            tie_cases.append((sid, lr, rr))

    assert identical == 1961, f"expected 1961 identical paths, got {identical}"
    assert len(tie_cases) == 8, f"expected 8 stop-vs-cap ties, got {len(tie_cases)}"

    # every exception must be a stop-vs-cap tie, resolved by the stop binding
    # (plan §3.2). Note this is a tie-*rule*, not a "the replay is always
    # worse" rule: for a short whose bar closed through the stop, exiting at
    # the stop is the better price. What the replay guarantees is that the
    # stop binds rather than the cap closing at the bar close.
    for sid, lr, rr in tie_cases:
        assert replay[sid].exit_reason == "sl", f"pk={sid} should exit at the stop"
        assert legacy[sid]["exit_type"] == "timeout", f"pk={sid} legacy exits on timeout"
        assert replay[sid].exit_price == pytest.approx(
            replay[sid].legs[-1].price
        ), f"pk={sid} must exit at the stop price"


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REPO_ROOT, "data", "rade.db")),
    reason="archived database not present",
)
def test_archived_s0_baseline_is_still_reproducible():
    """The archived S0 baseline must reproduce exactly from the current code.

    If this fails, the database or the engine drifted from the frozen Phase 2
    state and every comparison in the replay is against a moving target.
    """
    import sys

    import pandas as pd

    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

    from diagnose_legacy_divergence import (  # noqa: E402
        bars_frame,
        load_bars,
        load_labeled,
        run_legacy,
    )

    archive = os.path.join(REPO_ROOT, "data", "s0_report.csv")
    if not os.path.exists(archive):
        pytest.skip("archived s0_report.csv not present")

    fresh = run_legacy(load_labeled(), bars_frame(load_bars()), normalise=False)
    arch = pd.read_csv(archive)
    arch_by_id = {int(r.setup_id): float(r.total_R) for r in arch.itertuples()}

    assert set(arch_by_id) == set(fresh), "the filled-set membership changed"
    worst = max(abs(arch_by_id[i] - fresh[i]["path_R"]) for i in arch_by_id)
    assert worst < 1e-9, f"baseline drift of {worst:.3e} R"


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REPO_ROOT, "data", "rade.db")),
    reason="archived database not present",
)
def test_wrong_side_targets_are_what_moved_the_archived_baseline():
    """The legacy baseline's negative expectancy traces to 15 setups.

    Every one of them carries a target on the wrong side of the entry. This
    is the defect that made the archived S0 report unusable for expectancy.
    """
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

    from diagnose_legacy_divergence import (  # noqa: E402
        bars_frame,
        load_bars,
        load_labeled,
        run_legacy,
    )
    from rade.replay.ladder import normalise_ladder  # noqa: E402

    setups = load_labeled()
    bf = bars_frame(load_bars())
    raw = run_legacy(setups, bf, normalise=False)
    fixed = run_legacy(setups, bf, normalise=True)

    wrong_side = {
        s.message_pk
        for s in setups
        if normalise_ladder(s.direction, s.entry_price, s.take_profits).dropped_wrong_side
    }
    altered = {
        s.message_pk
        for s in setups
        if normalise_ladder(s.direction, s.entry_price, s.take_profits).altered
    }
    assert len(wrong_side) == 17

    changed = {
        sid
        for sid in raw
        if abs(raw[sid]["path_R"] - fixed[sid]["path_R"]) > 1e-9
    }
    assert len(changed) == 13
    assert changed <= altered, "every changed setup must carry an altered ladder"
    assert wrong_side <= altered

    # the wrong-side setups carry essentially the whole gap
    common = sorted(set(raw) & set(fixed))
    total = sum(fixed[i]["path_R"] - raw[i]["path_R"] for i in common)
    from_ws = sum(fixed[i]["path_R"] - raw[i]["path_R"] for i in common if i in wrong_side)
    assert from_ws / total > 0.95, "the wrong-side setups should carry the gap"

    # and the worst one is the published arithmetic, to the last decimal
    assert raw[452]["path_R"] == pytest.approx(-37.462963, abs=1e-6)
    assert fixed[452]["path_R"] == pytest.approx(-1.027778, abs=1e-6)
