"""Rotating status text: every 30 minutes, a random `status` line with the last 24 hours' fix count."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands, tasks

from bb import strings
from bb.timeutil import now

log = logging.getLogger(__name__)

ROTATE_MINUTES = 30
DAY = 86400.0
MAX_STATUS_LEN = 128  # Discord's limit for custom status text


def status_text(n: int) -> str:
    return strings.line("status", n=n)[:MAX_STATUS_LEN]


class Status(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.rotate.start()

    async def cog_unload(self) -> None:
        self.rotate.cancel()

    @tasks.loop(minutes=ROTATE_MINUTES)
    async def rotate(self) -> None:
        try:
            n = await self.bot.db.count_fixes_since(now() - DAY)
            await self.bot.change_presence(activity=discord.CustomActivity(name=status_text(n)))
        except Exception:
            log.exception("status: couldn't update presence")

    @rotate.before_loop
    async def _before_rotate(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Status(bot))
