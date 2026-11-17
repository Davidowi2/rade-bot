"""The simulation must keep passing, and must keep running production numbers.

``scripts/simulate_live_config.py`` is the only place in this repo where the
whole chain - parser, sizing, guards, engine, broker submission - is exercised
with the values from ``render.yaml`` rather than from test constants. That makes
it the closest thing we have to a rehearsal of the deployed bot, and a
rehearsal nobody runs is worse than none.

So it runs here, as a test. If a future change makes the bot refuse an ordinary
call, or lets a call through that should have been stopped, this goes red
without anyone remembering to run the script.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import simulate_live_config as sim  # noqa: E402


def test_the_simulation_reads_the_deployed_blueprint():
    """If it silently fell back to constants, the cases below prove nothing."""
    assert sim.BLUEPRINT.exists()
    assert sim.ENV["MIRROR_FIXED_LOTS"] == "0.02"
    assert sim.RISK.fixed_lots == pytest.approx(0.02)
    assert sim.RISK.max_trade_risk_usd == pytest.approx(30.0)
    assert sim.INSTRUMENT.contract_units_per_lot == pytest.approx(100.0)
    # The guard this repo spent an evening switching off. If someone turns it
    # back on, the simulation will start refusing the drift cases and this
    # assertion is where the reason is written down.
    assert sim.LIMITS.max_slippage == pytest.approx(0.0)


def test_every_simulated_case_behaves_as_expected(tmp_path):
    sim.CASES.clear()
    sim.sweep_stop_distance(tmp_path)
    sim.sweep_drift(tmp_path)
    sim.failure_modes(tmp_path)

    failed = [c for c in sim.CASES if c["verdict"] != "PASS"]
    assert not failed, "\n".join(
        f"{c['case']}: {'; '.join(c['problems'])}" for c in failed)


def test_an_ordinary_call_actually_reaches_the_broker(tmp_path):
    """The point of the whole exercise: a normal signal becomes an order."""
    world = sim.World(tmp_path / "ordinary")
    result, outcome = world.deliver(sim.sell_call(3915, 3925, 3905, 3900),
                                    market=sim.quote(3915))

    assert result.status == "paper_intent"
    assert outcome["executed"] is True
    assert len(world.broker.place_calls) == 1
    placed = world.broker.place_calls[0]
    assert placed["qty"] == pytest.approx(0.02)
    assert placed["side"] == "sell"
    assert placed["stop_loss"] == pytest.approx(3925.0)


def test_a_trade_is_still_refused_when_the_stop_cannot_fit_the_ceiling(tmp_path):
    """The one refusal that survives every other fix, and it is arithmetic."""
    world = sim.World(tmp_path / "wide")
    result, outcome = world.deliver(sim.sell_call(3915, 3950, 3905, 3900),
                                    market=sim.quote(3915))

    assert result.status == "blocked"
    assert outcome["executed"] is False
    assert "0.01" in result.reason and "30" in result.reason
