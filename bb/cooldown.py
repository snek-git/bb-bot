"""A per-user cooldown. @BB replies and Find source each keep their own."""

from __future__ import annotations

import time

COOLDOWN_SECONDS = 30.0


class Cooldown:
    """At most one hit per key per `seconds`."""

    def __init__(self, seconds: float = COOLDOWN_SECONDS) -> None:
        self.seconds = seconds
        self._last: dict[int, float] = {}

    def ready(self, key: int, now: float | None = None) -> bool:
        """True (and starts the cooldown) if `key` may act now."""
        now = time.monotonic() if now is None else now
        if len(self._last) > 512:
            self._last = {k: t for k, t in self._last.items() if now - t < self.seconds}
        last = self._last.get(key)
        if last is not None and now - last < self.seconds:
            return False
        self._last[key] = now
        return True
