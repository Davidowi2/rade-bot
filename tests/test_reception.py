"""Tests for the read-only reception accounting (observer step 1).

These pin the three-way distinction the observer depends on — new / revision /
duplicate — because getting it wrong in either direction is expensive: a
swallowed edit means the observer acts on stale levels, and a redelivery
counted as an edit means it acts twice.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from rade.observer.reception import (
    KIND_DUPLICATE,
    KIND_NEW,
    KIND_REVISION,
    Observation,
    Recorder,
    catchup_diff,
    latency_summary,
    text_hash,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CH = "@XAUUSD_FOREX4"


def obs(mid: int, text: str = "buy gold 4000", posted=None, received=None, **kw):
    posted = posted or T0
    received = received or (posted + timedelta(milliseconds=800))
    return Observation(
        channel=kw.pop("channel", CH),
        message_id=mid,
        posted_at=posted,
        received_at=received,
        text=text,
        **kw,
    )


# --------------------------------------------------------------------------
# new / revision / duplicate
# --------------------------------------------------------------------------

def test_first_sighting_is_new_at_revision_zero():
    r = Recorder()
    e = r.record(obs(1))
    assert e is not None
    assert e.kind == KIND_NEW
    assert e.revision == 0
    assert r.new_count == 1


def test_identical_redelivery_is_a_duplicate_not_an_event():
    """This is what a reconnect catch-up produces."""
    r = Recorder()
    r.record(obs(1))
    assert r.record(obs(1)) is None
    assert r.duplicates == 1
    assert len(r.events) == 1
    assert r.revision_count == 0


def test_changed_text_is_a_revision():
    r = Recorder()
    r.record(obs(1, "buy gold 4000 sl 3990"))
    e = r.record(obs(1, "buy gold 4000 sl 3995"))
    assert e is not None
    assert e.kind == KIND_REVISION
    assert e.revision == 1


def test_revisions_increment_on_each_edit():
    r = Recorder()
    r.record(obs(1, "a"))
    r.record(obs(1, "b"))
    e = r.record(obs(1, "c"))
    assert e.revision == 2
    assert r.revision_count == 2


def test_reverting_to_the_original_text_is_still_a_revision():
    """Text hashing compares against the last seen body, not a set of all bodies."""
    r = Recorder()
    r.record(obs(1, "a"))
    r.record(obs(1, "b"))
    e = r.record(obs(1, "a"))
    assert e.kind == KIND_REVISION
    assert e.revision == 2


def test_duplicate_after_a_revision_keeps_the_revision_number():
    r = Recorder()
    r.record(obs(1, "a"))
    r.record(obs(1, "b"))          # revision 1
    assert r.record(obs(1, "b")) is None   # duplicate of revision 1
    assert r.duplicates == 1


def test_the_same_id_on_a_different_channel_is_a_separate_message():
    r = Recorder()
    r.record(obs(1, "a"))
    e = r.record(obs(1, "a", channel="@other"))
    assert e is not None and e.kind == KIND_NEW


def test_empty_and_none_bodies_hash_identically():
    assert text_hash(None) == text_hash("")


# --------------------------------------------------------------------------
# latency
# --------------------------------------------------------------------------

def test_latency_is_received_minus_posted():
    o = obs(1, posted=T0, received=T0 + timedelta(milliseconds=1250))
    assert o.latency_ms == pytest.approx(1250.0)


def test_naive_timestamps_are_treated_as_utc_not_local():
    """The same trap that corrupted the replay: naive must never mean local."""
    naive = datetime(2026, 11, 12, 9, 0, 0)
    aware = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
    a = obs(1, posted=naive, received=naive + timedelta(seconds=1))
    b = obs(2, posted=aware, received=aware + timedelta(seconds=1))
    assert a.latency_ms == pytest.approx(b.latency_ms)


def test_latency_summary_refuses_to_invent_a_number_from_nothing():
    s = latency_summary([])
    assert s["n"] == 0
    assert s["p95_ms"] is None
    assert s["verdict"] == "insufficient sample"


def test_latency_summary_flags_a_weak_sample():
    s = latency_summary([100.0, 200.0])
    assert s["n"] == 2
    assert "weak sample" in s["verdict"]


def test_latency_summary_computes_p95():
    vals = [float(v) for v in range(1, 101)]  # 1..100
    s = latency_summary(vals)
    assert s["p50_ms"] == pytest.approx(50.5, abs=0.2)
    assert s["p95_ms"] == pytest.approx(95.05, abs=0.2)
    assert s["verdict"] == "ok"


# --------------------------------------------------------------------------
# clock skew — the host clock is not to be trusted
# --------------------------------------------------------------------------

def test_a_gross_clock_error_invalidates_latency_instead_of_reporting_it():
    """A 5-hour host clock error must not produce a plausible-looking p95."""
    s = latency_summary([500.0, 700.0, 900.0], skew_ms=-5.4 * 3600 * 1000)
    assert s["verdict"].startswith("INVALID")
    assert s["p95_ms"] is None
    assert s["p50_ms"] is None


def test_a_small_clock_error_is_corrected_not_rejected():
    """Raw 500ms with a +200ms skew is a true 700ms."""
    s = latency_summary([500.0, 500.0], skew_ms=200.0)
    assert s["p50_ms"] == pytest.approx(700.0)
    assert not s["verdict"].startswith("INVALID")


def test_skew_is_corrected_in_the_negative_direction_too():
    s = latency_summary([900.0], skew_ms=-300.0)
    assert s["p50_ms"] == pytest.approx(600.0)


def test_skew_at_the_tolerance_boundary_still_reports():
    from rade.observer.reception import SKEW_TOLERANCE_MS
    s = latency_summary([500.0], skew_ms=SKEW_TOLERANCE_MS)
    assert not s["verdict"].startswith("INVALID")
    s2 = latency_summary([500.0], skew_ms=SKEW_TOLERANCE_MS + 1)
    assert s2["verdict"].startswith("INVALID")


def test_an_empty_sample_with_a_bad_clock_is_still_invalid():
    s = latency_summary([], skew_ms=-5.4 * 3600 * 1000)
    assert s["verdict"].startswith("INVALID")


# --------------------------------------------------------------------------
# catch-up reconciliation
# --------------------------------------------------------------------------

def test_catchup_is_complete_when_the_sets_match():
    d = catchup_diff({1, 2, 3}, {1, 2, 3})
    assert d["complete"] and d["missed"] == 0 and d["extra"] == 0


def test_catchup_reports_what_a_disconnect_would_have_cost():
    d = catchup_diff({1, 2}, {1, 2, 3, 4})
    assert d["missed"] == 2
    assert d["missed_ids"] == [3, 4]
    assert not d["complete"]


def test_catchup_reports_messages_the_stream_saw_but_the_fetch_missed():
    """A live event for a message the history fetch cannot see is a red flag."""
    d = catchup_diff({1, 2, 99}, {1, 2})
    assert d["extra"] == 1
    assert d["extra_ids"] == [99]
    assert not d["complete"]


# --------------------------------------------------------------------------
# the end-to-end scenario step 1 has to pass
# --------------------------------------------------------------------------

def test_reconnect_scenario_recovers_without_duplicating():
    """Live sees 1-2, the link drops, 3-4 arrive unseen, catch-up recovers them,
    then the redelivery of 1-4 must add nothing."""
    r = Recorder()
    for mid in (1, 2):
        r.record(obs(mid, f"m{mid}", source="live"))
    for mid in (3, 4):
        r.record(obs(mid, f"m{mid}", source="catchup"))
    for mid in (1, 2, 3, 4):
        assert r.record(obs(mid, f"m{mid}", source="catchup")) is None

    assert r.new_count == 4
    assert r.revision_count == 0
    assert r.duplicates == 4
    assert catchup_diff(r.ids(), {1, 2, 3, 4})["complete"]


# --------------------------------------------------------------------------
# restart safety: a redelivery after a restart is not a new message
# --------------------------------------------------------------------------

def test_restore_turns_a_redelivery_into_a_duplicate():
    """A soak restarts. Everything the channel re-sends must be suppressed."""
    first = Recorder()
    first.record(obs(1, "buy gold 4000"))
    first.record(obs(2, "sl 3990"))

    second = Recorder()
    assert second.restore(list(first.events)) == 2
    assert second.record(obs(1, "buy gold 4000")) is None
    assert second.record(obs(2, "sl 3990")) is None
    assert second.new_count == 0
    assert second.duplicates == 2
    assert second.restored_events == 2


def test_restore_keeps_the_revision_number_so_a_later_edit_still_increments():
    first = Recorder()
    first.record(obs(1, "entry 4000"))
    first.record(obs(1, "entry 4001"))          # revision 1
    assert first.events[-1].revision == 1

    second = Recorder()
    second.restore(list(first.events))
    e = second.record(obs(1, "entry 4002"))     # a second edit, after a restart
    assert e is not None and e.revision == 2
    assert e.kind == KIND_REVISION


def test_restored_ids_are_known_before_the_fetch():
    """The catch-up diff must treat restored messages as already held."""
    r = Recorder()
    prior = Recorder()
    for mid in (1, 2, 3):
        prior.record(obs(mid, f"m{mid}"))
    r.restore(list(prior.events))
    assert r.known_ids() == {1, 2, 3}
    assert catchup_diff(r.known_ids(), {1, 2, 3})["complete"]
    # and a genuinely new message is still reported as missed
    assert catchup_diff(r.known_ids(), {1, 2, 3, 4})["missed"] == 1


def test_restore_does_not_double_count_the_prior_run():
    first = Recorder()
    first.record(obs(1))
    second = Recorder()
    second.restore(list(first.events))
    assert second.new_count == 0
    assert second.events == []


def test_load_events_skips_a_truncated_final_line(tmp_path):
    """A crash mid-write leaves half a line; the soak must still resume."""
    import json

    from rade.observer.reception import load_events

    path = tmp_path / "e.jsonl"
    r = Recorder()
    r.record(obs(1, "a"))
    r.record(obs(2, "b"))
    lines = [json.dumps(e.to_dict()) for e in r.events]
    path.write_text("\n".join(lines) + '\n{"channel": "@XAUUSD_FOREX4", "mess',
                    encoding="utf-8")

    got = load_events(path)
    assert [e.message_id for e in got] == [1, 2]


def test_load_events_of_a_missing_file_is_empty_not_an_error(tmp_path):
    from rade.observer.reception import load_events

    assert load_events(tmp_path / "nope.jsonl") == []


def test_a_broken_clock_blocks_the_reception_verdict_even_with_a_sample():
    """The 2s budget cannot be claimed on a host whose clock is hours out."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "probe_mod", root / "scripts" / "verify_telegram_reception.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    lat = latency_summary([900.0, 1100.0], skew_ms=19_000_000.0)
    v = mod._verdict(lat, {"n_live": 2, "n_fetched": 2, "missed": 0, "extra": 0,
                           "complete": True}, Recorder(), [], [], certifiable=False)
    assert v["clock"].startswith("BLOCKED")
    assert "INVALID" in v["reception"]


