"""Uptime heartbeat: while BB is connected to Discord, she pings HEARTBEAT_URL every few minutes.

The URL belongs to an outside uptime check (e.g. healthchecks.io). When the pings stop (the container
crashed, the server is down, Discord dropped her), that service alerts the owner. BB can't report her
own death, so the alarm has to live somewhere else. Without HEARTBEAT_URL this does nothing.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

BEAT_MINUTES = 5


class Heartbeat(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.config.heartbeat_url:
            self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(minutes=BEAT_MINUTES)
    async def tick(self) -> None:
        await self.beat()

    @tick.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def beat(self) -> bool:
        """One ping, only while BB is actually connected. True if the check got it."""
        url = self.bot.config.heartbeat_url
        if not url or not self.bot.is_ready() or self.bot.is_closed():
            return False
        try:
            async with self.bot.http_client.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    log.info("heartbeat: the uptime check answered HTTP %s", resp.status)
                    return False
                return True
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.info("heartbeat: couldn't reach the uptime check: %r", exc)
            return False


async def setup(bot: Any) -> None:
    await bot.add_cog(Heartbeat(bot))
