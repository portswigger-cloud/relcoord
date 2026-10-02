# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""How long a change or a diff spends in each of its phases.

The workflow log stamps each event with the time since the request started, so
a phase's duration could be read off the gap between two stamps. But a gap gets
attributed to whichever line ends it, and that line is often the start of the
next phase. The processor knows where each phase starts and ends, so it reports
the duration itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# How many outputs the summary names as the slowest to generate. Every output's
# time is in the event detail.
SLOWEST_OUTPUTS_REPORTED = 3

# Time no phase accounts for is reported only once there is enough of it to
# matter, so rounding does not put "other 0.0s" in every summary.
UNACCOUNTED_THRESHOLD_SECONDS = 0.1


@dataclass
class Lap:
    seconds: float = 0.0

    @property
    def took(self) -> str:
        """The duration as a message suffix."""
        return f"({self.seconds:.1f}s)"


class PhaseTimer:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._phases: dict[str, float] = {}
        self._outputs: dict[str, float] = {}

    @contextmanager
    def measure(self, phase: str, *, output: str | None = None) -> Iterator[Lap]:
        """Time a block, adding it to `phase` even if it raises."""
        lap = Lap()
        started = self._clock()
        try:
            yield lap
        finally:
            lap.seconds = self._clock() - started
            self._phases[phase] = self._phases.get(phase, 0.0) + lap.seconds
            if output is not None:
                self._outputs[output] = self._outputs.get(output, 0.0) + lap.seconds

    def summary(self) -> tuple[str, dict[str, Any]]:
        """The message and detail of the event that closes the stream."""
        total = self._clock() - self._started
        parts = [f"{phase} {seconds:.1f}s" for phase, seconds in self._phases.items()]
        unaccounted = total - sum(self._phases.values())
        if unaccounted >= UNACCOUNTED_THRESHOLD_SECONDS:
            parts.append(f"other {unaccounted:.1f}s")
        message = f"took {total:.1f}s"
        if parts:
            message += f": {', '.join(parts)}"
        if len(self._outputs) > 1:
            slowest = sorted(self._outputs.items(), key=lambda item: -item[1])
            message += "; slowest outputs: " + ", ".join(
                f"{name} {seconds:.1f}s"
                for name, seconds in slowest[:SLOWEST_OUTPUTS_REPORTED]
            )
        detail = {
            "total_seconds": round(total, 3),
            "phases": {phase: round(s, 3) for phase, s in self._phases.items()},
            "generate_by_output": {
                name: round(s, 3) for name, s in self._outputs.items()
            },
        }
        return message, detail
