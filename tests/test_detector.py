"""Tests for charge start/pause/end detection (custom_components/kilonaut/detector.py).

Loaded by file path, like test_pending.py: the package's __init__ pulls in Home
Assistant, and this module deliberately does not need it.

Run: python -m pytest tests
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "kilonaut" / "detector.py"
_spec = importlib.util.spec_from_file_location("kilonaut_detector", _PATH)
detector = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = detector  # dataclasses resolves annotations through sys.modules
_spec.loader.exec_module(detector)

Event = detector.Event

T0 = datetime(2026, 10, 24, 14, 27, 43, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make(threshold_w=100, grace_s=300):
    return detector.ChargeDetector(threshold_w=threshold_w, grace_s=grace_s)


# --- the meter baseline -----------------------------------------------------


def test_energy_is_the_counter_delta_since_the_start():
    d = make()
    d.energy(810.20)
    assert d.power(2200, at(0)) is Event.START
    d.energy(812.70)
    assert d.session_kwh() == 2.5


def test_a_start_while_the_counter_is_unavailable_uses_the_last_valid_reading():
    """24.10.2026: power came back 1.4 ms before energy, the charge had no baseline."""
    d = make()
    d.energy(810.214)
    d.energy(None)                      # Shelly reconnects, both sensors unavailable
    assert d.power(None, at(0)) is None
    assert d.power(2203, at(1)) is Event.START   # power is back first
    d.energy(810.232)                   # energy follows milliseconds later
    d.energy(816.591)
    assert d.session_kwh() == 6.377


def test_a_start_without_any_reading_takes_the_first_one_that_arrives():
    d = make()
    assert d.power(2200, at(0)) is Event.START
    assert d.session_kwh() is None
    d.energy(100.0)
    d.energy(101.5)
    assert d.session_kwh() == 1.5


def test_a_counter_that_goes_backwards_gives_no_energy_rather_than_negative():
    d = make()
    d.energy(500.0)
    d.power(2200, at(0))
    d.energy(3.0)  # meter replaced or reset
    assert d.session_kwh() is None


def test_unavailable_power_changes_nothing():
    d = make()
    d.energy(10.0)
    d.power(2200, at(0))
    assert d.power(None, at(60)) is None
    assert d.charging and not d.paused


# --- pause, resume, end (issue #2) -------------------------------------------


def test_a_short_dip_is_one_charge_not_two():
    """24.10.2026 16:27:43–16:28:06: a 23-second 'charge', then a new one."""
    d = make()
    d.energy(810.2019)
    assert d.power(2179, at(0)) is Event.START
    assert d.power(4.7, at(23)) is Event.PAUSE
    assert d.power(2203, at(79)) is Event.RESUME
    assert d.started_at == at(0)
    d.energy(816.5909)
    assert d.power(4.5, at(10403)) is Event.PAUSE

    charge = d.finish()
    assert charge.started_at == at(0)
    assert charge.ended_at == at(10403)   # when power fell, not when the timer ran out
    assert charge.energy_kwh == 6.389
    assert not d.charging


def test_staying_low_pauses_once():
    d = make()
    d.power(2200, at(0))
    assert d.power(3, at(10)) is Event.PAUSE
    assert d.power(0, at(20)) is None
    assert d.power(4, at(30)) is None


def test_after_the_end_a_new_charge_starts_from_scratch():
    d = make()
    d.energy(1.0)
    d.power(2200, at(0))
    d.energy(5.0)
    d.power(0, at(100))
    d.finish()

    assert d.power(2200, at(3600)) is Event.START
    assert d.started_at == at(3600)
    d.energy(6.0)
    assert d.session_kwh() == 1.0


def test_steady_charging_produces_no_events():
    d = make()
    d.power(2200, at(0))
    assert d.power(2210, at(60)) is None
    assert d.power(2190, at(120)) is None


def test_below_threshold_while_idle_is_nothing():
    d = make()
    assert d.power(50, at(0)) is None
    assert d.finish() is None