# --------------------------------------------------------------------------
# a failed measurement is not a passing measurement
# --------------------------------------------------------------------------

def test_an_unmeasured_clock_offset_is_not_zero():
    """A failed Date-header fetch used to return 0.0, which reads as PASS."""
    lat = latency_summary([900.0, 1100.0], skew_ms=None)
    assert lat["n"] == 2
    assert lat["p95_ms"] is None
    assert lat["skew_ms"] is None
    assert lat["verdict"].startswith("UNKNOWN")
    assert lat["uncorrected_latency_ms"] == [900.0, 1100.0]


def test_an_unmeasured_clock_does_not_report_the_channel_as_quiet():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "probe_mod2", root / "scripts" / "verify_telegram_reception.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    lat = latency_summary([], skew_ms=None)
    assert mod._reception_verdict(lat).startswith("UNKNOWN")


def test_an_incomplete_fetch_is_not_a_set_mismatch():
    """A fetch that broke after 85 messages must not report extra=85."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "probe_mod3", root / "scripts" / "verify_telegram_reception.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    diff = catchup_diff({1, 2, 3}, set())
    diff["fetch_complete"] = False
    diff["complete"] = False
    v = mod._verdict(latency_summary([], skew_ms=0.0), diff, Recorder(), [], [],
                     certifiable=True, clock_measured=True)
    assert v["catchup"].startswith("INCOMPLETE")
    assert "extra" not in v["catchup"]
    assert v["edits"].startswith("inconclusive")
