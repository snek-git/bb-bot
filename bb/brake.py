"""Burst brake: a hard cap on how fast BB posts, so no bug (known or not) can make her spam.

More than `per_channel` posts in one channel, or `per_server` across a server, within `window` seconds
trips the brake for that channel: BB posts nothing there for `cooldown` seconds.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)


class BurstBrake:
    def __init__(
        self,
        *,
        per_channel: int = 10,
        per_server: int = 40,
        window: float = 60.0,
        cooldown: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_channel, self.per_server = per_channel, per_server
        self.window, self.cooldown, self.clock = window, cooldown, clock
        self._channel: dict[int, deque[float]] = {}
        self._server: dict[int, deque[float]] = {}
        self._blocked_until: dict[int, float] = {}

    def _recent(self, log_: dict[int, deque[float]], key: int, now: float) -> deque[float]:
        times = log_.setdefault(key, deque())
        while times and now - times[0] > self.window:
            times.popleft()
        return times

    def allow(self, guild_id: int, channel_id: int) -> bool:
        """Record one post if it's allowed; False means don't post."""
        now = self.clock()
        if self._blocked_until.get(channel_id, 0.0) > now:
            return False
        channel = self._recent(self._channel, channel_id, now)
        server = self._recent(self._server, guild_id, now)
        if len(channel) >= self.per_channel or len(server) >= self.per_server:
            self._blocked_until[channel_id] = now + self.cooldown
            log.warning(
                "burst brake tripped in channel %s (guild %s): %d posts in the channel, %d in the server "
                "in %ds; pausing that channel for %ds",
                channel_id, guild_id, len(channel), len(server), self.window, self.cooldown,
            )
            return False
        channel.append(now)
        server.append(now)
        return True
