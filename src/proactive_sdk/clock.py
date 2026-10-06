"""Deterministic time sources (SPEC §5.2; SCHED-01).

Two clocks, never conflated:

- **wall** — UTC epoch milliseconds, persisted in the database (leases,
  deadlines, audit timestamps). Survives process restarts; also the clock
  that can be rolled back by the operator, which admission must tolerate
  without replaying already-admitted slots.
- **monotonic** — in-process milliseconds for scheduling waits and pacing.
  Never persisted; its value is meaningless across restarts.

``FakeClock`` exists so scheduler and fault-injection tests are fully
deterministic; production code must accept any Clock, never call
``time.*`` directly.

No I/O, no background threads.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FakeClock"]


@runtime_checkable
class Clock(Protocol):
    def wall_now_ms(self) -> int: ...
    def monotonic_ms(self) -> int: ...


class SystemClock:
    """Real time. Wall = UTC epoch ms, monotonic = CLOCK_MONOTONIC ms."""

    __slots__ = ()

    def wall_now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


class FakeClock:
    """Controllable clock for tests and fault injection.

    Wall and monotonic move independently: ``advance_wall`` simulates
    downtime and clock rollback (negative step allowed), ``advance_mono``
    simulates process-internal time passing. Neither advances on its own,
    so tests are reproducible.
    """

    __slots__ = ("_wall_ms", "_mono_ms")

    def __init__(self, wall_ms: int = 1_000_000_000_000, mono_ms: int = 0) -> None:
        if not isinstance(wall_ms, int) or not isinstance(mono_ms, int):
            raise TypeError("FakeClock times must be integer milliseconds")
        self._wall_ms = wall_ms
        self._mono_ms = mono_ms

    def wall_now_ms(self) -> int:
        return self._wall_ms

    def monotonic_ms(self) -> int:
        return self._mono_ms

    def advance_wall(self, step_ms: int) -> int:
        if not isinstance(step_ms, int):
            raise TypeError("step_ms must be an integer")
        self._wall_ms += step_ms
        return self._wall_ms

    def set_wall(self, wall_ms: int) -> int:
        """Jump to an absolute wall time, including backwards (rollback tests)."""
        if not isinstance(wall_ms, int):
            raise TypeError("wall_ms must be an integer")
        self._wall_ms = wall_ms
        return self._wall_ms

    def advance_mono(self, step_ms: int) -> int:
        if not isinstance(step_ms, int) or step_ms < 0:
            raise TypeError("monotonic time only moves forward")
        self._mono_ms += step_ms
        return self._mono_ms
