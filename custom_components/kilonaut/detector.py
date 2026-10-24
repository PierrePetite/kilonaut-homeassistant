"""When a charge starts and ends, and how much energy it took.

Two things went wrong here before this existed, both on 24.10.2026 and both on the
same Shelly:

- **A start without a meter reading.** The Shelly drops off the network for a moment
  every few minutes. Power and energy go `unavailable` together, but come back a few
  milliseconds apart — power first. A charge that started in that gap read the energy
  counter while it was still unavailable, kept `None` as its baseline, and could never
  compute a delta: the live tile showed watts and 0 kWh, and at the end the charge was
  dropped as "too small". 6.39 kWh, gone without a trace.
- **A pause is not an end** (issue #2). Power falling below the threshold ended the
  charge on the spot. Opening the car, cell balancing, a 23-second dip — each became an
  end, and the restart a new charge. One evening, four sessions.

So the energy counter is followed on its own, and the last valid reading is what a
charge starts from when the current one is missing. And power below the threshold only
*pauses* the charge; it ends once the pause has lasted `grace_s`, at the moment power
fell, not when the timer ran out.

Deliberately free of Home Assistant imports, like pending.py: the caller feeds in
readings and clock times and schedules the timer, so the logic is testable with pytest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Event(Enum):
    START = "start"    # a new charge began — send the first status, start heartbeats
    PAUSE = "pause"    # power fell below the threshold — start the grace timer
    RESUME = "resume"  # power came back during the pause — cancel the timer


@dataclass(frozen=True)
class FinishedCharge:
    started_at: datetime
    ended_at: datetime
    energy_kwh: float | None


class ChargeDetector:
    def __init__(self, threshold_w: float, grace_s: float) -> None:
        self._threshold_w = threshold_w
        self.grace = timedelta(seconds=grace_s)

        self._last_kwh: float | None = None
        self._start_kwh: float | None = None
        self._started_at: datetime | None = None
        self._paused_at: datetime | None = None

    @property
    def charging(self) -> bool:
        return self._started_at is not None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def energy(self, kwh: float | None) -> None:
        """A reading of the cumulative counter. `None` (unavailable) is ignored."""
        if kwh is None:
            return
        self._last_kwh = kwh
        # A charge that started without any reading at all (fresh Home Assistant start,
        # meter still loading) takes the first one that arrives. Late by seconds at
        # most — far better than no energy for the whole charge.
        if self.charging and self._start_kwh is None:
            self._start_kwh = kwh

    def power(self, watts: float | None, now: datetime) -> Event | None:
        """A reading of the power sensor. `None` (unavailable) changes nothing."""
        if watts is None:
            return None

        if watts > self._threshold_w:
            if not self.charging:
                self._started_at = now
                self._start_kwh = self._last_kwh
                self._paused_at = None
                return Event.START
            if self.paused:
                self._paused_at = None
                return Event.RESUME
            return None

        if self.charging and not self.paused:
            self._paused_at = now
            return Event.PAUSE
        return None

    def session_kwh(self) -> float | None:
        """Energy since the start, or None while there is no baseline."""
        if self._start_kwh is None or self._last_kwh is None:
            return None
        delta = self._last_kwh - self._start_kwh
        # A counter that went backwards was reset or replaced; a negative charge is
        # worse than none.
        if delta < 0:
            return None
        return round(delta, 3)

    def finish(self) -> FinishedCharge | None:
        """End the paused charge. Called when the grace timer runs out."""
        if not self.charging:
            return None
        charge = FinishedCharge(
            started_at=self._started_at,
            ended_at=self._paused_at or self._started_at,
            energy_kwh=self.session_kwh(),
        )
        self._started_at = self._start_kwh = self._paused_at = None
        return charge
